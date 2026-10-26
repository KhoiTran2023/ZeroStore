"""`zerostore service` and install.sh, with the service manager faked: nothing here
touches the machine's systemd or launchd. CI also installs for real (the install job)."""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from zerostore import service
from zerostore.cli import app
from zerostore.config import home_dir

ROOT = Path(__file__).resolve().parents[1]
runner = CliRunner()


@pytest.fixture(autouse=True)
def _no_real_config_home(monkeypatch):
    """Never let a test write or remove a real unit file (CI runners set XDG_CONFIG_HOME)."""
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)


class FakeRun:
    """Answers commands from a table of (prefix -> (returncode, stdout)); records every call."""

    def __init__(self, answers: dict[tuple[str, ...], object] | None = None) -> None:
        self.answers = answers or {}
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str]) -> subprocess.CompletedProcess:
        self.calls.append(list(args))
        best = None
        for prefix, answer in self.answers.items():
            if tuple(args[: len(prefix)]) == prefix and (best is None or len(prefix) > len(best[0])):
                best = (prefix, answer)
        answer = best[1] if best else (0, "")
        if isinstance(answer, list):  # a queue of answers, the last one repeating
            answer = answer.pop(0) if len(answer) > 1 else answer[0]
        code, out = answer
        return subprocess.CompletedProcess(args, code, stdout=out, stderr="" if code == 0 else out)

    def ran(self, *prefix: str) -> bool:
        return any(tuple(c[: len(prefix)]) == prefix for c in self.calls)


def plan(tmp_path, **kw) -> service.Plan:
    return service.Plan(config_path=tmp_path / "zs" / "zerostore.yaml", host="localhost", port=8765, **kw)


# --- systemd ---------------------------------------------------------------


def test_user_unit_runs_this_python_with_the_config(tmp_path, monkeypatch):
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    unit = service.Systemd("user", run=FakeRun(), home=tmp_path)
    text = unit.unit(plan(tmp_path, serve_args=["--port", "9000"], env={"ZEROSTORE_HOME": "/srv/z s"}))
    exec_line = next(line for line in text.splitlines() if line.startswith("ExecStart="))
    assert exec_line.startswith(
        f"ExecStart={service._systemd_quote(sys.executable)} -m zerostore serve --http"
    )
    assert f"--config {tmp_path / 'zs' / 'zerostore.yaml'}" in exec_line
    assert exec_line.endswith("--port 9000")
    assert 'Environment="ZEROSTORE_HOME=/srv/z s"' in text
    assert "Restart=on-failure" in text and "WantedBy=default.target" in text
    assert "network-online" not in text  # a user manager has no such target
    assert unit.path == tmp_path / ".config" / "systemd" / "user" / "zerostore.service"


def test_system_unit_waits_for_the_network(tmp_path):
    text = service.Systemd("system", run=FakeRun()).unit(plan(tmp_path))
    assert "After=network-online.target" in text and "WantedBy=multi-user.target" in text


@pytest.mark.parametrize(
    ("arg", "quoted"),
    [
        ("/usr/bin/python3", "/usr/bin/python3"),
        ("/home/a b/zerostore.yaml", '"/home/a b/zerostore.yaml"'),
        ("50%", "50%%"),
        ("$HOME", "$$HOME"),
        ('say "hi"', '"say \\"hi\\""'),
        ("back\\slash", '"back\\\\slash"'),
    ],
)
def test_systemd_quoting(arg, quoted):
    assert service._systemd_quote(arg) == quoted


def systemd_answers(linger: object = (0, "yes\n"), active="active\n") -> dict:
    return {
        ("systemctl", "--user", "is-active"): (0 if active == "active\n" else 3, active),
        ("systemctl", "--user", "is-enabled"): (0, "enabled\n"),
        ("systemctl", "--user", "show", "zerostore.service"): (0, "4321\n"),
        ("loginctl", "show-user"): linger,
    }


def test_install_writes_enables_starts_and_turns_on_lingering(tmp_path):
    run = FakeRun(systemd_answers(linger=[(0, "no\n"), (0, "yes\n")]))
    unit = service.Systemd("user", run=run, home=tmp_path)
    notes = service.install(unit, plan(tmp_path), in_use=lambda h, p: False, wait=lambda h, p: True)
    assert unit.path.exists()
    started = [c[2:] for c in run.calls if c[:2] == ["systemctl", "--user"]]
    assert started[:4] == [
        ["show-environment"],
        ["daemon-reload"],
        ["enable", "zerostore.service"],
        ["restart", "zerostore.service"],
    ]
    assert run.ran("loginctl", "enable-linger")
    assert notes == [
        "Turned on lingering for your user, so the service starts on boot, before anyone logs in."
    ]


def test_install_says_how_to_start_on_boot_when_lingering_is_refused(tmp_path):
    run = FakeRun(
        {**systemd_answers(linger=(0, "no\n")), ("loginctl", "enable-linger"): (1, "Access denied")}
    )
    unit = service.Systemd("user", run=run, home=tmp_path)
    notes = service.install(unit, plan(tmp_path), in_use=lambda h, p: False, wait=lambda h, p: True)
    assert "sudo loginctl enable-linger" in notes[0]
    assert unit.status().starts.startswith("when you log in")


def test_install_refuses_a_busy_port_and_writes_nothing(tmp_path):
    unit = service.Systemd("user", run=FakeRun(), home=tmp_path)
    with pytest.raises(service.ServiceError, match="already listening on port 8765"):
        service.install(unit, plan(tmp_path), in_use=lambda h, p: True, wait=lambda h, p: True)
    assert not unit.path.exists()


def test_reinstall_stops_the_old_service_before_checking_the_port(tmp_path):
    run = FakeRun(systemd_answers())
    unit = service.Systemd("user", run=run, home=tmp_path)
    unit.write(plan(tmp_path))
    order = []
    run_stop = unit.stop
    unit.stop = lambda: (order.append("stop"), run_stop())
    service.install(
        unit, plan(tmp_path), in_use=lambda h, p: order.append("port") or False, wait=lambda h, p: True
    )
    assert order[0] == "stop" and set(order[1:]) == {"port"}


def test_reinstall_waits_for_the_old_server_to_let_go_of_the_port(tmp_path):
    unit = service.Systemd("user", run=FakeRun(systemd_answers()), home=tmp_path)
    unit.write(plan(tmp_path))
    busy = [True, True, False]
    in_use = lambda h, p: busy.pop(0) if len(busy) > 1 else busy[0]  # noqa: E731
    service.install(unit, plan(tmp_path), in_use=in_use, wait=lambda h, p: True)
    assert busy == [False]

    with pytest.raises(service.ServiceError, match="already listening"):
        service.install(
            unit, plan(tmp_path), in_use=lambda h, p: True, wait=lambda h, p: True, stop_timeout=0
        )


def test_install_reports_a_service_that_never_answers(tmp_path):
    unit = service.Systemd("user", run=FakeRun(systemd_answers()), home=tmp_path)
    with pytest.raises(service.ServiceError, match="isn't answering on port 8765.*journalctl --user"):
        service.install(unit, plan(tmp_path), in_use=lambda h, p: False, wait=lambda h, p: False)


def test_start_failure_points_at_the_logs(tmp_path):
    run = FakeRun({("systemctl", "--user", "restart"): (1, "Job failed.")})
    unit = service.Systemd("user", run=run, home=tmp_path)
    with pytest.raises(
        service.ServiceError, match="starting the service failed: Job failed. See `journalctl"
    ):
        service.install(unit, plan(tmp_path), in_use=lambda h, p: False, wait=lambda h, p: True)


def test_no_systemd_explains_what_to_do_instead(tmp_path):
    run = FakeRun(
        {("systemctl", "--user", "show-environment"): (1, "Failed to connect to bus: No medium found")}
    )
    with pytest.raises(service.ServiceError) as caught:
        service.Systemd("user", run=run, home=tmp_path).check()
    message = str(caught.value)
    assert "can't reach systemd (Failed to connect to bus: No medium found)" in message
    assert "not `su` or `sudo -u`" in message and "zerostore serve --http" in message


def test_uninstall_stops_disables_and_removes(tmp_path):
    run = FakeRun()
    unit = service.Systemd("user", run=run, home=tmp_path)
    assert service.uninstall(unit) is False
    unit.write(plan(tmp_path))
    assert service.uninstall(unit) is True
    assert not unit.path.exists()
    assert run.ran("systemctl", "--user", "disable", "--now", "zerostore.service")


def test_systemd_status(tmp_path):
    unit = service.Systemd("user", run=FakeRun(systemd_answers()), home=tmp_path)
    unit.write(plan(tmp_path))
    state = unit.status()
    assert (state.installed, state.running, state.pid, state.starts) == (True, True, 4321, "on boot")

    stopped = service.Systemd("user", run=FakeRun(systemd_answers(active="failed\n")), home=tmp_path).status()
    assert (stopped.running, stopped.pid, stopped.detail) == (False, None, "failed")


# --- launchd ---------------------------------------------------------------


def test_launchd_agent(tmp_path):
    run = FakeRun()
    agent = service.Launchd(uid=501, run=run, home=tmp_path)
    service.install(
        agent, plan(tmp_path, env={"ZEROSTORE_HOME": "/z"}), in_use=lambda h, p: False, wait=lambda h, p: True
    )
    data = plistlib.loads(agent.path.read_bytes())
    assert agent.path == tmp_path / "Library" / "LaunchAgents" / "zerostore.serve.plist"
    assert data["Label"] == "zerostore.serve"
    assert data["ProgramArguments"][:5] == [sys.executable, "-m", "zerostore", "serve", "--http"]
    assert data["RunAtLoad"] is True and data["KeepAlive"] == {"SuccessfulExit": False}
    log = tmp_path / "zs" / "logs" / "serve.log"
    assert data["StandardErrorPath"] == str(log) and log.parent.is_dir()
    assert data["EnvironmentVariables"] == {"PYTHONUNBUFFERED": "1", "ZEROSTORE_HOME": "/z"}
    assert run.calls == [
        ["launchctl", "bootout", "gui/501/zerostore.serve"],
        ["launchctl", "bootstrap", "gui/501", str(agent.path)],
        ["launchctl", "enable", "gui/501/zerostore.serve"],
    ]
    assert agent.logs == f"tail -f {log}"


def test_launchd_stop_waits_for_the_server_to_exit(tmp_path):
    gone = subprocess.Popen([sys.executable, "-c", "pass"])
    gone.wait()
    printed = f"state = running\npid = {gone.pid}\n"
    agent = service.Launchd(uid=501, run=FakeRun({("launchctl", "print"): (0, printed)}), home=tmp_path)
    agent.stop()  # already exited: returns at once
    assert agent.run.ran("launchctl", "bootout", "gui/501/zerostore.serve")

    printed = f"state = running\npid = {os.getpid()}\n"
    agent = service.Launchd(uid=501, run=FakeRun({("launchctl", "print"): (0, printed)}), home=tmp_path)
    agent.stop_timeout = 0.3
    started = time.monotonic()
    agent.stop()  # still alive: gives up after stop_timeout
    assert time.monotonic() - started >= 0.3


def test_launchd_needs_a_login_session(tmp_path):
    run = FakeRun({("launchctl", "bootstrap"): (125, "Domain does not support specified action")})
    agent = service.Launchd(uid=501, run=run, home=tmp_path)
    with pytest.raises(service.ServiceError, match="logged in at the Mac"):
        service.install(agent, plan(tmp_path), in_use=lambda h, p: False, wait=lambda h, p: True)


def test_launchd_status(tmp_path):
    printed = "gui/501/zerostore.serve = {\n\tactive count = 1\n\tstate = running\n\tpid = 812\n}\n"
    agent = service.Launchd(uid=501, run=FakeRun({("launchctl", "print"): (0, printed)}), home=tmp_path)
    agent.write(plan(tmp_path))
    state = agent.status()
    assert (state.installed, state.running, state.pid, state.starts) == (True, True, 812, "when you log in")
    gone = service.Launchd(uid=501, run=FakeRun({("launchctl", "print"): (113, "Could not find service")}))
    assert gone.status().running is False


# --- helpers ---------------------------------------------------------------


def test_detect_refuses_other_systems(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    with pytest.raises(service.ServiceError, match="Linux \\(systemd\\) and macOS \\(launchd\\), not win32"):
        service.detect()


def test_shell_only_variables(tmp_path):
    config = tmp_path / "zerostore.yaml"
    config.write_text(
        'sources:\n  github: { repos: [a/b], token: "${GITHUB_TOKEN}" }\n'
        "ai:\n  llm_api_key: ${ZEROSTORE_LLM_API_KEY}\n  jev_api_key: ${JEV_API_KEY}\n"
        "# ${IN_A_COMMENT}\n"
    )
    (tmp_path / "secrets.env").write_text("ZEROSTORE_LLM_API_KEY=sk-1\n")
    env = {"GITHUB_TOKEN": "x", "ZEROSTORE_LLM_API_KEY": "y", "IN_A_COMMENT": "z"}
    assert service.shell_only_variables(config, env) == ["GITHUB_TOKEN"]


def test_port_checks():
    import socket

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        assert service.port_in_use("localhost", port)
        assert service.wait_for_port("0.0.0.0", port, timeout=1)
    assert not service.wait_for_port("127.0.0.1", port, timeout=0.3, sleep=lambda s: None)


def test_empty_zerostore_home_means_the_default(monkeypatch):
    monkeypatch.setenv("ZEROSTORE_HOME", "")
    assert home_dir() == Path("~/.zerostore").expanduser()


# --- the CLI ---------------------------------------------------------------


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "zs-home"
    monkeypatch.setenv("ZEROSTORE_HOME", str(home))
    monkeypatch.delenv("ZEROSTORE_CONFIG", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(app, ["init", "--no-rule"]).exit_code == 0
    return home


@pytest.fixture
def fake_systemd(tmp_path, monkeypatch):
    unit = service.Systemd("user", run=FakeRun(systemd_answers()), home=tmp_path / "user")
    monkeypatch.setattr(service, "detect", lambda: unit)
    monkeypatch.setattr(service, "port_in_use", lambda h, p: False)
    monkeypatch.setattr(service, "wait_for_port", lambda h, p: True)
    return unit


def test_cli_install_status_uninstall(home, fake_systemd):
    result = runner.invoke(app, ["service", "install", "--port", "9100"])
    assert result.exit_code == 0, result.output
    assert "Started ZeroStore in the background (systemd user unit, starts on boot)." in result.output
    assert "Web UI:  http://localhost:9100" in result.output
    assert "Agents:  http://localhost:9100/mcp" in result.output
    text = fake_systemd.path.read_text()
    assert f"--config {home / 'zerostore.yaml'} --port 9100" in text
    assert "--host" not in text  # not given, so zerostore.yaml decides
    assert f"Environment=ZEROSTORE_HOME={home}" in text

    again = runner.invoke(app, ["service", "install"])
    assert "Updated and restarted ZeroStore" in again.output
    assert "--port" not in fake_systemd.path.read_text()

    status = runner.invoke(app, ["service", "status"])
    assert status.exit_code == 0, status.output
    assert "ZeroStore service: running (pid 4321)" in status.output
    assert "journalctl --user -u zerostore.service -f" in status.output

    removed = runner.invoke(app, ["service", "uninstall"])
    assert "Stopped and removed the ZeroStore service" in removed.output
    assert not fake_systemd.path.exists()
    assert runner.invoke(app, ["service", "uninstall"]).output.startswith(
        "The ZeroStore service isn't installed."
    )


def test_cli_status_when_not_installed(home, fake_systemd):
    fake_systemd.run.answers[("systemctl", "--user", "is-active")] = (3, "inactive\n")
    result = runner.invoke(app, ["service", "status"])
    assert result.exit_code == 1
    assert "isn't installed. Set it up with `zerostore service install`" in result.output


def test_cli_install_refuses_plain_http_on_the_network(home, fake_systemd):
    result = runner.invoke(app, ["service", "install", "--host", "0.0.0.0"])
    assert result.exit_code == 1
    assert "refusing to serve plain HTTP on 0.0.0.0" in result.output
    assert not fake_systemd.path.exists()

    proxied = runner.invoke(
        app,
        [
            "service",
            "install",
            "--host",
            "0.0.0.0",
            "--behind-https-proxy",
            "--public-url",
            "https://zs.example.com",
        ],
    )
    assert proxied.exit_code == 0, proxied.output
    assert "Agents:  https://zs.example.com/mcp" in proxied.output
    assert (
        "--host 0.0.0.0 --behind-https-proxy --public-url https://zs.example.com"
        in fake_systemd.path.read_text()
    )


def test_cli_install_warns_about_shell_only_keys(home, fake_systemd, monkeypatch):
    config = home / "zerostore.yaml"
    config.write_text(
        config.read_text().replace("sources: {}", 'sources:\n  github: { repos: [a/b], token: "${GH_PAT}" }')
    )
    monkeypatch.setenv("GH_PAT", "ghp_x")
    result = runner.invoke(app, ["service", "install"])
    assert result.exit_code == 0, result.output
    assert "uses GH_PAT from this shell, which the service won't see" in result.output


def test_cli_explains_a_missing_service_manager(home, monkeypatch):
    run = FakeRun({("systemctl", "--user", "show-environment"): (1, "Failed to connect to bus")})
    monkeypatch.setattr(service, "detect", lambda: service.Systemd("user", run=run, home=home))
    result = runner.invoke(app, ["service", "install"])
    assert result.exit_code == 1
    assert "can't reach systemd" in result.output


def test_python_dash_m_zerostore_runs_the_cli():
    result = subprocess.run([sys.executable, "-m", "zerostore", "--version"], capture_output=True, text=True)
    assert result.returncode == 0 and result.stdout.startswith("zerostore ")


# --- install.sh --------------------------------------------------------------

needs_sh = pytest.mark.skipif(shutil.which("sh") is None, reason="needs a POSIX shell")

FAKE_UV = """#!/bin/sh
echo "uv $*" >> "$LOG"
case "$1 $2" in
  "tool install")
    mkdir -p "$TOOLS"
    cat > "$TOOLS/zerostore" <<'EOS'
#!/bin/sh
echo "zerostore $*" >> "$LOG"
if [ "$1 $2" = "service install" ]; then exit "${FAKE_SERVICE_RC:-0}"; fi
exit 0
EOS
    chmod +x "$TOOLS/zerostore" ;;
  "tool dir") echo "$TOOLS" ;;
esac
"""


def run_installer(tmp_path, *args: str, **env) -> tuple[subprocess.CompletedProcess, list[str]]:
    fakes = tmp_path / "fakes"
    fakes.mkdir(exist_ok=True)
    (fakes / "uv").write_text(FAKE_UV)
    (fakes / "uv").chmod(0o755)
    log = tmp_path / "calls.log"
    log.write_text("")
    full_env = {
        "PATH": f"{fakes}:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "LOG": str(log),
        "TOOLS": str(tmp_path / "tools"),
        "ZEROSTORE_PACKAGE": "/src/zerostore",
        **env,
    }
    result = subprocess.run(
        ["sh", str(ROOT / "install.sh"), *args], capture_output=True, text=True, env=full_env
    )
    return result, log.read_text().splitlines()


@needs_sh
def test_installer_installs_sets_up_starts_and_opens(tmp_path):
    result, calls = run_installer(tmp_path, ZEROSTORE_NO_OPEN="1")
    assert result.returncode == 0, result.stdout + result.stderr
    assert calls[0] == "uv tool install --force --reinstall-package zerostore --python >=3.10 /src/zerostore"
    assert [c for c in calls if c.startswith("zerostore")] == [
        "zerostore --version",
        "zerostore init --no-rule",
        "zerostore service install",
        "zerostore ui --no-open",
    ]
    assert "uv tool update-shell" in calls  # the tools folder isn't on PATH yet
    assert f'export PATH="{tmp_path / "tools"}:$PATH"' in result.stdout


@needs_sh
def test_installer_carries_on_when_the_service_cant_start(tmp_path):
    result, calls = run_installer(tmp_path, FAKE_SERVICE_RC="1")
    assert result.returncode == 0, result.stderr
    assert "zerostore service install" in calls
    assert not any(c.startswith("zerostore ui") for c in calls)
    assert "zerostore serve --http --open" in result.stdout


@needs_sh
def test_installer_can_skip_the_service(tmp_path):
    result, calls = run_installer(tmp_path, ZEROSTORE_NO_SERVICE="1")
    assert result.returncode == 0
    assert not any(c.startswith("zerostore service") or c.startswith("zerostore ui") for c in calls)


@needs_sh
def test_installer_refuses_other_systems(tmp_path):
    fakes = tmp_path / "fakes"
    fakes.mkdir()
    (fakes / "uname").write_text("#!/bin/sh\necho FreeBSD\n")
    (fakes / "uname").chmod(0o755)
    result, calls = run_installer(tmp_path)
    assert result.returncode == 1
    assert "this installer is for Linux and macOS" in result.stderr
    assert calls == []


# --- install.sh --domain: HTTPS with Caddy and Let's Encrypt, system tools faked ---

LOGGED = '#!/bin/sh\necho "{name} $*" >> "$LOG"\n'
DOMAIN_FAKES = {
    "id": "#!/bin/sh\necho 1000\n",  # not root, so every system change goes through sudo
    "sudo": '#!/bin/sh\necho "sudo $*" >> "$LOG"\nexec "$@"\n',
    "getent": '#!/bin/sh\n[ -n "$FAKE_DNS" ] && echo "$FAKE_DNS STREAM $2"\nexit 0\n',
    "curl": '#!/bin/sh\necho "curl $*" >> "$LOG"\ncase "$*" in *checkip*) echo 203.0.113.7 ;; esac\n',
    "systemctl": LOGGED.format(name="systemctl"),
    "gpg": LOGGED.format(name="gpg"),
    # Writes only inside the test's home; elsewhere (/etc/apt) it just records the call.
    "tee": '#!/bin/sh\necho "tee $*" >> "$LOG"\nfor last; do :; done\n'
    'case "$last" in "$HOME"/*) exec /usr/bin/tee "$@" ;; *) cat > /dev/null ;; esac\n',
}


def domain_fakes(tmp_path, *, caddy: bool) -> dict[str, str]:
    fakes = tmp_path / "fakes"
    fakes.mkdir(exist_ok=True)
    tools = dict(DOMAIN_FAKES)
    # `caddy version` answers like Caddy 2.11, the first release that gets IP address certificates.
    caddy_fake = LOGGED.format(name="caddy") + (
        '[ "$1" = version ] && echo "${FAKE_CADDY_VERSION:-v2.11.4} h1:x"\nexit 0\n'
    )
    if caddy:
        tools["caddy"] = caddy_fake
    else:  # apt-get installs it
        tools["apt-get"] = (
            LOGGED.format(name="apt-get")
            + f'if [ "$*" = "install -y caddy" ]; then printf \'%s\' \'{caddy_fake}\' > "{fakes}/caddy";'
            f' chmod +x "{fakes}/caddy"; fi\n'
        )
    for name, text in tools.items():
        (fakes / name).write_text(text)
        (fakes / name).chmod(0o755)
    caddyfile = tmp_path / "Caddyfile"
    caddyfile.write_text(":80 {\n\troot * /usr/share/caddy\n\tfile_server\n}\n")
    return {"ZEROSTORE_CADDYFILE": str(caddyfile), "ZEROSTORE_NO_OPEN": "1", "FAKE_DNS": "203.0.113.7"}


@needs_sh
def test_installer_serves_a_domain_over_https_with_caddy(tmp_path):
    env = domain_fakes(tmp_path, caddy=True)
    result, calls = run_installer(tmp_path, "--domain", "zs.example.com", **env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "zs.example.com points at this server (203.0.113.7)." in result.stdout
    caddyfile = (tmp_path / "Caddyfile").read_text()
    assert "zs.example.com {\n\treverse_proxy localhost:8765\n}" in caddyfile
    assert caddyfile.startswith(":80 {")  # the existing site is kept
    assert "sudo systemctl reload-or-restart caddy" in calls
    assert not any(c.startswith("apt-get") for c in calls)  # Caddy was already here
    zs = [c for c in calls if c.startswith("zerostore")]
    assert zs[2] == (
        "zerostore service install --port 8765 --behind-https-proxy --public-url https://zs.example.com"
    )
    assert "zerostore doctor --url https://zs.example.com" in zs
    assert "Agents add ZeroStore with https://zs.example.com/mcp" in result.stdout

    # Running it again doesn't add the site twice.
    again, _ = run_installer(tmp_path, "--domain=zs.example.com", **env)
    assert again.returncode == 0, again.stderr
    assert (tmp_path / "Caddyfile").read_text().count("zs.example.com {") == 1
    assert "Caddy already serves zs.example.com" in again.stdout


@needs_sh
def test_installer_installs_caddy_from_its_apt_repository(tmp_path):
    env = domain_fakes(tmp_path, caddy=False)
    result, calls = run_installer(tmp_path, "--domain", "zs.example.com", **env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "sudo apt-get install -y caddy" in calls
    assert any("dl.cloudsmith.io/public/caddy/stable/debian.deb.txt" in c for c in calls)
    assert "sudo tee /etc/apt/sources.list.d/caddy-stable.list" in calls
    installed, reloaded = "sudo apt-get install -y caddy", "sudo systemctl reload-or-restart caddy"
    assert calls.index(installed) < calls.index(reloaded)


@needs_sh
def test_installer_stops_when_the_domain_has_no_dns_record(tmp_path):
    env = {**domain_fakes(tmp_path, caddy=True), "FAKE_DNS": ""}
    result, calls = run_installer(tmp_path, "--domain", "zs.example.com", **env)
    assert result.returncode == 1
    assert "zs.example.com has no DNS record yet. Add an A record" in result.stderr
    assert not any(c.startswith("zerostore service") or c.startswith("caddy") for c in calls)


@needs_sh
def test_installer_notes_a_domain_pointing_elsewhere(tmp_path):
    env = {**domain_fakes(tmp_path, caddy=True), "FAKE_DNS": "198.51.100.9"}
    result, _ = run_installer(tmp_path, "--domain", "zs.example.com", **env)
    assert result.returncode == 0, result.stderr
    assert "points at 198.51.100.9" in result.stdout and "public address is 203.0.113.7" in result.stdout


@needs_sh
@pytest.mark.parametrize("bad", ["https://zs.example.com", "zs.example.com:443", "zs.example.com/mcp"])
def test_installer_wants_just_the_domain_name(tmp_path, bad):
    result, calls = run_installer(tmp_path, "--domain", bad)
    assert result.returncode == 1 and "just the name" in result.stderr
    assert calls == []


@needs_sh
def test_installer_serves_the_servers_ip_address_over_https(tmp_path):
    env = domain_fakes(tmp_path, caddy=True)
    result, calls = run_installer(tmp_path, "--ip", **env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "This server's public IP address is 203.0.113.7." in result.stdout
    caddyfile = (tmp_path / "Caddyfile").read_text()
    assert (
        "https://203.0.113.7 {\n\ttls {\n\t\tissuer acme {\n\t\t\tprofile shortlived\n\t\t}\n\t}\n"
        "\treverse_proxy localhost:8765\n}"
    ) in caddyfile
    assert caddyfile.startswith(":80 {")
    assert "sudo systemctl reload-or-restart caddy" in calls
    zs = [c for c in calls if c.startswith("zerostore")]
    assert (
        zs[2] == "zerostore service install --port 8765 --behind-https-proxy --public-url https://203.0.113.7"
    )
    assert "zerostore doctor --url https://203.0.113.7" in zs
    assert "Agents add ZeroStore with https://203.0.113.7/mcp" in result.stdout

    # Running it again, with the address given, doesn't add the site twice.
    again, _ = run_installer(tmp_path, "--ip=203.0.113.7", **env)
    assert again.returncode == 0, again.stderr
    assert (tmp_path / "Caddyfile").read_text().count("https://203.0.113.7 {") == 1
    assert "Caddy already serves 203.0.113.7" in again.stdout


@needs_sh
def test_installer_notes_an_ip_that_isnt_this_servers(tmp_path):
    env = domain_fakes(tmp_path, caddy=True)
    result, _ = run_installer(tmp_path, "--ip=198.51.100.9", **env)
    assert result.returncode == 0, result.stderr
    assert "this server's public address is 203.0.113.7, not 198.51.100.9" in result.stdout
    assert "https://198.51.100.9 {" in (tmp_path / "Caddyfile").read_text()


@needs_sh
def test_installer_needs_a_caddy_that_gets_ip_certificates(tmp_path):
    env = {**domain_fakes(tmp_path, caddy=True), "FAKE_CADDY_VERSION": "v2.10.2"}
    result, calls = run_installer(tmp_path, "--ip", **env)
    assert result.returncode == 1
    assert "Caddy v2.10.2 can't get a certificate for an IP address; 2.11 or newer can" in result.stderr
    assert not any(c.startswith("zerostore service") for c in calls)
    assert "203.0.113.7" not in (tmp_path / "Caddyfile").read_text()


@needs_sh
def test_installer_refuses_a_private_ip(tmp_path):
    env = domain_fakes(tmp_path, caddy=True)
    result, calls = run_installer(tmp_path, "--ip=172.31.5.10", **env)
    assert result.returncode == 1 and "172.31.5.10 is a private address" in result.stderr
    assert not any(c.startswith("zerostore service") or c.startswith("caddy") for c in calls)


@needs_sh
@pytest.mark.parametrize(
    ("args", "says"),
    [
        (["--ip=abc"], "IPv4 address"),
        (["--ip=203.0.113"], "IPv4 address"),
        (["--ip=203.0.113.7.1"], "IPv4 address"),
        (["--ip=203..113.7"], "IPv4 address"),
        (["--domain", "zs.example.com", "--ip"], "not both"),
    ],
)
def test_installer_checks_the_ip_option(tmp_path, args, says):
    result, calls = run_installer(tmp_path, *args)
    assert result.returncode == 1 and says in result.stderr
    assert calls == []


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck isn't installed")
def test_installer_passes_shellcheck():
    subprocess.run(["shellcheck", "-s", "sh", str(ROOT / "install.sh")], check=True)


def test_installer_is_executable():
    assert os.access(ROOT / "install.sh", os.X_OK)
