"""Configuration loading, validation and per-server merging for WheresMyGPU."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


class ConfigError(Exception):
    """Raised when a configuration file is missing, malformed or inconsistent."""


# --------------------------------------------------------------------------- #
# Environment expansion
# --------------------------------------------------------------------------- #

_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def _expand(value: Any) -> Any:
    """Recursively expand ``${VAR}`` / ``${VAR:-default}`` in all strings."""
    if isinstance(value, str):
        def repl(match: "re.Match[str]") -> str:
            name, default = match.group(1), match.group(2)
            got = os.environ.get(name)
            if got:
                return got
            if default is None:
                raise ConfigError(
                    "config references environment variable "
                    f"'${{{name}}}' but it is not set "
                    f"(set it, or use ${{{name}:-fallback}})"
                )
            return default

        return _ENV_RE.sub(repl, value)
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand(v) for v in value]
    return value


# --------------------------------------------------------------------------- #
# Small coercion helpers that produce readable errors
# --------------------------------------------------------------------------- #

def _req(data: dict, key: str, where: str) -> Any:
    if key not in data or data[key] in (None, ""):
        raise ConfigError(f"{where}: required field '{key}' is missing")
    return data[key]


def _as_bool(value: Any, where: str, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        low = value.strip().lower()
        if low in ("true", "yes", "on", "1"):
            return True
        if low in ("false", "no", "off", "0"):
            return False
    raise ConfigError(f"{where}: expected a boolean, got {value!r}")


def _as_int(value: Any, where: str, default: int, minimum: int | None = None) -> int:
    if value is None:
        out = default
    else:
        try:
            out = int(value)
        except (TypeError, ValueError):
            raise ConfigError(f"{where}: expected an integer, got {value!r}") from None
    if minimum is not None and out < minimum:
        raise ConfigError(f"{where}: must be >= {minimum}, got {out}")
    return out


def _as_float(value: Any, where: str, default: float, minimum: float | None = None) -> float:
    if value is None:
        out = default
    else:
        try:
            out = float(value)
        except (TypeError, ValueError):
            raise ConfigError(f"{where}: expected a number, got {value!r}") from None
    if minimum is not None and out < minimum:
        raise ConfigError(f"{where}: must be >= {minimum}, got {out}")
    return out


def _as_opt_float(value: Any, where: str) -> float | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ConfigError(f"{where}: expected a number or null, got {value!r}") from None


def _as_str_list(value: Any, where: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(v) for v in value]
    raise ConfigError(f"{where}: expected a string or list of strings, got {value!r}")


# --------------------------------------------------------------------------- #
# Dataclasses
# --------------------------------------------------------------------------- #

@dataclass
class AlertRule:
    """Thresholds that decide whether a GPU counts as free / low-load.

    Every configured condition is combined with AND, so a GPU is only reported
    as available when it satisfies all of them. Set an optional condition to
    ``null`` to switch it off.
    """

    min_free_memory_gb: float = 24.0
    min_free_memory_pct: float | None = None
    max_gpu_utilization: float = 5.0
    max_memory_utilization: float | None = 10.0
    max_temperature_c: float | None = None
    require_no_processes: bool = False
    max_process_memory_gb: float | None = None
    sustained_checks: int = 2
    cooldown_minutes: int = 60
    notify_on_busy: bool = False
    notify_on_recovery: bool = True
    notify_on_unreachable: bool = True
    unreachable_after_failures: int = 3

    @classmethod
    def from_dict(cls, data: dict | None, where: str) -> "AlertRule":
        data = data or {}
        if not isinstance(data, dict):
            raise ConfigError(f"{where}: expected a mapping of alert settings")
        default = cls()
        return cls(
            min_free_memory_gb=_as_float(
                data.get("min_free_memory_gb"), f"{where}.min_free_memory_gb",
                default.min_free_memory_gb, minimum=0.0),
            min_free_memory_pct=_as_opt_float(
                data.get("min_free_memory_pct"), f"{where}.min_free_memory_pct"),
            max_gpu_utilization=_as_float(
                data.get("max_gpu_utilization"), f"{where}.max_gpu_utilization",
                default.max_gpu_utilization, minimum=0.0),
            max_memory_utilization=_as_opt_float(
                data.get("max_memory_utilization"), f"{where}.max_memory_utilization"),
            max_temperature_c=_as_opt_float(
                data.get("max_temperature_c"), f"{where}.max_temperature_c"),
            require_no_processes=_as_bool(
                data.get("require_no_processes"), f"{where}.require_no_processes",
                default.require_no_processes),
            max_process_memory_gb=_as_opt_float(
                data.get("max_process_memory_gb"), f"{where}.max_process_memory_gb"),
            sustained_checks=_as_int(
                data.get("sustained_checks"), f"{where}.sustained_checks",
                default.sustained_checks, minimum=1),
            cooldown_minutes=_as_int(
                data.get("cooldown_minutes"), f"{where}.cooldown_minutes",
                default.cooldown_minutes, minimum=0),
            notify_on_busy=_as_bool(
                data.get("notify_on_busy"), f"{where}.notify_on_busy", default.notify_on_busy),
            notify_on_recovery=_as_bool(
                data.get("notify_on_recovery"), f"{where}.notify_on_recovery",
                default.notify_on_recovery),
            notify_on_unreachable=_as_bool(
                data.get("notify_on_unreachable"), f"{where}.notify_on_unreachable",
                default.notify_on_unreachable),
            unreachable_after_failures=_as_int(
                data.get("unreachable_after_failures"), f"{where}.unreachable_after_failures",
                default.unreachable_after_failures, minimum=1),
        )

    def merged_with(self, override: dict | None, where: str) -> "AlertRule":
        """Return a copy with per-server overrides applied on top of this rule."""
        if not override:
            return self
        return AlertRule.from_dict({**self.to_dict(), **(override or {})}, where)

    def to_dict(self) -> dict:
        return {
            "min_free_memory_gb": self.min_free_memory_gb,
            "min_free_memory_pct": self.min_free_memory_pct,
            "max_gpu_utilization": self.max_gpu_utilization,
            "max_memory_utilization": self.max_memory_utilization,
            "max_temperature_c": self.max_temperature_c,
            "require_no_processes": self.require_no_processes,
            "max_process_memory_gb": self.max_process_memory_gb,
            "sustained_checks": self.sustained_checks,
            "cooldown_minutes": self.cooldown_minutes,
            "notify_on_busy": self.notify_on_busy,
            "notify_on_recovery": self.notify_on_recovery,
            "notify_on_unreachable": self.notify_on_unreachable,
            "unreachable_after_failures": self.unreachable_after_failures,
        }


@dataclass
class ServerConfig:
    """One GPU server reachable over SSH."""

    name: str
    host: str
    user: str
    port: int = 22
    label: str = ""
    enabled: bool = True
    password: str | None = None
    password_file: str | None = None
    key_file: str | None = None
    key_passphrase: str | None = None
    transport: str | None = None          # None -> inherit ssh.transport
    alert_overrides: dict = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    extra_ssh_options: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict, where: str) -> "ServerConfig":
        if not isinstance(data, dict):
            raise ConfigError(f"{where}: expected a mapping")
        name = str(_req(data, "name", where))
        transport = data.get("transport")
        if transport is not None:
            transport = str(transport).strip().lower()
            if transport not in ("auto", "paramiko", "system_ssh"):
                raise ConfigError(
                    f"{where}.transport: must be one of auto|paramiko|system_ssh, "
                    f"got {transport!r}"
                )
        return cls(
            name=name,
            host=str(_req(data, "host", where)),
            user=str(data.get("user") or "root"),
            port=_as_int(data.get("port"), f"{where}.port", 22, minimum=1),
            label=str(data.get("label") or name),
            enabled=_as_bool(data.get("enabled"), f"{where}.enabled", True),
            password=data.get("password") or None,
            password_file=data.get("password_file") or None,
            key_file=data.get("key_file") or None,
            key_passphrase=data.get("key_passphrase") or None,
            transport=transport,
            alert_overrides=data.get("alert") or {},
            tags=_as_str_list(data.get("tags"), f"{where}.tags"),
            extra_ssh_options=data.get("ssh_options") or {},
        )

    # -- credentials -------------------------------------------------------- #

    def resolved_password(self) -> str | None:
        """Password from config, or from ``password_file`` when configured."""
        if self.password:
            return self.password
        if self.password_file:
            path = Path(os.path.expanduser(self.password_file))
            try:
                return path.read_text(encoding="utf-8").strip()
            except OSError as exc:
                raise ConfigError(
                    f"server '{self.name}': cannot read password_file {path}: {exc}"
                ) from exc
        return None

    def resolved_key_file(self) -> str | None:
        if not self.key_file:
            return None
        return os.path.expanduser(self.key_file)

    @property
    def target(self) -> str:
        return f"{self.user}@{self.host}:{self.port}"

    def describe(self) -> str:
        return f"{self.label} ({self.target})"


@dataclass
class SshSettings:
    connect_timeout: int = 20
    command_timeout: int = 30
    keepalive: int = 30
    max_retries: int = 2
    retry_backoff: float = 3.0
    transport: str = "auto"
    system_ssh_path: str = "ssh"
    nvidia_smi_paths: list[str] = field(default_factory=lambda: [
        "nvidia-smi",
        "/usr/bin/nvidia-smi",
        "/usr/local/nvidia/bin/nvidia-smi",
        "/usr/local/bin/nvidia-smi",
        "/opt/nvidia/bin/nvidia-smi",
    ])
    reuse_connection: bool = True

    @classmethod
    def from_dict(cls, data: dict | None, where: str) -> "SshSettings":
        data = data or {}
        if not isinstance(data, dict):
            raise ConfigError(f"{where}: expected a mapping")
        default = cls()
        transport = str(data.get("transport", default.transport)).strip().lower()
        if transport not in ("auto", "paramiko", "system_ssh"):
            raise ConfigError(
                f"{where}.transport: must be one of auto|paramiko|system_ssh, got {transport!r}"
            )
        paths = _as_str_list(data.get("nvidia_smi_paths"), f"{where}.nvidia_smi_paths")
        return cls(
            connect_timeout=_as_int(data.get("connect_timeout"), f"{where}.connect_timeout",
                                    default.connect_timeout, minimum=1),
            command_timeout=_as_int(data.get("command_timeout"), f"{where}.command_timeout",
                                    default.command_timeout, minimum=1),
            keepalive=_as_int(data.get("keepalive"), f"{where}.keepalive",
                              default.keepalive, minimum=0),
            max_retries=_as_int(data.get("max_retries"), f"{where}.max_retries",
                                default.max_retries, minimum=0),
            retry_backoff=_as_float(data.get("retry_backoff"), f"{where}.retry_backoff",
                                    default.retry_backoff, minimum=0.0),
            transport=transport,
            system_ssh_path=str(data.get("system_ssh_path") or default.system_ssh_path),
            nvidia_smi_paths=paths or default.nvidia_smi_paths,
            reuse_connection=_as_bool(data.get("reuse_connection"),
                                      f"{where}.reuse_connection", default.reuse_connection),
        )


@dataclass
class AppSettings:
    interval_seconds: int = 300
    jitter_seconds: int = 0
    log_dir: str = "logs"
    state_file: str = "state.json"
    history_file: str = "history.jsonl"
    history_csv: str | None = "history.csv"
    log_level: str = "INFO"
    console: bool = True
    encoding: str = "utf-8"

    @classmethod
    def from_dict(cls, data: dict | None, where: str) -> "AppSettings":
        data = data or {}
        if not isinstance(data, dict):
            raise ConfigError(f"{where}: expected a mapping")
        default = cls()
        level = str(data.get("log_level", default.log_level)).upper()
        if level not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
            raise ConfigError(f"{where}.log_level: unknown level {level!r}")
        csv_name = data.get("history_csv", default.history_csv)
        return cls(
            interval_seconds=_as_int(data.get("interval_seconds"), f"{where}.interval_seconds",
                                     default.interval_seconds, minimum=10),
            jitter_seconds=_as_int(data.get("jitter_seconds"), f"{where}.jitter_seconds",
                                   default.jitter_seconds, minimum=0),
            log_dir=str(data.get("log_dir") or default.log_dir),
            state_file=str(data.get("state_file") or default.state_file),
            history_file=str(data.get("history_file") or default.history_file),
            history_csv=(str(csv_name) if csv_name else None),
            log_level=level,
            console=_as_bool(data.get("console"), f"{where}.console", default.console),
            encoding=str(data.get("encoding") or default.encoding),
        )


@dataclass
class Config:
    """Fully-resolved application configuration."""

    path: Path
    app: AppSettings
    ssh: SshSettings
    alert: AlertRule
    servers: list[ServerConfig]
    channels: list[dict]

    @property
    def base_dir(self) -> Path:
        return self.path.parent

    def resolve_path(self, value: str) -> Path:
        path = Path(os.path.expanduser(value))
        return path if path.is_absolute() else (self.base_dir / path)

    def alert_for(self, server: ServerConfig) -> AlertRule:
        return self.alert.merged_with(
            server.alert_overrides, f"servers[{server.name}].alert"
        )

    def transport_for(self, server: ServerConfig) -> str:
        return server.transport or self.ssh.transport

    def enabled_servers(self) -> list[ServerConfig]:
        return [s for s in self.servers if s.enabled]

    def redacted(self) -> dict:
        """Config as a dict safe to log or print (secrets removed)."""
        def server_dict(s: ServerConfig) -> dict:
            return {
                "name": s.name,
                "label": s.label,
                "host": s.host,
                "port": s.port,
                "user": s.user,
                "enabled": s.enabled,
                "transport": self.transport_for(s),
                "password": "***" if (s.password or s.password_file) else None,
                "key_file": s.key_file,
                "tags": s.tags,
            }

        return {
            "app": {
                "interval_seconds": self.app.interval_seconds,
                "jitter_seconds": self.app.jitter_seconds,
                "log_level": self.app.log_level,
            },
            "ssh": {
                "transport": self.ssh.transport,
                "connect_timeout": self.ssh.connect_timeout,
                "command_timeout": self.ssh.command_timeout,
                "max_retries": self.ssh.max_retries,
            },
            "alert": self.alert.to_dict(),
            "servers": [server_dict(s) for s in self.servers],
            "notify": {"channels": [c.get("type") for c in self.channels]},
        }


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #

DEFAULT_CONFIG_NAMES = ("config.yaml", "config.yml", "wheresmygpu.yaml")


def find_config(explicit: str | os.PathLike | None = None) -> Path:
    """Locate the configuration file, preferring an explicit path."""
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_file():
            raise ConfigError(f"config file not found: {path}")
        return path
    env = os.environ.get("WHERESMYGPU_CONFIG")
    if env:
        path = Path(env).expanduser()
        if not path.is_file():
            raise ConfigError(f"WHERESMYGPU_CONFIG points to a missing file: {path}")
        return path
    for name in DEFAULT_CONFIG_NAMES:
        for base in (Path.cwd(), Path(__file__).resolve().parent.parent):
            candidate = base / name
            if candidate.is_file():
                return candidate
    raise ConfigError(
        "no configuration file found. Create config.yaml (see config.example.yaml) "
        "or pass --config PATH."
    )


def load_config(explicit: str | os.PathLike | None = None) -> Config:
    """Read, expand and validate the configuration file."""
    path = find_config(explicit)
    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read config {path}: {exc}") from exc

    try:
        raw = yaml.safe_load(raw_text) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: invalid YAML: {exc}") from exc

    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: top level must be a mapping")

    raw = _expand(raw)

    app = AppSettings.from_dict(raw.get("app"), "app")
    ssh = SshSettings.from_dict(raw.get("ssh"), "ssh")
    alert = AlertRule.from_dict(raw.get("alert"), "alert")

    servers_raw = raw.get("servers")
    if not servers_raw:
        raise ConfigError("servers: at least one GPU server must be configured")
    if not isinstance(servers_raw, list):
        raise ConfigError("servers: expected a list")

    servers: list[ServerConfig] = []
    seen: set[str] = set()
    for idx, item in enumerate(servers_raw):
        server = ServerConfig.from_dict(item, f"servers[{idx}]")
        if server.name in seen:
            raise ConfigError(f"servers[{idx}]: duplicate server name {server.name!r}")
        seen.add(server.name)
        if not server.enabled:
            continue
        if not (server.password or server.password_file or server.key_file):
            raise ConfigError(
                f"servers[{idx}] ({server.name}): configure one of "
                "'password', 'password_file' or 'key_file'"
            )
        servers.append(server)

    if not servers:
        raise ConfigError("servers: every configured server is disabled")

    notify_raw = raw.get("notify") or {}
    if not isinstance(notify_raw, dict):
        raise ConfigError("notify: expected a mapping")
    channels = notify_raw.get("channels")
    if channels is None:
        channels = [{"type": "console"}, {"type": "logfile"}]
    if not isinstance(channels, list) or not channels:
        raise ConfigError("notify.channels: expected a non-empty list")

    cleaned: list[dict] = []
    for idx, channel in enumerate(channels):
        if isinstance(channel, str):
            channel = {"type": channel}
        if not isinstance(channel, dict):
            raise ConfigError(f"notify.channels[{idx}]: expected a mapping or channel name")
        if not channel.get("type"):
            raise ConfigError(f"notify.channels[{idx}]: missing 'type'")
        cleaned.append(channel)

    if not any(
        _as_bool(c.get("enabled"), f"notify.channels[{i}].enabled", True)
        for i, c in enumerate(cleaned)
    ):
        raise ConfigError("notify.channels: all channels are disabled; enable at least one")

    return Config(path=path, app=app, ssh=ssh, alert=alert, servers=servers, channels=cleaned)