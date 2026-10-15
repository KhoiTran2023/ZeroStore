"""The retrieval layer: narrowing, reranking and "not sure"."""

from __future__ import annotations

import pytest
from fakes import FakeEngine

from zerostore.decisions.select import Engines
from zerostore.librarian.retrieval import Retriever
from zerostore.store.facts import Chunk


def chunk(doc_id, title, text, topics=(), updated="2026-10-01", confidential=False, ordinal=0):
    return Chunk(
        doc_id=doc_id,
        source="google_drive",
        title=title,
        text=text,
        source_url=f"https://example.com/{doc_id}",
        updated_at=updated,
        ordinal=ordinal,
        topics=list(topics),
        confidential=confidential,
    )


@pytest.fixture
def store(zs):
    zs.facts.replace_document(
        "pricing",
        [chunk("pricing", "Pricing 2026", "The Pro plan costs $29 per month.", ["pricing and plans"])],
    )
    zs.facts.replace_document(
        "roadmap", [chunk("roadmap", "Roadmap", "The mobile app ships in Q1.", ["product and roadmap"])]
    )
    zs.facts.replace_document(
        "notes", [chunk("notes", "Old notes", "Untagged: the Pro plan has priority support.")]
    )
    zs.facts.replace_document(
        "salaries",
        [chunk("salaries", "Salary bands", "The Pro engineer band starts at $150,000.", confidential=True)],
    )
    return zs


@pytest.fixture
def engine():
    return FakeEngine()


def answer(zs, engines, question, confidential=False):
    return Retriever(zs, engines).answer(question, include_confidential=confidential)


def test_reranked_answers_come_best_first_with_scores(store, engine):
    out = answer(store, Engines(narrow=engine, rerank=engine), "what does the pro plan cost per month")
    assert not out.not_sure and out.reranked_by == "Jev"
    assert out.facts[0]["title"] == "Pricing 2026"
    assert out.facts[0]["score"] >= out.facts[-1]["score"]
    assert out.topics == ["pricing and plans"]


def test_narrowing_keeps_untagged_facts(store, engine):
    out = answer(store, Engines(narrow=engine), "pro plan support")
    titles = {f["title"] for f in out.facts}
    assert "Old notes" in titles and "Roadmap" not in titles


def test_low_confidence_routing_searches_everything(store, engine):
    engine.topic_confidence = 0.4
    out = answer(store, Engines(narrow=engine), "pro plan")
    assert out.topics is None


def test_not_sure_below_the_cutoff(store, engine):
    out = answer(store, Engines(rerank=engine), "who is the head of legal in berlin")
    assert out.not_sure and out.facts == []


def test_a_wrong_topic_guess_never_hides_the_answer(store, engine):
    # Routing confidently picks pricing, but the answer is on the roadmap.
    engine.topic_rules = {"ships": "pricing and plans"}
    out = answer(store, Engines(narrow=engine, rerank=engine), "when the mobile app ships")
    assert not out.not_sure and out.facts[0]["title"] == "Roadmap"
    assert out.topics is None


def test_access_level_still_applies(store, engine):
    out = answer(store, Engines(rerank=engine), "pro engineer band", confidential=False)
    assert all(f["title"] != "Salary bands" for f in out.facts)
    out = answer(store, Engines(rerank=engine), "pro engineer band", confidential=True)
    assert out.facts[0]["title"] == "Salary bands"


def test_superseded_facts_are_not_served(store, engine):
    store.facts.supersede([chunk("notes", "", "").id], "pricing")
    out = answer(store, Engines(rerank=engine), "pro plan priority support")
    assert all(f["title"] != "Old notes" for f in out.facts)


def test_a_failing_reranker_falls_back_to_search_order(store, engine):
    engine.fail = "score"
    out = answer(store, Engines(rerank=engine), "pro plan cost")
    assert out.facts and "reranking skipped" in out.problems[0]


def test_a_failing_router_searches_everything(store, engine):
    engine.fail = "route"
    out = answer(store, Engines(narrow=engine), "pro plan cost")
    assert out.facts and out.topics is None and "narrowing skipped" in out.problems[0]


def test_without_engines_search_works_as_before(store):
    out = answer(store, Engines(), "pro plan cost")
    assert out.facts and out.reranked_by is None and "score" not in out.facts[0]
    assert answer(store, Engines(), "   ").not_sure


def test_the_reranker_sees_document_dates(store, engine):
    seen = []
    original = engine.score

    def spy(question, facts, rule):
        seen.extend(facts)
        return original(question, facts, rule)

    engine.score = spy
    answer(store, Engines(rerank=engine), "pro plan cost")
    assert any(f.startswith("Document: Pricing 2026 (updated 2026-10-01)") for f in seen)
