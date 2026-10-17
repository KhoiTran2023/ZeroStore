"""The retrieval layer: from an agent's question to a few cited facts, or "not sure".

1. The caller's access level (checked before this runs) sets which facts may be seen.
2. Narrow it down: `route` picks the likely topics, used as a pre-filter. When
   the engine isn't confident, everything is searched, so a wrong guess never
   hides the answer.
3. Search: hybrid vector + keyword search over the allowed facts.
4. Rerank: `score` each candidate, following the steering rule.
5. Good enough? If the best score is below `retrieval.answer_cutoff`, answer
   "not sure" instead of returning weak facts.
6. Answer: the top `retrieval.max_facts` facts, each with its source and date.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from zerostore.app import ZeroStore
from zerostore.decisions.base import NO_TOPIC, EngineError, topics_from
from zerostore.decisions.select import Engines, build_engines

log = logging.getLogger("zerostore.retrieval")

NARROW_AT = 0.60  # route confidence needed before the search is narrowed
POOL = 4  # candidates fetched per fact returned


@dataclass
class Answer:
    facts: list[dict[str, Any]] = field(default_factory=list)
    not_sure: bool = False
    topics: list[str] | None = None  # what the search was narrowed to, if anything
    reranked_by: str | None = None
    problems: list[str] = field(default_factory=list)


def rerank_text(row: dict[str, Any]) -> str:
    """What the reranker sees: the document, its date and the chunk, so it can tell old from new."""
    return f"Document: {row['title']} (updated {row['updated_at'] or 'unknown'})\n{row['text']}"


class Retriever:
    def __init__(self, zs: ZeroStore, engines: Engines | None = None):
        self.zs = zs
        self._engines = engines
        self._lock = threading.Lock()

    @property
    def engines(self) -> Engines:
        # Built on first use: the local reranker loads a model, which `serve` shouldn't wait for.
        with self._lock:
            if self._engines is None:
                self._engines = build_engines(self.zs.config)
            return self._engines

    def answer(self, question: str, *, include_confidential: bool, hidden_tags: Sequence[str] = ()) -> Answer:
        settings = self.zs.config.retrieval
        engines = self.engines
        out = Answer()
        pool = max(settings.max_facts * POOL, 20)

        if engines.narrow is not None:
            try:
                pick = engines.narrow.route([question], settings.topics)[0]
                if pick.choice != NO_TOPIC and pick.confidence >= NARROW_AT:
                    out.topics = topics_from(pick) or None
            except EngineError as exc:
                out.problems.append(f"narrowing skipped: {exc}")

        def search(topics: list[str] | None) -> list[dict[str, Any]]:
            return self.zs.facts.search(
                question,
                include_confidential=include_confidential,
                hidden_tags=hidden_tags,
                topics=topics,
                limit=pool,
            )

        candidates = search(out.topics)
        if out.topics and not candidates:
            out.topics, candidates = None, search(None)

        if engines.rerank is None or not candidates:
            out.facts = candidates[: settings.max_facts]
            out.not_sure = not out.facts
            return out

        try:
            scored = self._rerank(question, candidates)
            if out.topics and scored[0]["score"] < settings.answer_cutoff:
                # The narrowed search found nothing good; the answer may sit under another topic.
                seen = {row["id"] for row in candidates}
                extra = [row for row in search(None) if row["id"] not in seen]
                if extra:
                    scored = sorted(scored + self._rerank(question, extra), key=lambda r: -r["score"])
                out.topics = None
        except EngineError as exc:
            out.problems.append(f"reranking skipped: {exc}")
            out.facts = candidates[: settings.max_facts]
            return out

        out.reranked_by = engines.rerank.label
        if scored[0]["score"] < settings.answer_cutoff:
            out.not_sure = True
            return out
        out.facts = scored[: settings.max_facts]
        return out

    def _rerank(self, question: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        rule = self.zs.config.retrieval.rerank_rule
        scores = self.engines.rerank.score(question, [rerank_text(r) for r in rows], rule)  # type: ignore[union-attr]
        if len(scores) != len(rows):
            raise EngineError("the reranker returned the wrong number of scores")
        return sorted(
            ({**r, "score": s} for r, s in zip(rows, scores, strict=True)), key=lambda r: -r["score"]
        )
