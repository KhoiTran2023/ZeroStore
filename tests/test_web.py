"""The web UI: signing in, the pages, review, settings, tokens and sync, on the demo company."""

from __future__ import annotations

import html
import json
import os
import re
import shlex
import stat
import time

import anyio
import pytest
from fastapi.testclient import TestClient
from mcp import Client

from zerostore.agents.commands import program
from zerostore.config import load_config, read_secrets
from zerostore.demo import Demo
from zerostore.insights import activity
from zerostore.librarian.ingestion import Ingestor
from zerostore.mcp.http import create_http_app
from zerostore.mcp.server import build_server
from zerostore.web import auth, charts
from zerostore.web.app import connect_commands, create_web_app, review_view
from zerostore.web.settings import SettingsError, apply_form, form_values, save_text

BASE = "http://localhost"


@pytest.fixture
def web(demo):
    d, zs = demo
    return TestClient(create_web_app(zs, d.config_path), base_url=BASE)


def sign_in(client: TestClient, demo: Demo) -> None:
    response = client.post("/login", data={"token": demo.owner_token()}, follow_redirects=False)
    assert response.status_code == 303
    assert "zerostore_session" in response.cookies


def csrf(client: TestClient, path: str = "/settings") -> str:
    return re.search(r'name="csrf" value="([^"]+)"', client.get(path).text).group(1)


def with_review_item(demo: Demo, zs) -> None:
    demo.change_price()
    demo.add_note()
    Ingestor(zs).sync()


# Signing in -----------------------------------------------------------------


def test_pages_need_sign_in(web):
    response = web.get("/status", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"
    assert "Sign in to ZeroStore" in web.get("/login").text


def test_htmx_requests_are_told_to_go_to_sign_in(web):
    response = web.get("/status", headers={"HX-Request": "true"}, follow_redirects=False)
    assert response.headers["HX-Redirect"] == "/login"


def test_login_link_works_once(web, demo):
    d, zs = demo
    code = auth.create_login_code(zs)
    page = web.get(f"/login?code={code}")  # opening the link (or a preview bot) doesn't use it
    assert f'name="code" value="{code}"' in page.text
    response = web.post("/login", data={"code": code}, follow_redirects=False)
    assert response.status_code == 303
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie
    assert web.get("/status").status_code == 200

    fresh = TestClient(web.app, base_url=BASE)
    again = fresh.post("/login", data={"code": code}, follow_redirects=False)
    assert again.status_code == 200
    assert "already used" in again.text


def test_login_codes_expire_and_are_stored_hashed(demo):
    d, zs = demo
    code = auth.create_login_code(zs, now=1000.0)
    stored = [r["key"] for r in zs.state.query("SELECT key FROM meta WHERE key LIKE 'web_login:%'")]
    assert stored and all(code not in key for key in stored)
    with pytest.raises(auth.LoginError, match="expired"):
        auth.redeem_login_code(zs, code, now=1000.0 + auth.CODE_TTL + 1)
    with pytest.raises(auth.LoginError, match="already used"):
        auth.redeem_login_code(zs, "made-up")


def test_login_link_needs_the_saved_owner_token(web, demo):
    d, zs = demo
    (d.home / "owner-token").unlink()
    code = auth.create_login_code(zs)
    response = web.post("/login", data={"code": code})
    assert "no working owner token" in response.text


def test_only_owner_tokens_sign_in(web, demo):
    d, _ = demo
    agent = d.tokens()["cursor"]
    response = web.post("/login", data={"token": agent})
    assert "the web UI needs an owner token" in response.text
    assert web.post("/login", data={"token": "zs_" + "x" * 40}).status_code == 200
    assert web.get("/status", follow_redirects=False).status_code == 303


def test_sign_out_ends_the_session(web, demo):
    d, _ = demo
    sign_in(web, d)
    web.post("/logout", data={"csrf": csrf(web)})
    assert web.get("/status", follow_redirects=False).status_code == 303


def test_other_host_names_are_refused_on_localhost(web):
    assert web.get("/login", headers={"Host": "evil.example"}).status_code == 400
    assert web.get("/login", headers={"Host": "127.0.0.1:8765"}).status_code == 200
    assert web.get("/login", headers={"Host": "[::1]:8765"}).status_code == 200


def test_posts_need_the_form_token_and_same_origin(web, demo):
    d, _ = demo
    sign_in(web, d)
    assert web.post("/sync", data={}).status_code == 403
    assert web.post("/sync", data={"csrf": "wrong"}).status_code == 403
    token = csrf(web)
    cross = web.post("/sync", data={"csrf": token}, headers={"Origin": "http://evil.example"})
    assert cross.status_code == 403


def test_security_headers(web):
    response = web.get("/login")
    assert response.headers["X-Frame-Options"] == "DENY"
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]


# Pages ------------------------------------------------------------------------


def test_status_page(web, demo):
    d, _ = demo
    sign_in(web, d)
    page = web.get("/status").text
    assert "Up to date." in page
    assert "Google Drive" in page and "GitHub" in page and "Linear" in page
    assert "<svg" in page  # charts
    assert "changes per day" in page and "how sure the librarian was" in page
    assert web.get("/").url.path == "/status"


def test_pages_render(web, demo):
    d, _ = demo
    sign_in(web, d)
    for path, text in [
        ("/review", "Nothing waits for you"),
        ("/log", "# log"),
        ("/settings", "librarian"),
        ("/settings?view=yaml", "decision_engine: demo"),
        ("/setup?step=1", "your docs"),
        ("/setup?step=2", "the AI that judges what changed"),
        ("/setup?step=3", "Connect to Claude"),
        ("/static/htmx.min.js", "htmx"),
    ]:
        response = web.get(path)
        assert response.status_code == 200, path
        assert text in response.text, path


def test_log_hides_the_ui_page_views_and_filters(web, demo):
    d, zs = demo
    sign_in(web, d)
    web.get("/status")
    page = web.get("/log?kind=changes&q=pricing").text
    assert "Pricing 2026" in page
    assert "PEB-27" not in page
    rows = activity(zs, kind="questions")
    assert not any(r["action"] in ("status", "activity", "review_list") for r in rows)


# Review -----------------------------------------------------------------------


def test_review_shows_what_changes_and_approving_applies_it(web, demo):
    d, zs = demo
    with_review_item(d, zs)
    sign_in(web, d)
    page = web.get("/review").text
    assert "Add PEB-48 Drop the free tier?" in page
    assert "Add PEB-48 Drop the free tier??" not in page
    assert "The free tier covers up to 3 users" in page  # what agents are told now
    assert "Maybe we drop the free tier" in page  # what they'd be told
    item = zs.state.review_items("pending")[0]["id"]
    response = web.post(f"/review/{item}", data={"csrf": csrf(web, "/review"), "decision": "approve"})
    assert response.url.path == "/review"
    assert "is live now" in response.text
    assert zs.state.review_items("pending") == []


def test_rejecting_keeps_the_current_answer(web, demo):
    d, zs = demo
    with_review_item(d, zs)
    sign_in(web, d)
    item = zs.state.review_items("pending")[0]["id"]
    response = web.post(f"/review/{item}", data={"csrf": csrf(web, "/review"), "decision": "reject"})
    assert "Nothing waits for you" in response.text
    assert zs.state.get_review_item(item)["status"] == "rejected"


def test_deciding_twice_says_why(web, demo):
    d, zs = demo
    with_review_item(d, zs)
    sign_in(web, d)
    item = zs.state.review_items("pending")[0]["id"]
    token = csrf(web, "/review")
    web.post(f"/review/{item}", data={"csrf": token, "decision": "approve"})
    response = web.post(f"/review/{item}", data={"csrf": token, "decision": "approve"})
    assert f"no pending review item with id {item}" in response.text


def test_review_view_words():
    proposal = review_view(
        {"id": 3, "kind": "agent_proposal", "fact": "Staging runs on Postgres 17.", "proposed_by": "cursor"},
        0.8,
    )
    assert proposal["title"] == "Save “Staging runs on Postgres 17.”?"
    assert proposal["sure"] == "needs a yes"
    moved = review_view(
        {
            "id": 4,
            "kind": "delete",
            "document": "Pricing",
            "source": "google_drive",
            "moved_to": "Pricing 2027",
        },
        0.8,
    )
    assert moved["title"] == "Did Pricing move to Pricing 2027?"
    assert "Google Drive" in moved["why"]
    unsafe = review_view(
        {"id": 5, "kind": "change", "document": "X", "change": "added", "url": "javascript:x"}, 0.8
    )
    assert unsafe["link"] == ""


# Settings -----------------------------------------------------------------------


def test_settings_form_saves_and_keeps_comments(web, demo):
    d, zs = demo
    sign_in(web, d)
    form = {"csrf": csrf(web), "sections": "judgment,sync"}
    form.update(auto_apply_at="0.9", answer_cutoff="0.4", rerank_rule="Newest wins.", narrow_with="off")
    form.update(rerank_with="off", schedule="0 * * * *", agent_proposals="1")
    response = web.post("/settings", data=form)
    assert "Saved to zerostore.yaml" in response.text
    text = d.config_path.read_text()
    assert "# The ZeroStore demo" in text  # comments survive
    saved = load_config(d.config_path)
    assert saved.review.auto_apply_at == 0.9
    assert saved.retrieval.answer_cutoff == 0.4
    assert saved.sync.schedule == "0 * * * *"
    assert zs.config.review.auto_apply_at == 0.9  # the running server uses it for syncs


def test_bad_settings_are_not_saved(web, demo):
    d, _ = demo
    sign_in(web, d)
    before = d.config_path.read_text()
    form = {"csrf": csrf(web), "sections": "sync", "schedule": "every day"}
    response = web.post("/settings", data=form)
    assert "Not saved" in response.text
    assert d.config_path.read_text() == before


def test_keys_go_to_secrets_env_and_are_never_shown(web, demo):
    d, _ = demo
    sign_in(web, d)
    form = {"csrf": csrf(web), "sections": "ai", "llm_provider": "anthropic", "llm_model": "mid-tier"}
    form.update(llm_key="sk-test-secret-123", decision_engine="demo")
    web.post("/settings", data=form)
    assert "${ZEROSTORE_LLM_API_KEY}" in d.config_path.read_text()
    assert "sk-test-secret-123" not in d.config_path.read_text()
    secrets_path = d.config_path.parent / "secrets.env"
    assert read_secrets(secrets_path)["ZEROSTORE_LLM_API_KEY"] == "sk-test-secret-123"
    assert stat.S_IMODE(os.stat(secrets_path).st_mode) == 0o600
    assert load_config(d.config_path).ai.llm_api_key == "sk-test-secret-123"
    page = web.get("/settings").text
    assert "sk-test-secret-123" not in page
    assert "set (ZEROSTORE_LLM_API_KEY)" in page

    # A blank key field keeps the key; a new one replaces it in place.
    web.post("/settings", data={**form, "csrf": csrf(web), "llm_key": ""})
    assert load_config(d.config_path).ai.llm_api_key == "sk-test-secret-123"
    web.post("/settings", data={**form, "csrf": csrf(web), "llm_key": "sk-new"})
    assert secrets_path.read_text().count("ZEROSTORE_LLM_API_KEY=") == 1
    assert load_config(d.config_path).ai.llm_api_key == "sk-new"


def test_a_typed_key_is_not_overridden_by_a_shell_variable(demo, monkeypatch):
    d, _ = demo
    monkeypatch.setenv("LLM_API_KEY", "from-the-shell")
    apply_form(d.config_path, {"sections": "ai", "llm_provider": "openai", "llm_key": "typed"})
    assert load_config(d.config_path).ai.llm_api_key == "typed"


def test_sources_form(demo):
    d, _ = demo
    form = {"sections": "sources", "github_on": "1", "github_repos": "acme/app, acme/docs", "linear_on": "1"}
    form["linear_teams"] = "Eng"
    config = apply_form(d.config_path, form)
    assert config.sources.google_drive is None
    assert config.sources.github.repos == ["acme/app", "acme/docs"]
    assert "repos: [acme/app, acme/docs]" in d.config_path.read_text()
    with pytest.raises(SettingsError, match="owner/name"):
        apply_form(d.config_path, {**form, "github_repos": "not a repo"})
    with pytest.raises(SettingsError, match="line breaks"):
        apply_form(d.config_path, {**form, "linear_key": "a\nb"})


def test_yaml_view_validates_before_writing(web, demo):
    d, _ = demo
    sign_in(web, d)
    before = d.config_path.read_text()
    response = web.post("/settings/yaml", data={"csrf": csrf(web), "yaml": "sources: [oops"})
    assert "isn&#39;t valid YAML" in response.text or "isn't valid YAML" in response.text
    assert d.config_path.read_text() == before
    changed = before.replace("port: 8766", "port: 8799")
    web.post("/settings/yaml", data={"csrf": csrf(web), "yaml": changed})
    assert load_config(d.config_path).servers.port == 8799


def test_save_text_refuses_unknown_settings(demo):
    d, _ = demo
    with pytest.raises(SettingsError, match="problems"):
        save_text(d.config_path, d.config_path.read_text() + "surprise: 1\n")


def test_form_values_never_hold_keys(demo):
    d, _ = demo
    apply_form(d.config_path, {"sections": "ai", "llm_provider": "openai", "llm_key": "sk-abc"})
    values = form_values(d.config_path)
    assert values["llm_key"] == "set (ZEROSTORE_LLM_API_KEY)"
    assert values["jev_key"] == "not set"


# Tokens and connecting agents ------------------------------------------------------


def test_connect_to_claude_creates_a_token_and_shows_it_once(web, demo):
    d, zs = demo
    sign_in(web, d)
    data = {"csrf": csrf(web, "/setup?step=3"), "name": "claude-code", "level": "confidential"}
    page = web.post("/tokens", data={**data, "back": "/setup?step=3"}).text
    raw = re.search(r'id="new-token">(zs_[^<]+)<', page).group(1)
    assert zs.tokens.verify(raw).level.value == "confidential"
    line = f"claude mcp add zerostore -e ZEROSTORE_TOKEN={raw} -- {program()} serve -c"
    assert line in html.unescape(page)
    assert raw not in web.get("/setup?step=3").text  # shown once


def test_revoking_a_token(web, demo):
    d, zs = demo
    sign_in(web, d)
    cursor = next(t for t in zs.tokens.list() if t.name == "cursor")
    web.post(f"/tokens/{cursor.id}/revoke", data={"csrf": csrf(web)})
    assert all(t.id != cursor.id for t in zs.tokens.list())


def test_connect_commands_skip_the_config_flag_for_the_default_config(tmp_path, monkeypatch):
    monkeypatch.setenv("ZEROSTORE_HOME", str(tmp_path))
    plain = connect_commands("zs_tok", tmp_path / "zerostore.yaml")
    assert shlex.split(plain["claude_code"])[-2:] == [program(), "serve"]
    assert "http" not in plain
    other = tmp_path / "my folder" / "zerostore.yaml"
    both = connect_commands("zs_tok", other, "http://localhost:8765/mcp")
    assert shlex.split(both["claude_code"])[-3:] == ["serve", "-c", str(other)]
    assert shlex.split(both["claude_code_http"]) == [
        "claude", "mcp", "add", "--transport", "http", "zerostore", "http://localhost:8765/mcp",
        "--header", "Authorization: Bearer zs_tok",
    ]  # fmt: skip
    assert json.loads(both["http"])["mcpServers"]["zerostore"] == {
        "type": "http",
        "url": "http://localhost:8765/mcp",
        "headers": {"Authorization": "Bearer zs_tok"},
    }


# Sync from the page -------------------------------------------------------------------


def test_sync_runs_in_the_background(web, demo):
    d, zs = demo
    sign_in(web, d)
    d.change_price()
    response = web.post("/sync", data={"csrf": csrf(web, "/status")})
    assert response.url.path == "/status"
    syncer = web.app.state.syncer
    deadline = time.time() + 30
    while syncer.running and time.time() < deadline:
        time.sleep(0.05)
    finished = web.get("/sync/state", headers={"HX-Request": "true"})
    assert finished.status_code == 204
    assert finished.headers["HX-Refresh"] == "true"
    assert "Pricing 2026" in web.get("/status").text
    assert "$29" in "".join(
        c["text"] for c in zs.facts.chunks_of("google_drive:demo/Pebble/Decisions/Pricing 2026.md")
    )


# Serving next to MCP ------------------------------------------------------------------------


def test_http_app_serves_mcp_and_the_web_ui(demo):
    d, zs = demo
    app = create_http_app(zs, "127.0.0.1", config_path=d.config_path, mcp_url="http://localhost:8766/mcp")
    with TestClient(app, base_url=BASE) as client:
        assert client.get("/login").status_code == 200
        assert client.post("/mcp", json={}).status_code == 401


def test_http_app_without_the_web_ui(demo):
    d, zs = demo
    zs.config.web_ui = False
    app = create_http_app(zs, "127.0.0.1", config_path=d.config_path)
    with TestClient(app, base_url=BASE) as client:
        assert client.get("/login").status_code == 404


# Owner tools behind the pages -------------------------------------------------------------


def call(zs, token, tool, **args):
    async def go():
        async with Client(build_server(zs, lambda ctx: token)) as client:
            return await client.call_tool(tool, args)

    return anyio.run(go)


def test_status_and_activity_are_owner_only(demo):
    d, zs = demo
    owner, agent = d.owner_token(), d.tokens()["cursor"]
    status = call(zs, owner, "status").structured_content
    assert status["documents"] == 13
    assert len(status["charts"]["days"]) == 14
    assert status["last_sync"]["judged"] >= 1
    assert call(zs, agent, "status").is_error
    assert call(zs, agent, "activity").is_error
    items = call(zs, owner, "activity", kind="changes").structured_content["items"]
    assert items and all(i["kind"] == "change" for i in items)
    assert call(zs, owner, "activity", kind="nope").is_error


def test_searches_are_timed(demo):
    d, zs = demo
    call(zs, d.tokens()["cursor"], "search", query="What does the Pro plan cost?")
    rows = zs.state.query("SELECT duration_ms FROM access_log WHERE action = 'search'")
    assert rows and rows[-1]["duration_ms"] is not None
    status = call(zs, d.owner_token(), "status").structured_content
    assert status["charts"]["questions"][-1] == 1
    assert status["charts"]["median_ms"][-1] is not None


def test_review_list_shows_before_and_after(demo):
    d, zs = demo
    with_review_item(d, zs)
    items = call(zs, d.owner_token(), "review_list").structured_content["items"]
    item = items[0]
    assert any("free tier covers up to 3 users" in b["text"] for b in item["before"])
    assert any("Maybe we drop the free tier" in a["text"] for a in item["after"])


# Charts -------------------------------------------------------------------------------------


def test_nice_axis_tops():
    assert [charts.nice_max(v) for v in (0, 1, 3, 7, 12, 99, 101)] == [1, 1, 5, 10, 20, 100, 200]


def test_charts_empty_and_filled():
    days = [f"2026-10-{d:02d}" for d in range(1, 15)]
    assert "No changes" in charts.stacked_columns(days, [("a", charts.BLUE, [0] * 14)], "x", "No changes")
    svg = charts.stacked_columns(
        days, [("applied", charts.BLUE, [1] * 14), ("review", charts.ORANGE, [0] * 13 + [2])], "x", "none"
    )
    assert svg.count("<title>") == 14 and "legend" in svg and "<table>" in svg
    assert "nan" not in svg.lower()
    hist = charts.histogram([0] * 9 + [5], 0.8, "x", "none")
    assert "auto-apply 0.80" in hist
    line = charts.line(days, [None] * 13 + [12], "x", "ms", "none")
    assert "<circle" in line and "12 ms" in line
    assert "<script" not in charts.bars([("<script>", 1)], "x", "none")


# The CLI and the demo ----------------------------------------------------------------------


def test_zerostore_ui_prints_a_sign_in_link(demo):
    from typer.testing import CliRunner

    from zerostore.cli import app

    d, zs = demo
    result = CliRunner().invoke(app, ["ui", "--no-open", "-c", str(d.config_path)])
    assert result.exit_code == 0, result.output
    code = re.search(r"http://localhost:8766/login\?code=(\S+)", result.output).group(1)
    auth.redeem_login_code(zs, code)


def test_demo_keeps_its_owner_token_across_resets(demo):
    d, zs = demo
    first = d.owner_token()
    assert zs.tokens.verify(first).level.value == "owner"
    zs.close()
    d.install()
    again = d.open()
    d.create_tokens(again)
    assert d.owner_token() == first
    assert stat.S_IMODE(os.stat(d.home / "owner-token").st_mode) == 0o600
    again.close()


# What the second review found -------------------------------------------------------------


def test_a_revoked_owner_token_signs_out(web, demo):
    d, zs = demo
    sign_in(web, d)
    owner = zs.tokens.verify(d.owner_token())
    zs.tokens.revoke(owner.id)
    response = web.get("/status", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_behind_a_proxy(demo):
    d, zs = demo
    app = create_web_app(zs, d.config_path, bound_host="127.0.0.1", behind_proxy=True)
    client = TestClient(app, base_url="https://zs.example.com")
    assert client.get("/login").status_code == 200  # the public host name is fine
    headers = {
        "Origin": "https://zs.example.com",
        "Host": "127.0.0.1:8765",
        "X-Forwarded-Host": "zs.example.com",
    }
    response = client.post("/login", data={"token": d.owner_token()}, headers=headers, follow_redirects=False)
    assert response.status_code == 303
    assert "secure" in response.headers["set-cookie"].lower()
    other = {**headers, "Origin": "https://evil.example"}
    assert client.post("/login", data={"token": "x"}, headers=other).status_code == 403


def test_refused_sign_ins_are_limited(web):
    for _ in range(10):
        web.post("/login", data={"token": "zs_" + "x" * 40})
    assert "Too many tries" in web.post("/login", data={"token": "zs_" + "x" * 40}).text


def test_odd_form_tokens_are_refused_not_crashed(web, demo):
    d, _ = demo
    sign_in(web, d)
    assert web.post("/sync", data={"csrf": "é"}).status_code == 403


def test_sign_in_page_hides_the_config_path(web, demo):
    d, _ = demo
    assert str(d.config_path) not in web.get("/login").text


def test_saving_keeps_symlinks_and_permissions(demo, tmp_path):
    d, _ = demo
    real = tmp_path / "dotfiles" / "zerostore.yaml"
    real.parent.mkdir()
    real.write_text(d.config_path.read_text())
    os.chmod(real, 0o640)
    link = tmp_path / "linked" / "zerostore.yaml"
    link.parent.mkdir()
    link.symlink_to(real)
    apply_form(link, {"sections": "sync", "schedule": "0 * * * *"})
    assert link.is_symlink()
    assert "0 * * * *" in real.read_text()
    assert stat.S_IMODE(os.stat(real).st_mode) == 0o640


def test_yaml_view_line_endings_and_edits_made_elsewhere(demo):
    d, _ = demo
    text = d.config_path.read_text()
    version = form_values(d.config_path)["version"]
    save_text(d.config_path, text.replace("\n", "\r\n"), version)
    assert "\r" not in d.config_path.read_text()
    d.config_path.write_text(text + "# edited in another tab\n")
    with pytest.raises(SettingsError, match="changed since this page was opened"):
        apply_form(d.config_path, {"sections": "sync", "schedule": "0 * * * *", "version": version})


def test_names_yaml_would_misread_are_quoted(demo):
    d, _ = demo
    config = apply_form(
        d.config_path, {"sections": "sources", "linear_on": "1", "linear_teams": "On, Yes, 1.0, Eng"}
    )
    assert config.sources.linear.teams == ["On", "Yes", "1.0", "Eng"]


def test_scheduled_syncs_follow_saved_settings(demo):
    from datetime import datetime, timezone

    from zerostore.scheduler.runner import ANCHOR_KEY, SyncScheduler

    d, zs = demo
    zs.state.set_meta(ANCHOR_KEY, "2026-10-01T00:00:00+00:00")
    built = []

    def renew():
        built.append(1)
        return Ingestor(zs)

    clock = lambda: datetime(2026, 10, 2, 7, 0, tzinfo=timezone.utc)  # noqa: E731
    scheduler = SyncScheduler(Ingestor(zs), None, clock=clock, tz=timezone.utc, renew=renew)
    assert scheduler.schedule == zs.config.sync.schedule
    zs.config = apply_form(d.config_path, {"sections": "sync", "schedule": "0 5 * * *"})
    assert scheduler.schedule == "0 5 * * *"
    assert scheduler.run_if_due()
    assert built == [1]  # a fresh Ingestor, so a judge added since start is used


def test_scheduler_waits_while_there_are_no_sources(tmp_path, zs):
    from datetime import datetime, timezone

    from zerostore.scheduler.runner import ANCHOR_KEY, SyncScheduler

    zs.state.set_meta(ANCHOR_KEY, "2026-10-01T00:00:00+00:00")
    clock = lambda: datetime(2026, 10, 2, 7, 0, tzinfo=timezone.utc)  # noqa: E731
    scheduler = SyncScheduler(Ingestor(zs), None, clock=clock, tz=timezone.utc)
    assert not scheduler.run_if_due()
    assert zs.state.get_meta(ANCHOR_KEY).startswith("2026-10-02")


def test_zerostore_ui_uses_the_address_serve_announced(demo):
    from typer.testing import CliRunner

    from zerostore.cli import WEB_URL_KEY, app

    d, zs = demo
    zs.state.set_meta(WEB_URL_KEY, "https://zs.example.com")
    result = CliRunner().invoke(app, ["ui", "--no-open", "-c", str(d.config_path)])
    assert "https://zs.example.com/login?code=" in result.output
