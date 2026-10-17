"""Roles and tags: which agents see which information, set in the web UI or zerostore.yaml."""

from __future__ import annotations

import re

import anyio
import pytest
from fastapi.testclient import TestClient
from mcp import Client

from zerostore.access.roles import access_key, hidden_tags, key_tags
from zerostore.config import ConfigError, load_config, parse_config
from zerostore.librarian.ingestion import Ingestor
from zerostore.mcp.server import build_server
from zerostore.store.state import StateDB
from zerostore.web.app import create_web_app
from zerostore.web.settings import apply_form, form_values

HANDBOOK = "Company handbook"
RUNWAY = "Runway and salaries"
PRICING = "Pricing 2026"


def call(zs, token, tool, **args):
    async def go():
        async with Client(build_server(zs, lambda ctx: token)) as client:
            return await client.call_tool(tool, args)

    return anyio.run(go)


def titles(zs, token) -> set[str]:
    result = call(zs, token, "list_sources")
    assert not result.is_error, result.content
    return {d["title"] for d in result.structured_content["documents"]}


def tag_team(demo, roles: dict[str, list[str]] | None = None) -> None:
    """Tag the demo's Team folder `people` and give the `hr` role that tag, then sync."""
    d, zs = demo
    form = {
        "sections": "access",
        "version": form_values(d.config_path)["version"],
        "tag_0": "confidential",
        "kind_0": "google_drive",
        "place_0": "Pebble/Finance",
        "tag_1": "people",
        "kind_1": "google_drive",
        "place_1": "Pebble/Team",
        "tag_2": "",
        "kind_2": "google_drive",
        "place_2": "",
    }
    for i, (name, tags) in enumerate((roles if roles is not None else {"hr": ["people"]}).items()):
        form[f"role_{i}"], form[f"role_tags_{i}"] = name, ", ".join(tags)
    zs.config = apply_form(d.config_path, form)
    report = Ingestor(zs).sync()
    assert report.ok, report


# Settings ----------------------------------------------------------------------


def test_roles_must_name_defined_tags(tmp_path):
    base = {"sources": {}}
    parse_config({**base, "tags": {"sales": [{"github": "acme/sales"}]}, "roles": {"rep": ["sales"]}})
    with pytest.raises(ConfigError, match="aren't defined"):
        parse_config({**base, "roles": {"rep": ["sales"]}})
    with pytest.raises(ConfigError, match="lowercase"):
        parse_config({**base, "tags": {"Sales Team": [{"github": "acme/sales"}]}})
    with pytest.raises(ConfigError, match="under `confidential:`"):
        parse_config({**base, "tags": {"confidential": [{"github": "acme/x"}]}})
    with pytest.raises(ConfigError, match="at least one place"):
        parse_config({**base, "tags": {"sales": []}})


def test_the_form_writes_the_same_yaml_the_cli_reads(demo):
    d, _ = demo
    tag_team(demo, {"hr": ["people"], "leadership": ["people"]})
    text = d.config_path.read_text()
    assert "tags:" in text and "people:" in text and "roles:" in text
    cfg = load_config(d.config_path)
    assert [r.google_drive for r in cfg.confidential] == ["Pebble/Finance"]
    assert [r.google_drive for r in cfg.tags["people"]] == ["Pebble/Team"]
    assert cfg.roles == {"hr": ["people"], "leadership": ["people"]}
    # Reading it back gives the rows the form showed.
    rows = form_values(d.config_path)["access"]
    assert {"tag": "people", "kind": "google_drive", "place": "Pebble/Team"} in rows["places"]
    assert {"name": "hr", "tags": "people"} in rows["roles"]


def test_a_half_filled_row_is_refused(demo):
    d, _ = demo
    form = {"sections": "access", "version": form_values(d.config_path)["version"], "tag_0": "people"}
    form.update({"kind_0": "google_drive", "place_0": ""})
    with pytest.raises(Exception, match="both a tag and"):
        apply_form(d.config_path, form)


# Who sees what -----------------------------------------------------------------


def test_a_role_sees_its_tags_and_others_dont(demo):
    d, zs = demo
    tag_team(demo)
    no_role, _ = zs.tokens.create("plain", "default")
    hr, _ = zs.tokens.create("hr-bot", "default", role="hr")
    owner = d.owner_token()

    assert HANDBOOK not in titles(zs, no_role) and PRICING in titles(zs, no_role)
    assert HANDBOOK in titles(zs, hr) and PRICING in titles(zs, hr)
    assert RUNWAY not in titles(zs, hr)  # a role never lifts the confidential rule
    assert {HANDBOOK, RUNWAY, PRICING} <= titles(zs, owner)

    asked = call(zs, no_role, "search", query="who does what at pebble")
    assert HANDBOOK not in {f["document"] for f in asked.structured_content["facts"]}
    found = call(zs, hr, "search", query="who does what at pebble")
    assert HANDBOOK in {f["document"] for f in found.structured_content["facts"]}

    fact = found.structured_content["facts"][0]["id"]
    assert call(zs, no_role, "get_fact", id=fact).is_error
    assert not call(zs, hr, "get_fact", id=fact).is_error


def test_whats_changed_hides_tagged_documents(demo):
    d, zs = demo
    tag_team(demo)
    zs.state.audit("document_updated", doc_id="x", detail="Team note", access=",people,")
    plain, _ = zs.tokens.create("plain", "default")
    hr, _ = zs.tokens.create("hr-bot", "default", role="hr")

    def details(token):
        changes = call(zs, token, "whats_changed", since="30d").structured_content["changes"]
        return {c["detail"] for c in changes}

    assert "Team note" not in details(plain)
    assert "Team note" in details(hr)


def test_a_removed_tag_stays_hidden_until_the_next_sync(demo):
    d, zs = demo
    tag_team(demo)
    plain, _ = zs.tokens.create("plain", "default")
    zs.config = parse_config({**zs.config.model_dump(exclude_none=True), "tags": {}, "roles": {}})
    assert HANDBOOK not in titles(zs, plain)  # still tagged on disk: not turned public by a typo
    assert Ingestor(zs).sync().ok
    assert HANDBOOK in titles(zs, plain)


def test_tags_are_kept_with_each_fact(demo):
    d, zs = demo
    tag_team(demo)
    for row in zs.facts.chunks_of(next(k for k, v in zs.state.documents().items() if v["title"] == HANDBOOK)):
        assert row["access"] == ",people,"
    pricing = next(v for v in zs.state.documents().values() if v["title"] == PRICING)
    assert pricing["access"] == ""


def test_hidden_tags_rules():
    cfg = parse_config(
        {
            "sources": {},
            "tags": {"a": [{"github": "x/a"}], "b": [{"github": "x/b"}]},
            "roles": {"ra": ["a"], "both": ["a", "b"]},
        }
    )
    assert hidden_tags(cfg, None, owner=False) == ["a", "b"]
    assert hidden_tags(cfg, "ra", owner=False) == ["b"]
    assert hidden_tags(cfg, "both", owner=False) == []
    assert hidden_tags(cfg, "gone", owner=False, known=["old"]) == ["a", "b", "old"]
    assert hidden_tags(cfg, None, owner=True) == []
    assert access_key(["b", "a", "a"]) == ",a,b," and key_tags(",a,b,") == ["a", "b"] and access_key([]) == ""


# Tokens ------------------------------------------------------------------------


def test_tokens_get_only_roles_that_exist(demo):
    d, zs = demo
    tag_team(demo)
    owner = d.owner_token()
    bad = call(zs, owner, "token_create", name="x", level="default", role="nope")
    assert bad.is_error and "no role called 'nope'" in bad.content[0].text
    made = call(zs, owner, "token_create", name="x", level="default", role="hr").structured_content
    assert made["role"] == "hr"
    listed = {t["name"]: t["role"] for t in call(zs, owner, "token_list").structured_content["tokens"]}
    assert listed["x"] == "hr"
    assert not call(zs, owner, "token_set_role", id=made["id"], role=None).is_error
    assert zs.tokens.get(made["id"]).role is None
    assert call(zs, owner, "token_set_role", id=made["id"], role="nope").is_error
    owner_id = zs.tokens.verify(owner).id
    assert call(zs, owner, "token_set_role", id=owner_id, role="hr").is_error  # owners see everything


def test_the_role_column_is_added_to_older_files(tmp_path):
    path = tmp_path / "s.sqlite"
    StateDB(path).close()
    import sqlite3

    conn = sqlite3.connect(path)
    conn.execute("ALTER TABLE tokens DROP COLUMN role")
    conn.execute("ALTER TABLE documents DROP COLUMN access")
    conn.execute("ALTER TABLE audit DROP COLUMN access")
    conn.commit()
    conn.close()
    state = StateDB(path)
    assert state.query("SELECT role FROM tokens") == []
    assert state.access_keys() == set()
    state.close()


def test_cli_token_create_with_a_role(demo):
    from typer.testing import CliRunner

    from zerostore.cli import app

    d, zs = demo
    tag_team(demo)
    runner = CliRunner()
    made = runner.invoke(app, ["token", "create", "--name", "bot", "--role", "hr", "-c", str(d.config_path)])
    assert made.exit_code == 0, made.output
    assert "role hr" in made.output
    refused = runner.invoke(app, ["token", "create", "--name", "b2", "--role", "x", "-c", str(d.config_path)])
    assert refused.exit_code != 0 and "no role called 'x'" in refused.output
    listed = runner.invoke(app, ["token", "list", "-c", str(d.config_path)])
    assert re.search(r"default\s+hr\s+bot", listed.output)


# Web UI ------------------------------------------------------------------------


def test_the_web_ui_edits_roles_and_gives_them_to_tokens(demo):
    d, zs = demo
    web = TestClient(create_web_app(zs, d.config_path), base_url="http://localhost")
    web.post("/login", data={"token": d.owner_token()})
    page = web.get("/settings").text
    assert "who sees what" in page
    csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    version = re.search(r'value="access"><input type="hidden" name="version" value="([^"]+)"', page)
    form = {
        "csrf": csrf,
        "sections": "access",
        "version": version.group(1),
        "tag_0": "people",
        "kind_0": "google_drive",
        "place_0": "Pebble/Team",
        "role_0": "hr",
        "role_tags_0": "people",
    }
    saved = web.post("/settings", data=form)
    assert "Run a sync to apply tag changes" in saved.text
    assert zs.config.roles == {"hr": ["people"]}
    page = web.get("/settings").text
    assert 'value="hr"' in page and "no role (untagged only)" in page

    csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    web.post("/tokens", data={"csrf": csrf, "name": "hr-bot", "level": "default", "role": "hr"})
    [bot] = [t for t in zs.tokens.list() if t.name == "hr-bot"]
    assert bot.role == "hr"
    web.post(f"/tokens/{bot.id}/role", data={"csrf": csrf, "role": ""})
    assert zs.tokens.get(bot.id).role is None
