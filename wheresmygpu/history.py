"""Persist every polling round to JSONL and CSV for later inspection."""

from __future__ import annotations

import csv
import json
import logging
import time
from pathlib import Path

from .analyzer import GpuVerdict
from .nvidia import ServerSnapshot

log = logging.getLogger("wheresmygpu.history")

CSV_FIELDS = [
    "timestamp",
    "iso_time",
    "server",
    "server_label",
    "ok",
    "gpu_index",
    "gpu_name",
    "mem_total_mb",
    "mem_used_mb",
    "mem_free_mb",
    "mem_used_pct",
    "gpu_util_pct",
    "mem_util_pct",
    "temperature_c",
    "power_w",
    "power_limit_w",
    "proc_count",
    "proc_memory_mb",
    "processes",
    "is_free",
    "error",
]


class HistoryWriter:
    """Append-only JSONL + CSV recorder."""

    def __init__(
        self,
        jsonl_path: Path | None,
        csv_path: Path | None,
        enabled: bool = True,
    ) -> None:
        self.enabled = enabled
        self.jsonl_path = jsonl_path
        self.csv_path = csv_path
        self._csv_ready = False

    # -- helpers ------------------------------------------------------------ #

    @staticmethod
    def _iso(ts: float) -> str:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))

    def _prepare(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)

    # -- recording ---------------------------------------------------------- #

    def record(
        self,
        snapshot: ServerSnapshot,
        verdicts: list[GpuVerdict],
    ) -> None:
        if not self.enabled:
            return
        by_index = {v.gpu.index: v for v in verdicts}
        rows: list[dict] = []

        if snapshot.ok:
            for gpu in snapshot.gpus:
                verdict = by_index.get(gpu.index)
                row = {
                    "timestamp": round(snapshot.timestamp, 3),
                    "iso_time": self._iso(snapshot.timestamp),
                    "server": snapshot.server,
                    "server_label": snapshot.label,
                    "ok": True,
                    **gpu.to_row(),
                    "is_free": bool(verdict.is_free) if verdict else None,
                    "error": "",
                }
                rows.append(row)
        else:
            rows.append(
                {
                    "timestamp": round(snapshot.timestamp, 3),
                    "iso_time": self._iso(snapshot.timestamp),
                    "server": snapshot.server,
                    "server_label": snapshot.label,
                    "ok": False,
                    "gpu_index": "",
                    "gpu_name": "",
                    "mem_total_mb": "",
                    "mem_used_mb": "",
                    "mem_free_mb": "",
                    "mem_used_pct": "",
                    "gpu_util_pct": "",
                    "mem_util_pct": "",
                    "temperature_c": "",
                    "power_w": "",
                    "power_limit_w": "",
                    "proc_count": "",
                    "proc_memory_mb": "",
                    "processes": "",
                    "is_free": "",
                    "error": snapshot.error or "unknown error",
                }
            )

        if self.jsonl_path:
            try:
                self._prepare(self.jsonl_path)
                with self.jsonl_path.open("a", encoding="utf-8") as fh:
                    for row in rows:
                        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            except OSError as exc:
                log.warning("cannot write history JSONL %s: %s", self.jsonl_path, exc)

        if self.csv_path:
            try:
                self._prepare(self.csv_path)
                new_file = not self.csv_path.exists() or self.csv_path.stat().st_size == 0
                with self.csv_path.open("a", encoding="utf-8-sig", newline="") as fh:
                    writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS, extrasaction="ignore")
                    if new_file:
                        writer.writeheader()
                    for row in rows:
                        writer.writerow(row)
            except OSError as exc:
                log.warning("cannot write history CSV %s: %s", self.csv_path, exc)