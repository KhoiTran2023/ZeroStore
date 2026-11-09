"""Agents changing information, only where their role allows (agent_writes in zerostore.yaml)."""

from __future__ import annotations

import anyio
import pytest
import yaml
from mcp import Client
from test_roles import HANDBOOK, PRICING, RUNWAY, call, tag_team

from zerostore.config import ConfigError, load_config, parse_config
from zerostore.connectors import written as w
from zerostore.librarian.ingestion import Ingestor
from zerostore.mcp.server import build_server


def tool_names(zs, token) -> set[str]:
    async def go():
        async with Client(build_server(zs, lambda ctx: token)) as client:
            return {t.name for t in (await client.list_tools()).tools}

    return anyio.run(go)


def allow_writes(demo, roles, *, review=False, enabled=True):
    d, zs = demo
    raw = yaml.safe_load(d.config_path.read_text())
    raw["agent_writes"] = {
        "enabled": enabled,
        "review": review,
        "folder": str(d.config_path.parent / "written"),
        "roles": roles,
    }
    d.config_path.write_text(yaml.safe_dump(raw))
    zs.config = load_config(d.config_path)


def fact_from(zs, token, query, document):
    result = call(zs, token, "search", query=query)
    assert not result.is_error, result.content
    facts = [f for f in result.structured_content["facts"] if f["document"] == document]
    assert facts, result.structured_content
    return facts[0]


def ok(result):
    assert not result.is_error, result.content
    return result.structured_content


def refused(result, words):
    assert result.is_error
    assert words in result.content[0].text, result.content[0].text


@pytest.fixture
def team(demo):
    """Team folder tagged `people`; `hr` sees and may change it. Returns (zs, hr, plain, owner)."""
    d, zs = demo
    tag_team(demo, {"hr": ["people"], "sales": []})
    hr, _ = zs.tokens.create("hr-bot", "default", role="hr")
    plain, _ = zs.tokens.create("plain", "default")
    return zs, hr, plain, d.owner_token()


def test_writes_are_off_unless_the_settings_turn_them_on(demo, team):
    zs, hr, _, owner = team
    allow_writes(demo, {"hr": ["people"]}, enabled=False)
    assert "add_fact" not in tool_names(zs, hr) and "add_fact" not in tool_names(zs, owner)
    refused(call(zs, hr, "add_fact", text="We hire twice a year.", tag="people"), "turned off")


def test_only_roles_with_a_write_list_get_the_tools(demo, team):
    zs, hr, plain, owner = team
    allow_writes(demo, {"hr": ["people"]})
    assert {"add_fact", "change_fact", "remove_fact"} <= tool_names(zs, hr)
    assert {"add_fact", "change_fact", "remove_fact"} <= tool_names(zs, owner)
    assert "add_fact" not in tool_names(zs, plain)
    refused(call(zs, plain, "add_fact", text="Anything."), "can't change anything")


def test_a_role_adds_within_its_tags_and_nowhere_else(demo, team):
    zs, hr, plain, _ = team
    allow_writes(demo, {"hr": ["people"]})

    out = ok(call(zs, hr, "add_fact", text="New hires get a Lisbon onboarding week.", tag="people"))
    assert out["status"] == "saved" and out["fact_ids"]
    found = fact_from(zs, hr, "onboarding week for new hires", "Written by hr-bot")
    assert "Lisbon onboarding week" in found["text"]
    # Tagged people, so a token without the role doesn't see it.
    seen = ok(call(zs, plain, "search", query="onboarding week for new hires"))
    assert all(f["document"] != "Written by hr-bot" for f in seen["facts"])

    refused(
        call(zs, hr, "add_fact", text="The Pro plan is free."), "may only change information tagged people"
    )
    refused(call(zs, hr, "add_fact", text="x", tag="people", confidential=True), "confidential")
    refused(call(zs, hr, "add_fact", text="x", tag="nope"), "no tag called")
    denied = zs.state.query("SELECT COUNT(*) AS n FROM access_log WHERE action = 'add_fact' AND allowed = 0")
    assert denied[0]["n"] == 3


def test_a_correction_survives_the_next_sync(demo, team):
    """The sync re-reads the original document; the change must not be undone."""
    zs, hr, _, _ = team
    allow_writes(demo, {"hr": ["people"]})
    old = fact_from(zs, hr, "core hours Lisbon time", HANDBOOK)

    out = ok(call(zs, hr, "change_fact", id=old["id"], text="Core hours are 11:00 to 16:00 Lisbon time."))
    assert out["status"] == "saved"
    assert "outdated" in ok(call(zs, hr, "get_fact", id=old["id"])).get("status", "")
    new = fact_from(zs, hr, "core hours Lisbon time", f"Correction to {HANDBOOK} (by hr-bot)")
    assert "11:00 to 16:00" in new["text"]

    for _ in range(2):
        assert Ingestor(zs).sync().ok
        assert "outdated" in ok(call(zs, hr, "get_fact", id=old["id"])).get("status", "")
        assert ok(call(zs, hr, "get_fact", id=new["id"]))["text"] == new["text"]

    # Changing the correction rewrites it in place; removing it brings the original back.
    ok(call(zs, hr, "change_fact", id=new["id"], text="Core hours are 9:00 to 14:00 Lisbon time."))
    assert "9:00 to 14:00" in ok(call(zs, hr, "get_fact", id=new["id"]))["text"]
    ok(call(zs, hr, "remove_fact", id=new["id"]))
    refused(call(zs, hr, "get_fact", id=new["id"]), "no fact")
    assert "status" not in ok(call(zs, hr, "get_fact", id=old["id"]))


def test_a_role_cant_change_what_it_isnt_given(demo, team):
    d, zs = demo
    _, hr, _, owner = team
    allow_writes(demo, {"hr": ["people"]})
    pricing = fact_from(zs, hr, "what does the Pro plan cost", PRICING)
    refused(
        call(zs, hr, "change_fact", id=pricing["id"], text="Pro is $1."), "tagged people; this is untagged"
    )
    refused(call(zs, hr, "remove_fact", id=pricing["id"]), "tagged people")
    # Confidential facts aren't even visible to a default token.
    runway = fact_from(zs, owner, "how much runway do we have", RUNWAY)
    refused(call(zs, hr, "change_fact", id=runway["id"], text="x"), "no fact")
    # Facts from documents can be corrected but not removed.
    handbook = fact_from(zs, hr, "core hours Lisbon time", HANDBOOK)
    refused(call(zs, hr, "remove_fact", id=handbook["id"]), "only reads")
    assert not (d.config_path.parent / "written").exists() or not list(
        (d.config_path.parent / "written").iterdir()
    )


def test_untagged_needs_untagged_in_the_list(demo, team):
    zs, _, _, _ = team
    allow_writes(demo, {"hr": ["people", "untagged"]})
    hr2, _ = zs.tokens.create("hr-2", "default", role="hr")
    pricing = fact_from(zs, hr2, "what does the Pro plan cost", PRICING)
    out = ok(call(zs, hr2, "change_fact", id=pricing["id"], text="The Pro plan costs $35 a month."))
    assert out["status"] == "saved"
    answer = fact_from(zs, hr2, "what does the Pro plan cost", f"Correction to {PRICING} (by hr-2)")
    assert "$35" in answer["text"]


def test_review_mode_waits_for_the_owner(demo, team):
    zs, hr, _, owner = team
    allow_writes(demo, {"hr": ["people"]}, review=True)
    out = ok(call(zs, hr, "add_fact", text="Interviews always include a paid trial day.", tag="people"))
    assert out["status"] == "waiting for approval"
    seen = ok(call(zs, hr, "search", query="paid trial day interviews"))
    assert all(f["document"] != "Written by hr-bot" for f in seen["facts"])

    items = ok(call(zs, owner, "review_list"))["items"]
    item = next(i for i in items if i["kind"] == "agent_write")
    assert item["change"] == "add a fact" and item["tag"] == "people" and item["proposed_by"] == "hr-bot"
    ok(call(zs, owner, "review_decide", id=item["id"], approve=True))
    fact_from(zs, hr, "paid trial day interviews", "Written by hr-bot")


def test_turning_writes_off_keeps_what_was_written(demo, team):
    zs, hr, _, _ = team
    allow_writes(demo, {"hr": ["people"]})
    ok(call(zs, hr, "add_fact", text="Laptops are replaced every three years.", tag="people"))
    allow_writes(demo, {"hr": ["people"]}, enabled=False)
    assert Ingestor(zs).sync().ok
    fact_from(zs, hr, "laptops replaced every three years", "Written by hr-bot")


def test_write_lists_are_checked(tmp_path):
    base = {"sources": {}, "tags": {"sales": [{"github": "acme/sales"}], "hr": [{"github": "acme/hr"}]}}
    parse_config(
        {**base, "roles": {"rep": ["sales"]}, "agent_writes": {"roles": {"rep": ["sales", "untagged"]}}}
    )
    with pytest.raises(ConfigError, match="no role called"):
        parse_config({**base, "agent_writes": {"roles": {"rep": ["sales"]}}})
    with pytest.raises(ConfigError, match="can't see it"):
        parse_config({**base, "roles": {"rep": ["sales"]}, "agent_writes": {"roles": {"rep": ["hr"]}}})
    assert parse_config(base).agent_writes.enabled is False


def test_entries_round_trip(tmp_path):
    entry = w.Entry(id=w.new_id(), text="Hello: world", written_by="bot", tag="sales", corrects="abc")
    w.write_entry(tmp_path, entry)
    back = w.read_entry(tmp_path, entry.id)
    assert back == entry
    with pytest.raises(ValueError):
        w.entry_path(tmp_path, "../../etc/passwd")


def test_stdio_registers_the_write_tools_only_for_writers(demo, team):
    zs, hr, plain, _ = team
    allow_writes(demo, {"hr": ["people"]})
    from zerostore.access.tokens import AccessLevel
    from zerostore.mcp.server import may_write

    assert may_write(zs.config, zs.tokens.verify(hr)) and not may_write(zs.config, zs.tokens.verify(plain))

    async def names(writer):
        server = build_server(zs, lambda ctx: hr, only_level=AccessLevel.DEFAULT, only_writer=writer)
        async with Client(server) as client:
            return {t.name for t in (await client.list_tools()).tools}

    assert "add_fact" in anyio.run(names, True)
    assert "add_fact" not in anyio.run(names, False)
