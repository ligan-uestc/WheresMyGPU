"""Collect and parse ``nvidia-smi`` data from a remote GPU server."""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field

from .config import Config, ServerConfig
from .ssh_runner import SshError, SshRunner

log = logging.getLogger("wheresmygpu.nvidia")

MB_PER_GB = 1024.0

# Each field is quoted exactly as nvidia-smi expects. Fields that some drivers
# do not expose simply come back as "[N/A]" and parse to None.
_GPU_FIELDS = [
    "index",
    "uuid",
    "name",
    "memory.total",
    "memory.used",
    "memory.free",
    "utilization.gpu",
    "utilization.memory",
    "temperature.gpu",
    "power.draw",
    "power.limit",
]

_PROC_FIELDS = ["gpu_uuid", "pid", "process_name", "used_memory"]

_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")


def _num(value: str | None) -> float | None:
    """Parse a numeric cell, tolerating '[N/A]', '[Not Supported]' and units."""
    if value is None:
        return None
    text = value.strip()
    if not text or text.startswith("["):
        return None
    match = _NUM_RE.search(text)
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def _cells(line: str, expected: int) -> list[str]:
    parts = [c.strip() for c in line.split(",")]
    if len(parts) < expected:
        parts += [""] * (expected - len(parts))
    return parts


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #

@dataclass
class ProcessInfo:
    pid: int
    name: str
    used_memory_mb: float | None = None
    gpu_uuid: str | None = None
    user: str | None = None
    runtime: str | None = None

    @property
    def used_memory_gb(self) -> float:
        return (self.used_memory_mb or 0.0) / MB_PER_GB

    def short_name(self) -> str:
        return self.name.rsplit("/", 1)[-1][:48]

    def describe(self) -> str:
        mem = f"{self.used_memory_gb:.1f}G" if self.used_memory_mb is not None else "?"
        who = f"{self.user}:" if self.user else ""
        age = f" ({self.runtime})" if self.runtime else ""
        return f"{who}{self.short_name()}[{self.pid}] {mem}{age}"


@dataclass
class GpuInfo:
    index: int
    uuid: str
    name: str
    mem_total_mb: float
    mem_used_mb: float
    mem_free_mb: float
    util_gpu: float | None
    util_mem: float | None
    temperature_c: float | None
    power_w: float | None
    power_limit_w: float | None
    processes: list[ProcessInfo] = field(default_factory=list)

    # -- derived ------------------------------------------------------------ #

    @property
    def mem_total_gb(self) -> float:
        return self.mem_total_mb / MB_PER_GB

    @property
    def mem_used_gb(self) -> float:
        return self.mem_used_mb / MB_PER_GB

    @property
    def mem_free_gb(self) -> float:
        return self.mem_free_mb / MB_PER_GB

    @property
    def mem_used_pct(self) -> float:
        return (self.mem_used_mb / self.mem_total_mb * 100.0) if self.mem_total_mb else 0.0

    @property
    def mem_free_pct(self) -> float:
        return (self.mem_free_mb / self.mem_total_mb * 100.0) if self.mem_total_mb else 0.0

    @property
    def gpu_util(self) -> float:
        return self.util_gpu if self.util_gpu is not None else 0.0

    @property
    def memory_util(self) -> float:
        return self.util_mem if self.util_mem is not None else self.mem_used_pct

    @property
    def process_memory_gb(self) -> float:
        return sum((p.used_memory_mb or 0.0) for p in self.processes) / MB_PER_GB

    def label(self) -> str:
        return f"GPU {self.index} ({self.name})"

    def summary_line(self) -> str:
        temp = f"{self.temperature_c:.0f}C" if self.temperature_c is not None else "n/a"
        return (
            f"GPU {self.index} {self.name}: "
            f"VRAM {self.mem_used_gb:.1f}/{self.mem_total_gb:.0f}G "
            f"(free {self.mem_free_gb:.1f}G) | "
            f"util {self.gpu_util:.0f}% | mem-util {self.memory_util:.0f}% | "
            f"temp {temp} | {len(self.processes)} proc"
        )

    def to_row(self) -> dict:
        return {
            "gpu_index": self.index,
            "gpu_name": self.name,
            "gpu_uuid": self.uuid,
            "mem_total_mb": round(self.mem_total_mb, 1),
            "mem_used_mb": round(self.mem_used_mb, 1),
            "mem_free_mb": round(self.mem_free_mb, 1),
            "mem_used_pct": round(self.mem_used_pct, 2),
            "gpu_util_pct": None if self.util_gpu is None else round(self.gpu_util, 1),
            "mem_util_pct": round(self.memory_util, 1),
            "temperature_c": self.temperature_c,
            "power_w": self.power_w,
            "power_limit_w": self.power_limit_w,
            "proc_count": len(self.processes),
            "proc_memory_mb": round(self.process_memory_gb * MB_PER_GB, 1),
            "processes": "; ".join(p.describe() for p in self.processes),
        }


@dataclass
class ServerSnapshot:
    """Result of one polling round against one server."""

    server: str
    label: str
    ok: bool
    timestamp: float
    gpus: list[GpuInfo] = field(default_factory=list)
    error: str | None = None
    driver_version: str | None = None
    duration_s: float = 0.0
    consecutive_failures: int = 0

    def gpu(self, index: int) -> GpuInfo | None:
        for item in self.gpus:
            if item.index == index:
                return item
        return None


# --------------------------------------------------------------------------- #
# Collector
# --------------------------------------------------------------------------- #

class NvidiaSmiCollector:
    """Builds and executes ``nvidia-smi`` queries over an :class:`SshRunner`."""

    def __init__(self, server: ServerConfig, config: Config, runner: SshRunner) -> None:
        self.server = server
        self.config = config
        self.runner = runner

    # -- command construction ----------------------------------------------- #

    def gpu_query(self, smi: str) -> str:
        return f"{smi} --query-gpu={','.join(_GPU_FIELDS)} --format=csv,noheader,nounits"

    def process_query(self, smi: str) -> str:
        return (
            f"{smi} --query-compute-apps={','.join(_PROC_FIELDS)} "
            "--format=csv,noheader,nounits"
        )

    def driver_query(self, smi: str) -> str:
        return f"{smi} --query-gpu=driver_version --format=csv,noheader"

    # -- parsing ------------------------------------------------------------ #

    @staticmethod
    def parse_gpus(stdout: str) -> list[GpuInfo]:
        gpus: list[GpuInfo] = []
        for raw_line in stdout.splitlines():
            line = raw_line.strip()
            if not line or line.lower().startswith("index"):
                continue
            values = dict(zip(_GPU_FIELDS, _cells(line, len(_GPU_FIELDS))))

            index = _num(values["index"])
            total = _num(values["memory.total"]) or 0.0
            used = _num(values["memory.used"]) or 0.0
            free = _num(values["memory.free"])
            if free is None:
                free = max(total - used, 0.0)

            gpus.append(
                GpuInfo(
                    index=int(index) if index is not None else len(gpus),
                    uuid=values["uuid"],
                    name=values["name"] or "Unknown GPU",
                    mem_total_mb=total,
                    mem_used_mb=used,
                    mem_free_mb=free,
                    util_gpu=_num(values["utilization.gpu"]),
                    util_mem=_num(values["utilization.memory"]),
                    temperature_c=_num(values["temperature.gpu"]),
                    power_w=_num(values["power.draw"]),
                    power_limit_w=_num(values["power.limit"]),
                )
            )
        gpus.sort(key=lambda g: g.index)
        return gpus

    @staticmethod
    def parse_processes(stdout: str) -> tuple[list[ProcessInfo], dict[str, list[ProcessInfo]]]:
        """Parse the compute-apps query.

        Returns the flat process list plus a ``gpu_uuid -> processes`` mapping.
        """
        flat: list[ProcessInfo] = []
        grouped: dict[str, list[ProcessInfo]] = {}

        for raw_line in stdout.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            low = line.lower()
            if low.startswith("gpu_uuid") or "no running processes" in low:
                continue
            values = dict(zip(_PROC_FIELDS, _cells(line, len(_PROC_FIELDS))))

            pid = _num(values["pid"])
            if pid is None:
                continue

            proc = ProcessInfo(
                pid=int(pid),
                name=values["process_name"] or "unknown",
                used_memory_mb=_num(values["used_memory"]),
                gpu_uuid=values["gpu_uuid"] or None,
            )
            flat.append(proc)
            if proc.gpu_uuid:
                grouped.setdefault(proc.gpu_uuid, []).append(proc)

        return flat, grouped

    @staticmethod
    def parse_driver(stdout: str) -> str | None:
        for line in stdout.splitlines():
            line = line.strip()
            if line and not line.lower().startswith("driver_version"):
                return line
        return None

    @staticmethod
    def parse_ps(stdout: str) -> dict[int, tuple[str | None, str | None]]:
        """Parse ``ps -o pid=,user=,etime=`` output into ``pid -> (user, etime)``."""
        table: dict[int, tuple[str | None, str | None]] = {}
        for raw_line in stdout.splitlines():
            parts = raw_line.split(None, 2)
            if len(parts) < 2:
                continue
            try:
                pid = int(parts[0])
            except ValueError:
                continue
            user = parts[1]
            etime = parts[2].strip() if len(parts) > 2 else None
            table[pid] = (user, etime)
        return table

    # -- collection --------------------------------------------------------- #

    def collect(self, consecutive_failures: int = 0) -> ServerSnapshot:
        started = time.monotonic()
        snapshot = ServerSnapshot(
            server=self.server.name,
            label=self.server.label,
            ok=False,
            timestamp=time.time(),
            consecutive_failures=consecutive_failures,
        )
        try:
            smi = self.runner.resolve_nvidia_smi()

            gpu_result = self.runner.run(self.gpu_query(smi))
            if not gpu_result.ok:
                raise SshError(
                    f"nvidia-smi exited {gpu_result.exit_status}: "
                    f"{gpu_result.stderr.strip()[:200]}"
                )
            gpus = self.parse_gpus(gpu_result.stdout)
            if not gpus:
                raise SshError(
                    "nvidia-smi listed no GPUs (driver missing, or the container "
                    "has no GPU access)"
                )

            procs = self._collect_processes(smi)
            self._attach_processes(gpus, procs)

            try:
                drv = self.runner.run(self.driver_query(smi), timeout=15)
                snapshot.driver_version = self.parse_driver(drv.stdout) if drv.ok else None
            except SshError:
                snapshot.driver_version = None

            snapshot.gpus = gpus
            snapshot.ok = True

        except SshError as exc:
            snapshot.ok = False
            snapshot.error = str(exc)
        except Exception as exc:  # pragma: no cover - defensive
            snapshot.ok = False
            snapshot.error = f"{type(exc).__name__}: {exc}"

        snapshot.duration_s = time.monotonic() - started
        return snapshot

    def _collect_processes(self, smi: str) -> list[ProcessInfo]:
        """Fetch compute processes, enriching with owning user when possible."""
        try:
            result = self.runner.run(self.process_query(smi))
        except SshError as exc:
            log.debug("[%s] process query failed: %s", self.server.name, exc)
            return []

        if not result.ok:
            log.debug(
                "[%s] process query unsupported by this driver: %s",
                self.server.name, result.stderr.strip()[:160],
            )
            return []

        flat, _grouped = self.parse_processes(result.stdout)
        if not flat:
            return []

        # Enrich with user + runtime via ps; best effort only.
        pids = [str(p.pid) for p in flat]
        ps_cmd = f"ps -o pid=,user=,etime= -p {','.join(pids)} 2>/dev/null"
        try:
            ps_result = self.runner.run(ps_cmd, timeout=15)
            if ps_result.ok:
                table = self.parse_ps(ps_result.stdout)
                for proc in flat:
                    info = table.get(proc.pid)
                    if info:
                        proc.user, proc.runtime = info
        except SshError as exc:
            log.debug("[%s] ps enrichment skipped: %s", self.server.name, exc)

        return flat

    @staticmethod
    def _attach_processes(gpus: list[GpuInfo], procs: list[ProcessInfo]) -> None:
        """Attach each process to its GPU (by UUID, then by memory heuristic)."""
        if not procs:
            return

        by_uuid: dict[str, list[ProcessInfo]] = {}
        for proc in procs:
            if proc.gpu_uuid:
                by_uuid.setdefault(proc.gpu_uuid, []).append(proc)

        assigned: set[int] = set()
        for gpu in gpus:
            matched = by_uuid.get(gpu.uuid, [])
            gpu.processes = list(matched)
            assigned.update(id(p) for p in matched)

        leftovers = [p for p in procs if id(p) not in assigned]
        if not leftovers:
            return

        if len(gpus) == 1:
            gpus[0].processes.extend(leftovers)
            return

        # Multi-GPU host where the driver did not report UUIDs: assign each
        # process to the GPU with the largest unexplained memory footprint.
        for proc in sorted(leftovers, key=lambda p: -(p.used_memory_mb or 0.0)):
            target = max(
                gpus,
                key=lambda g: g.mem_used_mb - g.process_memory_gb * MB_PER_GB,
            )
            target.processes.append(proc)