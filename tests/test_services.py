"""The four services: web console, MCP server, vector DB and ingestion agent, and whether each is up."""

from __future__ import annotations

import json
import re

from fastapi.testclient import TestClient
from typer.testing import CliRunner

from zerostore import services
from zerostore.cli import app
from zerostore.services import HEARTBEAT_KEY, Heartbeat, Here, beat, check
from zerostore.web.app import create_web_app


def states(rows) -> dict[str, str]:
    return {r["name"]: r["state"] for r in rows}


def test_nothing_runs_until_the_server_starts(demo):
    _, zs = demo
    rows = check(zs)
    assert states(rows) == {
        "web console": "stopped",
        "mcp server": "stopped",
        "vector db": "running",  # read directly, with no server needed
        "ingestion agent": "stopped",
    }
    assert re.match(r"\d+ facts · answered in \d+ ms", rows[2]["detail"])


def test_the_heartbeat_tells_another_process_what_is_up(demo):
    _, zs = demo
    here = Here(web=True, mcp_url="http://127.0.0.1:8765/mcp", scheduler=lambda: True)
    beat(zs, here, "http://127.0.0.1:8765", now=1000.0)
    rows = {r["name"]: r for r in check(zs, now=1010.0)}
    assert {n: r["state"] for n, r in rows.items()} == dict.fromkeys(rows, "running")
    assert rows["web console"]["address"] == "http://127.0.0.1:8765"
    assert rows["mcp server"]["address"] == "http://127.0.0.1:8765/mcp"
    assert re.search(r"last sync \d\d-\d\d \d\d:\d\d ok", rows["ingestion agent"]["detail"])
    # Three missed beats and the server counts as stopped.
    assert states(check(zs, now=1000.0 + 4 * services.BEAT_SECONDS))["mcp server"] == "stopped"


def test_no_schedule_and_a_dead_sync_thread_show_differently(demo):
    _, zs = demo
    beat(zs, Here(web=True, mcp_url="http://x/mcp", scheduler=None), "http://x", now=1000.0)
    assert states(check(zs, now=1001.0))["ingestion agent"] == "off"
    beat(zs, Here(web=True, mcp_url="http://x/mcp", scheduler=lambda: False), "http://x", now=1000.0)
    row = check(zs, now=1001.0)[3]
    assert row["state"] == "error" and "restart the server" in row["detail"]


def test_a_broken_vector_db_is_an_error(demo, monkeypatch):
    _, zs = demo

    def broken():
        raise OSError("disk gone")

    monkeypatch.setattr(zs.facts, "count", broken)
    row = check(zs)[2]
    assert row["state"] == "error" and "disk gone" in row["detail"]


def test_stopping_the_heartbeat_marks_the_server_stopped(demo):
    _, zs = demo
    heartbeat = Heartbeat(zs, Here(web=True, mcp_url="http://x/mcp", scheduler=None), "http://x", every=0.05)
    heartbeat.start()
    try:
        assert states(check(zs))["mcp server"] == "running"
    finally:
        heartbeat.stop()
    assert json.loads(zs.state.get_meta(HEARTBEAT_KEY))["stopped"]
    assert states(check(zs))["mcp server"] == "stopped"


def test_the_console_shows_each_service(demo):
    d, zs = demo
    web = TestClient(
        create_web_app(zs, d.config_path, mcp_url="http://localhost/mcp", scheduler_alive=lambda: True),
        base_url="http://localhost",
    )
    web.post("/login", data={"token": d.owner_token()})
    page = web.get("/settings").text
    for name in ("web console", "mcp server", "vector db", "ingestion agent", "state db"):
        assert f"<td>{name}</td>" in page
    assert page.count("● running") == 5
    status = web.get("/status").text
    assert "● ingestion agent" in status and "● vector db" in status


def test_zerostore_status_lists_the_services(demo):
    d, zs = demo
    shown = CliRunner().invoke(app, ["status", "-c", str(d.config_path)])
    assert shown.exit_code == 0, shown.output
    assert "Services" in shown.output and re.search(r"▲ mcp server\s+stopped", shown.output)
    as_json = CliRunner().invoke(app, ["status", "--json", "-c", str(d.config_path)])
    assert [s["name"] for s in json.loads(as_json.output)["services"]][:2] == ["web console", "mcp server"]
