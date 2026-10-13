from __future__ import annotations

import pytest

from zerostore.app import ZeroStore
from zerostore.config import parse_config
from zerostore.store.embeddings import HashingEmbedder
from zerostore.store.facts import Chunk


@pytest.fixture
def config(tmp_path):
    return parse_config(
        {
            "sources": {},
            "servers": {"vectordb": str(tmp_path / "facts.lance"), "statedb": str(tmp_path / "state.sqlite")},
        }
    )


@pytest.fixture
def zs(config):
    store = ZeroStore.open(config, embedder=HashingEmbedder(64))
    yield store
    store.close()


def pricing_doc(price: str = "$20", updated: str = "2026-10-01") -> list[Chunk]:
    return [
        Chunk(
            doc_id="gdrive:pricing",
            source="google_drive",
            title="Pricing 2026",
            text=f"The Pro plan costs {price} per month.",
            source_url="https://docs.google.com/document/d/pricing",
            updated_at=updated,
            ordinal=0,
            topics=["pricing"],
        ),
        Chunk(
            doc_id="gdrive:pricing",
            source="google_drive",
            title="Pricing 2026",
            text="Enterprise plans are priced per contract.",
            source_url="https://docs.google.com/document/d/pricing",
            updated_at=updated,
            ordinal=1,
            topics=["pricing"],
        ),
    ]


def salary_doc() -> list[Chunk]:
    return [
        Chunk(
            doc_id="gdrive:salaries",
            source="google_drive",
            title="Salary bands",
            text="The Pro engineer salary band starts at $150,000.",
            source_url="https://docs.google.com/document/d/salaries",
            updated_at="2026-09-01",
            topics=["hr"],
            confidential=True,
        )
    ]


@pytest.fixture
def seeded(zs):
    zs.facts.replace_document("gdrive:pricing", pricing_doc())
    zs.facts.replace_document("gdrive:salaries", salary_doc())
    return zs
