"""Decision engine backends against fake APIs: Jev, your LLM, the local reranker, and engine selection."""

from __future__ import annotations

import json

import httpx
import pytest

from zerostore.config import parse_config
from zerostore.decisions.base import (
    COMPARE_OPTIONS,
    NO_TOPIC,
    Candidate,
    DeletedFile,
    EngineError,
    Pick,
    Unsupported,
    YesNo,
    topics_from,
)
from zerostore.decisions.jev import JevEngine
from zerostore.decisions.llm import LLMEngine, resolve_model
from zerostore.decisions.local import LocalReranker
from zerostore.decisions.select import build_engines, build_judge

TOPICS = ["pricing and plans", "engineering"]


def no_sleep(_):
    pass


# Shared answer types -----------------------------------------------------------


def test_answer_types():
    assert YesNo(0.9).yes and YesNo(0.9).confidence == pytest.approx(0.9)
    assert not YesNo(0.2).yes and YesNo(0.2).confidence == pytest.approx(0.8)
    pick = Pick("pricing and plans", {"pricing and plans": 0.6, "engineering": 0.3, NO_TOPIC: 0.1}, 0.5)
    assert topics_from(pick) == ["pricing and plans", "engineering"]
    assert topics_from(Pick(NO_TOPIC, {NO_TOPIC: 0.9}, 0.9)) == []


# Jev -----------------------------------------------------------------------------


class FakeJev:
    """Answers each Jev question by type, recording requests."""

    def __init__(self):
        self.requests: list[dict] = []
        self.statuses: list[int] = []
        self.noul = 0.93
        self.choice: str | None = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.requests.append({"url": str(request.url), "headers": dict(request.headers), "body": body})
        if self.statuses:
            return httpx.Response(self.statuses.pop(0), json={"detail": "busy"})
        answers = {}
        for qid, q in body["questions"].items():
            if q["type"] == "noul":
                answers[qid] = {"type": "noul", "noul": self.noul}
            elif q["type"] == "choice":
                options = list(q["criteria"])
                choice = self.choice if self.choice in options else options[0]
                answers[qid] = {
                    "type": "choice",
                    "choice": choice,
                    "probabilities": {o: (0.8 if o == choice else 0.2 / (len(options) - 1)) for o in options},
                    "confidence": 0.74,
                }
            else:
                levels = len(q["criteria"])
                answers[qid] = {
                    "type": "score",
                    "score": 3.0 if "pro plan" in json.dumps(body["state"]).lower() else 0.5,
                    "legend": {str(i): t for i, t in enumerate(q["criteria"])},
                    "probabilities": {str(i): 1 / levels for i in range(levels)},
                    "confidence": 0.6,
                }
        return httpx.Response(200, json={"model": "jev-1.13.0", "answers": answers, "usage": {}})


def jev(fake, **kw):
    client = httpx.Client(transport=httpx.MockTransport(fake))
    return JevEngine("tsk_test", client=client, sleep=no_sleep, **kw)


def test_jev_request_shape_and_keep():
    fake = FakeJev()
    [answer] = jev(fake).keep(["The Pro plan costs $29."])
    assert answer.probability == pytest.approx(0.93)
    req = fake.requests[0]
    assert req["url"] == "https://api.typesafe.ai/v1/systemone"
    assert req["headers"]["authorization"] == "Bearer tsk_test"
    assert req["body"]["model"] == "jev-latest"
    assert jev(fake).cache_id == "jev:jev-latest" and jev(fake, model="jev-2").cache_id == "jev:jev-2"
    assert req["body"]["state"] == {"text": "The Pro plan costs $29."}
    assert req["body"]["questions"]["keep"]["type"] == "noul"
    assert "`text`" in req["body"]["questions"]["keep"]["instructions"]


def test_jev_classify_asks_keep_and_topic_in_one_request():
    fake = FakeJev()
    fake.choice = "pricing and plans"
    results = jev(fake).classify(["a", "b", "c"], TOPICS)
    assert len(fake.requests) == 3
    assert set(fake.requests[0]["body"]["questions"]) == {"keep", "topic"}
    criteria = fake.requests[0]["body"]["questions"]["topic"]["criteria"]
    assert list(criteria) == [*TOPICS, NO_TOPIC]
    keep, topic = results[0]
    assert keep.yes and topic.choice == "pricing and plans" and topic.confidence == pytest.approx(0.74)


def test_jev_compare_and_on_delete():
    fake = FakeJev()
    fake.choice = "changed"
    [pick] = jev(fake).compare([("Pro is $29", "Pro is $20")])
    assert pick.choice == "changed"
    body = fake.requests[0]["body"]
    assert body["state"] == {"NEW": "Pro is $29", "EXISTING": "Pro is $20"}
    assert list(body["questions"]["relation"]["criteria"]) == list(COMPARE_OPTIONS)

    fake.choice = "moved_to_0"
    pick = jev(fake).on_delete(
        DeletedFile("Pricing", "google_drive", "Company", "Pro is $29"),
        [Candidate("pricing.md", "github", "Pro is $29", True)],
    )
    assert pick.choice == "moved_to_0"
    state = fake.requests[-1]["body"]["state"]
    assert state["candidates"]["0"]["added_in_the_same_sync"] is True


def test_jev_scores_map_levels_onto_0_to_1():
    fake = FakeJev()
    scores = jev(fake).score(
        "pro plan price?", ["The Pro plan costs $29.", "Lunch at noon"], "Prefer current."
    )
    # Level 3 of 0..4 is 0.75; one request carries every fact as its own question.
    assert scores[0] == pytest.approx(0.75) and len(fake.requests) == 1
    body = fake.requests[0]["body"]
    assert set(body["questions"]) == {"f0", "f1"} and body["state"]["rule"] == "Prefer current."


def test_jev_retries_when_busy_then_succeeds():
    fake = FakeJev()
    fake.statuses = [529, 429]
    engine = jev(fake)
    assert engine.keep(["x"])[0].yes
    assert engine.calls == 3


def test_jev_errors_are_clear():
    fake = FakeJev()
    fake.statuses = [401]
    with pytest.raises(EngineError, match="refused the API key"):
        jev(fake).keep(["x"])
    fake.statuses = [529] * 4
    with pytest.raises(EngineError, match="529"):
        jev(fake, retries=3).keep(["x"])

    def unanswered(request):
        return httpx.Response(200, json={"answers": {}})

    with pytest.raises(EngineError, match="unanswered"):
        JevEngine("k", client=httpx.Client(transport=httpx.MockTransport(unanswered))).keep(["x"])

    def wrong_choice(request):
        return httpx.Response(200, json={"answers": {"relation": {"type": "choice", "choice": "maybe"}}})

    with pytest.raises(EngineError, match="wasn't one of the options"):
        JevEngine("k", client=httpx.Client(transport=httpx.MockTransport(wrong_choice))).compare([("a", "b")])
    with pytest.raises(EngineError, match="API key"):
        JevEngine("")


def test_jev_base_url_and_model_are_configurable():
    fake = FakeJev()
    jev(fake, base_url="https://openrouter.ai/api/", model="jev-1.13.0").keep(["x"])
    assert fake.requests[0]["url"] == "https://openrouter.ai/api/v1/systemone"
    assert fake.requests[0]["body"]["model"] == "jev-1.13.0"


# Your LLM --------------------------------------------------------------------------


class FakeLLM:
    """Answers structured-output calls in the Anthropic or OpenAI format."""

    def __init__(self, provider: str):
        self.provider = provider
        self.requests: list[dict] = []
        self.answer = None  # dict, or a function of the prompt
        self.status = 200

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.requests.append({"url": str(request.url), "headers": dict(request.headers), "body": body})
        if self.status != 200:
            return httpx.Response(self.status, json={"error": "nope"})
        prompt = body["messages"][-1]["content"]
        answer = self.answer(prompt) if callable(self.answer) else self.answer
        if self.provider == "anthropic":
            return httpx.Response(
                200, json={"content": [{"type": "tool_use", "name": "answer", "input": answer}]}
            )
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(answer)}}]})


def llm(fake, provider="anthropic", model="mid-tier", key="sk-test", **kw):
    client = httpx.Client(transport=httpx.MockTransport(fake))
    return LLMEngine(provider, model, key, client=client, sleep=no_sleep, **kw)


def test_llm_anthropic_classify_batches_and_forces_the_answer_tool():
    fake = FakeLLM("anthropic")

    def answer(prompt):
        n = prompt.count("<item id=")
        return {
            "answers": [
                {
                    "id": i,
                    "keep_probability": 0.1 if i == 1 else 0.9,
                    "topic": "engineering",
                    "topic_confidence": 0.8,
                }
                for i in range(n)
            ]
        }

    fake.answer = answer
    engine = llm(fake)
    results = engine.classify(["Deploys run on Fridays.", "Lunch?"], TOPICS)
    assert [k.yes for k, _ in results] == [True, False]
    assert results[0][1].choice == "engineering"
    req = fake.requests[0]
    assert req["url"] == "https://api.anthropic.com/v1/messages"
    assert req["headers"]["x-api-key"] == "sk-test" and req["headers"]["anthropic-version"]
    assert req["body"]["model"] == "claude-sonnet-5-5"
    assert req["body"]["tool_choice"] == {"type": "tool", "name": "answer"}
    enum = req["body"]["tools"][0]["input_schema"]["properties"]["answers"]["items"]["properties"]["topic"][
        "enum"
    ]
    assert enum == [*TOPICS, NO_TOPIC]
    assert engine.label == "your LLM (claude-sonnet-5-5)"
    assert engine.cache_id == "llm:anthropic:claude-sonnet-5-5"
    assert llm(
        FakeLLM("openrouter"), provider="openrouter", model="anthropic/claude-sonnet-5-5"
    ).cache_id == ("llm:openrouter:anthropic/claude-sonnet-5-5")


def test_llm_splits_large_batches():
    fake = FakeLLM("anthropic")
    fake.answer = lambda prompt: {
        "answers": [
            {"id": i, "keep_probability": 0.9, "topic": NO_TOPIC, "topic_confidence": 0.5}
            for i in range(prompt.count("<item id="))
        ]
    }
    assert len(llm(fake).classify([f"t{i}" for i in range(20)], TOPICS)) == 20
    assert len(fake.requests) == 2


def test_llm_openai_compatible_uses_json_schema():
    fake = FakeLLM("openai")
    fake.answer = {"answers": [{"id": 0, "relation": "changed", "confidence": 0.88}]}
    [pick] = llm(fake, provider="openai").compare([("Pro is $29", "Pro is $20")])
    assert pick.choice == "changed" and pick.confidence == pytest.approx(0.88)
    req = fake.requests[0]
    assert req["url"] == "https://api.openai.com/v1/chat/completions"
    assert req["headers"]["authorization"] == "Bearer sk-test"
    assert req["body"]["response_format"]["type"] == "json_schema"
    assert req["body"]["response_format"]["json_schema"]["strict"] is True


def test_llm_on_delete_and_score():
    fake = FakeLLM("gemini")
    fake.answer = {"action": "moved_to_0", "confidence": 0.9}
    pick = llm(fake, provider="gemini").on_delete(
        DeletedFile("Pricing", "google_drive", "Company", "Pro is $29"),
        [Candidate("p.md", "github", "x", True)],
    )
    assert pick.choice == "moved_to_0"
    assert fake.requests[0]["url"].startswith("https://generativelanguage.googleapis.com/v1beta/openai/")
    fake.answer = {"answers": [{"id": 0, "score": 0.9}, {"id": 1, "score": 1.7}]}
    assert llm(fake, provider="gemini").score("q", ["a", "b"], "rule") == [0.9, 1.0]


def test_llm_problems_are_engine_errors():
    fake = FakeLLM("anthropic")
    fake.answer = {"answers": [{"id": 0, "relation": "changed", "confidence": 0.9}]}
    with pytest.raises(EngineError, match="skipped 1 of 2"):
        llm(fake).compare([("a", "b"), ("c", "d")])
    fake.answer = {"answers": [{"id": 0, "relation": "sideways", "confidence": 0.9}]}
    with pytest.raises(EngineError, match="wasn't one of the options"):
        llm(fake).compare([("a", "b")])
    fake.status = 401
    with pytest.raises(EngineError, match="refused the key"):
        llm(fake).compare([("a", "b")])
    with pytest.raises(EngineError, match="needs a key"):
        LLMEngine("openai", "mid-tier", None)


def test_ollama_runs_without_a_key_but_needs_a_model_name():
    with pytest.raises(EngineError, match="name a model"):
        resolve_model("ollama", "mid-tier")
    fake = FakeLLM("ollama")
    fake.answer = {"answers": [{"id": 0, "score": 0.4}]}
    engine = llm(fake, provider="ollama", model="llama3.3", key=None)
    assert engine.score("q", ["a"], "rule") == [0.4]
    assert fake.requests[0]["url"] == "http://localhost:11434/v1/chat/completions"
    assert "authorization" not in fake.requests[0]["headers"]


# Local reranker --------------------------------------------------------------------


class FakeCrossEncoder:
    def rerank(self, query, documents):
        return [4.0 if "pro" in d.lower() else -4.0 for d in documents]


def test_local_reranker_scores_only():
    engine = LocalReranker(encoder=FakeCrossEncoder())
    high, low = engine.score("pro price", ["Pro plan is $29", "Lunch"], "ignored")
    assert high > 0.95 and low < 0.05
    assert engine.score("q", [], "r") == []
    with pytest.raises(Unsupported):
        engine.keep(["x"])


# Which engine answers what -------------------------------------------------------


def cfg(ai=None, retrieval=None):
    return parse_config({"sources": {}, "ai": ai or {}, "retrieval": retrieval or {}})


def test_selection_prefers_jev_then_llm_then_nothing(monkeypatch):
    sentinel = object()
    monkeypatch.setattr("zerostore.decisions.select.make_local", lambda: sentinel)
    both = cfg({"jev_api_key": "j", "llm_provider": "anthropic", "llm_api_key": "a"})
    engines = build_engines(both)
    assert engines.judge.name == "jev" and engines.narrow is engines.judge and engines.rerank is engines.judge
    assert (
        build_judge(
            cfg({"decision_engine": "llm", "jev_api_key": "j", "llm_provider": "openai", "llm_api_key": "o"})
        ).name
        == "llm"
    )
    assert build_judge(cfg({"llm_provider": "ollama", "llm_model": "llama3.3"})).name == "llm"
    nothing = build_engines(cfg())
    assert nothing.judge is None and nothing.narrow is None and nothing.rerank is sentinel


def test_selection_per_step(monkeypatch):
    sentinel = object()
    monkeypatch.setattr("zerostore.decisions.select.make_local", lambda: sentinel)
    engines = build_engines(
        cfg(
            {"jev_api_key": "j", "llm_provider": "anthropic", "llm_api_key": "a"},
            {"narrow_with": "llm", "rerank_with": "local"},
        )
    )
    assert engines.judge.name == "jev" and engines.narrow.name == "llm" and engines.rerank is sentinel
    off = build_engines(cfg({"jev_api_key": "j"}, {"narrow_with": "off", "rerank_with": "off"}))
    assert off.narrow is None and off.rerank is None


def test_settings_refuse_an_engine_without_its_key():
    from zerostore.config import ConfigError

    with pytest.raises(ConfigError, match="jev_api_key"):
        cfg({}, {"rerank_with": "jev"})
    with pytest.raises(ConfigError, match="llm_provider"):
        cfg({"llm_provider": "openai"}, {"narrow_with": "llm"})
    with pytest.raises(ConfigError, match="llm_provider must be one of"):
        cfg({"llm_provider": "skynet"})


def test_unreadable_numbers_are_engine_errors_not_crashes():
    fake = FakeLLM("anthropic")
    fake.answer = {
        "answers": [{"id": 0, "keep_probability": None, "topic": "engineering", "topic_confidence": 0.8}]
    }
    with pytest.raises(EngineError, match="where a number was expected"):
        llm(fake).classify(["x"], TOPICS)
    fake.answer = {"answers": [{"id": 0, "relation": ["changed"], "confidence": 0.9}]}
    with pytest.raises(EngineError, match="wasn't one of the options"):
        llm(fake).compare([("a", "b")])

    def null_noul(request):
        return httpx.Response(200, json={"answers": {"keep": {"type": "noul", "noul": None}}})

    with pytest.raises(EngineError):
        JevEngine("k", client=httpx.Client(transport=httpx.MockTransport(null_noul))).keep(["x"])
