"""Agents proposing facts: the person approves in the agent app, or later in the review queue."""

from __future__ import annotations

import anyio
import mcp_types as types
import pytest
from fakes import FakeEngine
from mcp import Client

from zerostore.decisions.select import Engines
from zerostore.mcp.server import build_server


class Caller:
    def __init__(self, token):
        self.token = token

    def __call__(self, ctx):
        return self.token


def propose(server, args, elicitation=None):
    async def go():
        kwargs = {"elicitation_callback": elicitation} if elicitation else {}
        async with Client(server, **kwargs) as client:
            result = await client.call_tool("propose_fact", args)
            assert not result.is_error, result.content[0].text
            return result.structured_content

    return anyio.run(go)


def tool(server, name, args=None):
    async def go():
        async with Client(server) as client:
            result = await client.call_tool(name, args or {})
            assert not result.is_error, result.content[0].text
            return result.structured_content

    return anyio.run(go)


@pytest.fixture
def agent(zs):
    raw, info = zs.tokens.create("cursor-laptop", "default")
    return raw, info


FACT = {"fact": "Refunds are allowed within 30 days.", "quote": "user: let's make refunds 30 days"}


def searchable(zs, query="refund window days"):
    return " ".join(r["text"] for r in zs.facts.search(query, include_confidential=True))


def test_the_person_saves_it_in_the_agent_app(zs, agent):
    asked = []

    async def person(context, params):
        asked.append(params)
        return types.ElicitResult(
            action="accept", content={"fact": params.requested_schema["properties"]["fact"]["default"]}
        )

    out = propose(build_server(zs, Caller(agent[0])), FACT, person)
    assert out["status"] == "saved"
    assert "Save this to company facts?" in asked[0].message and "30 days" in asked[0].message
    decisions = zs.config.decisions_path().read_text()
    assert (
        "Refunds are allowed within 30 days." in decisions and "the person using cursor-laptop" in decisions
    )
    assert "30 days" in searchable(zs)
    assert zs.state.review_items("pending") == []


def test_the_person_can_edit_before_saving(zs, agent):
    async def person(context, params):
        return types.ElicitResult(action="accept", content={"fact": "Refunds are allowed within 14 days."})

    propose(build_server(zs, Caller(agent[0])), FACT, person)
    text = zs.config.decisions_path().read_text()
    assert "14 days" in text and "within 30 days" not in text


def test_declining_drops_it(zs, agent):
    async def person(context, params):
        return types.ElicitResult(action="decline")

    out = propose(build_server(zs, Caller(agent[0])), FACT, person)
    assert out["status"] == "not saved"
    assert not zs.config.decisions_path().exists()
    assert zs.state.get_review_item(out["review_id"])["status"] == "rejected"


def test_cancelling_or_no_prompt_support_leaves_it_for_review(zs, agent):
    async def person(context, params):
        return types.ElicitResult(action="cancel")

    out = propose(build_server(zs, Caller(agent[0])), FACT, person)
    assert out["status"] == "waiting for approval"
    out = propose(build_server(zs, Caller(agent[0])), FACT)  # this client can't show prompts
    assert out["status"] == "waiting for approval"
    assert len(zs.state.review_items("pending")) == 2


def test_auto_approve_tokens_skip_the_question(zs, agent):
    zs.tokens.set_auto_approve(agent[1].id, True)
    out = propose(build_server(zs, Caller(agent[0])), FACT)
    assert out["status"] == "saved"
    assert "auto-approve on token cursor-laptop" in zs.config.decisions_path().read_text()


def test_owner_approves_from_the_queue_with_new_wording(zs, agent):
    out = propose(build_server(zs, Caller(agent[0])), FACT)
    owner, _ = zs.tokens.create("khoi", "owner")
    server = build_server(zs, Caller(owner))
    [item] = tool(server, "review_list")["items"]
    assert item["kind"] == "agent_proposal" and item["proposed_by"] == "cursor-laptop"
    done = tool(
        server, "review_decide", {"id": out["review_id"], "approve": True, "fact": "Refunds: 21 days."}
    )
    assert done["status"] == "approved" and "agents can find it now" in done["result"]
    assert "21 days" in searchable(zs, "refunds days")
    # Approved facts are a real source: the next sync keeps them, and nobody judges them again.
    from zerostore.librarian.ingestion import Ingestor

    engine = FakeEngine()
    report = Ingestor(zs, judge=engine).sync()
    assert report.changes == [] and engine.calls["keep"] == 0


def test_owner_reviews_changes_over_mcp(zs):
    from fakes import FakeConnector

    from zerostore.config import GoogleDriveSource
    from zerostore.librarian.ingestion import Ingestor

    zs.config.sources.google_drive = GoogleDriveSource(folders=["Company"])
    drive, engine = FakeConnector(), FakeEngine()
    engine.keep_default = 0.6
    drive.put("pricing", "Pricing 2026", "The Pro plan costs $29 per month.")
    Ingestor(zs, lambda n, c: drive, judge=engine).sync()
    owner, _ = zs.tokens.create("khoi", "owner")
    server = build_server(zs, Caller(owner), engines=Engines())
    [item] = tool(server, "review_list")["items"]
    assert item["kind"] == "change" and item["document"] == "Pricing 2026" and item["confidence"] == 0.6
    done = tool(server, "review_decide", {"id": item["id"], "approve": True})
    assert done["result"] == "Pricing 2026 is live now"
    facts = tool(server, "search", {"query": "pro plan cost"})["facts"]
    assert "$29" in facts[0]["text"]


def test_search_returns_scores_when_reranked(seeded):
    raw, _ = seeded.tokens.create("a", "default")
    engine = FakeEngine()
    server = build_server(seeded, Caller(raw), engines=Engines(rerank=engine))
    facts = tool(server, "search", {"query": "pro plan per month"})["facts"]
    assert facts[0]["document"] == "Pricing 2026" and 0 < facts[0]["score"] <= 1
    out = tool(server, "search", {"query": "who runs the berlin office"})
    assert out["answer"] == "not sure"


def test_get_fact_flags_outdated_facts(seeded):
    from conftest import pricing_doc

    raw, _ = seeded.tokens.create("a", "default")
    fact_id = pricing_doc()[0].id
    seeded.facts.supersede([fact_id], "gdrive:pricing-2027")
    got = tool(build_server(seeded, Caller(raw), engines=Engines()), "get_fact", {"id": fact_id})
    assert got["status"].startswith("outdated")


def test_nobody_is_asked_when_the_token_may_not_propose(zs):
    owner, _ = zs.tokens.create("khoi", "owner")
    asked = []

    async def person(context, params):
        asked.append(params)
        return types.ElicitResult(action="accept", content={"fact": "x"})

    async def go():
        async with Client(build_server(zs, Caller(owner)), elicitation_callback=person) as client:
            return await client.call_tool("propose_fact", FACT)

    result = anyio.run(go)
    assert result.is_error and "can't use propose_fact" in result.content[0].text
    assert asked == [] and zs.state.review_items("pending") == []


def test_confidential_proposals_stay_confidential(zs):
    raw, _ = zs.tokens.create("finance-agent", "confidential")

    async def person(context, params):
        return types.ElicitResult(action="accept", content={"fact": "The Q3 bonus pool is $400k."})

    out = propose(build_server(zs, Caller(raw)), {"fact": "Q3 bonus pool", "quote": "q"}, person)
    assert out["status"] == "saved"
    assert not zs.config.decisions_path().exists()
    assert "$400k" in (zs.config.decisions_path().parent / "decisions-confidential.md").read_text()
    public = " ".join(r["text"] for r in zs.facts.search("bonus pool", include_confidential=False))
    private = " ".join(r["text"] for r in zs.facts.search("bonus pool", include_confidential=True))
    assert "$400k" not in public and "$400k" in private
