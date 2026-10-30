"""Wires the configured pieces together: config, state, tokens, fact store."""

from __future__ import annotations

import importlib.util
import logging
from dataclasses import dataclass

from zerostore.access.tokens import TokenStore
from zerostore.config import Config
from zerostore.store.embeddings import (
    EMBEDDING_MODELS,
    Embedder,
    EmbeddingUnavailable,
    FastEmbedEmbedder,
    HashingEmbedder,
    ProviderEmbedder,
)
from zerostore.store.facts import FactStore
from zerostore.store.state import StateDB

log = logging.getLogger("zerostore")


def planned_embedder(config: Config) -> str:
    """The name of the embedder this config asks for, without starting it.

    Your AI provider's embedding service when it has one (and `ai.embeddings`
    is `provider`), otherwise the local fastembed model when installed, and the
    hashing embedder as a last resort.
    """
    ai = config.ai
    if ai.embeddings == "basic":
        return "hashing-256"
    if ai.embeddings == "provider" and ai.has_llm and ai.llm_provider in EMBEDDING_MODELS:
        return f"{ai.llm_provider}:{EMBEDDING_MODELS[ai.llm_provider]}"
    if importlib.util.find_spec("fastembed") is not None:
        return f"fastembed:{FastEmbedEmbedder.DEFAULT_MODEL}"
    return "hashing-256"


def embedder_from_name(name: str, config: Config) -> Embedder:
    kind, _, model = name.partition(":")
    if name.startswith("hashing-"):
        return HashingEmbedder(int(name.split("-", 1)[1]))
    if kind == "fastembed":
        return FastEmbedEmbedder(model)
    if kind in EMBEDDING_MODELS:
        ai = config.ai
        if ai.llm_provider != kind or not ai.has_llm:
            raise RuntimeError(f"the {name} embedder needs ai.llm_provider: {kind} with its key")
        return ProviderEmbedder(kind, ai.llm_api_key, model=model, base_url=ai.llm_base_url)
    raise RuntimeError(f"unknown embedder {name!r}")


def no_model_note(config: Config) -> str:
    """What to say when the store matches words because no embedding model is available."""
    ai = config.ai
    if ai.has_llm or ai.has_jev:
        who = ai.llm_provider if ai.has_llm else "Jev"
        return (
            f"Search finds facts by matching words and your AI ranks them ({who} has no embeddings). "
            "To search by meaning too: pip install 'zerostore[local]', then run `zerostore reindex`."
        )
    return (
        "No embedding model is set up, so search falls back to word matching. "
        "For better results add an AI key, or: pip install 'zerostore[local]'"
    )


def make_embedder(config: Config, quiet: bool = False, built_with: str | None = None) -> Embedder:
    """Start the embedder for this install.

    When the fact store was built with a different embedder than the config now
    asks for (say an AI key was added), keep using the store's embedder so
    nothing breaks, and say how to switch: `zerostore reindex`.
    """
    wanted = planned_embedder(config)
    name = wanted
    if built_with and built_with != wanted:
        name = built_with
        if not quiet:
            log.warning(
                "The fact store was built with the %s embedder; your settings now ask for %s. "
                "Still using %s. Run `zerostore reindex` to rebuild with %s.",
                built_with,
                wanted,
                built_with,
                wanted,
            )
    if name == "hashing-256" == wanted and config.ai.embeddings != "basic" and not quiet:
        log.warning(no_model_note(config))
    try:
        return embedder_from_name(name, config)
    except EmbeddingUnavailable as exc:
        if built_with is not None:
            raise  # the store's vectors came from this model; another one can't search them
        # A new, empty store: start with word matching rather than not at all.
        log.warning("%s. Using word matching for now; run `zerostore reindex` once it loads.", exc)
        return HashingEmbedder()


@dataclass
class ZeroStore:
    config: Config
    state: StateDB
    tokens: TokenStore
    facts: FactStore

    @classmethod
    def open(cls, config: Config, embedder: Embedder | None = None, quiet: bool = False) -> ZeroStore:
        state = StateDB(config.servers.statedb_path())
        uri = config.servers.vectordb_uri()
        if embedder is None:
            embedder = make_embedder(config, quiet=quiet, built_with=FactStore.built_with(uri))
        facts = FactStore(uri, embedder)
        return cls(config=config, state=state, tokens=TokenStore(state), facts=facts)

    def close(self) -> None:
        self.state.close()
