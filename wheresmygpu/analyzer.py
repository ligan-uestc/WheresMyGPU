"""Decide whether a GPU counts as free / low-load, and explain why."""

from __future__ import annotations

from dataclasses import dataclass, field

from .config import AlertRule
from .nvidia import GpuInfo


@dataclass
class Condition:
    """One threshold test, with a human-readable explanation."""

    name: str
    ok: bool
    detail: str

    def mark(self) -> str:
        return "PASS" if self.ok else "FAIL"


@dataclass
class GpuVerdict:
    """The full evaluation of one GPU against one :class:`AlertRule`."""

    server: str
    server_label: str
    gpu: GpuInfo
    rule: AlertRule
    conditions: list[Condition] = field(default_factory=list)

    @property
    def is_free(self) -> bool:
        """True when every configured idle condition is satisfied."""
        return bool(self.conditions) and all(c.ok for c in self.conditions)

    def failing(self) -> list[Condition]:
        return [c for c in self.conditions if not c.ok]

    def failing_summary(self) -> str:
        bad = self.failing()
        if not bad:
            return "all conditions met"
        return "; ".join(f"{c.name}: {c.detail}" for c in bad)

    def conditions_summary(self) -> str:
        return " | ".join(f"{c.name}={c.mark()}" for c in self.conditions)

    def metrics(self) -> dict:
        gpu = self.gpu
        return {
            "server": self.server,
            "server_label": self.server_label,
            "gpu_index": gpu.index,
            "gpu_name": gpu.name,
            "mem_total_gb": round(gpu.mem_total_gb, 1),
            "mem_used_gb": round(gpu.mem_used_gb, 1),
            "mem_free_gb": round(gpu.mem_free_gb, 1),
            "mem_used_pct": round(gpu.mem_used_pct, 1),
            "gpu_util_pct": round(gpu.gpu_util, 1),
            "mem_util_pct": round(gpu.memory_util, 1),
            "temperature_c": gpu.temperature_c,
            "power_w": gpu.power_w,
            "power_limit_w": gpu.power_limit_w,
            "process_count": len(gpu.processes),
            "process_memory_gb": round(gpu.process_memory_gb, 1),
            "processes": [p.describe() for p in gpu.processes],
            "is_free": self.is_free,
        }

    def headline(self) -> str:
        gpu = self.gpu
        return (
            f"{self.server_label} · GPU {gpu.index} ({gpu.name}) 空闲"
        )

    def detail_lines(self) -> list[str]:
        gpu = self.gpu
        temp = f"{gpu.temperature_c:.0f}°C" if gpu.temperature_c is not None else "n/a"
        power = (
            f"{gpu.power_w:.0f}W / {gpu.power_limit_w:.0f}W"
            if gpu.power_w is not None and gpu.power_limit_w
            else "n/a"
        )
        lines = [
            f"剩余显存: {gpu.mem_free_gb:.1f} GB / {gpu.mem_total_gb:.0f} GB "
            f"(已用 {gpu.mem_used_gb:.1f} GB, {gpu.mem_used_pct:.0f}%)",
            f"GPU 利用率: {gpu.gpu_util:.0f}%   显存利用率: {gpu.memory_util:.0f}%",
            f"温度: {temp}   功耗: {power}",
            f"进程数: {len(gpu.processes)}",
        ]
        if gpu.processes:
            for proc in gpu.processes[:8]:
                lines.append(f"  - {proc.describe()}")
            if len(gpu.processes) > 8:
                lines.append(f"  - … 另有 {len(gpu.processes) - 8} 个进程")
        return lines


def evaluate(gpu: GpuInfo, rule: AlertRule, server: str, server_label: str) -> GpuVerdict:
    """Test one GPU against the configured idle thresholds.

    All conditions are combined with AND: a GPU is only reported as available
    when it satisfies *every* configured threshold.
    """
    conditions: list[Condition] = []

    free_ok = gpu.mem_free_gb >= rule.min_free_memory_gb
    conditions.append(
        Condition(
            "free_memory",
            free_ok,
            f"{gpu.mem_free_gb:.1f}G free vs required >= {rule.min_free_memory_gb:.1f}G",
        )
    )

    if rule.min_free_memory_pct is not None:
        free_pct_ok = gpu.mem_free_pct >= rule.min_free_memory_pct
        conditions.append(
            Condition(
                "free_memory_pct",
                free_pct_ok,
                f"{gpu.mem_free_pct:.0f}% free vs required >= "
                f"{rule.min_free_memory_pct:.0f}%",
            )
        )

    util_ok = gpu.gpu_util <= rule.max_gpu_utilization
    conditions.append(
        Condition(
            "gpu_utilization",
            util_ok,
            f"{gpu.gpu_util:.0f}% vs allowed <= {rule.max_gpu_utilization:.0f}%",
        )
    )

    if rule.max_memory_utilization is not None:
        mem_util_ok = gpu.memory_util <= rule.max_memory_utilization
        conditions.append(
            Condition(
                "memory_utilization",
                mem_util_ok,
                f"{gpu.memory_util:.0f}% vs allowed <= {rule.max_memory_utilization:.0f}%",
            )
        )

    if rule.max_temperature_c is not None:
        if gpu.temperature_c is None:
            conditions.append(
                Condition("temperature", True, "temperature unavailable; not enforced")
            )
        else:
            conditions.append(
                Condition(
                    "temperature",
                    gpu.temperature_c <= rule.max_temperature_c,
                    f"{gpu.temperature_c:.0f}C vs allowed <= {rule.max_temperature_c:.0f}C",
                )
            )

    if rule.require_no_processes:
        conditions.append(
            Condition(
                "no_processes",
                len(gpu.processes) == 0,
                f"{len(gpu.processes)} process(es); none allowed",
            )
        )

    if rule.max_process_memory_gb is not None:
        pmem = gpu.process_memory_gb
        conditions.append(
            Condition(
                "process_memory",
                pmem <= rule.max_process_memory_gb,
                f"{pmem:.1f}G in processes vs allowed <= {rule.max_process_memory_gb:.1f}G",
            )
        )

    return GpuVerdict(
        server=server,
        server_label=server_label,
        gpu=gpu,
        rule=rule,
        conditions=conditions,
    )


def unsatisfiable_reason(gpu: GpuInfo, rule: AlertRule) -> str | None:
    """Flag thresholds that can never be met on this particular GPU.

    The classic mistake is ``min_free_memory_gb: 24`` on a 24 GB card: free
    memory can never exceed total memory, so the rule would stay silent
    forever and the user would think the monitor is broken.
    """
    reasons: list[str] = []

    if gpu.mem_total_gb > 0 and rule.min_free_memory_gb >= gpu.mem_total_gb:
        reasons.append(
            f"min_free_memory_gb={rule.min_free_memory_gb:g} >= total VRAM "
            f"{gpu.mem_total_gb:g}G of {gpu.name}: needs an entirely empty card, "
            "so it will (practically) never fire — set it a few GB below total"
        )

    if rule.min_free_memory_pct is not None and rule.min_free_memory_pct >= 100.0:
        reasons.append(
            f"min_free_memory_pct={rule.min_free_memory_pct:g} requires a 100% free card"
        )

    if rule.max_process_memory_gb is not None and rule.max_process_memory_gb >= gpu.mem_total_gb > 0:
        reasons.append(
            f"max_process_memory_gb={rule.max_process_memory_gb:g} exceeds total VRAM "
            f"{gpu.mem_total_gb:g}G, so it can never block anything"
        )

    return "; ".join(reasons) if reasons else None


def rule_description(rule: AlertRule) -> str:
    """One-line summary of the active thresholds, for logs and startup banners."""
    parts = [
        f"free VRAM >= {rule.min_free_memory_gb:.0f}G",
        f"GPU util <= {rule.max_gpu_utilization:.0f}%",
    ]
    if rule.min_free_memory_pct is not None:
        parts.insert(1, f"free VRAM >= {rule.min_free_memory_pct:.0f}%")
    if rule.max_memory_utilization is not None:
        parts.append(f"mem util <= {rule.max_memory_utilization:.0f}%")
    if rule.max_temperature_c is not None:
        parts.append(f"temp <= {rule.max_temperature_c:.0f}C")
    if rule.require_no_processes:
        parts.append("no processes")
    if rule.max_process_memory_gb is not None:
        parts.append(f"proc mem <= {rule.max_process_memory_gb:.0f}G")
    parts.append(f"sustained x{rule.sustained_checks}")
    parts.append(f"cooldown {rule.cooldown_minutes}min")
    return ", ".join(parts)