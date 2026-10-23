"""End-to-end tests: real paramiko SSH -> nvidia-smi parse -> analyse -> alert.

These spin up :class:`wheresmygpu.mock_server.MockGpuServer`, so the whole stack is
exercised for real (socket, SSH auth, exec channels, CSV parsing, verdicts,
alert dispatch) without needing a GPU box.
"""

from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wheresmygpu.config import load_config                          # noqa: E402
from wheresmygpu.mock_server import (                               # noqa: E402
    DEFAULT_PASSWORD,
    DEFAULT_USER,
    MockGpu,
    MockGpuCluster,
    MockGpuServer,
)
from wheresmygpu.monitor import Monitor                             # noqa: E402
from wheresmygpu.notify import AlertEvent, Channel                  # noqa: E402
from wheresmygpu.nvidia import NvidiaSmiCollector                   # noqa: E402
from wheresmygpu.ssh_runner import SshError, SshRunner              # noqa: E402


class CapturingChannel(Channel):
    """Notification channel that just remembers what it was asked to send."""

    type = "capture"
    display = "test capture"

    def __init__(self) -> None:
        super().__init__({})
        self.events: list[AlertEvent] = []

    def deliver(self, event: AlertEvent) -> None:
        self.events.append(event)

    def kinds(self) -> list[str]:
        return [e.kind for e in self.events]


CONFIG_TEMPLATE = """
app:
  interval_seconds: 10
  state_file: state.json
  history_file: history.jsonl
  history_csv: history.csv
  log_level: WARNING
  console: false

ssh:
  transport: paramiko
  connect_timeout: 8
  command_timeout: 15
  max_retries: 0
  reuse_connection: true

alert:
  min_free_memory_gb: 20.0
  max_gpu_utilization: 5.0
  max_memory_utilization: 10.0
  sustained_checks: 1
  cooldown_minutes: 0
  notify_on_unreachable: true
  unreachable_after_failures: 1
  notify_on_recovery: true

servers:
  - name: mock-1
    label: 模拟机
    host: 127.0.0.1
    port: {port}
    user: {user}
    password: {password}

notify:
  channels:
    - type: console
"""


class EndToEndBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="wheresmygpu-e2e-")
        self.tmpdir = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def make_config(self, port: int, **overrides):
        text = CONFIG_TEMPLATE.format(
            port=port, user=DEFAULT_USER, password=DEFAULT_PASSWORD
        )
        for key, value in overrides.items():
            text = text.replace(key, value)
        path = self.tmpdir / "config.yaml"
        path.write_text(text, encoding="utf-8")
        return load_config(path)

    def make_monitor(self, port: int, **overrides):
        monitor = Monitor(self.make_config(port, **overrides))
        capture = CapturingChannel()
        monitor.channels = [capture]
        self.addCleanup(monitor.close)
        return monitor, capture


class TestCollectorOverRealSsh(EndToEndBase):
    def test_reads_gpus_and_processes(self):
        cluster = MockGpuCluster([MockGpu.busy(), MockGpu()])
        with MockGpuServer(cluster) as server:
            config = self.make_config(server.port)
            runner = SshRunner(config.servers[0], config)
            self.addCleanup(runner.close)

            collector = NvidiaSmiCollector(config.servers[0], config, runner)
            snapshot = collector.collect()

            self.assertTrue(snapshot.ok, f"collect failed: {snapshot.error}")
            self.assertEqual(len(snapshot.gpus), 2)
            self.assertEqual(snapshot.driver_version, "535.104.05")

            busy, idle = snapshot.gpus
            self.assertEqual(busy.index, 0)
            self.assertEqual(busy.gpu_util, 98.0)
            self.assertAlmostEqual(busy.mem_free_gb, 1476 / 1024, places=2)
            self.assertEqual(len(busy.processes), 2)
            # `ps` enrichment supplies the owning user
            self.assertEqual(busy.processes[0].user, "alice")
            self.assertEqual(busy.processes[0].runtime, "01:23:45")

            self.assertEqual(idle.index, 1)
            self.assertEqual(idle.gpu_util, 0.0)
            self.assertAlmostEqual(idle.mem_free_gb, 24064 / 1024, places=2)
            self.assertEqual(idle.processes, [])

    def test_nvidia_smi_discovery_is_cached(self):
        with MockGpuServer(MockGpuCluster([MockGpu()])) as server:
            config = self.make_config(server.port)
            runner = SshRunner(config.servers[0], config)
            self.addCleanup(runner.close)

            self.assertEqual(runner.resolve_nvidia_smi(), "nvidia-smi")
            probes_after_first = sum("command -v" in c for c in server.commands)
            runner.resolve_nvidia_smi()
            self.assertEqual(
                sum("command -v" in c for c in server.commands), probes_after_first
            )

    def test_wrong_password_is_reported_clearly(self):
        with MockGpuServer(MockGpuCluster([MockGpu()])) as server:
            config = self.make_config(server.port, )
            config.servers[0].password = "definitely-wrong"
            runner = SshRunner(config.servers[0], config)
            self.addCleanup(runner.close)

            collector = NvidiaSmiCollector(config.servers[0], config, runner)
            snapshot = collector.collect()
            self.assertFalse(snapshot.ok)
            self.assertIn("Authentication", snapshot.error or "")

    def test_connection_refused_is_reported(self):
        config = self.make_config(1)  # nothing listens on port 1
        runner = SshRunner(config.servers[0], config)
        self.addCleanup(runner.close)
        collector = NvidiaSmiCollector(config.servers[0], config, runner)
        snapshot = collector.collect()
        self.assertFalse(snapshot.ok)
        self.assertIsNotNone(snapshot.error)


class TestMonitorOverRealSsh(EndToEndBase):
    def test_alert_fires_when_a_gpu_is_free(self):
        cluster = MockGpuCluster([MockGpu.busy(), MockGpu()])
        with MockGpuServer(cluster) as server:
            monitor, capture = self.make_monitor(server.port)
            snapshots = monitor.run_round()

            self.assertTrue(all(s.ok for s in snapshots), snapshots)
            self.assertEqual(capture.kinds(), ["gpu_free"])
            event = capture.events[0]
            self.assertEqual(event.gpu_index, 1)
            self.assertEqual(event.server, "mock-1")
            self.assertGreater(event.metrics["mem_free_gb"], 20.0)
            self.assertLess(event.metrics["gpu_util_pct"], 5.0)
            self.assertIn("GPU 1", event.title)

            # history + state were written
            self.assertTrue((self.tmpdir / "history.jsonl").is_file())
            self.assertTrue((self.tmpdir / "state.json").is_file())

    def test_no_alert_while_everything_is_busy(self):
        cluster = MockGpuCluster([MockGpu.busy(), MockGpu.busy()])
        with MockGpuServer(cluster) as server:
            monitor, capture = self.make_monitor(server.port)
            monitor.run_round()
            self.assertEqual(capture.kinds(), [])

    def test_alert_when_a_busy_gpu_frees_up(self):
        cluster = MockGpuCluster([MockGpu.busy(), MockGpu.busy()])
        with MockGpuServer(cluster) as server:
            monitor, capture = self.make_monitor(server.port)

            monitor.run_round()
            self.assertEqual(capture.kinds(), [], "nothing free yet")

            cluster.gpus[0].free_up()
            monitor.run_round()
            self.assertEqual(capture.kinds(), ["gpu_free"])
            self.assertEqual(capture.events[0].gpu_index, 0)

    def test_sustained_checks_debounce_a_single_sample(self):
        cluster = MockGpuCluster([MockGpu()])
        with MockGpuServer(cluster) as server:
            monitor, capture = self.make_monitor(
                server.port, **{"sustained_checks: 1": "sustained_checks: 2"}
            )
            monitor.run_round()
            self.assertEqual(capture.kinds(), [], "first sample must not alert")
            monitor.run_round()
            self.assertEqual(capture.kinds(), ["gpu_free"], "second sample alerts")

    def test_unreachable_then_recovery(self):
        cluster = MockGpuCluster([MockGpu()])
        server = MockGpuServer(cluster).start()
        port = server.port
        monitor, capture = self.make_monitor(port)

        monitor.run_round()
        server.stop()
        time.sleep(0.3)

        for _ in range(3):
            monitor.run_round()
        self.assertIn("server_unreachable", capture.kinds())

        # bring an equivalent server back on the same port
        revived = MockGpuServer(cluster, port=port).start()
        self.addCleanup(revived.stop)
        time.sleep(0.3)
        monitor.run_round()
        self.assertIn("server_recovered", capture.kinds())

    def test_render_table_and_dry_run_do_not_send(self):
        cluster = MockGpuCluster([MockGpu()])
        with MockGpuServer(cluster) as server:
            config = self.make_config(server.port)
            monitor = Monitor(config, dry_run=True)
            self.addCleanup(monitor.close)
            capture = CapturingChannel()
            monitor.channels = [capture]

            snapshots = monitor.run_round()
            self.assertEqual(capture.kinds(), [], "dry-run must not dispatch")
            table = monitor.render_table(snapshots)
            self.assertIn("FREE", table)
            self.assertIn("模拟机", table)


if __name__ == "__main__":
    unittest.main(verbosity=2)