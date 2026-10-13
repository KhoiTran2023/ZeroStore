"""The 30-second demo path: a source changes, `zerostore sync` runs, agents see the new answer."""

from __future__ import annotations

import anyio
import pytest
import yaml
from fakes import FakeConnector
from mcp import Client
from typer.testing import CliRunner

from zerostore.app import ZeroStore
from zerostore.cli import app
from zerostore.config import load_config
from zerostore.mcp.server import build_server

runner = CliRunner()


@pytest.fixture
def setup(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("ZEROSTORE_HOME", str(home))
    monkeypatch.delenv("ZEROSTORE_CONFIG", raising=False)
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(app, ["init", "--no-rule"]).exit_code == 0
    config_path = home / "zerostore.yaml"
    data = yaml.safe_load(config_path.read_text())
    data["sources"] = {"google_drive": {"folders": ["Company"]}}
    config_path.write_text(yaml.safe_dump(data))

    drive = FakeConnector("google_drive")
    drive.put("pricing", "Pricing 2026", "The Pro plan costs $20 per month.")
    monkeypatch.setattr("zerostore.librarian.ingestion.make_connector", lambda name, config: drive)
    return config_path, drive


def ask(config_path, level="default"):
    zs = ZeroStore.open(load_config(config_path), quiet=True)
    raw, _ = zs.tokens.create("agent", level)
    server = build_server(zs, lambda ctx: raw)

    async def go():
        async with Client(server) as client:
            return await client.call_tool("search", {"query": "What does the Pro plan cost?"})

    result = anyio.run(go)
    zs.close()
    return result.structured_content["facts"][0]["text"]


def test_ceo_changes_the_price(setup):
    config_path, drive = setup
    first = runner.invoke(app, ["sync"])
    assert first.exit_code == 0, first.output
    assert "1 change · Pricing 2026" in first.output
    assert "$20" in ask(config_path)

    drive.put("pricing", "Pricing 2026", "The Pro plan costs $29 per month.", version="v2")
    second = runner.invoke(app, ["sync"])
    assert "~ Pricing 2026  (google_drive, updated)" in second.output
    assert "$29" in ask(config_path)

    status = runner.invoke(app, ["status"])
    assert "Last sync:" in status.output and "1 change · Pricing 2026" in status.output


def test_sync_reports_failures_with_exit_code(setup):
    _, drive = setup
    drive.fail_listing = True
    result = runner.invoke(app, ["sync"])
    assert result.exit_code == 1
    assert "source unreachable" in result.output


def test_sync_without_sources(tmp_path, monkeypatch):
    monkeypatch.setenv("ZEROSTORE_HOME", str(tmp_path / "h"))
    monkeypatch.chdir(tmp_path)
    runner.invoke(app, ["init", "--no-rule"])
    result = runner.invoke(app, ["sync"])
    assert result.exit_code == 1 and "no sources" in result.output


def test_owner_can_sync_over_mcp(setup):
    config_path, drive = setup
    zs = ZeroStore.open(load_config(config_path), quiet=True)
    owner, _ = zs.tokens.create("me", "owner")
    agent, _ = zs.tokens.create("agent", "default")

    async def call(token):
        async with Client(build_server(zs, lambda ctx: token)) as client:
            return await client.call_tool("sync_now", {})

    denied = anyio.run(call, agent)
    assert denied.is_error
    done = anyio.run(call, owner)
    assert not done.is_error, done.content
    assert done.structured_content["summary"] == "1 change · Pricing 2026"
    assert done.structured_content["changes"] == [
        {"action": "added", "document": "Pricing 2026", "source": "google_drive"}
    ]
    zs.close()
