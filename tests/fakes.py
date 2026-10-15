"""Test doubles: a connector that serves documents from a dict, and a scripted decision engine."""

from __future__ import annotations

import re
from collections import Counter

from zerostore.connectors.base import Connector, ConnectorError, ItemRef, SourceDocument
from zerostore.decisions.base import (
    NO_TOPIC,
    Candidate,
    DecisionEngine,
    DeletedFile,
    EngineError,
    Pick,
    YesNo,
)


class FakeConnector(Connector):
    def __init__(self, name: str = "google_drive"):
        self.name = name
        self.docs: dict[str, SourceDocument] = {}
        self.fetched: list[str] = []
        self.fail_listing = False
        self.fail_fetch: set[str] = set()
        self.closed = False

    def put(self, key, title, text, *, version="v1", location="Company", labels=(), updated="2026-10-01"):
        self.docs[key] = SourceDocument(
            doc_id=f"{self.name}:{key}",
            source=self.name,
            title=title,
            text=text,
            url=f"https://example.com/{key}",
            updated_at=updated,
            version=version,
            location=location,
            labels=list(labels),
        )

    def list_items(self):
        if self.fail_listing:
            raise ConnectorError("source unreachable")
        return [ItemRef(d.doc_id, d.version, d.title, {"key": k}) for k, d in self.docs.items()]

    def fetch(self, ref):
        self.fetched.append(ref.extra["key"])
        if ref.extra["key"] in self.fail_fetch:
            raise ConnectorError("timeout")
        doc = self.docs[ref.extra["key"]]
        return doc if doc.text.strip() else None

    def close(self):
        self.closed = True


class FakeEngine(DecisionEngine):
    """A scripted decision engine: plain rules instead of a model, so tests read like stories.

    - keep: probability from the first `keep_rules` substring found, else `keep_default`.
    - route: topic from the first `topic_rules` substring found, else none.
    - compare: "changed" when both texts mention the same subject in `subjects`
      but differ, else "new"; confidence `compare_confidence`.
    - on_delete: `delete_answer` (choice, confidence), or a function of the inputs.
    - score: share of the question's words found in the fact.
    """

    name = "fake"
    label = "Jev"

    def __init__(self):
        self.keep_rules: dict[str, float] = {}
        self.keep_default = 0.97
        self.topic_rules = {
            "salary": "people and hiring",
            "plan": "pricing and plans",
            "price": "pricing and plans",
            "roadmap": "product and roadmap",
        }
        self.topic_confidence = 0.9
        self.subjects = ["pro plan", "launch date", "refund"]
        self.compare_confidence = 0.97
        self.delete_answer = ("retire", 0.95)
        self.calls: Counter[str] = Counter()
        self.seen_delete: list[tuple[DeletedFile, list[Candidate]]] = []
        self.fail: str | None = None

    def _check(self, kind: str) -> None:
        self.calls[kind] += 1
        if self.fail == kind or self.fail == "all":
            raise EngineError("the engine is down")

    def keep(self, texts):
        self._check("keep")
        out = []
        for text in texts:
            low = text.lower()
            prob = next((p for s, p in self.keep_rules.items() if s in low), self.keep_default)
            out.append(YesNo(prob))
        return out

    def route(self, texts, topics):
        self._check("route")
        out = []
        for text in texts:
            low = text.lower()
            topic = next((t for s, t in self.topic_rules.items() if s in low and t in topics), NO_TOPIC)
            out.append(Pick(topic, {topic: self.topic_confidence}, self.topic_confidence))
        return out

    def compare(self, pairs):
        self._check("compare")
        out = []
        for new, old in pairs:
            shared = any(s in new.lower() and s in old.lower() for s in self.subjects)
            label = "changed" if shared and new.split("\n")[-1] != old.split("\n")[-1] else "new"
            out.append(Pick(label, {label: self.compare_confidence}, self.compare_confidence))
        return out

    def on_delete(self, deleted, candidates):
        self._check("on_delete")
        self.seen_delete.append((deleted, candidates))
        answer = (
            self.delete_answer(deleted, candidates) if callable(self.delete_answer) else self.delete_answer
        )
        return Pick(answer[0], {answer[0]: answer[1]}, answer[1])

    def score(self, question, facts, rule):
        self._check("score")
        words = set(re.findall(r"[a-z0-9$]+", question.lower()))
        out = []
        for fact in facts:
            found = set(re.findall(r"[a-z0-9$]+", fact.lower()))
            out.append(len(words & found) / max(len(words), 1))
        return out
