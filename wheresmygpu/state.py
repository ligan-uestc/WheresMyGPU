"""Persistent monitor state: debounce counters, cooldowns, failure streaks.

State is written atomically after every round so that restarting the app does
not re-send alerts the user has already seen.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

log = logging.getLogger("wheresmygpu.state")


@dataclass
class GpuState:
    """Per-GPU bookkeeping."""

    consecutive_free: int = 0
    last_state: str = "unknown"          # unknown | free | busy
    last_alert_ts: float = 0.0
    free_since: float | None = None
    last_seen_ts: float = 0.0
    last_metrics: dict = field(default_factory=dict)

    @property
    def is_confirmed_free(self) -> bool:
        return self.last_state == "free"


@dataclass
class ServerState:
    """Per-server bookkeeping."""

    consecutive_failures: int = 0
    unreachable_alerted: bool = False
    last_ok_ts: float = 0.0
    last_error: str | None = None
    gpus: dict[str, GpuState] = field(default_factory=dict)

    def gpu(self, index: int) -> GpuState:
        return self.gpus.setdefault(str(index), GpuState())


class MonitorState:
    """Container for all persisted state, with atomic save/load."""

    VERSION = 1

    def __init__(self, path: Path) -> None:
        self.path = path
        self.servers: dict[str, ServerState] = {}
        self.started_ts: float = time.time()
        self.rounds: int = 0
        self.alerts_sent: int = 0

    # -- access ------------------------------------------------------------- #

    def server(self, name: str) -> ServerState:
        return self.servers.setdefault(name, ServerState())

    def gpu(self, server: str, index: int) -> GpuState:
        return self.server(server).gpu(index)

    # -- persistence -------------------------------------------------------- #

    @classmethod
    def load(cls, path: Path) -> "MonitorState":
        state = cls(path)
        if not path.is_file():
            return state
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.warning("state file %s is unreadable (%s); starting fresh", path, exc)
            try:
                shutil.copy2(path, path.with_suffix(path.suffix + ".corrupt"))
            except OSError:
                pass
            return state

        try:
            state.started_ts = float(raw.get("started_ts") or time.time())
            state.rounds = int(raw.get("rounds") or 0)
            state.alerts_sent = int(raw.get("alerts_sent") or 0)
            for name, sdata in (raw.get("servers") or {}).items():
                server = ServerState(
                    consecutive_failures=int(sdata.get("consecutive_failures") or 0),
                    unreachable_alerted=bool(sdata.get("unreachable_alerted")),
                    last_ok_ts=float(sdata.get("last_ok_ts") or 0.0),
                    last_error=sdata.get("last_error"),
                )
                for key, gdata in (sdata.get("gpus") or {}).items():
                    server.gpus[key] = GpuState(
                        consecutive_free=int(gdata.get("consecutive_free") or 0),
                        last_state=str(gdata.get("last_state") or "unknown"),
                        last_alert_ts=float(gdata.get("last_alert_ts") or 0.0),
                        free_since=(
                            float(gdata["free_since"])
                            if gdata.get("free_since") is not None
                            else None
                        ),
                        last_seen_ts=float(gdata.get("last_seen_ts") or 0.0),
                        last_metrics=gdata.get("last_metrics") or {},
                    )
                state.servers[name] = server
        except (TypeError, ValueError) as exc:
            log.warning("state file %s has an unexpected shape (%s); starting fresh", path, exc)
            return cls(path)
        return state

    def save(self) -> None:
        payload = {
            "version": self.VERSION,
            "saved_ts": time.time(),
            "started_ts": self.started_ts,
            "rounds": self.rounds,
            "alerts_sent": self.alerts_sent,
            "servers": {
                name: {
                    "consecutive_failures": s.consecutive_failures,
                    "unreachable_alerted": s.unreachable_alerted,
                    "last_ok_ts": s.last_ok_ts,
                    "last_error": s.last_error,
                    "gpus": {k: asdict(v) for k, v in s.gpus.items()},
                }
                for name, s in self.servers.items()
            },
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            os.replace(tmp, self.path)
        except OSError as exc:
            log.warning("could not persist state to %s: %s", self.path, exc)
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    # -- cooldown helpers --------------------------------------------------- #

    @staticmethod
    def cooldown_elapsed(last_alert_ts: float, cooldown_minutes: int, now: float) -> bool:
        if cooldown_minutes <= 0:
            return True
        return (now - last_alert_ts) >= cooldown_minutes * 60

    @staticmethod
    def cooldown_remaining(last_alert_ts: float, cooldown_minutes: int, now: float) -> float:
        if cooldown_minutes <= 0:
            return 0.0
        return max(0.0, cooldown_minutes * 60 - (now - last_alert_ts))