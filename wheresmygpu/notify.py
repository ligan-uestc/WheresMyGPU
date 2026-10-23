"""Pluggable notification channels.

Every channel implements :meth:`Channel.send` and is isolated: a failing channel
logs a warning and returns ``False`` instead of disturbing the monitor loop.

Pick any combination in ``config.yaml`` under ``notify.channels``.  Run
``python -m wheresmygpu --test-notify`` to verify delivery end to end.
"""

from __future__ import annotations

import json
import logging
import os
import smtplib
import ssl
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from email.message import EmailMessage
from pathlib import Path
from typing import Any

log = logging.getLogger("wheresmygpu.notify")

IS_WINDOWS = os.name == "nt"


# --------------------------------------------------------------------------- #
# Event model
# --------------------------------------------------------------------------- #

@dataclass
class AlertEvent:
    """A single thing worth telling the user about."""

    kind: str                 # gpu_free | gpu_busy | server_unreachable | server_recovered | test
    title: str
    body: str
    level: str = "info"       # info | warning | critical
    server: str = ""
    server_label: str = ""
    gpu_index: int | None = None
    timestamp: float = field(default_factory=time.time)
    metrics: dict = field(default_factory=dict)
    lines: list[str] = field(default_factory=list)

    @property
    def is_alert(self) -> bool:
        return self.level in ("warning", "critical")

    def to_payload(self) -> dict:
        return {
            "kind": self.kind,
            "level": self.level,
            "title": self.title,
            "body": self.body,
            "server": self.server,
            "server_label": self.server_label,
            "gpu_index": self.gpu_index,
            "timestamp": self.timestamp,
            "metrics": self.metrics,
        }


# --------------------------------------------------------------------------- #
# Base channel
# --------------------------------------------------------------------------- #

class Channel:
    """Base class for delivery backends."""

    type = "base"
    display = "Base"

    def __init__(self, options: dict | None = None) -> None:
        self.options = options or {}
        self.enabled = _bool(self.options.get("enabled"), True)
        self.retries = max(0, int(self.options.get("retries", 1) or 0))
        self.timeout = float(self.options.get("timeout", 10) or 10)

    # -- API ---------------------------------------------------------------- #

    def send(self, event: AlertEvent) -> bool:
        """Deliver one event. Never raise; return success instead."""
        if not self.enabled:
            return False
        attempts = self.retries + 1
        last: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                self.deliver(event)
                return True
            except Exception as exc:
                last = exc
                log.debug(
                    "channel %s attempt %d/%d failed: %s", self.type, attempt, attempts, exc
                )
                if attempt < attempts:
                    time.sleep(min(2.0 * attempt, 5.0))
        log.warning("notification channel %s failed: %s", self.type, last)
        return False

    def deliver(self, event: AlertEvent) -> None:
        raise NotImplementedError

    def describe(self) -> str:
        return self.type


def _bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _require(options: dict, key: str, channel: str) -> Any:
    value = options.get(key)
    if value in (None, ""):
        raise ValueError(f"notify channel '{channel}' requires option '{key}'")
    return value


# --------------------------------------------------------------------------- #
# Local / zero-dependency channels
# --------------------------------------------------------------------------- #

class ConsoleChannel(Channel):
    """Print alerts to the terminal, with colour on supporting terminals."""

    type = "console"
    display = "终端输出"

    _COLORS = {
        "info": "\033[36m",
        "warning": "\033[33m",
        "critical": "\033[31m",
    }
    _RESET = "\033[0m"

    def deliver(self, event: AlertEvent) -> None:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(event.timestamp))
        use_color = (
            self.options.get("color", True)
            and sys.stdout.isatty()
            and not os.environ.get("NO_COLOR")
        )
        color = self._COLORS.get(event.level, "") if use_color else ""
        reset = self._RESET if use_color else ""

        bar = "=" * 72
        out = sys.stdout
        out.write(f"\n{color}{bar}\n")
        out.write(f"[{stamp}] {event.title}{reset}\n")
        for line in event.lines or event.body.splitlines():
            out.write(f"{line}\n")
        out.write(f"{color}{bar}{reset}\n")
        out.flush()


class LogFileChannel(Channel):
    """Append alerts to a dedicated file (independent of the main log)."""

    type = "logfile"
    display = "告警文件"

    def __init__(self, options: dict | None = None, base_dir: Path | None = None) -> None:
        super().__init__(options)
        self.base_dir = base_dir or Path.cwd()

    def _path(self) -> Path:
        raw = self.options.get("path") or "logs/alerts.log"
        path = Path(os.path.expanduser(str(raw)))
        if not path.is_absolute():
            path = self.base_dir / path
        return path

    def deliver(self, event: AlertEvent) -> None:
        path = self._path()
        path.parent.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(event.timestamp))
        with path.open("a", encoding="utf-8") as fh:
            fh.write(f"\n[{stamp}] [{event.level.upper()}] {event.title}\n")
            for line in event.lines or event.body.splitlines():
                fh.write(f"    {line}\n")
            fh.write(json.dumps(event.to_payload(), ensure_ascii=False) + "\n")

    def describe(self) -> str:
        return f"logfile -> {self._path()}"


class JsonlChannel(Channel):
    """Append raw JSON events to a file, for later analysis."""

    type = "jsonl"
    display = "JSONL 事件流"

    def __init__(self, options: dict | None = None, base_dir: Path | None = None) -> None:
        super().__init__(options)
        self.base_dir = base_dir or Path.cwd()

    def deliver(self, event: AlertEvent) -> None:
        raw = self.options.get("path") or "logs/alerts.jsonl"
        path = Path(os.path.expanduser(str(raw)))
        if not path.is_absolute():
            path = self.base_dir / path
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event.to_payload(), ensure_ascii=False) + "\n")


# --------------------------------------------------------------------------- #
# Windows desktop channels
# --------------------------------------------------------------------------- #

_TOAST_PS1 = r"""
param(
    [Parameter(Mandatory=$true)][string]$Title,
    [Parameter(Mandatory=$true)][string]$Message,
    [string]$AppId = "WheresMyGPU"
)
$ErrorActionPreference = "Stop"
[void][Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime]
[void][Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime]
function Esc([string]$s) { $s.Replace('&','&amp;').Replace('<','&lt;').Replace('>','&gt;') }
$xmlString = @"
<toast activationType="foreground">
  <visual>
    <binding template="ToastGeneric">
      <text>$(Esc $Title)</text>
      <text>$(Esc $Message)</text>
    </binding>
  </visual>
  <audio src="ms-winsoundevent:Notification.Default" />
</toast>
"@
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml($xmlString)
$toast = New-Object Windows.UI.Notifications.ToastNotification $xml
try {
    ([Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($AppId)).Show($toast)
} catch {
    $fallback = "{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"
    ([Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($fallback)).Show($toast)
}
"""

_TOAST_SCRIPT_PATH: Path | None = None


def _toast_script() -> Path:
    """Materialise the toast helper script once per process."""
    global _TOAST_SCRIPT_PATH
    if _TOAST_SCRIPT_PATH and _TOAST_SCRIPT_PATH.is_file():
        return _TOAST_SCRIPT_PATH
    target = Path(tempfile.gettempdir()) / "wheresmygpu_toast.ps1"
    target.write_text(_TOAST_PS1, encoding="utf-8-sig")
    _TOAST_SCRIPT_PATH = target
    return target


class WindowsToastChannel(Channel):
    """Native Windows 10/11 toast notification (no third-party packages)."""

    type = "windows_toast"
    display = "Windows 桌面通知"

    def deliver(self, event: AlertEvent) -> None:
        if not IS_WINDOWS:
            raise RuntimeError("windows_toast channel requires Windows")

        powershell = Path(os.environ.get("SystemRoot", r"C:\Windows")) / (
            "System32/WindowsPowerShell/v1.0/powershell.exe"
        )
        exe = str(powershell) if powershell.is_file() else "powershell.exe"

        # The toast body is limited in practice; keep the first few lines.
        message = "\n".join((event.lines or event.body.splitlines())[:6])
        if len(message) > 700:
            message = message[:697] + "..."

        app_id = str(self.options.get("app_id") or "WheresMyGPU")

        proc = subprocess.run(
            [
                exe, "-NoProfile", "-NonInteractive",
                "-ExecutionPolicy", "Bypass",
                "-File", str(_toast_script()),
                "-Title", event.title,
                "-Message", message,
                "-AppId", app_id,
            ],
            capture_output=True,
            timeout=self.timeout,
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                proc.stderr.decode("utf-8", errors="replace").strip()[:300]
                or f"powershell exited {proc.returncode}"
            )


class SoundChannel(Channel):
    """Audible cue on Windows so an alert is noticed in the background."""

    type = "sound"
    display = "提示音"

    def deliver(self, event: AlertEvent) -> None:
        if not IS_WINDOWS:
            return
        import winsound

        if event.level == "critical":
            for freq in (880, 1180, 880, 1180):
                winsound.Beep(freq, 180)
        else:
            winsound.PlaySound(
                "SystemExclamation", winsound.SND_ALIAS | winsound.SND_ASYNC
            )


class CommandChannel(Channel):
    """Run a local command; event fields arrive via ``WHERESMYGPU_*`` env vars."""

    type = "command"
    display = "本地命令"

    def deliver(self, event: AlertEvent) -> None:
        command = _require(self.options, "command", self.type)
        env = dict(os.environ)
        env.update(
            {
                "WHERESMYGPU_KIND": event.kind,
                "WHERESMYGPU_LEVEL": event.level,
                "WHERESMYGPU_TITLE": event.title,
                "WHERESMYGPU_BODY": event.body,
                "WHERESMYGPU_SERVER": event.server,
                "WHERESMYGPU_GPU_INDEX": "" if event.gpu_index is None else str(event.gpu_index),
                "WHERESMYGPU_JSON": json.dumps(event.to_payload(), ensure_ascii=False),
            }
        )
        proc = subprocess.run(
            command, shell=True, capture_output=True, timeout=self.timeout,
            env=env, check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"command exited {proc.returncode}: "
                f"{proc.stderr.decode('utf-8', errors='replace').strip()[:200]}"
            )


# --------------------------------------------------------------------------- #
# HTTP webhook channels
# --------------------------------------------------------------------------- #

def _post_json(url: str, payload: dict, timeout: float, headers: dict | None = None) -> str:
    import requests

    response = requests.post(
        url,
        json=payload,
        timeout=timeout,
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    text = response.text[:400]
    response.raise_for_status()
    return text


class WebhookChannel(Channel):
    """Generic JSON POST; shape the body with ``payload_template`` if needed."""

    type = "webhook"
    display = "通用 Webhook"

    def deliver(self, event: AlertEvent) -> None:
        url = _require(self.options, "url", self.type)
        payload = event.to_payload()
        template = self.options.get("payload_template")
        if template:
            payload = _render_template(template, payload, event)
        _post_json(url, payload, self.timeout, self.options.get("headers"))


def _render_template(template: Any, payload: dict, event: AlertEvent) -> Any:
    """Substitute ``{placeholder}`` tokens inside a nested template structure."""
    flat = {
        **{k: v for k, v in payload.items() if not isinstance(v, (dict, list))},
        "body_lines": "\n".join(event.lines or event.body.splitlines()),
    }

    def render(node: Any) -> Any:
        if isinstance(node, str):
            out = node
            for key, value in flat.items():
                out = out.replace("{" + key + "}", str(value))
            return out
        if isinstance(node, dict):
            return {k: render(v) for k, v in node.items()}
        if isinstance(node, list):
            return [render(v) for v in node]
        return node

    return render(template)


class FeishuChannel(Channel):
    """飞书 / Lark custom bot webhook."""

    type = "feishu"
    display = "飞书机器人"

    def deliver(self, event: AlertEvent) -> None:
        url = self.options.get("webhook") or _require(self.options, "url", self.type)
        color = {"critical": "red", "warning": "orange"}.get(event.level, "blue")
        card = {
            "msg_type": "interactive",
            "card": {
                "config": {"wide_screen_mode": True},
                "header": {
                    "template": color,
                    "title": {"tag": "plain_text", "content": event.title},
                },
                "elements": [
                    {
                        "tag": "div",
                        "text": {
                            "tag": "lark_md",
                            "content": "\n".join(event.lines or event.body.splitlines()),
                        },
                    }
                ],
            },
        }
        text = {"msg_type": "text", "content": {"text": f"{event.title}\n{event.body}"}}
        payload = text if self.options.get("as_text") else card
        result = _post_json(url, payload, self.timeout)
        _check_im_response(result, "feishu", ("code", "StatusCode"))


class DingTalkChannel(Channel):
    """钉钉自定义机器人 webhook."""

    type = "dingtalk"
    display = "钉钉机器人"

    def deliver(self, event: AlertEvent) -> None:
        url = self.options.get("webhook") or _require(self.options, "url", self.type)
        keyword = self.options.get("keyword") or ""
        body = "\n".join(event.lines or event.body.splitlines())
        text = f"{keyword}{event.title}\n\n{body}" if keyword else f"{event.title}\n\n{body}"
        payload = {
            "msgtype": "markdown",
            "markdown": {"title": event.title, "text": text},
        }
        if self.options.get("at_mobiles"):
            payload["at"] = {"atMobiles": list(self.options["at_mobiles"])}
        if self.options.get("at_all"):
            payload.setdefault("at", {})["isAtAll"] = True
        result = _post_json(url, payload, self.timeout)
        _check_im_response(result, "dingtalk", ("errcode",))


class WeComChannel(Channel):
    """企业微信群机器人 webhook."""

    type = "wecom"
    display = "企业微信机器人"

    def deliver(self, event: AlertEvent) -> None:
        url = self.options.get("webhook") or _require(self.options, "url", self.type)
        body = "\n".join(event.lines or event.body.splitlines())
        content = f"**{event.title}**\n{body}"
        payload = {"msgtype": "markdown", "markdown": {"content": content}}
        if self.options.get("mentioned_mobile_list"):
            payload["msgtype"] = "text"
            payload["text"] = {
                "content": f"{event.title}\n{body}",
                "mentioned_mobile_list": list(self.options["mentioned_mobile_list"]),
            }
        result = _post_json(url, payload, self.timeout)
        _check_im_response(result, "wecom", ("errcode",))


class SlackChannel(Channel):
    """Slack incoming webhook."""

    type = "slack"
    display = "Slack"

    def deliver(self, event: AlertEvent) -> None:
        url = self.options.get("webhook") or _require(self.options, "url", self.type)
        body = "\n".join(event.lines or event.body.splitlines())
        _post_json(url, {"text": f"*{event.title}*\n{body}"}, self.timeout)


class DiscordChannel(Channel):
    """Discord webhook."""

    type = "discord"
    display = "Discord"

    def deliver(self, event: AlertEvent) -> None:
        url = self.options.get("webhook") or _require(self.options, "url", self.type)
        body = "\n".join(event.lines or event.body.splitlines())
        _post_json(url, {"content": f"**{event.title}**\n{body}"}, self.timeout)


class TelegramChannel(Channel):
    """Telegram bot API."""

    type = "telegram"
    display = "Telegram"

    def deliver(self, event: AlertEvent) -> None:
        token = _require(self.options, "bot_token", self.type)
        chat_id = _require(self.options, "chat_id", self.type)
        body = "\n".join(event.lines or event.body.splitlines())
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        _post_json(
            url,
            {"chat_id": chat_id, "text": f"{event.title}\n\n{body}"},
            self.timeout,
        )


class ServerChanChannel(Channel):
    """Server 酱 — pushes to WeChat via sctapi.ftqq.com."""

    type = "serverchan"
    display = "Server 酱"

    def deliver(self, event: AlertEvent) -> None:
        key = _require(self.options, "send_key", self.type)
        body = "\n".join(event.lines or event.body.splitlines())
        url = f"https://sctapi.ftqq.com/{key}.send"
        _post_json(url, {"title": event.title, "desp": body}, self.timeout)


class BarkChannel(Channel):
    """Bark — iOS push."""

    type = "bark"
    display = "Bark (iOS)"

    def deliver(self, event: AlertEvent) -> None:
        base = str(self.options.get("server") or "https://api.day.app").rstrip("/")
        key = _require(self.options, "key", self.type)
        body = "\n".join(event.lines or event.body.splitlines())
        url = f"{base}/{key}"
        _post_json(
            url,
            {"title": event.title, "body": body, "group": self.options.get("group", "WheresMyGPU")},
            self.timeout,
        )


def _check_im_response(text: str, channel: str, code_keys: tuple[str, ...]) -> None:
    """Some IM webhooks answer HTTP 200 with an error code in the body."""
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return
    if not isinstance(data, dict):
        return
    for key in code_keys:
        if key in data:
            value = data[key]
            if value not in (0, "0", None):
                raise RuntimeError(f"{channel} rejected the message: {data}")
            return
    if data.get("ok") is False:
        raise RuntimeError(f"{channel} rejected the message: {data}")


class EmailChannel(Channel):
    """SMTP delivery, using SSL on 465 or STARTTLS elsewhere."""

    type = "email"
    display = "邮件 (SMTP)"

    def deliver(self, event: AlertEvent) -> None:
        host = _require(self.options, "smtp_host", self.type)
        port = int(self.options.get("smtp_port") or 465)
        user = self.options.get("username")
        password = self.options.get("password")
        sender = self.options.get("from") or user
        if not sender:
            raise ValueError("notify channel 'email' requires 'from' or 'username'")
        recipients = self.options.get("to")
        if isinstance(recipients, str):
            recipients = [recipients]
        if not recipients:
            raise ValueError("notify channel 'email' requires 'to'")

        message = EmailMessage()
        message["Subject"] = event.title
        message["From"] = sender
        message["To"] = ", ".join(recipients)
        message.set_content("\n".join(event.lines or event.body.splitlines()))

        use_ssl = _bool(self.options.get("use_ssl"), port == 465)
        context = ssl.create_default_context()
        if use_ssl:
            with smtplib.SMTP_SSL(host, port, timeout=self.timeout, context=context) as smtp:
                if user:
                    smtp.login(user, password or "")
                smtp.send_message(message)
        else:
            with smtplib.SMTP(host, port, timeout=self.timeout) as smtp:
                smtp.ehlo()
                if _bool(self.options.get("starttls"), True):
                    smtp.starttls(context=context)
                    smtp.ehlo()
                if user:
                    smtp.login(user, password or "")
                smtp.send_message(message)


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #

CHANNEL_TYPES: dict[str, type[Channel]] = {
    cls.type: cls
    for cls in (
        ConsoleChannel,
        LogFileChannel,
        JsonlChannel,
        WindowsToastChannel,
        SoundChannel,
        CommandChannel,
        WebhookChannel,
        FeishuChannel,
        DingTalkChannel,
        WeComChannel,
        SlackChannel,
        DiscordChannel,
        TelegramChannel,
        ServerChanChannel,
        BarkChannel,
        EmailChannel,
    )
}


def build_channels(config) -> list[Channel]:
    """Instantiate the configured channels, skipping failures with a warning."""
    channels: list[Channel] = []
    base_dir = config.base_dir

    for index, spec in enumerate(config.channels):
        options = dict(spec)
        kind = str(options.pop("type")).strip().lower()
        cls = CHANNEL_TYPES.get(kind)
        if cls is None:
            log.warning(
                "unknown notification channel %r (known: %s)",
                kind, ", ".join(sorted(CHANNEL_TYPES)),
            )
            continue
        if not _bool(options.get("enabled"), True):
            log.debug("channel %s disabled", kind)
            continue
        try:
            if cls in (LogFileChannel, JsonlChannel):
                channel = cls(options, base_dir=base_dir)
            else:
                channel = cls(options)
        except Exception as exc:
            log.warning("channel %s could not be created: %s", kind, exc)
            continue
        channels.append(channel)

    if not channels:
        log.warning("no usable notification channels; falling back to console")
        channels.append(ConsoleChannel({}))

    return channels


def notify_all(channels: list[Channel], event: AlertEvent) -> int:
    """Deliver one event to every channel; return how many succeeded."""
    delivered = 0
    for channel in channels:
        if channel.send(event):
            delivered += 1
    if delivered:
        log.info(
            "alert delivered to %d/%d channel(s): %s",
            delivered, len(channels), event.title,
        )
    else:
        log.error("alert could not be delivered to any channel: %s", event.title)
    return delivered