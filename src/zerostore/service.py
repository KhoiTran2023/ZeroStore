"""Keeps `zerostore serve --http` running in the background, for `zerostore service`.

Linux gets a systemd unit: a user unit (`~/.config/systemd/user/zerostore.service`)
that starts on boot once lingering is on, or a system unit when run as root. macOS
gets a launchd agent (`~/Library/LaunchAgents/zerostore.serve.plist`) that starts
when you log in. Both restart the server if it crashes, and both run the same
Python that is running now, so a pipx or uv install keeps working after upgrades.

Every command goes through a `Run` callable, so tests can check what would run
without touching the machine's service manager.
"""

from __future__ import annotations

import getpass
import os
import plistlib
import re
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from zerostore.config import SECRETS_FILENAME, read_secrets

UNIT_NAME = "zerostore.service"
LAUNCHD_LABEL = "zerostore.serve"
LOG_FILENAME = "serve.log"

Run = Callable[[list[str]], subprocess.CompletedProcess]


def run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, check=False)


class ServiceError(Exception):
    """The service can't be set up, started or found on this machine."""


@dataclass
class Plan:
    """What the service runs: `zerostore serve --http` with these options."""

    config_path: Path
    host: str
    port: int
    serve_args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)

    def command(self) -> list[str]:
        # The interpreter's own path (not resolved: a venv's python is a symlink that must stay one).
        return [
            sys.executable,
            "-m",
            "zerostore",
            "serve",
            "--http",
            "--config",
            str(self.config_path),
        ] + list(self.serve_args)


@dataclass
class Status:
    installed: bool
    running: bool
    starts: str  # when it starts by itself, e.g. "on boot"
    path: Path
    logs: str  # the command that shows its output
    pid: int | None = None
    detail: str = ""


def _fail_message(result: subprocess.CompletedProcess, what: str) -> str:
    said = (result.stderr or result.stdout or "").strip().rstrip(".")
    return f"{what} failed" + (f": {said}" if said else "")


class Systemd:
    """A systemd unit: a user unit, or a system unit when run as root."""

    def __init__(self, scope: str = "user", run: Run = run, home: Path | None = None) -> None:
        self.scope = scope
        self.run = run
        home = home or Path.home()
        if scope == "system":
            self.path = Path("/etc/systemd/system") / UNIT_NAME
            self.ctl = ["systemctl"]
            self.logs = f"journalctl -u {UNIT_NAME} -f"
        else:
            config_home = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config")
            self.path = config_home / "systemd" / "user" / UNIT_NAME
            self.ctl = ["systemctl", "--user"]
            self.logs = f"journalctl --user -u {UNIT_NAME} -f"
        self.name = "systemd system unit" if scope == "system" else "systemd user unit"

    def check(self) -> None:
        if not shutil.which("systemctl") and self.run is run:
            raise ServiceError("systemctl isn't installed, so there is no systemd to run the service")
        result = self.run(self.ctl + ["show-environment"])
        if result.returncode != 0:
            said = " ".join((result.stderr or result.stdout or "").split()).rstrip(".")
            hint = (
                "If systemd runs here, log in as this user directly or with `ssh` (not `su` or `sudo -u`) "
                "so your user's systemd is running. "
                if self.scope == "user"
                else ""
            )
            raise ServiceError(
                f"can't reach systemd ({said or 'no answer'}). {hint}Without it, keep "
                "`zerostore serve --http` running yourself, in a terminal or your own process manager."
            )

    def unit(self, plan: Plan) -> str:
        lines = [
            "[Unit]",
            "Description=ZeroStore: MCP server, web UI and timed syncs",
            "Documentation=https://github.com/KhoiTran2023/ZeroStore",
        ]
        if self.scope == "system":
            lines += ["Wants=network-online.target", "After=network-online.target"]
        lines += [
            "",
            "[Service]",
            "Type=simple",
            "ExecStart=" + " ".join(_systemd_quote(arg) for arg in plan.command()),
            "Restart=on-failure",
            "RestartSec=5",
            "Environment=PYTHONUNBUFFERED=1",
        ]
        lines += [f"Environment={_systemd_quote(f'{k}={v}')}" for k, v in sorted(plan.env.items())]
        lines += [
            "",
            "[Install]",
            "WantedBy=" + ("multi-user.target" if self.scope == "system" else "default.target"),
            "",
        ]
        return "\n".join(lines)

    def write(self, plan: Plan) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(self.unit(plan), encoding="utf-8")

    def start(self) -> None:
        for args, what in (
            (["daemon-reload"], "reloading systemd"),
            (["enable", UNIT_NAME], "enabling the service"),
            (["restart", UNIT_NAME], "starting the service"),
        ):
            result = self.run(self.ctl + args)
            if result.returncode != 0:
                raise ServiceError(_fail_message(result, what) + f". See `{self.logs}`.")

    def stop(self) -> None:
        self.run(self.ctl + ["stop", UNIT_NAME])

    def remove(self) -> None:
        self.run(self.ctl + ["disable", "--now", UNIT_NAME])
        self.path.unlink(missing_ok=True)
        self.run(self.ctl + ["daemon-reload"])
        self.run(self.ctl + ["reset-failed", UNIT_NAME])

    def starts(self) -> str:
        if self.scope == "system" or self.lingering():
            return "on boot"
        return "when you log in (turn on lingering so it starts on boot)"

    def lingering(self) -> bool:
        result = self.run(["loginctl", "show-user", getpass.getuser(), "--property=Linger", "--value"])
        return result.returncode == 0 and result.stdout.strip() == "yes"

    def after_start(self) -> list[str]:
        """User units only start at boot when lingering is on; turn it on, or say how."""
        if self.scope == "system" or self.lingering():
            return []
        result = self.run(["loginctl", "enable-linger", getpass.getuser()])
        if result.returncode == 0 and self.lingering():
            return [
                "Turned on lingering for your user, so the service starts on boot, before anyone logs in."
            ]
        return [
            "It starts when you log in. To start it on boot instead, run: "
            f"sudo loginctl enable-linger {getpass.getuser()}"
        ]

    def status(self) -> Status:
        installed = self.path.exists()
        active = self.run(self.ctl + ["is-active", UNIT_NAME]).stdout.strip()
        pid = None
        if active == "active":
            shown = self.run(self.ctl + ["show", UNIT_NAME, "--property=MainPID", "--value"]).stdout.strip()
            pid = int(shown) if shown.isdigit() and shown != "0" else None
        enabled = self.run(self.ctl + ["is-enabled", UNIT_NAME]).stdout.strip()
        return Status(
            installed=installed,
            running=active == "active",
            starts=self.starts() if enabled == "enabled" else "only when started by hand (not enabled)",
            path=self.path,
            logs=self.logs,
            pid=pid,
            detail=active or "unknown",
        )


class Launchd:
    """A launchd agent in ~/Library/LaunchAgents: starts when you log in."""

    name = "launchd agent"

    def __init__(self, uid: int | None = None, run: Run = run, home: Path | None = None) -> None:
        self.run = run
        home = home or Path.home()
        self.uid = os.getuid() if uid is None else uid
        self.path = home / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
        self.domain = f"gui/{self.uid}"
        self.target = f"{self.domain}/{LAUNCHD_LABEL}"
        self.log_path: Path | None = None
        self.stop_timeout = 15.0

    @property
    def logs(self) -> str:
        log = self.log_path or self._written_log()
        return f"tail -f {log}" if log else "Console.app"

    def _written_log(self) -> Path | None:
        try:
            data = plistlib.loads(self.path.read_bytes())
        except (OSError, plistlib.InvalidFileException, ValueError):
            return None
        log = data.get("StandardErrorPath")
        return Path(log) if log else None

    def check(self) -> None:
        if not shutil.which("launchctl") and self.run is run:
            raise ServiceError("launchctl isn't available, so launchd can't run the service")

    def plist(self, plan: Plan) -> bytes:
        self.log_path = plan.config_path.parent / "logs" / LOG_FILENAME
        return plistlib.dumps(
            {
                "Label": LAUNCHD_LABEL,
                "ProgramArguments": plan.command(),
                "RunAtLoad": True,
                "KeepAlive": {"SuccessfulExit": False},
                "ThrottleInterval": 5,
                "StandardOutPath": str(self.log_path),
                "StandardErrorPath": str(self.log_path),
                "EnvironmentVariables": {"PYTHONUNBUFFERED": "1", **plan.env},
            }
        )

    def write(self, plan: Plan) -> None:
        data = self.plist(plan)
        assert self.log_path is not None
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_bytes(data)

    def start(self) -> None:
        self.run(["launchctl", "bootout", self.target])
        result = self.run(["launchctl", "bootstrap", self.domain, str(self.path)])
        if result.returncode != 0:
            raise ServiceError(
                _fail_message(result, "loading the launchd agent")
                + ". launchd agents need you logged in at the Mac (not only over ssh)."
            )
        self.run(["launchctl", "enable", self.target])

    def stop(self) -> None:
        # bootout returns before the server has exited; wait, so its port is free when this returns.
        pid = self.status().pid
        self.run(["launchctl", "bootout", self.target])
        if pid:
            _wait_until(lambda: not _alive(pid), timeout=self.stop_timeout)

    def remove(self) -> None:
        self.stop()
        self.path.unlink(missing_ok=True)

    def after_start(self) -> list[str]:
        return []

    def status(self) -> Status:
        result = self.run(["launchctl", "print", self.target])
        state = re.search(r"^\s*state = (\S+)", result.stdout, re.MULTILINE)
        pid = re.search(r"^\s*pid = (\d+)", result.stdout, re.MULTILINE)
        loaded = result.returncode == 0
        return Status(
            installed=self.path.exists(),
            running=loaded and bool(state) and state.group(1) == "running",
            starts="when you log in" if self.path.exists() else "never (not installed)",
            path=self.path,
            logs=self.logs,
            pid=int(pid.group(1)) if pid else None,
            detail=(state.group(1) if state else "loaded") if loaded else "not loaded",
        )


Manager = Systemd | Launchd


def detect(run: Run = run) -> Manager:
    """The service manager for this machine."""
    if sys.platform == "darwin":
        return Launchd(run=run)
    if sys.platform.startswith("linux"):
        return Systemd("system" if os.geteuid() == 0 else "user", run=run)
    raise ServiceError(
        f"`zerostore service` works on Linux (systemd) and macOS (launchd), not {sys.platform}. "
        "Run `zerostore serve --http` with your own process manager instead."
    )


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _systemd_quote(arg: str) -> str:
    """One ExecStart argument, quoted the way systemd reads it (`%` and `$` are its own syntax)."""
    arg = arg.replace("%", "%%").replace("$", "$$")
    if arg and not re.search(r"[\s\"'\\;]", arg):
        return arg
    return '"' + arg.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v)


def shell_only_variables(config_path: Path, environ: dict[str, str] | None = None) -> list[str]:
    """`${NAME}`s in the config that only this shell has: the service won't see them."""
    environ = dict(os.environ) if environ is None else environ
    try:
        data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return []
    names = {m for s in _strings(data) for m in re.findall(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", s)}
    saved = read_secrets(config_path.parent / SECRETS_FILENAME)
    return sorted(n for n in names if n in environ and n not in saved)


def connect_host(host: str) -> str:
    """Where to reach a server bound to `host` from this machine."""
    if host in ("0.0.0.0", "", "localhost"):
        return "127.0.0.1"
    if host == "::":
        return "::1"
    return host.strip("[]")


def port_in_use(host: str, port: int) -> bool:
    try:
        with socket.create_connection((connect_host(host), port), timeout=0.5):
            return True
    except OSError:
        return False


def _wait_until(
    done: Callable[[], bool], timeout: float, sleep: Callable[[float], None] = time.sleep
) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        if done():
            return True
        if time.monotonic() >= deadline:
            return False
        sleep(0.25)


def wait_for_port(
    host: str, port: int, timeout: float = 30.0, sleep: Callable[[float], None] = time.sleep
) -> bool:
    return _wait_until(lambda: port_in_use(host, port), timeout, sleep)


def install(
    manager: Manager,
    plan: Plan,
    *,
    in_use: Callable[[str, int], bool] | None = None,
    wait: Callable[[str, int], bool] | None = None,
    stop_timeout: float = 15.0,
) -> list[str]:
    """Write the service, start it and wait until it answers. Returns notes to show."""
    in_use = in_use or port_in_use
    wait = wait or wait_for_port
    manager.check()
    if manager.path.exists():
        manager.stop()
        # launchd's bootout returns before the old server has let go of its port.
        _wait_until(lambda: not in_use(plan.host, plan.port), timeout=stop_timeout)
    if in_use(plan.host, plan.port):
        raise ServiceError(
            f"something is already listening on port {plan.port}. Stop it (a `zerostore serve --http` "
            "running in a terminal, say) or pass --port."
        )
    manager.write(plan)
    manager.start()
    if not wait(plan.host, plan.port):
        raise ServiceError(
            f"the service started but isn't answering on port {plan.port}. See `{manager.logs}`."
        )
    return manager.after_start()


def uninstall(manager: Manager) -> bool:
    """Stop and remove the service. Returns False when it wasn't installed."""
    manager.check()
    if not manager.path.exists():
        return False
    manager.remove()
    return True
