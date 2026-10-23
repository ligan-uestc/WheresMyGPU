"""A tiny in-process SSH server that emulates a GPU box running ``nvidia-smi``.

It exists so the whole pipeline (SSH -> parse -> analyse -> notify) can be
exercised without touching a real GPU server, and so you can try the monitor out
before configuring credentials.

Try it:

    # terminal 1 — fake GPU box on port 2222, starts busy then frees up after 20s
    python -m wheresmygpu.mock_server --port 2222 --gpus 2 --busy 1 --idle-after 20

    # terminal 2 — point a config at it
    python -m wheresmygpu --config demo.yaml --interval 5s
"""

from __future__ import annotations

import argparse
import logging
import queue
import socket
import threading
import time
from dataclasses import dataclass, field

log = logging.getLogger("wheresmygpu.mock")

DEFAULT_PASSWORD = "wheresmygpu-demo"
DEFAULT_USER = "ubuntu"
SMI = "nvidia-smi"


# --------------------------------------------------------------------------- #
# Fake hardware
# --------------------------------------------------------------------------- #

@dataclass
class MockGpu:
    """One emulated GPU."""

    name: str = "NVIDIA GeForce RTX 3090"
    total_mb: int = 24576
    used_mb: int = 512
    util: int = 0
    mem_util: int = 3
    temp: int = 40
    power: float = 20.0
    power_limit: float = 370.0
    processes: list[tuple[int, str, int]] = field(default_factory=list)

    @classmethod
    def busy(cls, name: str = "NVIDIA GeForce RTX 3090") -> "MockGpu":
        return cls(
            name=name,
            used_mb=23100,
            util=98,
            mem_util=45,
            temp=71,
            power=341.2,
            processes=[(12345, "python", 22000), (12399, "python3.10", 1100)],
        )

    def free_up(self) -> None:
        self.used_mb = 512
        self.util = 0
        self.mem_util = 3
        self.temp = 42
        self.power = 18.55
        self.processes = []


class MockGpuCluster:
    """Renders ``nvidia-smi`` output for a set of :class:`MockGpu`."""

    def __init__(self, gpus: list[MockGpu] | None = None, driver: str = "535.104.05") -> None:
        self.gpus = list(gpus) if gpus else [MockGpu()]
        self.driver = driver
        self._pid = 40000

    # -- rendering ---------------------------------------------------------- #

    def gpu_csv(self) -> str:
        rows = []
        for index, gpu in enumerate(self.gpus):
            free = gpu.total_mb - gpu.used_mb
            rows.append(
                f"{index}, GPU-mock-{index:04d}, {gpu.name}, {gpu.total_mb}, "
                f"{gpu.used_mb}, {free}, {gpu.util}, {gpu.mem_util}, {gpu.temp}, "
                f"{gpu.power}, {gpu.power_limit}"
            )
        return "\n".join(rows) + "\n"

    def proc_csv(self) -> str:
        rows = []
        for index, gpu in enumerate(self.gpus):
            for pid, name, mem in gpu.processes:
                rows.append(f"GPU-mock-{index:04d}, {pid}, {name}, {mem}")
        return "\n".join(rows) + "\n"

    def ps_out(self) -> str:
        rows = []
        for gpu in self.gpus:
            for pid, _name, _mem in gpu.processes:
                rows.append(f"{pid} alice 01:23:45")
        return "\n".join(rows) + "\n"

    def respond(self, command: str) -> str:
        """Produce the stdout a real shell would return for ``command``."""
        cmd = command.strip()
        if "command -v" in cmd:
            return f"FOUND:{SMI}\n"
        if "--query-compute-apps" in cmd:
            return self.proc_csv()
        if "driver_version" in cmd:
            return self.driver + "\n"
        if "--query-gpu" in cmd:
            return self.gpu_csv()
        if cmd.startswith("ps "):
            return self.ps_out()
        return ""

    # -- state changes ------------------------------------------------------ #

    def free_up_all(self) -> None:
        for gpu in self.gpus:
            gpu.free_up()

    def occupy(self, index: int) -> None:
        if 0 <= index < len(self.gpus):
            self.gpus[index].used_mb = 23100
            self.gpus[index].util = 98
            self.gpus[index].mem_util = 45
            self.gpus[index].processes = [(self._pid, "python", 22000)]
            self._pid += 1


# --------------------------------------------------------------------------- #
# SSH server
# --------------------------------------------------------------------------- #

def _build_interface_class():
    """Import paramiko lazily so the package works without it installed."""
    import paramiko

    class _Interface(paramiko.ServerInterface):
        def __init__(self, password: str, commands: list[str]) -> None:
            self.password = password
            self.commands = commands
            self.exec_queue: "queue.Queue[tuple]" = queue.Queue()

        def check_auth_password(self, username, password):  # noqa: D102
            if password == self.password:
                return paramiko.AUTH_SUCCESSFUL
            return paramiko.AUTH_FAILED

        def get_allowed_auths(self, username):  # noqa: D102
            return "password"

        def check_channel_request(self, kind, chanid):  # noqa: D102
            if kind == "session":
                return paramiko.OPEN_SUCCEEDED
            return paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

        def check_channel_exec_request(self, channel, command):  # noqa: D102
            text = (
                command.decode("utf-8", errors="replace")
                if isinstance(command, (bytes, bytearray))
                else str(command)
            )
            self.commands.append(text)
            self.exec_queue.put((channel, text))
            return True

    return _Interface


class MockGpuServer:
    """A real SSH endpoint (paramiko) that answers commands from a cluster."""

    def __init__(
        self,
        cluster: MockGpuCluster | None = None,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        user: str = DEFAULT_USER,
        password: str = DEFAULT_PASSWORD,
    ) -> None:
        import paramiko  # noqa: F401  (fail fast with a clear ImportError)

        self.cluster = cluster or MockGpuCluster()
        self.host = host
        self.user = user
        self.password = password
        self.commands: list[str] = []

        self._interface_cls = _build_interface_class()
        self._host_key = paramiko.RSAKey.generate(2048)
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((host, port))
        self._sock.listen(8)
        self._sock.settimeout(0.3)
        self.port = self._sock.getsockname()[1]

    # -- lifecycle ---------------------------------------------------------- #

    def start(self) -> "MockGpuServer":
        thread = threading.Thread(target=self._accept_loop, name="mock-ssh", daemon=True)
        thread.start()
        self._threads.append(thread)
        log.debug("mock GPU server listening on %s:%d", self.host, self.port)
        return self

    def stop(self) -> None:
        self._stop.set()
        try:
            self._sock.close()
        except OSError:
            pass
        for thread in self._threads:
            thread.join(timeout=2.0)

    def __enter__(self) -> "MockGpuServer":
        return self.start()

    def __exit__(self, *_exc) -> None:
        self.stop()

    # -- serving ------------------------------------------------------------ #

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                client, _addr = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            thread = threading.Thread(
                target=self._serve_client, args=(client,), daemon=True
            )
            thread.start()
            self._threads.append(thread)

    def _serve_client(self, client: socket.socket) -> None:
        import paramiko

        transport = None
        try:
            transport = paramiko.Transport(client)
            transport.add_server_key(self._host_key)
            interface = self._interface_cls(self.password, self.commands)
            transport.start_server(server=interface)

            while not self._stop.is_set() and transport.is_active():
                try:
                    channel, command = interface.exec_queue.get(timeout=0.2)
                except queue.Empty:
                    continue
                try:
                    output = self.cluster.respond(command)
                    if output:
                        channel.sendall(output.encode("utf-8"))
                    channel.send_exit_status(0)
                except Exception as exc:  # pragma: no cover - defensive
                    log.debug("mock exec failed: %s", exc)
                finally:
                    try:
                        channel.close()
                    except Exception:
                        pass
        except Exception as exc:  # pragma: no cover - defensive
            log.debug("mock client handling stopped: %s", exc)
        finally:
            if transport is not None:
                try:
                    transport.close()
                except Exception:
                    pass
            try:
                client.close()
            except OSError:
                pass


# --------------------------------------------------------------------------- #
# Standalone demo
# --------------------------------------------------------------------------- #

def build_demo_yaml(port: int, user: str, password: str, interval: int = 60) -> str:
    """A ready-to-run config pointing at a :class:`MockGpuServer`."""
    return f"""# 自动生成的演示配置 —— 指向本机的模拟 GPU 服务器
app:
  interval_seconds: {interval}
  state_file: logs/demo-state.json
  history_file: logs/demo-history.jsonl
  history_csv: logs/demo-history.csv
  log_level: INFO

ssh:
  transport: paramiko
  connect_timeout: 10

alert:
  min_free_memory_gb: 20.0
  max_gpu_utilization: 5.0
  max_memory_utilization: 10.0
  sustained_checks: 1
  cooldown_minutes: 1

servers:
  - name: mock-gpu-1
    label: 模拟服务器 1
    host: 127.0.0.1
    port: {port}
    user: {user}
    password: {password}

notify:
  channels:
    - type: console
    - type: logfile
      path: logs/demo-alerts.log
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="wheresmygpu.mock_server",
        description="模拟一台运行 nvidia-smi 的 GPU 服务器，用于在没有真实机器时试用 WheresMyGPU。",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=2222)
    parser.add_argument("--user", default=DEFAULT_USER)
    parser.add_argument("--password", default=DEFAULT_PASSWORD)
    parser.add_argument("--gpus", type=int, default=2, help="模拟几张 GPU")
    parser.add_argument("--busy", type=int, default=0, help="让前 N 张卡一开始处于忙碌状态")
    parser.add_argument("--idle-after", type=float, default=0.0,
                        help="多少秒后把忙碌的卡释放为空闲（0 = 不释放）")
    parser.add_argument("--write-demo-config", metavar="PATH",
                        help="顺便生成一份指向本模拟服务器的演示配置")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    gpus = [
        MockGpu.busy() if i < args.busy else MockGpu()
        for i in range(max(1, args.gpus))
    ]
    cluster = MockGpuCluster(gpus)
    server = MockGpuServer(
        cluster, host=args.host, port=args.port,
        user=args.user, password=args.password,
    ).start()

    print(f"模拟 GPU 服务器已启动: {args.user}@{args.host}:{server.port}")
    print(f"  密码: {args.password}")
    print(f"  GPU 数量: {len(cluster.gpus)}（{args.busy} 张忙碌）")

    if args.write_demo_config:
        from pathlib import Path

        path = Path(args.write_demo_config)
        path.write_text(
            build_demo_yaml(server.port, args.user, args.password), encoding="utf-8"
        )
        print(f"  演示配置已写入: {path}")
        print(f"  接着运行: python -m wheresmygpu --config \"{path}\" --once")

    if args.idle_after > 0:
        def release() -> None:
            time.sleep(args.idle_after)
            cluster.free_up_all()
            print(f"[{time.strftime('%H:%M:%S')}] 已把模拟 GPU 释放为空闲状态")

        threading.Thread(target=release, daemon=True).start()
        print(f"  {args.idle_after:.0f} 秒后自动释放为空闲")

    print("\n按 Ctrl+C 停止。")
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        server.stop()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())