"""Embeddings: your provider's service, the local model, and keeping a store's own embedder."""

from __future__ import annotations

import json
import logging

import httpx
import pytest

from zerostore.app import ZeroStore, embedder_from_name, make_embedder, planned_embedder
from zerostore.config import parse_config
from zerostore.store.embeddings import (
    EmbeddingError,
    EmbeddingUnavailable,
    FastEmbedEmbedder,
    HashingEmbedder,
    ProviderEmbedder,
)


def fake_embeddings(dim=3, status=200):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append({"url": str(request.url), "headers": dict(request.headers), "body": body})
        if status != 200:
            return httpx.Response(status)
        data = [{"index": i, "embedding": [float(len(t))] * dim} for i, t in enumerate(body["input"])]
        return httpx.Response(200, json={"data": list(reversed(data))})

    return handler, calls


def test_openai_embeddings_batch_and_keep_order():
    handler, calls = fake_embeddings()
    client = httpx.Client(transport=httpx.MockTransport(handler))
    emb = ProviderEmbedder("openai", "sk-test", client=client)
    assert emb.name == "openai:text-embedding-3-small" and emb.dim == 1536  # known size: no probe call
    texts = [f"text {'x' * i}" for i in range(100)]
    vectors = emb.embed(texts)
    assert [v[0] for v in vectors] == [float(len(t)) for t in texts]
    assert len(calls) == 2 and calls[0]["url"] == "https://api.openai.com/v1/embeddings"
    assert calls[0]["headers"]["authorization"] == "Bearer sk-test"


def test_ollama_embeddings_probe_their_size():
    handler, calls = fake_embeddings(dim=768)
    emb = ProviderEmbedder("ollama", None, client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert emb.dim == 768 and calls[0]["url"] == "http://localhost:11434/v1/embeddings"


def test_embedding_errors():
    handler, _ = fake_embeddings(status=401)
    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(EmbeddingError, match="401"):
        ProviderEmbedder("openai", "k", client=client, retries=0).embed(["x"])
    with pytest.raises(EmbeddingError, match="no embedding service"):
        ProviderEmbedder("anthropic", "k")


def cfg(tmp_path, ai=None):
    return parse_config(
        {
            "sources": {},
            "ai": ai or {},
            "servers": {"vectordb": str(tmp_path / "f.lance"), "statedb": str(tmp_path / "s.sqlite")},
        }
    )


def test_which_embedder_a_config_asks_for(tmp_path, monkeypatch):
    monkeypatch.setattr("importlib.util.find_spec", lambda name: None)
    assert planned_embedder(cfg(tmp_path)) == "hashing-256"
    openai = cfg(tmp_path, {"llm_provider": "openai", "llm_api_key": "k"})
    assert planned_embedder(openai) == "openai:text-embedding-3-small"
    # Anthropic has no embedding service: local model, or hashing without one.
    assert planned_embedder(cfg(tmp_path, {"llm_provider": "anthropic", "llm_api_key": "k"})) == "hashing-256"
    assert planned_embedder(
        cfg(tmp_path, {"llm_provider": "openai", "llm_api_key": "k", "embeddings": "local"})
    ) == ("hashing-256")
    monkeypatch.setattr("importlib.util.find_spec", lambda name: object())
    assert planned_embedder(cfg(tmp_path)).startswith("fastembed:")


def test_a_store_keeps_its_embedder_until_reindex(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr("importlib.util.find_spec", lambda name: None)
    first = ZeroStore.open(cfg(tmp_path))
    assert first.facts.embedder.name == "hashing-256"
    first.close()
    # An OpenAI key is added later: the existing store keeps working with its own embedder.
    with caplog.at_level(logging.WARNING):
        later = ZeroStore.open(cfg(tmp_path, {"llm_provider": "openai", "llm_api_key": "k"}))
    assert later.facts.embedder.name == "hashing-256"
    assert "zerostore reindex" in caplog.text
    later.close()


def test_embedder_from_name():
    config = parse_config({"sources": {}})
    assert isinstance(embedder_from_name("hashing-64", config), HashingEmbedder)
    with pytest.raises(RuntimeError, match="needs ai.llm_provider: openai"):
        embedder_from_name("openai:text-embedding-3-small", config)
    with pytest.raises(RuntimeError, match="unknown embedder"):
        embedder_from_name("word2vec", config)
    assert make_embedder(config, quiet=True, built_with="hashing-64").dim == 64


class _BlockedTextEmbedding:
    """fastembed when the model can't be downloaded (offline, or Hugging Face blocked)."""

    def __init__(self, model_name):
        raise ValueError(f"Could not load model {model_name} from any source.")


@pytest.fixture
def blocked_fastembed(monkeypatch):
    import sys
    import types

    module = types.ModuleType("fastembed")
    module.TextEmbedding = _BlockedTextEmbedding
    monkeypatch.setitem(sys.modules, "fastembed", module)
    monkeypatch.setattr("importlib.util.find_spec", lambda name: object())


def test_a_local_model_that_cant_download_says_so_plainly(blocked_fastembed):
    with pytest.raises(EmbeddingUnavailable, match="huggingface.co") as caught:
        FastEmbedEmbedder()
    assert "ai.embeddings: basic" in str(caught.value)


def test_a_new_store_starts_with_word_matching_when_the_model_cant_download(
    tmp_path, blocked_fastembed, caplog
):
    with caplog.at_level(logging.WARNING):
        store = ZeroStore.open(cfg(tmp_path))
    assert store.facts.embedder.name == "hashing-256"
    assert "zerostore reindex" in caplog.text
    store.close()


def test_a_store_built_with_the_local_model_refuses_another(tmp_path, blocked_fastembed):
    # Its vectors came from that model, so word-matching vectors can't search them.
    with pytest.raises(EmbeddingUnavailable):
        make_embedder(cfg(tmp_path), quiet=True, built_with=f"fastembed:{FastEmbedEmbedder.DEFAULT_MODEL}")


def test_the_word_matching_note_doesnt_ask_for_a_key_you_added(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr("importlib.util.find_spec", lambda name: None)
    with caplog.at_level(logging.WARNING):
        make_embedder(cfg(tmp_path))
    assert "add an AI key" in caplog.text
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        make_embedder(cfg(tmp_path, {"llm_provider": "anthropic", "llm_api_key": "k"}))
    assert "anthropic has no embeddings" in caplog.text and "add an AI key" not in caplog.text
    caplog.clear()
    # A store kept on word matching after an OpenAI key was added gets only the reindex note.
    with caplog.at_level(logging.WARNING):
        make_embedder(cfg(tmp_path, {"llm_provider": "openai", "llm_api_key": "k"}), built_with="hashing-256")
    assert "zerostore reindex" in caplog.text and "has no embeddings" not in caplog.text
