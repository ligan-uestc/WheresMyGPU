"""Serialise a :class:`~wheresmygpu.config.Config` back to a YAML file.

The GUI edits a config that was already loaded and validated, so this module
only has to turn the dataclasses into plain YAML. It deliberately writes a
commented, human-editable file -- the same file the CLI reads -- so the GUI and
the headless monitor always stay in sync.
"""

from __future__ import annotations

import time
from pathlib import Path

import yaml

from .config import Config, ServerConfig

HEADER = """\
# =============================================================================
#  WheresMyGPU 配置文件
#
#  这个文件由图形界面（run_gui.bat）自动维护，也可以手工编辑。
#  手工改完记得在界面里点一下「重新加载」。
#
#  上次保存: {saved}
# =============================================================================
"""


def _server_to_dict(server: ServerConfig) -> dict:
    """ServerConfig -> YAML mapping, omitting empty optional fields."""
    out: dict = {
        "name": server.name,
        "label": server.label,
        "host": server.host,
        "port": server.port,
        "user": server.user,
        "enabled": server.enabled,
    }
    for key, value in (
        ("password", server.password),
        ("password_file", server.password_file),
        ("key_file", server.key_file),
        ("key_passphrase", server.key_passphrase),
        ("transport", server.transport),
    ):
        if value:
            out[key] = value

    if server.alert_overrides:
        out["alert"] = dict(server.alert_overrides)
    if server.tags:
        out["tags"] = list(server.tags)
    if server.extra_ssh_options:
        out["ssh_options"] = dict(server.extra_ssh_options)
    return out


def config_to_dict(config: Config) -> dict:
    """The whole config as a plain, YAML-ready dictionary."""
    app = config.app
    ssh = config.ssh

    app_dict: dict = {
        "interval_seconds": app.interval_seconds,
        "jitter_seconds": app.jitter_seconds,
        "log_dir": app.log_dir,
        "state_file": app.state_file,
        "history_file": app.history_file,
        "history_csv": app.history_csv,
        "log_level": app.log_level,
        "console": app.console,
    }

    ssh_dict: dict = {
        "transport": ssh.transport,
        "connect_timeout": ssh.connect_timeout,
        "command_timeout": ssh.command_timeout,
        "keepalive": ssh.keepalive,
        "max_retries": ssh.max_retries,
        "retry_backoff": ssh.retry_backoff,
        "reuse_connection": ssh.reuse_connection,
        "nvidia_smi_paths": list(ssh.nvidia_smi_paths),
    }

    return {
        "app": app_dict,
        "ssh": ssh_dict,
        "alert": config.alert.to_dict(),
        "servers": [_server_to_dict(s) for s in config.servers],
        "notify": {"channels": [dict(c) for c in config.channels]},
    }


def dump_config(config: Config, path: Path | str | None = None) -> Path:
    """Write ``config`` to ``path`` (defaults to where it was loaded from).

    A timestamped ``.bak`` copy of the previous file is kept so a bad edit can
    always be undone.
    """
    target = Path(path) if path is not None else Path(config.path)
    target.parent.mkdir(parents=True, exist_ok=True)

    if target.is_file() and target.stat().st_size:
        backup = target.with_suffix(target.suffix + ".bak")
        try:
            backup.write_bytes(target.read_bytes())
        except OSError:
            pass

    data = config_to_dict(config)
    body = yaml.safe_dump(
        data,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
        indent=2,
        width=100,
    )

    text = HEADER.format(saved=time.strftime("%Y-%m-%d %H:%M:%S")) + "\n" + body

    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(target)
    return target


# --------------------------------------------------------------------------- #
# Notification channel helpers used by the GUI
# --------------------------------------------------------------------------- #

EMAIL_KEY_ORDER = (
    "smtp_host",
    "smtp_port",
    "use_ssl",
    "starttls",
    "username",
    "password",
    "from",
    "to",
)


def find_channel(channels: list[dict], kind: str) -> dict | None:
    """First channel spec of ``kind``, or ``None``."""
    for spec in channels:
        if str(spec.get("type", "")).strip().lower() == kind:
            return spec
    return None


def upsert_channel(channels: list[dict], spec: dict) -> list[dict]:
    """Insert or replace a channel spec, matching on ``type``.

    Returns a new list; the input is not modified.
    """
    kind = str(spec.get("type", "")).strip().lower()
    out: list[dict] = []
    replaced = False
    for existing in channels:
        if str(existing.get("type", "")).strip().lower() == kind:
            if not replaced:
                out.append(dict(spec))
                replaced = True
            continue
        out.append(dict(existing))
    if not replaced:
        out.append(dict(spec))
    return out


def remove_channel(channels: list[dict], kind: str) -> list[dict]:
    """Drop every channel spec of ``kind``."""
    kind = kind.strip().lower()
    return [
        dict(c) for c in channels
        if str(c.get("type", "")).strip().lower() != kind
    ]


def has_channel(channels: list[dict], kind: str) -> bool:
    """True when ``kind`` is present and not explicitly disabled."""
    spec = find_channel(channels, kind)
    if spec is None:
        return False
    return bool(spec.get("enabled", True))