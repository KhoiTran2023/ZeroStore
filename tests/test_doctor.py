"""`zerostore doctor`: what an agent needs before ZeroStore is added to it."""

from __future__ import annotations

import datetime
import json

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from tests.servers import running
from zerostore.cli import app
from zerostore.config import parse_config
from zerostore.doctor import FAIL, OK, WARN, run
from zerostore.mcp.http import create_http_app
from zerostore.services import Here, beat


def found(checks, group=None) -> list[tuple[str, str]]:
    return [(c.level, c.says) for c in checks if group in (None, c.group)]


def levels(checks, group) -> list[str]:
    return [c.level for c in checks if c.group == group]


def problems(checks):
    """Failures in reaching the server; no heartbeat is written here, so services always fail."""
    return [c for c in checks if c.level == FAIL and c.group in ("address", "sign-in")]


def serving(demo, base: str) -> None:
    """Pretend `zerostore serve --http` runs at `base`, as its heartbeat says."""
    _, zs = demo
    beat(zs, Here(web=True, mcp_url=f"{base}/mcp", scheduler=lambda: True), base)


# Settings and services ------------------------------------------------------------


def test_a_stopped_server_is_one_problem_with_one_fix(demo):
    d, zs = demo
    checks = run(zs, d.config_path)
    stopped = [c for c in checks if c.group == "services" and c.level == FAIL]
    assert len(stopped) == 1 and "service install" in stopped[0].fix
    assert levels(checks, "address") == [WARN]  # not checked while stopped, and said so


def test_missing_keys_are_named_with_the_fix(demo, tmp_path):
    _, zs = demo
    zs.config = parse_config(
        {
            "sources": {
                "google_drive": {"folders": ["Company"], "credentials": str(tmp_path / "nope.json")},
                "github": {"repos": ["acme/app"]},
                "linear": {"teams": ["Product"]},
            },
            "web_ui": False,
        }
    )
    checks = {c.says.split(" ")[0]: c for c in run(zs) if c.group == "settings"}
    assert checks["Google"].level == FAIL and "Sign in with Google" in checks["Google"].fix
    assert checks["GitHub"].level == WARN and "token" in checks["GitHub"].fix
    assert checks["Linear"].level == FAIL
    assert checks["the"].level == WARN and "web_ui" in checks["the"].fix


def test_tags_no_role_can_see_are_pointed_out(demo):
    _, zs = demo
    zs.config = parse_config(
        {
            "sources": {},
            "tags": {"people": [{"github": "x/a"}], "money": [{"github": "x/b"}]},
            "roles": {"hr": ["people"]},
        }
    )
    zs.tokens.create("hr-bot", "default", role="hr")
    access = found(run(zs), "access")
    assert (OK, "role hr sees people; 1 token(s) have it") in access
    assert (WARN, "no role sees money, so only owner tokens do") in access


# The address agents use -----------------------------------------------------------


def test_a_local_server_is_fine_for_local_agents_only(demo):
    d, zs = demo
    with running(build=lambda url: create_http_app(zs, config_path=d.config_path, mcp_url=url)) as url:
        base = url.removesuffix("/mcp")
        serving(demo, base)
        checks = run(zs, d.config_path)
    address = found(checks, "address")
    assert address[0][0] == WARN and "only for agents on this computer" in address[0][1]
    assert (OK, f"{base}/mcp answers and asks every caller for a token") in address
    assert levels(checks, "sign-in") == [OK]
    assert FAIL not in {c.level for c in checks}


def test_a_public_https_address_passes(demo):
    d, zs = demo
    base = "https://zs.example.com"
    web = TestClient(create_http_app(zs, config_path=d.config_path, mcp_url=f"{base}/mcp"), base_url=base)
    checks = run(zs, d.config_path, url=base, client=web)
    assert found(checks, "address")[0] == (OK, f"agents use {base}/mcp over HTTPS")
    assert found(checks, "sign-in") == [
        (OK, "Claude, ChatGPT and Grok can sign in here; you allow each one on the web UI")
    ]


def test_a_public_name_the_server_wasnt_started_with_is_caught(demo):
    d, zs = demo
    # Started for one public name, reached by another.
    app_ = create_http_app(zs, config_path=d.config_path, mcp_url="https://zs.example.com/mcp")
    other = "https://other.example.com"
    checks = run(zs, url=other, client=TestClient(app_, base_url=other))
    [problem] = problems(checks)
    assert "calls itself https://zs.example.com" in problem.says
    assert problem.fix == f"restart with --public-url {other}"


def test_a_server_started_without_its_public_name_is_caught(demo):
    d, zs = demo
    # A proxy forwards https://zs.example.com to a server that thinks it's on localhost.
    app_ = create_http_app(zs, config_path=d.config_path, mcp_url="http://localhost:8765/mcp")
    base = "https://zs.example.com"
    checks = run(zs, url=base, client=TestClient(app_, base_url=base))
    [problem] = problems(checks)
    assert problem.group == "sign-in" and "localhost" in problem.says
    assert problem.fix == f"restart with --public-url {base}"


def test_plain_http_on_a_network_address_fails(demo):
    _, zs = demo
    [first] = [c for c in run(zs, url="http://10.0.0.5:8765") if c.group == "address"]
    assert first.level == FAIL and "--behind-https-proxy" in first.fix


@pytest.fixture
def self_signed(tmp_path):
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    cert_file, key_file = tmp_path / "cert.pem", tmp_path / "key.pem"
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        )
    )
    return str(cert_file), str(key_file)


def test_a_self_signed_certificate_is_the_root_crt_problem(demo, self_signed):
    d, zs = demo
    build = lambda url: create_http_app(zs, config_path=d.config_path, mcp_url=url)  # noqa: E731
    with running(build=build, tls=self_signed) as url:
        checks = run(zs, url=url.removesuffix("/mcp"), client=httpx.Client(timeout=5))
    [problem] = problems(checks)
    assert "root.crt" in problem.says and "public certificate" in problem.fix


# The command ----------------------------------------------------------------------


def test_the_command_exits_1_on_a_problem_and_prints_json(demo):
    d, zs = demo
    runner = CliRunner()
    shown = runner.invoke(app, ["doctor", "-c", str(d.config_path)])
    assert shown.exit_code == 1, shown.output
    assert "✗ the server isn't running" in shown.output and "→ zerostore service install" in shown.output
    assert "1 problem to fix first." in shown.output
    as_json = runner.invoke(app, ["doctor", "--json", "-c", str(d.config_path)])
    rows = json.loads(as_json.output)
    assert {"group", "level", "says", "fix"} <= set(rows[0])
