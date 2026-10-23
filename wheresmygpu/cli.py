"""Command-line interface for WheresMyGPU."""

from __future__ import annotations

import argparse
import logging
import shutil
import sys
import time
from pathlib import Path

from . import __version__
from .analyzer import evaluate, rule_description, unsatisfiable_reason
from .config import ConfigError, load_config
from .monitor import Monitor, setup_logging
from .notify import CHANNEL_TYPES, AlertEvent, build_channels
from .ssh_runner import SshError

log = logging.getLogger("wheresmygpu.cli")

CHANNEL_HELP = {
    "console": "在终端彩色打印（零依赖，默认开启）",
    "logfile": "写入独立的告警文件 logs/alerts.log",
    "jsonl": "把每个事件以 JSON 追加到文件",
    "windows_toast": "Windows 10/11 原生桌面通知（零依赖）",
    "sound": "Windows 提示音",
    "command": "执行任意本地命令，事件通过 WHERESMYGPU_* 环境变量传入",
    "webhook": "通用 HTTP POST，可自定义 JSON 模板",
    "feishu": "飞书 / Lark 群机器人 webhook",
    "dingtalk": "钉钉群机器人 webhook",
    "wecom": "企业微信群机器人 webhook",
    "slack": "Slack Incoming Webhook",
    "discord": "Discord Webhook",
    "telegram": "Telegram Bot API",
    "serverchan": "Server 酱（推送到微信）",
    "bark": "Bark（推送到 iOS）",
    "email": "SMTP 邮件",
}


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def _parse_interval(value: str) -> int:
    """Accept 300, '300s', '5m', '1h' and return seconds."""
    text = str(value).strip().lower()
    multiplier = 1
    if text.endswith("s"):
        text, multiplier = text[:-1], 1
    elif text.endswith("m"):
        text, multiplier = text[:-1], 60
    elif text.endswith("h"):
        text, multiplier = text[:-1], 3600
    try:
        seconds = int(float(text) * multiplier)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"invalid interval {value!r}; use seconds or 300/5m/1h"
        ) from None
    if seconds < 10:
        raise argparse.ArgumentTypeError("interval must be at least 10 seconds")
    return seconds


def _reconfigure_stdio() -> None:
    """Make stdout/stderr UTF-8 so emoji and Chinese render on any console."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


# --------------------------------------------------------------------------- #
# subcommands
# --------------------------------------------------------------------------- #

def cmd_init(args: argparse.Namespace) -> int:
    target = Path(args.config or "config.yaml").expanduser().absolute()
    if target.exists() and not args.force:
        print(f"配置文件已存在：{target}\n（如需覆盖请加 --force）")
        return 1

    template = Path(__file__).resolve().parent.parent / "config.example.yaml"
    if not template.is_file():
        print(f"找不到模板文件 {template}", file=sys.stderr)
        return 2

    shutil.copyfile(template, target)
    print(f"已创建配置文件：{target}")
    print("请编辑其中的 servers 段，填入两台服务器的 SSH 信息，然后运行：")
    print(f"  python -m wheresmygpu --config \"{target}\" --check")
    return 0


def cmd_channels(_args: argparse.Namespace) -> int:
    print("可用的提醒通道：\n")
    for name in sorted(CHANNEL_TYPES):
        print(f"  {name:<16} {CHANNEL_HELP.get(name, '')}")
    print("\n在 config.yaml 的 notify.channels 中按需启用，例如：")
    print(
        "\n".join(
            [
                "  notify:",
                "    channels:",
                "      - type: console",
                "      - type: windows_toast",
                "        app_id: WheresMyGPU",
                "      - type: feishu",
                "        webhook: https://open.feishu.cn/open-apis/bot/v2/hook/xxxx",
                "      - type: dingtalk",
                "        webhook: https://oapi.dingtalk.com/robot/send?access_token=xxxx",
                "        keyword: GPU",
                "      - type: email",
                "        smtp_host: smtp.qq.com",
                "        smtp_port: 465",
                "        username: you@qq.com",
                "        password: ${SMTP_PASSWORD}",
                "        to: you@qq.com",
            ]
        )
    )
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    setup_logging(config, args.log_level)

    print(f"配置文件: {config.path}")
    print(f"服务器数量: {len(config.enabled_servers())}")
    print(f"提醒通道: {', '.join(str(c.get('type')) for c in config.channels)}")
    print()

    from .nvidia import NvidiaSmiCollector
    from .ssh_runner import SshRunner

    failures = 0
    for server in config.enabled_servers():
        rule = config.alert_for(server)
        print(f"── {server.describe()}  [{config.transport_for(server)}]")
        print(f"   判定阈值: {rule_description(rule)}")
        runner = SshRunner(server, config)
        try:
            smi = runner.resolve_nvidia_smi()
            print(f"   nvidia-smi: {smi}")
        except SshError as exc:
            failures += 1
            print(f"   ✗ 无法连接或找不到 nvidia-smi: {exc}")
            print()
            continue

        collector = NvidiaSmiCollector(server, config, runner)
        snapshot = collector.collect()
        if snapshot.ok:
            print(f"   ✓ 连接成功，驱动 {snapshot.driver_version or '未知'}，"
                  f"发现 {len(snapshot.gpus)} 张 GPU")
            for gpu in snapshot.gpus:
                verdict = evaluate(gpu, rule, server.name, server.label)
                mark = "空闲 ✅" if verdict.is_free else "忙"
                print(f"     - {gpu.summary_line()}  → {mark}")
                if not verdict.is_free:
                    print(f"       未满足: {verdict.failing_summary()}")
                reason = unsatisfiable_reason(gpu, rule)
                if reason:
                    print(f"       ⚠️  阈值警告: {reason}")
        else:
            failures += 1
            print(f"   ✗ 采集失败: {snapshot.error}")
        print()
        runner.close()

    print("检查完成：" + ("全部正常 ✅" if failures == 0 else f"{failures} 台服务器有问题 ❌"))
    return 0 if failures == 0 else 1


def cmd_test_notify(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    setup_logging(config, args.log_level)

    channels = build_channels(config)
    event = AlertEvent(
        kind="test",
        title="🔔 WheresMyGPU 提醒通道测试",
        body="如果你看到这条消息，说明该提醒通道配置正确。",
        level="warning",
        server="test",
        server_label="测试服务器",
        gpu_index=0,
        timestamp=time.time(),
        metrics={"test": True},
        lines=[
            "这是一条测试提醒，来自 WheresMyGPU。",
            f"时间: {time.strftime('%Y-%m-%d %H:%M:%S')}",
            "收到即表示该通道可用；未收到请检查配置与网络。",
        ],
    )

    print(f"正在测试 {len(channels)} 个提醒通道…\n")
    ok = 0
    for channel in channels:
        started = time.monotonic()
        success = channel.send(event)
        elapsed = time.monotonic() - started
        ok += 1 if success else 0
        print(f"  [{'✓' if success else '✗'}] {channel.type:<16} "
              f"{channel.describe():<40} {elapsed:.2f}s")
    print(f"\n成功 {ok}/{len(channels)} 个通道。")
    return 0 if ok else 1


def cmd_run(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    log_path = setup_logging(config, args.log_level)
    log.info("WheresMyGPU %s starting; log file: %s", __version__, log_path)

    monitor = Monitor(
        config,
        dry_run=args.dry_run,
        interval_override=args.interval,
    )

    if args.once:
        monitor.print_banner()
        snapshots = monitor.run_round()
        table = monitor.render_table(snapshots)
        print(table, flush=True)
        monitor.close()
        return 0

    return monitor.run_forever()


def cmd_status(args: argparse.Namespace) -> int:
    """Print the last known state without contacting the servers."""
    import json

    config = load_config(args.config)
    state_path = config.resolve_path(config.app.state_file)
    if not state_path.is_file():
        print(f"尚无状态文件 {state_path}；请先运行监控。")
        return 1

    data = json.loads(state_path.read_text(encoding="utf-8"))
    saved = data.get("saved_ts")
    print(f"状态文件: {state_path}")
    if saved:
        print(f"最后更新: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(saved))} "
              f"({(time.time() - saved) / 60:.1f} 分钟前)")
    print(f"累计轮次: {data.get('rounds')}   累计提醒: {data.get('alerts_sent')}\n")

    for name, server in (data.get("servers") or {}).items():
        print(f"── {name}")
        print(f"   连续失败: {server.get('consecutive_failures')}   "
              f"最后成功: "
              f"{time.strftime('%H:%M:%S', time.localtime(server['last_ok_ts'])) if server.get('last_ok_ts') else '从未'}")
        if server.get("last_error"):
            print(f"   最后错误: {server['last_error']}")
        for index, gpu in sorted((server.get("gpus") or {}).items()):
            metrics = gpu.get("last_metrics") or {}
            seen = gpu.get("last_seen_ts")
            when = (
                time.strftime("%H:%M:%S", time.localtime(seen)) if seen else "?"
            )
            free_gb = metrics.get("mem_free_gb", "?")
            util = metrics.get("gpu_util_pct", "?")
            procs = metrics.get("process_count", "?")
            print(
                f"   GPU {index}: 状态={gpu.get('last_state'):<8} "
                f"剩余显存={free_gb}G 利用率={util}% 进程={procs} "
                f"({when})"
            )
        print()
    return 0


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #

def cmd_stop(args) -> int:
    """Stop WheresMyGPU processes: the GUI window and/or background monitors."""
    import os
    import subprocess

    # Match only python processes whose command line mentions wheresmygpu, and
    # never our own PID (this very command line contains "wheresmygpu" too).
    script = (
        "(Get-CimInstance Win32_Process).Where({"
        " $_.Name -like 'python*' -and"
        " $_.CommandLine -like '*wheresmygpu*' -and"
        f" $_.ProcessId -ne {os.getpid()}"
        "}).ProcessId"
    )
    try:
        probe = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"无法查询进程列表: {exc}", file=sys.stderr)
        return 1

    pids = [int(token) for token in probe.stdout.split() if token.isdigit()]
    if not pids:
        print("没有发现正在运行的 WheresMyGPU 进程。")
        return 0

    stopped = 0
    for pid in pids:
        result = subprocess.run(
            ["taskkill", "/pid", str(pid), "/f"], capture_output=True, text=True
        )
        if result.returncode == 0:
            print(f"  已停止 PID {pid}")
            stopped += 1
        else:
            detail = (result.stderr or result.stdout).strip()
            print(f"  无法停止 PID {pid}: {detail}")

    print(f"\n共停止 {stopped} / {len(pids)} 个进程。")
    return 0 if stopped else 1

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wheresmygpu",
        description="持续监控多台 GPU 服务器，在显卡空闲时自动提醒。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  python -m wheresmygpu --init                  # 生成 config.yaml\n"
            "  python -m wheresmygpu --check                 # 校验配置与连通性\n"
            "  python -m wheresmygpu --test-notify           # 测试提醒通道\n"
            "  python -m wheresmygpu --once                  # 只跑一轮\n"
            "  python -m wheresmygpu --once --dry-run        # 只看判定，不发提醒\n"
            "  python -m wheresmygpu                         # 持续监控（默认）\n"
            "  python -m wheresmygpu --channels              # 查看所有提醒方式\n"
            "  python -m wheresmygpu --status                # 查看上次监控状态\n"
        ),
    )
    parser.add_argument("--config", "-c", help="配置文件路径（默认 ./config.yaml）")
    parser.add_argument("--interval", "-i", type=_parse_interval,
                        help="轮询间隔，如 300 / 5m / 1h（覆盖配置文件）")
    parser.add_argument("--once", action="store_true",
                        help="只执行一轮检查后退出")
    parser.add_argument("--dry-run", action="store_true",
                        help="只评估和打印，不发送提醒、不写状态")
    parser.add_argument("--test-notify", action="store_true",
                        help="向所有已配置通道发送一条测试提醒后退出")
    parser.add_argument("--check", action="store_true",
                        help="校验配置、SSH 连接与 nvidia-smi 可用性")
    parser.add_argument("--status", action="store_true",
                        help="打印上一次监控的缓存状态")
    parser.add_argument("--init", action="store_true",
                        help="生成配置文件模板")
    parser.add_argument("--force", action="store_true",
                        help="配合 --init 覆盖已存在的配置文件")
    parser.add_argument("--channels", action="store_true",
                        help="列出所有可用的提醒通道")
    parser.add_argument("--gui", action="store_true",
                        help="打开图形界面（调节阈值、增删服务器、配置邮件提醒）")
    parser.add_argument("--stop", action="store_true",
                        help="停止正在运行的 WheresMyGPU 进程（界面和后台监控）")
    parser.add_argument("--log-level", help="日志级别 DEBUG/INFO/WARNING/ERROR")
    parser.add_argument("--version", action="version", version=f"wheresmygpu {__version__}")
    return parser


def main(argv: list[str] | None = None) -> int:
    _reconfigure_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.gui:
            from .gui import run_gui

            return run_gui(args.config)
        if args.stop:
            return cmd_stop(args)
        if args.init:
            return cmd_init(args)
        if args.channels:
            return cmd_channels(args)
        if args.check:
            return cmd_check(args)
        if args.test_notify:
            return cmd_test_notify(args)
        if args.status:
            return cmd_status(args)
        return cmd_run(args)
    except ConfigError as exc:
        print(f"\n配置错误: {exc}\n", file=sys.stderr)
        print("提示: 运行 `python -m wheresmygpu --init` 生成配置模板。", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\n已中断。")
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())