"""Tests for batching a whole round's alerts into one notification."""

from __future__ import annotations

import unittest

from wheresmygpu.monitor import Monitor
from wheresmygpu.notify import AlertEvent


def _metrics(gpu: int, label: str, free_gb: float = 23.6, procs: int = 0) -> dict:
    return {
        "server": f"s-{label}",
        "server_label": label,
        "gpu_index": gpu,
        "gpu_name": "NVIDIA GeForce RTX 3090",
        "mem_total_gb": 24.0,
        "mem_used_gb": round(24.0 - free_gb, 1),
        "mem_free_gb": free_gb,
        "mem_used_pct": 0.4,
        "gpu_util_pct": 0.0,
        "mem_util_pct": 0.0,
        "temperature_c": 41.0,
        "power_w": 27.0,
        "power_limit_w": 350.0,
        "process_count": procs,
        "process_memory_gb": 0.0,
        "processes": [f"pid 1234 python train.py ({gpu}M)"] if procs else [],
        "is_free": True,
    }


def _free_event(server: str, label: str, gpu: int, free_gb: float = 23.6,
                procs: int = 0) -> AlertEvent:
    metrics = _metrics(gpu, label, free_gb, procs)
    lines = [
        f"服务器: {label}  (ubuntu@10.0.0.1:22)",
        f"GPU: #{gpu} NVIDIA GeForce RTX 3090",
        "",
        f"剩余显存: {free_gb} GB / 24 GB (已用 0.0 GB, 0%)",
        "GPU 利用率: 0%   显存利用率: 0%",
        "温度: 41°C   功耗: 27W / 350W",
        f"进程数: {procs}",
        "",
        "判定阈值: free VRAM >= 20G, GPU util <= 5%",
        "连续满足 2 次检查（约 10m）",
        "检测时间: 2026-10-23 11:30:00",
    ]
    return AlertEvent(
        kind="gpu_free",
        title=f"🎮 GPU 空闲可用了：{label} · GPU {gpu}",
        body="\n".join(lines),
        level="info",
        server=server,
        server_label=label,
        gpu_index=gpu,
        timestamp=1_700_000_000.0,
        metrics=metrics,
        lines=lines,
    )


class BatchEventsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.monitor = Monitor.__new__(Monitor)  # batching needs no config

    # --- pass-through ---------------------------------------------------- #

    def test_single_event_passes_through_untouched(self) -> None:
        event = _free_event("s1", "服务器1", 0)
        result = self.monitor._batch_events([event])
        self.assertEqual(len(result), 1)
        self.assertIs(result[0], event, "a lone alert keeps its full detail")
        self.assertEqual(result[0].title, event.title)

    def test_empty_round_yields_nothing(self) -> None:
        self.assertEqual(self.monitor._batch_events([]), [])

    # --- the headline requirement --------------------------------------- #

    def test_sixteen_free_gpus_become_one_email(self) -> None:
        events = [_free_event("s1", "服务器1", i) for i in range(8)]
        events += [_free_event("s2", "服务器2", i) for i in range(8)]

        result = self.monitor._batch_events(events)

        self.assertEqual(len(result), 1, "16 free GPUs must produce exactly one event")
        merged = result[0]
        self.assertEqual(merged.kind, "gpu_free")
        self.assertIn("16", merged.title)
        self.assertIn("2", merged.title)

        # every GPU appears exactly once in the listing
        for index in range(8):
            self.assertIn(f"GPU #{index}  RTX 3090", merged.body)
        self.assertEqual(merged.body.count("剩余 23.6G/24G"), 16)

        # grouped by server, with a count per group
        self.assertIn("【服务器1】8 张", merged.body)
        self.assertIn("【服务器2】8 张", merged.body)

        # short enough to actually read
        self.assertLess(len(merged.lines), 30, "a 16-GPU email must stay compact")

    def test_merged_email_states_the_threshold_once(self) -> None:
        merged = self.monitor._batch_events(
            [_free_event("s1", "服务器1", i) for i in range(4)]
        )[0]
        self.assertEqual(merged.body.count("判定阈值: "), 1)

    def test_batch_is_sorted_by_gpu_index(self) -> None:
        events = [_free_event("s1", "服务器1", i) for i in (5, 1, 3)]
        merged = self.monitor._batch_events(events)[0]
        listed = [line for line in merged.lines if line.startswith("  · GPU #")]
        self.assertEqual(
            [line.split("#")[1].split()[0] for line in listed], ["1", "3", "5"]
        )

    # --- correctness of the merge --------------------------------------- #

    def test_different_kinds_stay_separate(self) -> None:
        free = [_free_event("s1", "服务器1", 0), _free_event("s1", "服务器1", 1)]
        busy = AlertEvent(
            kind="gpu_busy",
            title="GPU 已占用：服务器1 · GPU 2",
            body="x",
            server="s1",
            server_label="服务器1",
            gpu_index=2,
            metrics=_metrics(2, "服务器1", free_gb=1.0, procs=2),
            lines=["服务器: 服务器1", "GPU: #2 NVIDIA GeForce RTX 3090"],
        )
        result = self.monitor._batch_events(free + [busy])

        self.assertEqual(sorted(e.kind for e in result), ["gpu_busy", "gpu_free"])
        free_event = next(e for e in result if e.kind == "gpu_free")
        self.assertIn("2", free_event.title)

    def test_merged_level_is_the_worst_in_the_group(self) -> None:
        mild = _free_event("s1", "服务器1", 0)
        severe = _free_event("s1", "服务器1", 1)
        severe.level = "warning"

        merged = self.monitor._batch_events([mild, severe])[0]
        self.assertEqual(merged.level, "warning")

    def test_process_list_survives_the_merge(self) -> None:
        merged = self.monitor._batch_events(
            [_free_event("s1", "服务器1", 0, procs=2), _free_event("s1", "服务器1", 1)]
        )[0]
        self.assertIn("pid 1234 python train.py", merged.body)

    def test_servers_are_labelled_once_in_the_header(self) -> None:
        merged = self.monitor._batch_events(
            [_free_event("s1", "服务器1", 0), _free_event("s2", "服务器2", 0)]
        )[0]
        self.assertIn("涉及服务器: 服务器1、服务器2", merged.body)
        self.assertEqual(merged.server_label, "服务器1、服务器2")
        self.assertIsNone(merged.gpu_index)
        self.assertEqual(merged.metrics["batched"], 2)

    def test_unreachable_servers_merge_too(self) -> None:
        events = [
            AlertEvent(
                kind="server_unreachable",
                title=f"⚠️ GPU 服务器无法连接：服务器{i}",
                body="x",
                level="critical",
                server=f"s{i}",
                server_label=f"服务器{i}",
                lines=[f"服务器: 服务器{i}", "连续失败: 3 次", "最后错误: timeout"],
            )
            for i in (1, 2, 3)
        ]
        result = self.monitor._batch_events(events)
        self.assertEqual(len(result), 1)
        merged = result[0]
        self.assertEqual(merged.level, "critical")
        self.assertIn("3", merged.title)
        self.assertIn("连续失败: 3 次", merged.body)

    def test_missing_metrics_do_not_crash(self) -> None:
        thin = _free_event("s1", "服务器1", 0)
        thin.metrics = {"gpu_index": 0}
        merged = self.monitor._batch_events([thin, _free_event("s1", "服务器1", 1)])[0]
        self.assertIn("n/a", merged.body)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()