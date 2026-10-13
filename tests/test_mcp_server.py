"""The MCP server, driven by a real MCP client in-process."""

from __future__ import annotations

import anyio
import pytest
from mcp import Client

from zerostore.access.tokens import AccessLevel
from zerostore.mcp import descriptions as d
from zerostore.mcp.server import build_server, parse_since, tools_for


class Caller:
    """A token source the tests can swap mid-session."""

    def __init__(self, token=None):
        self.token = token

    def __call__(self, ctx):
        return self.token


def run(coro_fn):
    return anyio.run(coro_fn)


async def call(server, name, args=None):
    async with Client(server) as client:
        return await client.call_tool(name, args or {})


def ok(result):
    assert not result.is_error, result.content[0].text
    return result.structured_content


def err(result):
    assert result.is_error
    return result.content[0].text


@pytest.fixture
def tokens(seeded):
    return {level: seeded.tokens.create(level, level)[0] for level in ("default", "confidential", "owner")}


def test_instructions_and_descriptions_reach_the_client(seeded, tokens):
    server = build_server(seeded, Caller(tokens["default"]))

    async def go():
        async with Client(server) as client:
            listed = {t.name: t for t in (await client.list_tools()).tools}
            return client.instructions, client.server_info, listed

    instructions, server_info, listed = run(go)
    assert instructions == d.SERVER_INSTRUCTIONS
    assert server_info.name == "ZeroStore"
    assert listed["search"].description == d.SEARCH
    assert "before answering" in listed["search"].description
    assert listed["search"].annotations.read_only_hint is True


def test_default_token_never_sees_confidential(seeded, tokens):
    server = build_server(seeded, Caller(tokens["default"]))
    facts = ok(run(lambda: call(server, "search", {"query": "pro salary band"})))["facts"]
    assert facts and all(f["document"] != "Salary bands" for f in facts)
    docs = ok(run(lambda: call(server, "list_sources")))["documents"]
    assert [x["title"] for x in docs] == ["Pricing 2026"]


def test_confidential_token_sees_everything(seeded, tokens):
    server = build_server(seeded, Caller(tokens["confidential"]))
    facts = ok(run(lambda: call(server, "search", {"query": "pro salary band"})))["facts"]
    assert any(f["document"] == "Salary bands" for f in facts)


def test_search_answers_carry_sources(seeded, tokens):
    server = build_server(seeded, Caller(tokens["default"]))
    fact = ok(run(lambda: call(server, "search", {"query": "pro plan price"})))["facts"][0]
    assert set(fact) == {"id", "text", "document", "source", "source_url", "updated_at"}
    assert fact["source_url"].startswith("https://docs.google.com/")


def test_not_sure_when_store_is_empty(zs):
    raw, _ = zs.tokens.create("a", "default")
    server = build_server(zs, Caller(raw))
    assert ok(run(lambda: call(server, "search", {"query": "pricing"}))) == {
        "answer": "not sure",
        "facts": [],
    }


def test_get_fact_hides_confidential_from_default(seeded, tokens):
    from conftest import salary_doc

    fact_id = salary_doc()[0].id
    assert "no fact" in err(
        run(lambda: call(build_server(seeded, Caller(tokens["default"])), "get_fact", {"id": fact_id}))
    )
    got = ok(run(lambda: call(build_server(seeded, Caller(tokens["owner"])), "get_fact", {"id": fact_id})))
    assert got["document"] == "Salary bands"


@pytest.mark.parametrize("token", [None, "", "zs_not_a_real_token"])
def test_bad_tokens_are_refused_and_logged(seeded, token):
    server = build_server(seeded, Caller(token))
    assert "token" in err(run(lambda: call(server, "search", {"query": "pro"})))
    rows = seeded.state.query("SELECT token_id, action, allowed FROM access_log")
    assert [tuple(r) for r in rows] == [(None, "search", 0)]


def test_revoking_takes_effect_mid_session(seeded, tokens):
    caller = Caller(tokens["default"])
    server = build_server(seeded, caller)

    async def go():
        async with Client(server) as client:
            first = await client.call_tool("search", {"query": "pro"})
            info = seeded.tokens.verify(tokens["default"])
            seeded.tokens.revoke(info.id)
            second = await client.call_tool("search", {"query": "pro"})
            return first, second

    first, second = run(go)
    ok(first)
    assert "revoked" in err(second)


def test_setup_tools_are_owner_only(seeded, tokens):
    for level in ("default", "confidential"):
        server = build_server(seeded, Caller(tokens[level]))
        message = err(run(lambda s=server: call(s, "token_create", {"name": "x", "level": "owner"})))
        assert f"{level} access level" in message
    assert len(seeded.tokens.list()) == 3


def test_owner_cannot_propose_but_agents_can(seeded, tokens):
    owner = build_server(seeded, Caller(tokens["owner"]))
    assert "can't use propose_fact" in err(
        run(lambda: call(owner, "propose_fact", {"fact": "x", "quote": "y"}))
    )
    agent = build_server(seeded, Caller(tokens["default"]))
    out = ok(
        run(lambda: call(agent, "propose_fact", {"fact": "Pro is $29 now", "quote": "user: make it $29"}))
    )
    assert out["status"] == "waiting for approval"
    item = seeded.state.get_review_item(out["review_id"])
    assert item["kind"] == "agent_proposal" and item["payload"]["fact"] == "Pro is $29 now"


def test_proposals_can_be_turned_off(seeded, tokens):
    seeded.config.agent_proposals = False
    agent = build_server(seeded, Caller(tokens["default"]))
    assert "turned off" in err(run(lambda: call(agent, "propose_fact", {"fact": "x", "quote": "y"})))


def test_owner_manages_tokens_and_reviews(seeded, tokens):
    owner = build_server(seeded, Caller(tokens["owner"]))
    created = ok(run(lambda: call(owner, "token_create", {"name": "cursor", "level": "default"})))
    assert created["token"].startswith("zs_")
    listed = ok(run(lambda: call(owner, "token_list")))["tokens"]
    assert "cursor" in [t["name"] for t in listed] and all("token" not in t for t in listed)
    assert ok(run(lambda: call(owner, "token_revoke", {"id": created["id"]}))) == {"revoked": created["id"]}
    own_id = seeded.tokens.verify(tokens["owner"]).id
    assert "can't revoke the token you're using" in err(
        run(lambda: call(owner, "token_revoke", {"id": own_id}))
    )

    item_id = seeded.state.add_review_item("agent_proposal", {"fact": "f", "quote": "q"})
    assert [i["id"] for i in ok(run(lambda: call(owner, "review_list")))["items"]] == [item_id]
    assert (
        ok(run(lambda: call(owner, "review_decide", {"id": item_id, "approve": False})))["status"]
        == "rejected"
    )
    assert "no pending" in err(run(lambda: call(owner, "review_decide", {"id": item_id, "approve": True})))


def test_whats_changed_filters_housekeeping_and_confidential(seeded, tokens):
    seeded.state.audit("document_updated", doc_id="gdrive:pricing", detail="Pro $20 -> $29", confidence=0.97)
    seeded.state.audit("document_updated", doc_id="gdrive:salaries", detail="bands", confidential=True)
    default = build_server(seeded, Caller(tokens["default"]))
    changes = ok(run(lambda: call(default, "whats_changed", {"since": "7d"})))["changes"]
    assert [c["doc_id"] for c in changes] == ["gdrive:pricing"]  # no token_created rows, no confidential
    owner = build_server(seeded, Caller(tokens["owner"]))
    assert len(ok(run(lambda: call(owner, "whats_changed", {"since": "2000-01-01"})))["changes"]) == 2
    assert "couldn't read" in err(run(lambda: call(owner, "whats_changed", {"since": "last tuesday"})))


def test_stdio_style_server_lists_only_allowed_tools(seeded, tokens):
    server = build_server(seeded, Caller(tokens["default"]), only_level=AccessLevel.DEFAULT)

    async def names():
        async with Client(server) as client:
            return {t.name for t in (await client.list_tools()).tools}

    assert run(names) == tools_for(AccessLevel.DEFAULT)
    assert "token_create" not in run(names)


def test_every_call_is_logged(seeded, tokens):
    server = build_server(seeded, Caller(tokens["default"]))
    run(lambda: call(server, "search", {"query": "pro plan"}))
    run(lambda: call(server, "token_list"))
    rows = [
        tuple(r) for r in seeded.state.query("SELECT action, detail, allowed FROM access_log ORDER BY id")
    ]
    assert rows == [("search", "pro plan", 1), ("token_list", None, 0)]


def test_parse_since():
    from datetime import datetime, timezone

    now = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)
    assert parse_since("7d", now) == "2026-10-06T12:00:00+00:00"
    assert parse_since("24h", now) == "2026-10-12T12:00:00+00:00"
    assert parse_since("2026-10-01") == "2026-10-01T00:00:00+00:00"
    assert parse_since("2026-10-01T08:00:00Z") == "2026-10-01T08:00:00+00:00"
    assert parse_since("2026-10-01T10:00:00+02:00") == "2026-10-01T08:00:00+00:00"
