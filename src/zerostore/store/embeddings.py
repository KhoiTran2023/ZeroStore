"""Embeddings: turn text into vectors for the fact store.

v1 uses your AI provider's embedding service when it has one, and a small
local model (fastembed, CPU) when it doesn't. The provider embedder arrives
with the LLM decision engine; this module holds the interface, the local
model, and a dependency-free hashing embedder used by tests and the demo.
"""

from __future__ import annotations

import hashlib
import math
import re
from typing import Protocol, runtime_checkable

_WORD = re.compile(r"[a-z0-9$€£%.]+")


@runtime_checkable
class Embedder(Protocol):
    name: str
    dim: int

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class HashingEmbedder:
    """Deterministic bag-of-words vectors via feature hashing.

    It captures word overlap, not meaning, so it is only for tests, the
    offline demo, and machines with no model available. Keyword search in the
    fact store covers the same ground; real deployments use a model.
    """

    def __init__(self, dim: int = 256):
        self.dim = dim
        self.name = f"hashing-{dim}"

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._one(t) for t in texts]

    def _one(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for word in _WORD.findall(text.lower()):
            digest = hashlib.blake2b(word.encode(), digest_size=8).digest()
            bucket = int.from_bytes(digest[:4], "little") % self.dim
            sign = 1.0 if digest[4] & 1 else -1.0
            vec[bucket] += sign
        norm = math.sqrt(sum(v * v for v in vec))
        return [v / norm for v in vec] if norm else vec


class FastEmbedEmbedder:
    """Small local model on CPU. Install with `pip install 'zerostore[local]'`."""

    DEFAULT_MODEL = "BAAI/bge-small-en-v1.5"

    def __init__(self, model: str = DEFAULT_MODEL):
        try:
            from fastembed import TextEmbedding
        except ImportError as exc:  # pragma: no cover - depends on optional extra
            raise RuntimeError(
                "local embeddings need the optional extra: pip install 'zerostore[local]'"
            ) from exc
        self._model = TextEmbedding(model_name=model)
        self.name = f"fastembed:{model}"
        self.dim = len(next(iter(self._model.embed(["dimension probe"]))))

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [list(map(float, v)) for v in self._model.embed(texts)]
