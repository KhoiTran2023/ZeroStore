from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from zerostore.cli import app
from zerostore.mcp.descriptions import AGENT_RULE

runner = CliRunner()


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "zs-home"
    monkeypatch.setenv("ZEROSTORE_HOME", str(home))
    monkeypatch.delenv("ZEROSTORE_CONFIG", raising=False)
    monkeypatch.chdir(tmp_path)
    return home


def zs(*args, **kwargs):
    result = runner.invoke(app, list(args), **kwargs)
    return result


def test_version():
    assert "zerostore 0.1.0" in zs("--version").output


def test_init_creates_settings_and_private_owner_token(home, tmp_path):
    result = zs("init", "--no-rule")
    assert result.exit_code == 0, result.output
    config = home / "zerostore.yaml"
    token_file = home / "owner-token"
    assert config.exists() and token_file.exists()
    assert token_file.stat().st_mode & 0o777 == 0o600
    assert config.stat().st_mode & 0o777 == 0o600
    assert home.stat().st_mode & 0o777 == 0o700
    assert token_file.read_text().startswith("zs_")
    assert not (tmp_path / "CLAUDE.md").exists()


def test_init_twice_keeps_one_owner_token(home):
    zs("init", "--no-rule")
    first = (home / "owner-token").read_text()
    result = zs("init", "--no-rule")
    assert "already saved" in result.output
    assert (home / "owner-token").read_text() == first
    listed = zs("token", "list").output
    assert listed.count("owner") == 2  # one row: level + name columns


def test_init_with_rule_writes_the_line(home, tmp_path):
    (tmp_path / "AGENTS.md").write_text("# Notes\n")
    zs("init", "--rule")
    assert AGENT_RULE in (tmp_path / "AGENTS.md").read_text()
    assert not (tmp_path / "CLAUDE.md").exists()  # only files that already exist get it


def test_token_lifecycle(home):
    zs("init", "--no-rule")
    created = zs("token", "create", "--name", "cursor-laptop", "--level", "default")
    assert created.exit_code == 0, created.output
    raw = created.output.strip().splitlines()[-1]
    assert raw.startswith("zs_")
    token_id = created.output.split()[2]
    listing = zs("token", "list").output
    assert "cursor-laptop" in listing and raw not in listing
    assert zs("token", "revoke", token_id).exit_code == 0
    assert "cursor-laptop" not in zs("token", "list").output
    assert "revoked" in zs("token", "list", "--all").output
    assert zs("token", "revoke", token_id).exit_code == 1


def test_bad_level_is_rejected(home):
    zs("init", "--no-rule")
    assert zs("token", "create", "--name", "x", "--level", "admin").exit_code != 0


def test_status_json(home):
    zs("init", "--no-rule")
    info = json.loads(zs("status", "--json").output)
    assert info["documents"] == 0 and info["active_tokens"] == 1 and info["sources_configured"] == []


def test_status_counts_approved_decisions_as_a_source(home):
    zs("init", "--no-rule")
    (home / "decisions.md").write_text("# Approved decisions\n\n## Refunds\n\nFull refund within 30 days.\n")
    assert json.loads(zs("status", "--json").output)["sources_configured"] == ["decisions"]
    assert "Sources:          decisions" in zs("status").output


def test_commands_without_init_explain_what_to_do(home):
    result = zs("status")
    assert result.exit_code == 1
    assert "zerostore init" in result.output


def test_connect_and_undo(home, tmp_path):
    assert zs("connect", "cursor", "--yes").exit_code == 0
    assert AGENT_RULE in (tmp_path / ".cursorrules").read_text()
    assert "already has" in zs("connect", "cursor", "--yes").output
    assert zs("connect", "cursor", "--undo").exit_code == 0
    assert not (tmp_path / ".cursorrules").exists()


def test_connect_asks_first(home, tmp_path):
    result = zs("connect", "claude", input="n\n")
    assert AGENT_RULE in result.output
    assert not (tmp_path / "CLAUDE.md").exists()


def test_connect_unknown_agent(home):
    assert zs("connect", "clippy").exit_code == 1


def test_serve_refuses_plain_http_off_localhost(home):
    zs("init", "--no-rule")
    result = zs("serve", "--http", "--host", "0.0.0.0")
    assert result.exit_code == 1
    assert "HTTPS" in result.output


def test_serve_stdio_needs_a_valid_token(home, monkeypatch):
    zs("init", "--no-rule")
    monkeypatch.setenv("ZEROSTORE_TOKEN", "zs_wrong")
    result = zs("serve")
    assert result.exit_code == 1
    assert "ZEROSTORE_TOKEN" in result.output


def setup_review_items(home):
    """Init, then queue one change and one proposal straight through the library."""
    from fakes import FakeConnector, FakeEngine

    from zerostore.app import ZeroStore
    from zerostore.config import GoogleDriveSource, load_config
    from zerostore.librarian.ingestion import Ingestor
    from zerostore.store.embeddings import HashingEmbedder

    zs("init", "--no-rule")
    store = ZeroStore.open(load_config(home / "zerostore.yaml"), embedder=HashingEmbedder())
    store.config.sources.google_drive = GoogleDriveSource(folders=["Company"])
    drive, engine = FakeConnector(), FakeEngine()
    engine.keep_default = 0.6
    drive.put("pricing", "Pricing 2026", "The Pro plan costs $29 per month.")
    Ingestor(store, lambda n, c: drive, judge=engine).sync()
    store.state.add_review_item(
        "agent_proposal", {"fact": "Refunds: 30 days.", "quote": "q"}, created_by="cursor"
    )
    store.close()


def test_review_list_approve_and_reject(home, monkeypatch):
    monkeypatch.setattr("importlib.util.find_spec", lambda name: None)
    setup_review_items(home)
    listed = zs("review")
    assert listed.exit_code == 0, listed.output
    assert "[1] Added: Pricing 2026 (google_drive) · confidence 0.60" in listed.output
    assert "[2] Proposed fact: Refunds: 30 days." in listed.output
    approved = zs("review", "approve", "2", "--fact", "Refunds are allowed within 30 days.")
    assert approved.exit_code == 0, approved.output
    assert "Approved 2: saved; agents can find it now." in approved.output
    assert "within 30 days" in (home / "decisions.md").read_text()
    rejected = zs("review", "reject", "1")
    assert "Rejected 1: kept the current version of Pricing 2026." in rejected.output
    assert "Nothing is waiting for review." in zs("review").output
    again = zs("review", "approve", "1")
    assert again.exit_code == 1 and "no pending review item" in again.output


def test_status_names_the_decision_engine(home, monkeypatch):
    monkeypatch.setattr("importlib.util.find_spec", lambda name: None)
    zs("init", "--no-rule")
    out = json.loads(zs("status", "--json").output)
    assert out["decision_engine"].startswith("none") and out["embedder"] == "hashing-256"


def test_sync_and_reindex_read_decisions_md(home, monkeypatch):
    monkeypatch.setattr("importlib.util.find_spec", lambda name: None)
    zs("init", "--no-rule")
    assert "no sources are set up yet" in zs("sync").output
    from zerostore.connectors.decisions import append_decision

    append_decision(home / "decisions.md", "The Pro plan costs $29 per month.", "khoi")
    synced = zs("sync")
    assert synced.exit_code == 0, synced.output
    assert "+ Approved decisions  (decisions, added)" in synced.output
    rebuilt = zs("reindex", "--yes")
    assert rebuilt.exit_code == 0, rebuilt.output
    assert "Rebuilding with the hashing-256 embedder" in rebuilt.output
    assert "1 change · Approved decisions" in rebuilt.output


def test_reindex_waits_for_a_running_sync_and_keeps_rejections(home, monkeypatch):
    monkeypatch.setattr("importlib.util.find_spec", lambda name: None)
    from zerostore.config import load_config
    from zerostore.connectors.decisions import append_decision
    from zerostore.store.state import StateDB

    zs("init", "--no-rule")
    append_decision(home / "decisions.md", "The Pro plan costs $29 per month.", "khoi")
    zs("sync")
    state = StateDB(load_config(home / "zerostore.yaml").servers.statedb_path())
    assert state.acquire_lock("sync", "someone-else", 60)
    busy = zs("reindex", "--yes")
    assert busy.exit_code == 1 and "sync is running" in busy.output
    assert json.loads(zs("status", "--json").output)["chunks"] > 0  # nothing was emptied
    state.release_lock("sync", "someone-else")
    state.reject_version("decisions:decisions.md", "some-rejected-version")
    done = zs("reindex", "--yes")
    assert done.exit_code == 0, done.output
    assert state.documents()["decisions:decisions.md"]["rejected_fingerprint"] is None  # applied fresh
