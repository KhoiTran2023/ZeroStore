"""Embeddings: turn text into vectors for the fact store.

v1 uses your AI provider's embedding service when it has one (OpenAI, or a
local Ollama), and a small local model (fastembed, CPU) when it doesn't. A
dependency-free hashing embedder covers tests, the demo and machines with
neither.
"""

from __future__ import annotations

import hashlib
import math
import re
import time
from typing import Protocol, runtime_checkable

import httpx

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


# Providers with an embedding service, and the model used for it.
EMBEDDING_MODELS = {"openai": "text-embedding-3-small", "ollama": "nomic-embed-text"}
_KNOWN_DIMS = {"text-embedding-3-small": 1536, "text-embedding-3-large": 3072}


class EmbeddingError(RuntimeError):
    pass


class ProviderEmbedder:
    """Your AI provider's embedding service (OpenAI-style `/embeddings`)."""

    BATCH = 96

    def __init__(
        self,
        provider: str,
        api_key: str | None,
        *,
        model: str | None = None,
        base_url: str | None = None,
        client: httpx.Client | None = None,
        retries: int = 3,
    ):
        from zerostore.decisions.llm import BASE_URLS

        if provider not in EMBEDDING_MODELS:
            raise EmbeddingError(f"{provider} has no embedding service ZeroStore can use")
        self.provider = provider
        self.model = model or EMBEDDING_MODELS[provider]
        self.name = f"{provider}:{self.model}"
        self.url = (base_url or BASE_URLS[provider]).rstrip("/") + "/embeddings"
        self.client = client or httpx.Client(timeout=60)
        if api_key:
            self.client.headers.update({"Authorization": f"Bearer {api_key}"})
        self.retries = retries
        self.dim = _KNOWN_DIMS.get(self.model) or len(self.embed(["dimension probe"])[0])

    def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), self.BATCH):
            out.extend(self._batch(texts[i : i + self.BATCH]))
        return out

    def _batch(self, texts: list[str]) -> list[list[float]]:
        for attempt in range(self.retries + 1):
            try:
                response = self.client.post(self.url, json={"model": self.model, "input": texts})
            except httpx.HTTPError as exc:
                if attempt < self.retries:
                    time.sleep(2**attempt)
                    continue
                raise EmbeddingError(f"{self.provider} embeddings could not be reached: {exc}") from exc
            if response.status_code in (429, 500, 502, 503, 504) and attempt < self.retries:
                time.sleep(2**attempt)
                continue
            if response.status_code >= 400:
                raise EmbeddingError(f"{self.provider} embeddings returned {response.status_code}")
            try:
                rows = sorted(response.json()["data"], key=lambda r: r["index"])
                vectors = [list(map(float, r["embedding"])) for r in rows]
            except (KeyError, TypeError, ValueError) as exc:
                raise EmbeddingError(f"{self.provider} sent embeddings ZeroStore couldn't read") from exc
            if len(vectors) != len(texts):
                raise EmbeddingError(
                    f"{self.provider} returned {len(vectors)} vectors for {len(texts)} texts"
                )
            return vectors
        raise AssertionError("unreachable")  # pragma: no cover
