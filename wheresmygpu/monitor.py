"""The monitoring engine: poll servers, evaluate thresholds, raise alerts."""

from __future__ import annotations

import logging
import random
import signal
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .analyzer import GpuVerdict, evaluate, rule_description, unsatisfiable_reason
from .config import Config, ServerConfig
from .history import HistoryWriter
from .notify import AlertEvent, Channel, build_channels, notify_all
from .nvidia import GpuInfo, NvidiaSmiCollector, ServerSnapshot
from .ssh_runner import SshRunner
from .state import MonitorState

log = logging.getLogger("wheresmygpu.monitor")


def _ts(value: float | None = None) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(value or time.time()))


def _fmt_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


class Monitor:
    """Owns the polling loop, the SSH connections and the alert decisions."""

    def __init__(
        self,
        config: Config,
        *,
        dry_run: bool = False,
        interval_override: int | None = None,
    ) -> None:
        self.config = config
        self.dry_run = dry_run
        self.interval = interval_override or config.app.interval_seconds

        self.channels: list[Channel] = build_channels(config)
        self.state = MonitorState.load(config.resolve_path(config.app.state_file))
        self.history = HistoryWriter(
            config.resolve_path(config.app.history_file) if config.app.history_file else None,
            config.resolve_path(config.app.history_csv) if config.app.history_csv else None,
        )

        self._runners: dict[str, SshRunner] = {}
        self._collectors: dict[str, NvidiaSmiCollector] = {}
        for server in config.enabled_servers():
            runner = SshRunner(server, config)
            self._runners[server.name] = runner
            self._collectors[server.name] = NvidiaSmiCollector(server, config, runner)

        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._warned_unsatisfiable: set = set()

        # Optional hooks for embedders (the GUI). They are called from the
        # polling thread, so any failure is swallowed: a broken UI listener must
        # never take the monitoring loop down with it.
        self.on_snapshots = None   # callable(list[ServerSnapshot]) -> None
        self.on_event = None       # callable(AlertEvent) -> None

    def _emit_snapshots(self, snapshots: list[ServerSnapshot]) -> None:
        """Hand a completed round to the embedder, if one is attached."""
        if self.on_snapshots is None:
            return
        try:
            self.on_snapshots(snapshots)
        except Exception:  # pragma: no cover - defensive
            log.exception("on_snapshots hook failed")

    def _emit_event(self, event: AlertEvent) -> None:
        """Tell the embedder about an alert before it is delivered."""
        if self.on_event is None:
            return
        try:
            self.on_event(event)
        except Exception:  # pragma: no cover - defensive
            log.exception("on_event hook failed")

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    def request_stop(self, *_args) -> None:
        if not self._stop.is_set():
            log.info("stop requested; finishing the current round…")
            self._stop.set()

    def _install_signal_handlers(self) -> None:
        for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
            sig = getattr(signal, name, None)
            if sig is None:
                continue
            try:
                signal.signal(sig, self.request_stop)
            except (ValueError, OSError, RuntimeError):
                # Not available in this context (e.g. a non-main thread).
                pass

    def close(self) -> None:
        for runner in self._runners.values():
            try:
                runner.close()
            except Exception:
                pass

    def run_forever(self) -> int:
        """Poll until interrupted. Returns a process exit code."""
        self._install_signal_handlers()
        self.print_banner()

        consecutive_round_errors = 0
        while not self._stop.is_set():
            round_started = time.monotonic()
            try:
                self.run_round()
                consecutive_round_errors = 0
            except Exception as exc:  # pragma: no cover - defensive
                consecutive_round_errors += 1
                log.exception("polling round failed (%d in a row): %s",
                              consecutive_round_errors, exc)
                if consecutive_round_errors >= 5:
                    log.error("5 consecutive round failures; backing off for 5 minutes")
                    self._wait(300)
                    consecutive_round_errors = 0

            if self._stop.is_set():
                break

            elapsed = time.monotonic() - round_started
            delay = max(5.0, self.interval - elapsed)
            if self.config.app.jitter_seconds:
                delay += random.uniform(0, self.config.app.jitter_seconds)
            next_at = time.strftime("%H:%M:%S", time.localtime(time.time() + delay))
            log.info(
                "round finished in %.1fs; next check at %s (in %s)",
                elapsed, next_at, _fmt_duration(delay),
            )
            self._wait(delay)

        self.print_shutdown_summary()
        self.close()
        return 0

    def _wait(self, seconds: float) -> None:
        """Sleep in small steps so Ctrl+C is handled promptly."""
        deadline = time.monotonic() + seconds
        while not self._stop.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            self._stop.wait(min(1.0, remaining))

    # ------------------------------------------------------------------ #
    # One round
    # ------------------------------------------------------------------ #

    def run_round(self) -> list[ServerSnapshot]:
        self.state.rounds += 1
        now = time.time()
        log.info("── round #%d ──────────────────────────────────────────", self.state.rounds)

        snapshots = self._poll_all()

        round_events: list[AlertEvent] = []

        for snapshot in snapshots:
            server = self._server(snapshot.server)
            rule = self.config.alert_for(server)

            events = self._handle_server(snapshot, rule, now)

            verdicts: list[GpuVerdict] = []
            if snapshot.ok:
                for gpu in snapshot.gpus:
                    verdict, event = self._handle_gpu(snapshot, gpu, rule, now)
                    verdicts.append(verdict)
                    if event:
                        events.append(event)

            self._log_snapshot(snapshot, verdicts)
            self.history.record(snapshot, verdicts)

            if snapshot.ok:
                self._warn_unsatisfiable(snapshot, rule)

            round_events.extend(events)

            if not snapshot.ok:
                # A failed round must not silently look "busy".
                self.state.server(snapshot.server).last_error = snapshot.error

        # One notification per kind per round: a round that finds 16 free GPUs
        # must send one email, not sixteen. The per-GPU state machine above has
        # already run, so merging affects delivery only, never bookkeeping.
        for event in self._batch_events(round_events):
            self._emit_event(event)
            self._dispatch(event)

        self._emit_snapshots(snapshots)

        if not self.dry_run:
            self.state.save()

        return snapshots

    def _poll_all(self) -> list[ServerSnapshot]:
        servers = self.config.enabled_servers()
        if len(servers) == 1:
            return [self._poll_one(servers[0])]

        results: dict[str, ServerSnapshot] = {}
        with ThreadPoolExecutor(
            max_workers=min(len(servers), 8), thread_name_prefix="wheresmygpu-poll"
        ) as pool:
            futures = {pool.submit(self._poll_one, s): s for s in servers}
            for future in as_completed(futures):
                server = futures[future]
                try:
                    results[server.name] = future.result()
                except Exception as exc:  # pragma: no cover - defensive
                    results[server.name] = ServerSnapshot(
                        server=server.name,
                        label=server.label,
                        ok=False,
                        timestamp=time.time(),
                        error=f"{type(exc).__name__}: {exc}",
                    )
        return [results[s.name] for s in servers]

    def _poll_one(self, server: ServerConfig) -> ServerSnapshot:
        collector = self._collectors[server.name]
        previous = self.state.server(server.name).consecutive_failures
        return collector.collect(consecutive_failures=previous)

    def _server(self, name: str) -> ServerConfig:
        for server in self.config.enabled_servers():
            if server.name == name:
                return server
        raise KeyError(f"unknown server {name!r}")

    # ------------------------------------------------------------------ #
    # Decision logic
    # ------------------------------------------------------------------ #

    def _handle_gpu(
        self,
        snapshot: ServerSnapshot,
        gpu: GpuInfo,
        rule,
        now: float,
    ) -> tuple[GpuVerdict, AlertEvent | None]:
        verdict = evaluate(gpu, rule, snapshot.server, snapshot.label)
        st = self.state.gpu(snapshot.server, gpu.index)
        st.last_seen_ts = now
        st.last_metrics = verdict.metrics()

        was_free = st.is_confirmed_free
        event: AlertEvent | None = None

        if verdict.is_free:
            st.consecutive_free += 1
            if st.free_since is None:
                st.free_since = now
            confirmed = st.consecutive_free >= rule.sustained_checks

            if not confirmed:
                log.info(
                    "[%s] %s looks free (%d/%d consecutive checks before alerting)",
                    snapshot.server, gpu.label(), st.consecutive_free, rule.sustained_checks,
                )
                return verdict, None

            if self.state.cooldown_elapsed(st.last_alert_ts, rule.cooldown_minutes, now):
                event = self._free_event(snapshot, verdict, st, now)
                st.last_alert_ts = now
            else:
                remaining = self.state.cooldown_remaining(
                    st.last_alert_ts, rule.cooldown_minutes, now
                )
                log.info(
                    "[%s] %s is free but alert cooldown has %s left; not repeating",
                    snapshot.server, gpu.label(), _fmt_duration(remaining),
                )
            st.last_state = "free"

        else:
            st.consecutive_free = 0
            st.free_since = None
            if was_free and rule.notify_on_busy:
                if self.state.cooldown_elapsed(st.last_alert_ts, rule.cooldown_minutes, now):
                    event = self._busy_event(snapshot, verdict, now)
                    st.last_alert_ts = now
            st.last_state = "busy"

        return verdict, event

    def _handle_server(
        self, snapshot: ServerSnapshot, rule, now: float
    ) -> list[AlertEvent]:
        st = self.state.server(snapshot.server)
        events: list[AlertEvent] = []

        if snapshot.ok:
            st.consecutive_failures = 0
            st.last_ok_ts = snapshot.timestamp
            st.last_error = None
            if st.unreachable_alerted:
                st.unreachable_alerted = False
                if rule.notify_on_recovery:
                    events.append(self._recovery_event(snapshot))
        else:
            st.consecutive_failures += 1
            st.last_error = snapshot.error
            log.warning(
                "[%s] unreachable (%d consecutive): %s",
                snapshot.server, st.consecutive_failures, snapshot.error,
            )
            if (
                rule.notify_on_unreachable
                and not st.unreachable_alerted
                and st.consecutive_failures >= rule.unreachable_after_failures
            ):
                st.unreachable_alerted = True
                events.append(self._unreachable_event(snapshot, st.consecutive_failures))

        return events

    # ------------------------------------------------------------------ #
    # Batching
    # ------------------------------------------------------------------ #

    #: kinds worth merging across servers within a single round
    _BATCHABLE = ("gpu_free", "gpu_busy", "server_unreachable", "server_recovered")

    _LEVEL_RANK = {"info": 0, "warning": 1, "critical": 2}

    def _batch_events(self, events: list[AlertEvent]) -> list[AlertEvent]:
        """Merge one round's events into at most one notification per kind."""
        if len(events) <= 1:
            return list(events)

        grouped: dict[str, list[AlertEvent]] = {}
        order: list[str] = []
        for event in events:
            if event.kind not in grouped:
                grouped[event.kind] = []
                order.append(event.kind)
            grouped[event.kind].append(event)

        result: list[AlertEvent] = []
        for kind in order:
            group = grouped[kind]
            if len(group) == 1 or kind not in self._BATCHABLE:
                result.extend(group)
            else:
                result.append(self._merge_events(kind, group))
        return result

    def _merge_events(self, kind: str, group: list[AlertEvent]) -> AlertEvent:
        """Fold several same-kind events into one compact, scannable summary.

        A lone event keeps its full detail (see _batch_events, which passes it
        through untouched). A merged one is deliberately compact, so a round
        that finds 16 free GPUs produces a readable list instead of 16 copies
        of the same eight-line block.
        """
        now = time.time()
        count = len(group)
        labels = sorted({e.server_label for e in group if e.server_label})
        joined = "、".join(labels)

        if kind == "gpu_free":
            title = f"🎮 {count} 张 GPU 空闲可用了 · {len(labels)} 台服务器"
        elif kind == "gpu_busy":
            title = f"{count} 张 GPU 已被占用 · {len(labels)} 台服务器"
        elif kind == "server_unreachable":
            title = f"{count} 台 GPU 服务器无法连接"
        elif kind == "server_recovered":
            title = f"{count} 台 GPU 服务器已恢复"
        else:
            title = f"{count} 条 GPU 通知"

        level = "info"
        for event in group:
            if self._LEVEL_RANK.get(event.level, 0) > self._LEVEL_RANK.get(level, 0):
                level = event.level

        lines = [f"本轮检查命中 {count} 项，已汇总成一封邮件（{_ts(now)}）"]
        if joined:
            lines.append(f"涉及服务器: {joined}")
        lines.append("")

        # GPU batches read best grouped by server
        gpu_events = [e for e in group if e.metrics.get("gpu_index") is not None]
        if len(gpu_events) == count:
            by_server: dict[str, list[AlertEvent]] = {}
            for event in group:
                by_server.setdefault(event.server_label or "?", []).append(event)
            for label in sorted(by_server):
                members = sorted(by_server[label], key=lambda e: e.metrics.get("gpu_index") or 0)
                lines.append(f"【{label}】{len(members)} 张")
                for event in members:
                    lines.append("  · " + self._compact_line(event.metrics))
                    for proc in (event.metrics.get("processes") or [])[:3]:
                        lines.append("        " + proc)
                lines.append("")
        else:
            lines.extend("  · " + self._headline(event) for event in group)
            lines.append("")

        # the threshold set is shared across a batch, so state it once
        threshold = next(
            (line for event in group for line in event.lines
             if line.startswith("判定阈值: ")),
            None,
        )
        if threshold:
            lines.append(threshold)

        return AlertEvent(
            kind=kind,
            title=title,
            body="\n".join(lines),
            level=level,
            server="",
            server_label=joined,
            gpu_index=None,
            timestamp=now,
            metrics={"batched": count, "servers": labels},
            lines=lines,
        )

    @staticmethod
    def _compact_line(metrics: dict) -> str:
        """One readable line per GPU, built from the structured metrics."""
        def num(key: str, fmt: str = "{:.1f}") -> str:
            value = metrics.get(key)
            return fmt.format(value) if isinstance(value, (int, float)) else "n/a"

        name = str(metrics.get("gpu_name") or "").replace("NVIDIA GeForce ", "")
        bits = [
            f"剩余 {num('mem_free_gb')}G/{num('mem_total_gb', '{:.0f}')}G",
            f"利用率 {num('gpu_util_pct', '{:.0f}')}%",
            f"显存 {num('mem_util_pct', '{:.0f}')}%",
            f"进程 {metrics.get('process_count', 0)}",
        ]
        if isinstance(metrics.get("temperature_c"), (int, float)):
            bits.append(f"{num('temperature_c', '{:.0f}')}°C")
        return f"GPU #{metrics.get('gpu_index')}  {name} — " + " · ".join(bits)

    @staticmethod
    def _headline(event: AlertEvent) -> str:
        """A one-line summary for events that carry no GPU metrics."""
        head = event.server_label or event.title
        for line in event.lines:
            stripped = line.strip()
            if stripped.startswith(("剩余显存:", "连续失败:", "驱动版本:", "最后错误:")):
                return head + "  —  " + stripped
        return head

    # ------------------------------------------------------------------ #
    # Event construction
    # ------------------------------------------------------------------ #

    def _free_event(self, snapshot, verdict, st, now) -> AlertEvent:
        rule = verdict.rule
        gpu = verdict.gpu
        free_duration = _fmt_duration(now - (st.free_since or now))

        title = f"🎮 GPU 空闲可用了：{snapshot.label} · GPU {gpu.index}"
        lines = [
            f"服务器: {snapshot.label}  ({self._server(snapshot.server).target})",
            f"GPU: #{gpu.index} {gpu.name}",
            "",
            *verdict.detail_lines(),
            "",
            f"判定阈值: {rule_description(rule)}",
            f"连续满足 {st.consecutive_free} 次检查（约 {free_duration}）",
            f"检测时间: {_ts(now)}",
        ]
        return AlertEvent(
            kind="gpu_free",
            title=title,
            body="\n".join(lines),
            level="warning" if self._is_scarce(verdict) else "info",
            server=snapshot.server,
            server_label=snapshot.label,
            gpu_index=gpu.index,
            timestamp=now,
            metrics=verdict.metrics(),
            lines=lines,
        )

    def _busy_event(self, snapshot, verdict, now) -> AlertEvent:
        gpu = verdict.gpu
        title = f"GPU 已占用：{snapshot.label} · GPU {gpu.index}"
        lines = [
            f"服务器: {snapshot.label}",
            f"GPU: #{gpu.index} {gpu.name}",
            "",
            *verdict.detail_lines(),
            "",
            f"检测时间: {_ts(now)}",
        ]
        return AlertEvent(
            kind="gpu_busy",
            title=title,
            body="\n".join(lines),
            level="info",
            server=snapshot.server,
            server_label=snapshot.label,
            gpu_index=gpu.index,
            timestamp=now,
            metrics=verdict.metrics(),
            lines=lines,
        )

    def _unreachable_event(self, snapshot, failures: int) -> AlertEvent:
        title = f"⚠️ GPU 服务器无法连接：{snapshot.label}"
        lines = [
            f"服务器: {snapshot.label}  ({self._server(snapshot.server).target})",
            f"连续失败: {failures} 次",
            f"最后错误: {snapshot.error}",
            f"检测时间: {_ts(snapshot.timestamp)}",
        ]
        return AlertEvent(
            kind="server_unreachable",
            title=title,
            body="\n".join(lines),
            level="critical",
            server=snapshot.server,
            server_label=snapshot.label,
            timestamp=snapshot.timestamp,
            lines=lines,
        )

    def _recovery_event(self, snapshot) -> AlertEvent:
        lines = [
            f"服务器: {snapshot.label}  ({self._server(snapshot.server).target})",
            f"驱动版本: {snapshot.driver_version or 'unknown'}",
            f"恢复时间: {_ts(snapshot.timestamp)}",
            "",
        ]
        for gpu in snapshot.gpus:
            lines.append(gpu.summary_line())
        return AlertEvent(
            kind="server_recovered",
            title=f"✅ GPU 服务器已恢复：{snapshot.label}",
            body="\n".join(lines),
            level="info",
            server=snapshot.server,
            server_label=snapshot.label,
            timestamp=snapshot.timestamp,
            lines=lines,
        )

    @staticmethod
    def _is_scarce(verdict: GpuVerdict) -> bool:
        """Escalate when a free GPU is unusually valuable (very little used)."""
        gpu = verdict.gpu
        return gpu.mem_used_pct < 1.0 and gpu.gpu_util < 1.0 and not gpu.processes

    # ------------------------------------------------------------------ #
    # Output
    # ------------------------------------------------------------------ #

    def _dispatch(self, event: AlertEvent) -> None:
        if self.dry_run:
            log.info("[dry-run] would alert: %s", event.title)
            for line in event.lines:
                log.info("[dry-run]   %s", line)
            return
        delivered = notify_all(self.channels, event)
        if delivered:
            with self._lock:
                self.state.alerts_sent += 1

    def _warn_unsatisfiable(self, snapshot: ServerSnapshot, rule) -> None:
        """Warn once per GPU when a threshold can never be met on that model."""
        for gpu in snapshot.gpus:
            key = (
                snapshot.server,
                gpu.index,
                rule.min_free_memory_gb,
                rule.min_free_memory_pct,
                rule.max_process_memory_gb,
            )
            if key in self._warned_unsatisfiable:
                continue
            reason = unsatisfiable_reason(gpu, rule)
            if reason:
                self._warned_unsatisfiable.add(key)
                log.warning(
                    "[%s] GPU %d (%s): 阈值可能永远无法满足 — %s",
                    snapshot.server, gpu.index, gpu.name, reason,
                )

    def _log_snapshot(self, snapshot: ServerSnapshot, verdicts: list[GpuVerdict]) -> None:
        if not snapshot.ok:
            log.warning("[%s] poll failed after %.1fs: %s",
                        snapshot.server, snapshot.duration_s, snapshot.error)
            return

        driver = f" driver={snapshot.driver_version}" if snapshot.driver_version else ""
        log.info(
            "[%s] %d GPU(s) in %.1fs%s",
            snapshot.server, len(snapshot.gpus), snapshot.duration_s, driver,
        )
        by_index = {v.gpu.index: v for v in verdicts}
        for gpu in snapshot.gpus:
            verdict = by_index.get(gpu.index)
            flag = " FREE" if verdict and verdict.is_free else ""
            log.info("    %s%s", gpu.summary_line(), flag)
            for line in gpu.processes[:5]:
                log.info("        · %s", line.describe())
            if len(gpu.processes) > 5:
                log.info("        · … %d more process(es)", len(gpu.processes) - 5)

    # -- display width helpers (CJK glyphs occupy two terminal columns) ----- #

    @staticmethod
    def _display_width(text: str) -> int:
        import unicodedata

        width = 0
        for ch in text:
            if unicodedata.combining(ch):
                continue
            width += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        return width

    @classmethod
    def _pad(cls, text: str, width: int, align: str = "left") -> str:
        gap = " " * max(0, width - cls._display_width(text))
        return gap + text if align == "right" else text + gap

    @classmethod
    def _ellipsize(cls, text: str, width: int) -> str:
        if cls._display_width(text) <= width:
            return text
        import unicodedata

        out, used = "", 0
        for ch in text:
            step = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
            if used + step > width - 1:
                break
            out += ch
            used += step
        return out + "…"

    def render_table(self, snapshots: list[ServerSnapshot]) -> str:
        """Compact fixed-width table used by ``--once``.

        Padding is computed from terminal display width so that Chinese server
        labels line up with the ASCII headers.
        """
        pad = self._pad
        columns = [
            ("SERVER", 16, "left"),
            ("GPU", 3, "left"),
            ("NAME", 24, "left"),
            ("VRAM used/total", 16, "left"),
            ("FREE", 7, "right"),
            ("UTIL", 5, "right"),
            ("MEM%", 5, "right"),
            ("TEMP", 5, "left"),
            ("PROC", 5, "right"),
            ("STATE", 0, "left"),
        ]
        header = " ".join(pad(name, width, align) for name, width, align in columns)
        rows = [header, "-" * self._display_width(header)]

        for snapshot in snapshots:
            server = self._server(snapshot.server)
            rule = self.config.alert_for(server)
            label = self._ellipsize(snapshot.label, 15)

            if not snapshot.ok:
                rows.append(
                    " ".join(
                        [
                            pad(label, 16), pad("-", 3), pad("-", 24), pad("-", 16),
                            pad("-", 7, "right"), pad("-", 5, "right"),
                            pad("-", 5, "right"), pad("-", 5), pad("-", 5, "right"),
                            "UNREACHABLE",
                        ]
                    )
                )
                continue

            for gpu in snapshot.gpus:
                verdict = evaluate(gpu, rule, snapshot.server, snapshot.label)
                temp = (
                    f"{gpu.temperature_c:.0f}C" if gpu.temperature_c is not None else "n/a"
                )
                vram = f"{gpu.mem_used_gb:.1f}/{gpu.mem_total_gb:.0f}G"
                state = "FREE ✅" if verdict.is_free else "busy"
                rows.append(
                    " ".join(
                        [
                            pad(label, 16),
                            pad(str(gpu.index), 3),
                            pad(self._ellipsize(gpu.name, 24), 24),
                            pad(vram, 16),
                            pad(f"{gpu.mem_free_gb:.1f}G", 7, "right"),
                            pad(f"{gpu.gpu_util:.0f}%", 5, "right"),
                            pad(f"{gpu.memory_util:.0f}%", 5, "right"),
                            pad(temp, 5),
                            pad(str(len(gpu.processes)), 5, "right"),
                            state,
                        ]
                    )
                )
        return "\n".join(rows)

    def print_banner(self) -> None:
        line = "=" * 78
        servers = self.config.enabled_servers()
        out = [
            "",
            line,
            f" WheresMyGPU — 持续 GPU 空闲监控   (每 {self.interval}s / "
            f"{self.interval / 60:.1f} 分钟轮询一次)",
            line,
            f" 监控服务器 ({len(servers)}):",
        ]
        for server in servers:
            rule = self.config.alert_for(server)
            out.append(
                f"   - {server.describe():<44} [{self.config.transport_for(server)}]"
            )
            out.append(f"       {rule_description(rule)}")
        out += [
            f" 提醒通道: {', '.join(c.type for c in self.channels)}",
            f" 配置文件: {self.config.path}",
            f" 状态文件: {self.config.resolve_path(self.config.app.state_file)}",
            f" 历史记录: {self.config.resolve_path(self.config.app.history_file)}",
            line,
            "",
        ]
        if self.dry_run:
            out.insert(3, " 模式: DRY-RUN（只评估与打印，不发送提醒、不写状态）")
        text = "\n".join(out)
        if self.config.app.console:
            print(text, flush=True)
        else:
            log.info("%s", text)

    def print_shutdown_summary(self) -> None:
        uptime = _fmt_duration(time.time() - self.state.started_ts)
        text = (
            f"\nWheresMyGPU 已停止。运行时长 {uptime}，共 {self.state.rounds} 轮检查，"
            f"发送 {self.state.alerts_sent} 条提醒。\n"
        )
        if self.config.app.console:
            print(text, flush=True)
        else:
            log.info("%s", text.strip())


def setup_logging(config: Config, level: str | None = None) -> Path:
    """Configure console + rotating file logging. Returns the log file path."""
    from logging.handlers import RotatingFileHandler

    log_dir = config.resolve_path(config.app.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"wheresmygpu-{time.strftime('%Y%m%d')}.log"

    effective = (level or config.app.log_level).upper()
    root = logging.getLogger("wheresmygpu")
    root.setLevel(effective)
    root.handlers.clear()
    root.propagate = False

    fmt = logging.Formatter(
        fmt="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = RotatingFileHandler(
        log_path, maxBytes=8 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    file_handler.setLevel(effective)
    root.addHandler(file_handler)

    if config.app.console:
        import sys

        stream = logging.StreamHandler(sys.stdout)
        stream.setFormatter(fmt)
        stream.setLevel(effective)
        root.addHandler(stream)

    logging.getLogger("paramiko").setLevel(logging.WARNING)
    return log_path