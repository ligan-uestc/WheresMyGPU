"""Unit tests for WheresMyGPU. Run with:  python -m unittest discover -s tests -v"""

from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wheresmygpu.analyzer import (                                   # noqa: E402
    evaluate,
    rule_description,
    unsatisfiable_reason,
)
from wheresmygpu.config import AlertRule, ConfigError, load_config   # noqa: E402
from wheresmygpu.history import HistoryWriter                        # noqa: E402
from wheresmygpu.nvidia import GpuInfo, NvidiaSmiCollector, ServerSnapshot  # noqa: E402
from wheresmygpu.state import MonitorState                           # noqa: E402

# --------------------------------------------------------------------------- #
# Realistic nvidia-smi fixtures
# --------------------------------------------------------------------------- #

GPU_CSV = """0, GPU-4f2a1b9c-1111-2222-3333-444455556666, NVIDIA GeForce RTX 3090, 24576, 23100, 1476, 98, 45, 71, 341.20, 370.00
1, GPU-9e8d7c6b-aaaa-bbbb-cccc-ddddeeeeffff, NVIDIA GeForce RTX 3090, 24576, 512, 24064, 0, 3, 42, 18.55, 370.00
"""

GPU_CSV_WITH_NA = """0, GPU-xxxx, NVIDIA A100-SXM4-40GB, 40960, 40960, 0, [N/A], [N/A], [N/A], [N/A], [N/A]
"""

PROC_CSV = """GPU-4f2a1b9c-1111-2222-3333-444455556666, 12345, python, 22000
GPU-4f2a1b9c-1111-2222-3333-444455556666, 12399, /usr/bin/python3.10, 1100
"""

PS_OUT = """  12345 alice    3-04:15:22
  12399 bob         12:03:44
"""


def make_gpu(index=0, free_mb=24000.0, total_mb=24576.0, util=0.0,
             mem_util=0.0, procs=None) -> GpuInfo:
    used = total_mb - free_mb
    return GpuInfo(
        index=index,
        uuid=f"GPU-test-{index}",
        name="NVIDIA GeForce RTX 3090",
        mem_total_mb=total_mb,
        mem_used_mb=used,
        mem_free_mb=free_mb,
        util_gpu=util,
        util_mem=mem_util,
        temperature_c=40.0,
        power_w=20.0,
        power_limit_w=370.0,
        processes=list(procs or []),
    )


CONFIG_TEMPLATE = """
app:
  interval_seconds: 300
  state_file: state.json
ssh:
  transport: paramiko
alert:
  min_free_memory_gb: 20
  max_gpu_utilization: 5
  max_memory_utilization: 10
  sustained_checks: 2
  cooldown_minutes: 60
servers:
  - name: gpu-a
    label: A 机
    host: 10.0.0.1
    port: 2222
    user: ubuntu
    password: secret-a
  - name: gpu-b
    label: B 机
    host: 10.0.0.2
    port: 2223
    user: ubuntu
    password: ${WHERESMYGPU_TEST_PW:-}
    key_file: /tmp/id_rsa
    alert:
      min_free_memory_gb: 8
notify:
  channels:
    - type: console
    - type: logfile
"""


class TempConfigMixin:
    def write_config(self, text: str) -> Path:
        tmpdir = tempfile.mkdtemp(prefix="wheresmygpu-test-")
        path = Path(tmpdir) / "config.yaml"
        path.write_text(text, encoding="utf-8")
        self.addCleanup(lambda: None)
        return path


# --------------------------------------------------------------------------- #
# nvidia-smi parsing
# --------------------------------------------------------------------------- #

class TestParsing(unittest.TestCase):
    def test_parse_gpus(self):
        gpus = NvidiaSmiCollector.parse_gpus(GPU_CSV)
        self.assertEqual(len(gpus), 2)

        busy, idle = gpus
        self.assertEqual(busy.index, 0)
        self.assertAlmostEqual(busy.mem_total_gb, 24.0, places=2)
        self.assertAlmostEqual(busy.mem_used_gb, 22.56, places=2)
        self.assertAlmostEqual(busy.mem_free_gb, 1.44, places=2)
        self.assertEqual(busy.gpu_util, 98.0)
        self.assertEqual(busy.memory_util, 45.0)
        self.assertEqual(busy.temperature_c, 71.0)
        self.assertEqual(busy.power_w, 341.2)

        self.assertEqual(idle.index, 1)
        self.assertAlmostEqual(idle.mem_free_gb, 23.5, places=2)
        self.assertEqual(idle.gpu_util, 0.0)

    def test_parse_gpus_handles_na(self):
        gpus = NvidiaSmiCollector.parse_gpus(GPU_CSV_WITH_NA)
        self.assertEqual(len(gpus), 1)
        gpu = gpus[0]
        self.assertIsNone(gpu.util_gpu)
        self.assertIsNone(gpu.temperature_c)
        self.assertEqual(gpu.gpu_util, 0.0)          # None treated as 0 for rules
        self.assertEqual(gpu.mem_free_gb, 0.0)

    def test_parse_gpus_fills_missing_free_column(self):
        line = "0, GPU-x, RTX 3090, 24576, 1000\n"
        gpus = NvidiaSmiCollector.parse_gpus(line)
        self.assertAlmostEqual(gpus[0].mem_free_gb, 23576 / 1024, places=3)

    def test_parse_gpus_empty(self):
        self.assertEqual(NvidiaSmiCollector.parse_gpus(""), [])

    def test_parse_processes_groups_by_uuid(self):
        flat, grouped = NvidiaSmiCollector.parse_processes(PROC_CSV)
        self.assertEqual(len(flat), 2)
        self.assertEqual(flat[0].pid, 12345)
        self.assertEqual(flat[0].name, "python")
        self.assertAlmostEqual(flat[0].used_memory_gb, 22000 / 1024, places=3)
        self.assertEqual(len(grouped["GPU-4f2a1b9c-1111-2222-3333-444455556666"]), 2)

    def test_parse_processes_skips_header_and_empty(self):
        flat, grouped = NvidiaSmiCollector.parse_processes(
            "gpu_uuid, pid, process_name, used_memory\n"
        )
        self.assertEqual(flat, [])
        self.assertEqual(grouped, {})

    def test_parse_ps(self):
        table = NvidiaSmiCollector.parse_ps(PS_OUT)
        self.assertEqual(table[12345], ("alice", "3-04:15:22"))
        self.assertEqual(table[12399], ("bob", "12:03:44"))

    def test_attach_processes_by_uuid(self):
        gpus = NvidiaSmiCollector.parse_gpus(GPU_CSV)
        flat, _ = NvidiaSmiCollector.parse_processes(PROC_CSV)
        NvidiaSmiCollector._attach_processes(gpus, flat)
        self.assertEqual(len(gpus[0].processes), 2)
        self.assertEqual(len(gpus[1].processes), 0)
        self.assertAlmostEqual(gpus[0].process_memory_gb, 23100 / 1024, places=2)

    def test_attach_processes_single_gpu_fallback(self):
        gpus = NvidiaSmiCollector.parse_gpus(
            "0, GPU-x, RTX 3090, 24576, 1000, 23576, 0, 0, 40, 20, 370\n"
        )
        flat, _ = NvidiaSmiCollector.parse_processes(
            "no_uuid, 777, python, 900\n"
        )
        NvidiaSmiCollector._attach_processes(gpus, flat)
        self.assertEqual(len(gpus[0].processes), 1)

    def test_process_describe(self):
        flat, _ = NvidiaSmiCollector.parse_processes(PROC_CSV)
        flat[0].user, flat[0].runtime = "alice", "3-04:15:22"
        text = flat[0].describe()
        self.assertIn("alice", text)
        self.assertIn("12345", text)


# --------------------------------------------------------------------------- #
# Threshold analysis
# --------------------------------------------------------------------------- #

class TestAnalyzer(unittest.TestCase):
    def rule(self, **kw) -> AlertRule:
        base = dict(
            min_free_memory_gb=20.0,
            max_gpu_utilization=5.0,
            max_memory_utilization=10.0,
        )
        base.update(kw)
        return AlertRule.from_dict(base, "test")

    def test_idle_gpu_is_free(self):
        gpu = make_gpu(free_mb=24064.0, util=0.0, mem_util=3.0)
        verdict = evaluate(gpu, self.rule(), "s", "server")
        self.assertTrue(verdict.is_free)
        self.assertEqual(verdict.failing(), [])

    def test_busy_memory_blocks(self):
        gpu = make_gpu(free_mb=1476.0, util=98.0, mem_util=45.0)
        verdict = evaluate(gpu, self.rule(), "s", "server")
        self.assertFalse(verdict.is_free)
        names = {c.name for c in verdict.failing()}
        self.assertIn("free_memory", names)
        self.assertIn("gpu_utilization", names)

    def test_all_conditions_are_anded(self):
        # plenty of free VRAM, but the GPU is busy -> not free
        gpu = make_gpu(free_mb=24000.0, util=80.0)
        verdict = evaluate(gpu, self.rule(), "s", "server")
        self.assertFalse(verdict.is_free)
        self.assertEqual([c.name for c in verdict.failing()], ["gpu_utilization"])

    def test_24gb_card_can_never_satisfy_24gb_rule(self):
        """Regression guard for the 3090 case.

        A 3090 reports 24576 MiB total (= exactly 24 GiB), and in practice shows
        slightly less than that as free. Requiring ">= 24G free" therefore asks
        for a 100% empty card and would never fire; 20G is the sane setting.
        """
        idle_3090 = make_gpu(index=1, free_mb=24064.0, util=0.0, mem_util=0.0)
        self.assertAlmostEqual(idle_3090.mem_total_gb, 24.0, places=2)
        self.assertAlmostEqual(idle_3090.mem_free_gb, 23.5, places=2)

        strict = self.rule(min_free_memory_gb=24.0)
        self.assertFalse(evaluate(idle_3090, strict, "s", "server").is_free)

        sane = self.rule(min_free_memory_gb=20.0)
        self.assertTrue(evaluate(idle_3090, sane, "s", "server").is_free)

        # the impossible threshold is detectable, so the app can warn about it
        self.assertIsNotNone(unsatisfiable_reason(idle_3090, strict))
        self.assertIsNone(unsatisfiable_reason(idle_3090, sane))

    def test_percentage_rule(self):
        gpu = make_gpu(free_mb=24000.0, util=0.0, mem_util=2.0)
        rule = self.rule(min_free_memory_pct=90.0)
        self.assertTrue(evaluate(gpu, rule, "s", "server").is_free)

        tight = self.rule(min_free_memory_pct=99.0)
        verdict = evaluate(gpu, tight, "s", "server")
        self.assertFalse(verdict.is_free)
        self.assertIn("free_memory_pct", [c.name for c in verdict.failing()])

    def test_optional_conditions_can_be_disabled(self):
        gpu = make_gpu(free_mb=24000.0, util=0.0, mem_util=99.0)
        rule = self.rule(max_memory_utilization=None)
        self.assertTrue(evaluate(gpu, rule, "s", "server").is_free)

    def test_require_no_processes(self):
        from wheresmygpu.nvidia import ProcessInfo

        gpu = make_gpu(free_mb=24000.0, procs=[ProcessInfo(pid=1, name="sleep", used_memory_mb=10.0)])
        rule = self.rule(require_no_processes=True)
        verdict = evaluate(gpu, rule, "s", "server")
        self.assertFalse(verdict.is_free)
        self.assertIn("no_processes", [c.name for c in verdict.failing()])

    def test_max_process_memory(self):
        from wheresmygpu.nvidia import ProcessInfo

        gpu = make_gpu(
            free_mb=24000.0,
            procs=[ProcessInfo(pid=1, name="train", used_memory_mb=5000.0)],
        )
        rule = self.rule(max_process_memory_gb=1.0)
        self.assertFalse(evaluate(gpu, rule, "s", "server").is_free)
        loose = self.rule(max_process_memory_gb=10.0)
        self.assertTrue(evaluate(gpu, loose, "s", "server").is_free)

    def test_metrics_and_description(self):
        gpu = make_gpu(free_mb=24064.0, util=0.0, mem_util=3.0)
        verdict = evaluate(gpu, self.rule(), "s", "server")
        metrics = verdict.metrics()
        self.assertEqual(metrics["gpu_index"], 0)
        self.assertTrue(metrics["is_free"])
        self.assertIn("free VRAM", rule_description(self.rule()))
        self.assertTrue(verdict.detail_lines())


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

class TestConfig(TempConfigMixin, unittest.TestCase):
    def load(self, text=CONFIG_TEMPLATE):
        return load_config(self.write_config(text))

    def test_loads_two_servers(self):
        config = self.load()
        self.assertEqual(len(config.enabled_servers()), 2)
        self.assertEqual(config.app.interval_seconds, 300)
        self.assertEqual(config.servers[0].port, 2222)
        self.assertEqual(config.servers[0].resolved_password(), "secret-a")

    def test_per_server_alert_override(self):
        config = self.load()
        rule_a = config.alert_for(config.servers[0])
        rule_b = config.alert_for(config.servers[1])
        self.assertEqual(rule_a.min_free_memory_gb, 20.0)
        self.assertEqual(rule_b.min_free_memory_gb, 8.0)
        # untouched fields are inherited
        self.assertEqual(rule_b.max_gpu_utilization, 5.0)

    def test_relative_paths_resolve_against_config_dir(self):
        config = self.load()
        self.assertEqual(config.resolve_path("logs"), config.path.parent / "logs")

    def test_absolute_paths_are_kept(self):
        config = self.load()
        self.assertEqual(config.resolve_path("D:/tmp/x.log"), Path("D:/tmp/x.log"))

    def test_env_expansion_with_default(self):
        config = self.load()
        # ${WHERESMYGPU_TEST_PW:-} with the variable unset yields an empty password
        self.assertIsNone(config.servers[1].password)
        self.assertEqual(config.servers[1].key_file, "/tmp/id_rsa")

    def test_missing_env_var_without_default_raises(self):
        text = CONFIG_TEMPLATE.replace("${WHERESMYGPU_TEST_PW:-}", "${WHERESMYGPU_DEFINITELY_UNSET}")
        with self.assertRaises(ConfigError):
            self.load(text)

    def test_duplicate_server_name_rejected(self):
        text = CONFIG_TEMPLATE.replace("name: gpu-b", "name: gpu-a")
        with self.assertRaises(ConfigError):
            self.load(text)

    def test_server_without_credentials_rejected(self):
        text = CONFIG_TEMPLATE.replace("password: secret-a", "password: null")
        with self.assertRaises(ConfigError):
            self.load(text)

    def test_unknown_transport_rejected(self):
        text = CONFIG_TEMPLATE.replace("transport: paramiko", "transport: telnet")
        with self.assertRaises(ConfigError):
            self.load(text)

    def test_interval_below_minimum_rejected(self):
        text = CONFIG_TEMPLATE.replace("interval_seconds: 300", "interval_seconds: 1")
        with self.assertRaises(ConfigError):
            self.load(text)

    def test_redacted_hides_password(self):
        config = self.load()
        blob = json.dumps(config.redacted(), ensure_ascii=False)
        self.assertNotIn("secret-a", blob)
        self.assertIn("***", blob)

    def test_empty_servers_rejected(self):
        text = "servers: []\n"
        with self.assertRaises(ConfigError):
            self.load(text)


# --------------------------------------------------------------------------- #
# State persistence
# --------------------------------------------------------------------------- #

class TestState(unittest.TestCase):
    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            state = MonitorState.load(path)
            gpu = state.gpu("gpu-a", 0)
            gpu.consecutive_free = 3
            gpu.last_state = "free"
            gpu.last_alert_ts = 1234.5
            gpu.free_since = 1000.0
            state.server("gpu-a").consecutive_failures = 2
            state.rounds = 7
            state.alerts_sent = 4
            state.save()

            restored = MonitorState.load(path)
            self.assertEqual(restored.rounds, 7)
            self.assertEqual(restored.alerts_sent, 4)
            r_gpu = restored.gpu("gpu-a", 0)
            self.assertEqual(r_gpu.consecutive_free, 3)
            self.assertEqual(r_gpu.last_state, "free")
            self.assertEqual(r_gpu.last_alert_ts, 1234.5)
            self.assertEqual(r_gpu.free_since, 1000.0)
            self.assertEqual(restored.server("gpu-a").consecutive_failures, 2)

    def test_corrupt_state_starts_fresh_and_backs_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text("{not json at all", encoding="utf-8")
            state = MonitorState.load(path)
            self.assertEqual(state.rounds, 0)
            self.assertTrue(path.with_suffix(".json.corrupt").is_file())

    def test_missing_state_is_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = MonitorState.load(Path(tmp) / "nope.json")
            self.assertEqual(state.servers, {})

    def test_cooldown_logic(self):
        now = 10_000.0
        self.assertTrue(MonitorState.cooldown_elapsed(0.0, 60, now))
        self.assertFalse(MonitorState.cooldown_elapsed(now - 60, 60, now))
        self.assertTrue(MonitorState.cooldown_elapsed(now - 3600, 60, now))
        self.assertTrue(MonitorState.cooldown_elapsed(now, 0, now))
        self.assertAlmostEqual(
            MonitorState.cooldown_remaining(now - 60, 60, now), 3540.0, places=1
        )


# --------------------------------------------------------------------------- #
# History
# --------------------------------------------------------------------------- #

class TestHistory(unittest.TestCase):
    def test_writes_jsonl_and_csv(self):
        gpus = NvidiaSmiCollector.parse_gpus(GPU_CSV)
        snapshot = ServerSnapshot(
            server="gpu-a", label="A", ok=True, timestamp=time.time(), gpus=gpus
        )
        rule = AlertRule.from_dict(
            {"min_free_memory_gb": 20, "max_gpu_utilization": 5}, "test"
        )
        verdicts = [evaluate(g, rule, "gpu-a", "A") for g in gpus]

        with tempfile.TemporaryDirectory() as tmp:
            jsonl = Path(tmp) / "h.jsonl"
            csv = Path(tmp) / "h.csv"
            writer = HistoryWriter(jsonl, csv)
            writer.record(snapshot, verdicts)

            lines = [json.loads(x) for x in jsonl.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(lines), 2)
            self.assertEqual(lines[1]["is_free"], True)
            self.assertEqual(lines[0]["is_free"], False)

            text = csv.read_text(encoding="utf-8-sig")
            self.assertIn("gpu_index", text)
            self.assertEqual(len(text.strip().splitlines()), 3)  # header + 2 rows

            # second round must not duplicate the header
            writer.record(snapshot, verdicts)
            text2 = csv.read_text(encoding="utf-8-sig")
            self.assertEqual(text2.count("gpu_index"), 1)

    def test_records_failures(self):
        snapshot = ServerSnapshot(
            server="gpu-a", label="A", ok=False, timestamp=time.time(),
            error="connection refused",
        )
        with tempfile.TemporaryDirectory() as tmp:
            jsonl = Path(tmp) / "h.jsonl"
            HistoryWriter(jsonl, None).record(snapshot, [])
            row = json.loads(jsonl.read_text(encoding="utf-8").strip())
            self.assertFalse(row["ok"])
            self.assertEqual(row["error"], "connection refused")


# --------------------------------------------------------------------------- #
# Alert decision state machine
# --------------------------------------------------------------------------- #

class TestAlertDecisions(TempConfigMixin, unittest.TestCase):
    def make_monitor(self, config_text=CONFIG_TEMPLATE):
        from wheresmygpu.monitor import Monitor

        config = load_config(self.write_config(config_text))
        monitor = Monitor(config, dry_run=True)
        self.addCleanup(monitor.close)
        return monitor

    @staticmethod
    def snapshot(gpu, name="gpu-a", label="A"):
        return ServerSnapshot(
            server=name, label=label, ok=True, timestamp=time.time(), gpus=[gpu]
        )

    def test_alerts_only_after_sustained_checks(self):
        monitor = self.make_monitor()
        rule = monitor.config.alert_for(monitor.config.servers[0])
        self.assertEqual(rule.sustained_checks, 2)
        gpu = make_gpu(free_mb=24064.0, util=0.0, mem_util=3.0)
        snap = self.snapshot(gpu)
        now = time.time()

        _v, first = monitor._handle_gpu(snap, gpu, rule, now)
        self.assertIsNone(first, "must not alert on the first free sample")
        self.assertEqual(monitor.state.gpu("gpu-a", 0).consecutive_free, 1)

        _v, second = monitor._handle_gpu(snap, gpu, rule, now + 300)
        self.assertIsNotNone(second, "must alert on the second consecutive free sample")
        self.assertEqual(second.kind, "gpu_free")
        self.assertEqual(second.gpu_index, 0)
        self.assertIn("A", second.title)

    def test_cooldown_suppresses_repeat_alert(self):
        monitor = self.make_monitor()
        rule = monitor.config.alert_for(monitor.config.servers[0])
        gpu = make_gpu(free_mb=24064.0, util=0.0, mem_util=3.0)
        snap = self.snapshot(gpu)
        base = time.time()

        monitor._handle_gpu(snap, gpu, rule, base)
        _v, event = monitor._handle_gpu(snap, gpu, rule, base + 300)
        self.assertIsNotNone(event)

        # still free, well inside the 60 minute cooldown
        _v, repeat = monitor._handle_gpu(snap, gpu, rule, base + 600)
        self.assertIsNone(repeat, "cooldown must suppress the repeat alert")

        # the cooldown runs from the *last alert*, so a reminder becomes
        # possible only after that point plus cooldown_minutes
        last_alert = base + 300
        _v, still_suppressed = monitor._handle_gpu(snap, gpu, rule, last_alert + 3599)
        self.assertIsNone(still_suppressed, "just inside the cooldown window")

        _v, later = monitor._handle_gpu(snap, gpu, rule, last_alert + 3601)
        self.assertIsNotNone(later, "reminder allowed once the cooldown expires")

    def test_busy_gpu_never_alerts(self):
        monitor = self.make_monitor()
        rule = monitor.config.alert_for(monitor.config.servers[0])
        gpu = make_gpu(free_mb=1476.0, util=98.0, mem_util=45.0)
        snap = self.snapshot(gpu)
        now = time.time()
        for step in range(5):
            _v, event = monitor._handle_gpu(snap, gpu, rule, now + step * 300)
            self.assertIsNone(event)
        self.assertEqual(monitor.state.gpu("gpu-a", 0).last_state, "busy")

    def test_noise_sample_resets_the_streak(self):
        """A single low-utilization sample must not be enough on its own."""
        monitor = self.make_monitor()
        rule = monitor.config.alert_for(monitor.config.servers[0])
        free_gpu = make_gpu(free_mb=24064.0, util=0.0, mem_util=3.0)
        busy_gpu = make_gpu(free_mb=1476.0, util=98.0, mem_util=45.0)
        now = time.time()

        monitor._handle_gpu(self.snapshot(free_gpu), free_gpu, rule, now)
        _v, event = monitor._handle_gpu(self.snapshot(busy_gpu), busy_gpu, rule, now + 300)
        self.assertIsNone(event)
        self.assertEqual(monitor.state.gpu("gpu-a", 0).consecutive_free, 0)

    def test_notify_on_busy_opt_in(self):
        text = CONFIG_TEMPLATE.replace(
            "cooldown_minutes: 60", "cooldown_minutes: 60\n  notify_on_busy: true"
        )
        monitor = self.make_monitor(text)
        rule = monitor.config.alert_for(monitor.config.servers[0])
        self.assertTrue(rule.notify_on_busy)

        free_gpu = make_gpu(free_mb=24064.0, util=0.0, mem_util=3.0)
        busy_gpu = make_gpu(free_mb=1476.0, util=98.0, mem_util=45.0)
        base = time.time()

        monitor._handle_gpu(self.snapshot(free_gpu), free_gpu, rule, base)
        monitor._handle_gpu(self.snapshot(free_gpu), free_gpu, rule, base + 300)
        self.assertEqual(monitor.state.gpu("gpu-a", 0).last_state, "free")

        # the 60 minute cooldown is measured from the last alert (base + 300)
        _v, event = monitor._handle_gpu(
            self.snapshot(busy_gpu), busy_gpu, rule, base + 300 + 3601
        )
        self.assertIsNotNone(event)
        self.assertEqual(event.kind, "gpu_busy")

    def test_unreachable_alert_after_threshold(self):
        monitor = self.make_monitor()
        rule = monitor.config.alert_for(monitor.config.servers[0])
        self.assertEqual(rule.unreachable_after_failures, 3)

        def fail(ts):
            snap = ServerSnapshot(
                server="gpu-a", label="A", ok=False, timestamp=ts, error="timeout"
            )
            return monitor._handle_server(snap, rule, ts)

        self.assertEqual(fail(time.time()), [])
        self.assertEqual(fail(time.time() + 1), [])
        events = fail(time.time() + 2)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].kind, "server_unreachable")
        self.assertEqual(events[0].level, "critical")
        # must not repeat every round
        self.assertEqual(fail(time.time() + 3), [])

    def test_recovery_alert(self):
        monitor = self.make_monitor()
        rule = monitor.config.alert_for(monitor.config.servers[0])
        now = time.time()
        for step in range(3):
            monitor._handle_server(
                ServerSnapshot(server="gpu-a", label="A", ok=False,
                               timestamp=now + step, error="timeout"),
                rule, now + step,
            )
        gpu = make_gpu(free_mb=24064.0)
        events = monitor._handle_server(
            self.snapshot(gpu), rule, now + 10
        )
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].kind, "server_recovered")
        self.assertEqual(monitor.state.server("gpu-a").consecutive_failures, 0)

    def test_render_table_marks_free_and_unreachable(self):
        monitor = self.make_monitor()
        free = make_gpu(index=1, free_mb=24064.0, util=0.0, mem_util=3.0)
        busy = make_gpu(index=0, free_mb=1476.0, util=98.0, mem_util=45.0)
        both = ServerSnapshot(
            server="gpu-a", label="A", ok=True, timestamp=time.time(),
            gpus=[busy, free],
        )
        table = monitor.render_table(
            [
                both,
                ServerSnapshot(server="gpu-b", label="B", ok=False,
                               timestamp=time.time(), error="boom"),
            ]
        )
        self.assertIn("FREE", table)
        self.assertIn("UNREACHABLE", table)
        self.assertIn("busy", table)


# --------------------------------------------------------------------------- #
# Notification channels
# --------------------------------------------------------------------------- #

class TestChannels(unittest.TestCase):
    def test_registry_is_complete(self):
        from wheresmygpu.notify import CHANNEL_TYPES

        for expected in ("console", "logfile", "windows_toast", "sound", "webhook",
                         "feishu", "dingtalk", "wecom", "email", "telegram",
                         "serverchan", "slack", "discord", "jsonl", "command", "bark"):
            self.assertIn(expected, CHANNEL_TYPES)

    def test_console_and_logfile_deliver(self):
        from wheresmygpu.notify import AlertEvent, ConsoleChannel, LogFileChannel

        event = AlertEvent(
            kind="test", title="t", body="b", level="warning",
            lines=["line one", "line two"],
        )
        self.assertTrue(ConsoleChannel({}).send(event))
        with tempfile.TemporaryDirectory() as tmp:
            channel = LogFileChannel({"path": "alerts.log"}, base_dir=Path(tmp))
            self.assertTrue(channel.send(event))
            written = (Path(tmp) / "alerts.log").read_text(encoding="utf-8")
            self.assertIn("line one", written)

    def test_disabled_channel_is_skipped(self):
        from wheresmygpu.notify import AlertEvent, ConsoleChannel

        channel = ConsoleChannel({"enabled": False})
        self.assertFalse(channel.send(AlertEvent(kind="t", title="t", body="b")))

    def test_webhook_template_rendering(self):
        from wheresmygpu.notify import AlertEvent, _render_template

        event = AlertEvent(
            kind="gpu_free", title="GPU 空闲", body="b", server="gpu-a",
            server_label="A", gpu_index=2,
        )
        out = _render_template(
            {"content": "{title} on {server} gpu {gpu_index}", "n": 5},
            event.to_payload(),
            event,
        )
        self.assertEqual(out["content"], "GPU 空闲 on gpu-a gpu 2")
        self.assertEqual(out["n"], 5)

    def test_im_error_response_detected(self):
        from wheresmygpu.notify import _check_im_response

        _check_im_response('{"code":0}', "feishu", ("code",))
        with self.assertRaises(RuntimeError):
            _check_im_response('{"code":19001,"msg":"invalid"}', "feishu", ("code",))
        with self.assertRaises(RuntimeError):
            _check_im_response('{"errcode":310000,"errmsg":"keyword"}', "dingtalk", ("errcode",))

    def test_build_channels_from_config(self):
        from wheresmygpu.notify import build_channels

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            path.write_text(CONFIG_TEMPLATE, encoding="utf-8")
            channels = build_channels(load_config(path))
            kinds = [c.type for c in channels]
            self.assertIn("console", kinds)
            self.assertIn("logfile", kinds)

    def test_unknown_channel_is_ignored(self):
        from wheresmygpu.notify import build_channels

        text = CONFIG_TEMPLATE.replace(
            "    - type: logfile", "    - type: logfile\n    - type: nonsense"
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            path.write_text(text, encoding="utf-8")
            kinds = [c.type for c in build_channels(load_config(path))]
            self.assertNotIn("nonsense", kinds)
            self.assertIn("console", kinds)


if __name__ == "__main__":
    unittest.main(verbosity=2)