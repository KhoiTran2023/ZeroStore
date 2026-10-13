"""Wires the configured pieces together: config, state, tokens, fact store."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from zerostore.access.tokens import TokenStore
from zerostore.config import Config
from zerostore.store.embeddings import Embedder, FastEmbedEmbedder, HashingEmbedder
from zerostore.store.facts import FactStore
from zerostore.store.state import StateDB

log = logging.getLogger("zerostore")


def make_embedder(config: Config, quiet: bool = False) -> Embedder:
    """Pick the embedder for this install.

    The provider embedder (your AI account) lands with the LLM decision engine.
    Until then, and for providers without one, the local fastembed model is
    used when installed, and the hashing embedder otherwise.
    """
    try:
        return FastEmbedEmbedder()
    except RuntimeError:
        if not quiet:
            log.warning(
                "No embedding model is installed, so search falls back to word matching. "
                "For better results: pip install 'zerostore[local]'"
            )
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
        facts = FactStore(config.servers.vectordb_uri(), embedder or make_embedder(config, quiet=quiet))
        return cls(config=config, state=state, tokens=TokenStore(state), facts=facts)

    def close(self) -> None:
        self.state.close()
