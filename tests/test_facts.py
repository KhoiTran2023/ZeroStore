import pytest
from conftest import pricing_doc, salary_doc

from zerostore.store.embeddings import HashingEmbedder
from zerostore.store.facts import Chunk, FactStore


def texts(rows):
    return [r["text"] for r in rows]


def test_empty_store_returns_nothing(zs):
    assert zs.facts.search("anything", include_confidential=True) == []
    assert zs.facts.search("   ", include_confidential=True) == []


def test_confidential_chunks_are_filtered_before_search(seeded):
    public = seeded.facts.search("pro salary band", include_confidential=False)
    assert all(not r["confidential"] for r in public)
    assert "salary" not in " ".join(texts(public))
    everything = seeded.facts.search("pro salary band", include_confidential=True)
    assert any(r["confidential"] for r in everything)


def test_replacing_a_document_swaps_its_chunks(seeded):
    seeded.facts.replace_document("gdrive:pricing", pricing_doc("$29", "2026-10-12")[:1])
    rows = seeded.facts.search("pro plan cost per month", include_confidential=False)
    joined = " ".join(texts(rows))
    assert "$29" in joined and "$20" not in joined
    assert "Enterprise" not in joined  # the dropped chunk is gone too
    assert seeded.facts.count() == 2


def test_topic_filter(seeded):
    rows = seeded.facts.search("pro", include_confidential=True, topics=["hr"])
    assert {r["doc_id"] for r in rows} == {"gdrive:salaries"}


def test_get_respects_access(seeded):
    fact_id = salary_doc()[0].id
    assert seeded.facts.get(fact_id, include_confidential=False) is None
    assert seeded.facts.get(fact_id, include_confidential=True)["title"] == "Salary bands"
    assert "vector" not in seeded.facts.get(fact_id, include_confidential=True)


def test_sources_lists_documents(seeded):
    docs = seeded.facts.sources(include_confidential=False)
    assert [(d["doc_id"], d["chunks"]) for d in docs] == [("gdrive:pricing", 2)]
    assert len(seeded.facts.sources(include_confidential=True)) == 2


def test_quotes_in_ids_are_safe(zs):
    doc_id = "gdrive:it's ' OR 1=1 --"
    chunk = Chunk(doc_id, "google_drive", "Quote", "Quoted text here.", "u", "2026-01-01")
    zs.facts.replace_document(doc_id, [chunk])
    zs.facts.replace_document("other", [Chunk("other", "github", "O", "Other text.", "u", "2026-01-01")])
    zs.facts.delete_document(doc_id)
    assert [d["doc_id"] for d in zs.facts.sources(include_confidential=True)] == ["other"]


def test_chunk_must_match_document(zs):
    with pytest.raises(ValueError):
        zs.facts.replace_document("a", [Chunk("b", "github", "B", "t", "u", "2026")])


def test_reopening_with_another_embedder_is_refused(config, seeded):
    with pytest.raises(RuntimeError, match="reindex"):
        FactStore(config.servers.vectordb_uri(), HashingEmbedder(32))
    reopened = FactStore(config.servers.vectordb_uri(), HashingEmbedder(64))
    assert reopened.count() == 3


def test_optimize_keeps_results(seeded):
    seeded.facts.optimize()
    assert "Enterprise plans are priced per contract." in texts(
        seeded.facts.search("enterprise contract", include_confidential=False)
    )


def test_superseded_chunks_leave_search_and_come_back(seeded):
    old_id = pricing_doc()[0].id
    seeded.facts.supersede([old_id], "gdrive:pricing-2027")
    assert "$20" not in " ".join(texts(seeded.facts.search("pro plan price", include_confidential=True)))
    assert seeded.facts.get(old_id, include_confidential=True)["superseded_by"] == "gdrive:pricing-2027"
    assert seeded.facts.restore_superseded("gdrive:pricing-2027") == 1
    assert "$20" in " ".join(texts(seeded.facts.search("pro plan price", include_confidential=True)))
    assert seeded.facts.restore_superseded("gdrive:pricing-2027") == 0


def test_search_can_exclude_a_document(seeded):
    rows = seeded.facts.search("pro plan price", include_confidential=True, exclude_doc="gdrive:pricing")
    assert rows and all(r["doc_id"] != "gdrive:pricing" for r in rows)


def test_stores_from_before_superseding_are_upgraded(tmp_path):
    import lancedb
    import pyarrow as pa

    uri = str(tmp_path / "old.lance")
    embedder = HashingEmbedder(64)
    old = pa.schema(
        [f for f in FactStore(str(tmp_path / "x.lance"), embedder)._schema() if f.name != "superseded_by"],
        metadata={b"zerostore.embedder": embedder.name.encode()},
    )
    table = lancedb.connect(uri).create_table("chunks", schema=old)
    row = {**{k: v for k, v in pricing_doc()[0].__dict__.items() if k != "superseded_by"}}
    table.add([{**row, "id": pricing_doc()[0].id, "vector": embedder.embed(["x"])[0]}])
    store = FactStore(uri, embedder)
    assert store.chunks_of("gdrive:pricing")[0]["superseded_by"] == ""
    store.replace_document("gdrive:pricing", pricing_doc("$29"))
    assert FactStore.built_with(uri) == embedder.name


def test_empty_starts_over_with_a_new_embedder(config, seeded):
    uri = config.servers.vectordb_uri()
    fresh = FactStore.empty(uri, HashingEmbedder(32))
    assert fresh.count() == 0 and FactStore.built_with(uri) == "hashing-32"
    assert FactStore.built_with(str(config.servers.vectordb_uri()) + "-none") is None
