"""SSH transport for WheresMyGPU.

Two interchangeable backends are provided:

``paramiko``
    Pure-Python transport. Supports password *and* key authentication, and keeps
    a connection alive between polling rounds.
``system_ssh``
    Shells out to the OpenSSH client in batch mode. Useful when the remote host
    only accepts modern key types that older paramiko builds cannot negotiate.

``auto`` tries paramiko first and transparently falls back to ``system_ssh``.
"""

from __future__ import annotations

import logging
import os
import shlex
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass

from .config import Config, ServerConfig, SshSettings

log = logging.getLogger("wheresmygpu.ssh")


class SshError(RuntimeError):
    """Any failure while establishing a connection or running a command."""


@dataclass
class CommandResult:
    command: str
    stdout: str
    stderr: str
    exit_status: int

    @property
    def ok(self) -> bool:
        return self.exit_status == 0


# --------------------------------------------------------------------------- #
# Backends
# --------------------------------------------------------------------------- #

class _ParamikoBackend:
    """Persistent paramiko connection."""

    name = "paramiko"

    def __init__(self, server: ServerConfig, ssh: SshSettings) -> None:
        self.server = server
        self.settings = ssh
        self._client = None
        self._lock = threading.Lock()

    # -- lifecycle ---------------------------------------------------------- #

    def _connect(self):
        try:
            import paramiko
        except ImportError as exc:  # pragma: no cover - depends on environment
            raise SshError("paramiko is not installed (pip install paramiko)") from exc

        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        password = self.server.resolved_password()
        key_file = self.server.resolved_key_file()

        # Fall back to the ssh-agent / default keys only when no explicit
        # credential is configured. Apart from being more predictable, this
        # avoids paramiko picking up a legacy DSA key from the agent, which
        # aborts the handshake with
        # "ValueError: q must be exactly 160, 224, or 256 bits long".
        use_implicit = not (password or key_file)

        kwargs: dict = {
            "hostname": self.server.host,
            "port": self.server.port,
            "username": self.server.user,
            "timeout": self.settings.connect_timeout,
            "banner_timeout": self.settings.connect_timeout,
            "auth_timeout": self.settings.connect_timeout,
            "allow_agent": use_implicit,
            "look_for_keys": use_implicit,
        }

        if key_file:
            # Load the key ourselves and hand paramiko a ready pkey instead of
            # letting it retry the file as RSA, DSA, ECDSA and Ed25519 in turn.
            # That retry loop constructs a bogus DSA key for non-DSA files, and
            # signing with it makes modern `cryptography` raise
            # "q must be exactly 160, 224, or 256 bits long" *inside the
            # transport thread*, which aborts the whole handshake before
            # password authentication is ever attempted. DSA is deprecated and
            # disabled by default on modern OpenSSH servers, so we skip it.
            pkey = None
            for key_cls in (paramiko.Ed25519Key, paramiko.ECDSAKey, paramiko.RSAKey):
                try:
                    pkey = key_cls.from_private_key_file(
                        key_file, password=self.server.key_passphrase
                    )
                    break
                except Exception:
                    continue
            if pkey is not None:
                kwargs["pkey"] = pkey
            else:
                log.warning(
                    "[%s] could not load private key %s; falling back to "
                    "paramiko's default key handling", self.server.name, key_file
                )
                kwargs["key_filename"] = key_file
        if password:
            kwargs["password"] = password
        if self.server.key_passphrase:
            kwargs["passphrase"] = self.server.key_passphrase

        try:
            client.connect(**kwargs)
        except Exception as exc:
            try:
                client.close()
            except Exception:
                pass
            raise SshError(f"{self.server.target}: {type(exc).__name__}: {exc}") from exc

        transport = client.get_transport()
        if transport is not None and self.settings.keepalive:
            transport.set_keepalive(self.settings.keepalive)
        return client

    def _ensure_client(self):
        with self._lock:
            if self._client is not None:
                transport = self._client.get_transport()
                if transport is not None and transport.is_active():
                    return self._client
                self._close_locked()
            self._client = self._connect()
            return self._client

    def _close_locked(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None

    def close(self) -> None:
        with self._lock:
            self._close_locked()

    # -- commands ----------------------------------------------------------- #

    def run(self, command: str, timeout: int, reuse: bool = True) -> CommandResult:
        if reuse:
            return self._run_reused(command, timeout)
        return self._run_fresh(command, timeout)

    def _run_fresh(self, command: str, timeout: int) -> CommandResult:
        client = self._connect()
        try:
            return self._exec(client, command, timeout)
        finally:
            try:
                client.close()
            except Exception:
                pass

    def _run_reused(self, command: str, timeout: int) -> CommandResult:
        client = self._ensure_client()
        try:
            return self._exec(client, command, timeout)
        except SshError:
            # A stale connection is the most likely cause; reconnect once.
            with self._lock:
                self._close_locked()
            client = self._ensure_client()
            return self._exec(client, command, timeout)

    def _exec(self, client, command: str, timeout: int) -> CommandResult:
        try:
            _stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
            out = stdout.read().decode("utf-8", errors="replace")
            err = stderr.read().decode("utf-8", errors="replace")
            status = stdout.channel.recv_exit_status()
        except Exception as exc:
            raise SshError(f"{self.server.target}: command failed: {exc}") from exc
        return CommandResult(command, out, err, status)


class _SystemSshBackend:
    """OpenSSH client in batch mode (one process per round)."""

    name = "system_ssh"

    def __init__(self, server: ServerConfig, ssh: SshSettings) -> None:
        self.server = server
        self.settings = ssh
        self._password = server.resolved_password()

    def close(self) -> None:
        return None

    def run(self, command: str, timeout: int, reuse: bool = True) -> CommandResult:
        if self._password:
            raise SshError(
                "system_ssh transport cannot use password authentication; "
                "install paramiko (pip install paramiko), configure key_file, "
                "or copy your public key to the server"
            )

        args = [
            self.settings.system_ssh_path,
            "-o", "BatchMode=yes",
            "-o", f"ConnectTimeout={self.settings.connect_timeout}",
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", "ServerAliveInterval=15",
            "-o", "ServerAliveCountMax=3",
            "-p", str(self.server.port),
        ]
        key_file = self.server.resolved_key_file()
        if key_file:
            args += ["-i", key_file, "-o", "IdentitiesOnly=yes"]
        args += [f"{self.server.user}@{self.server.host}", command]

        env = dict(os.environ)
        env.setdefault("LC_ALL", "C")

        try:
            proc = subprocess.run(
                args,
                capture_output=True,
                timeout=timeout,
                env=env,
                check=False,
            )
        except FileNotFoundError as exc:
            raise SshError(
                f"ssh client not found at {self.settings.system_ssh_path!r}"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise SshError(
                f"{self.server.target}: command timed out after {timeout}s"
            ) from exc

        return CommandResult(
            command,
            proc.stdout.decode("utf-8", errors="replace"),
            proc.stderr.decode("utf-8", errors="replace"),
            proc.returncode,
        )


class _SudoPasswordBackend:
    """paramiko backend that can feed a sudo password to ``sudo -S``.

    Only used when the remote ``nvidia-smi`` needs elevation, which is rare.
    """

    name = "sudo-password"


# --------------------------------------------------------------------------- #
# Public facade
# --------------------------------------------------------------------------- #

class SshRunner:
    """Runs commands on one server, retrying and switching backends on failure.

    The runner is intentionally stateful so a long-running monitor can reuse a
    single SSH connection across polling rounds.
    """

    def __init__(self, server: ServerConfig, config: Config) -> None:
        self.server = server
        self.config = config
        self.settings = config.ssh
        self._transport = config.transport_for(server)
        self._backend = None
        self._resolved_smi: str | None = None
        self._lock = threading.Lock()

    # -- backend selection -------------------------------------------------- #

    def _make_backend(self, kind: str):
        if kind == "paramiko":
            return _ParamikoBackend(self.server, self.settings)
        return _SystemSshBackend(self.server, self.settings)

    def _backend_order(self) -> list[str]:
        if self._transport == "paramiko":
            return ["paramiko"]
        if self._transport == "system_ssh":
            return ["system_ssh"]
        return ["paramiko", "system_ssh"]

    def close(self) -> None:
        with self._lock:
            if self._backend is not None:
                try:
                    self._backend.close()
                except Exception:
                    pass
                self._backend = None

    # -- command execution -------------------------------------------------- #

    def run(self, command: str, timeout: int | None = None) -> CommandResult:
        timeout = timeout or self.settings.command_timeout
        reuse = self.settings.reuse_connection
        last_error: Exception | None = None

        for kind in self._backend_order():
            attempts = self.settings.max_retries + 1
            for attempt in range(1, attempts + 1):
                try:
                    with self._lock:
                        if self._backend is None or self._backend.name != kind:
                            if self._backend is not None:
                                try:
                                    self._backend.close()
                                except Exception:
                                    pass
                            self._backend = self._make_backend(kind)
                        backend = self._backend

                    started = time.monotonic()
                    result = backend.run(command, timeout, reuse=reuse)
                    elapsed = time.monotonic() - started
                    log.debug(
                        "[%s] %s -> exit=%s in %.2fs",
                        self.server.name, command, result.exit_status, elapsed,
                    )
                    return result
                except SshError as exc:
                    last_error = exc
                    log.debug(
                        "[%s] %s backend attempt %d/%d failed: %s",
                        self.server.name, kind, attempt, attempts, exc,
                    )
                    self.close()
                    if attempt < attempts and self.settings.retry_backoff:
                        time.sleep(self.settings.retry_backoff * attempt)

        raise SshError(
            f"{self.server.target}: all transports failed. Last error: {last_error}"
        )

    # -- nvidia-smi discovery ----------------------------------------------- #

    def resolve_nvidia_smi(self, refresh: bool = False) -> str:
        """Find a working ``nvidia-smi`` invocation on the remote host."""
        if self._resolved_smi and not refresh:
            return self._resolved_smi

        probes = list(self.settings.nvidia_smi_paths)
        quoted = " ".join(shlex.quote(p) for p in probes)
        script = (
            f"for c in {quoted}; do "
            'if command -v "$c" >/dev/null 2>&1; then echo "FOUND:$c"; exit 0; fi; '
            "done; exit 127"
        )
        result = self.run(script, timeout=min(self.settings.command_timeout, 20))
        for line in result.stdout.splitlines():
            line = line.strip()
            if line.startswith("FOUND:"):
                self._resolved_smi = line[len("FOUND:"):].strip()
                log.info("[%s] nvidia-smi located at %s", self.server.name, self._resolved_smi)
                return self._resolved_smi

        raise SshError(
            f"{self.server.target}: nvidia-smi not found in any of "
            f"{probes} (stderr: {result.stderr.strip()[:200]})"
        )