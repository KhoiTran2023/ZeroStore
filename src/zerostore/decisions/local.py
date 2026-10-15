"""A small reranker that runs on your CPU (fastembed cross-encoder).

It only answers `score`: no key, no cost, nothing leaves the machine. It
can't follow a steering rule, so the rule is ignored. Install with
`pip install 'zerostore[local]'`.
"""

from __future__ import annotations

import math

from zerostore.decisions.base import Candidate, DecisionEngine, DeletedFile, Pick, Unsupported, YesNo

DEFAULT_MODEL = "Xenova/ms-marco-MiniLM-L-6-v2"


class LocalReranker(DecisionEngine):
    name = "local"
    label = "the local reranker"

    def __init__(self, model: str = DEFAULT_MODEL, encoder=None):
        if encoder is None:
            try:
                from fastembed.rerank.cross_encoder import TextCrossEncoder
            except ImportError as exc:  # pragma: no cover - depends on optional extra
                raise RuntimeError(
                    "the local reranker needs the optional extra: pip install 'zerostore[local]'"
                ) from exc
            encoder = TextCrossEncoder(model_name=model)
        self._encoder = encoder

    def score(self, question: str, facts: list[str], rule: str) -> list[float]:
        if not facts:
            return []
        # Cross-encoders return raw relevance logits; a sigmoid maps them onto 0-1.
        return [1.0 / (1.0 + math.exp(-float(x))) for x in self._encoder.rerank(question, facts)]

    def keep(self, texts: list[str]) -> list[YesNo]:
        raise Unsupported("the local reranker only scores search results")

    def route(self, texts: list[str], topics: list[str]) -> list[Pick]:
        raise Unsupported("the local reranker only scores search results")

    def compare(self, pairs: list[tuple[str, str]]) -> list[Pick]:
        raise Unsupported("the local reranker only scores search results")

    def on_delete(self, deleted: DeletedFile, candidates: list[Candidate]) -> Pick:
        raise Unsupported("the local reranker only scores search results")
