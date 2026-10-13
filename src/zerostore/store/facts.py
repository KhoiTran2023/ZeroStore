"""The fact store: document chunks in LanceDB.

Each row is one chunk of a source document, stored word for word with its
source link, last-updated date, topic tags and a `confidential` flag. The store
holds only the current version of each document; history lives in the audit
table. It runs in-process, on local disk or on S3 (`s3://bucket/path`).
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from typing import Any

import lancedb
import pyarrow as pa

from zerostore.store.embeddings import Embedder

TABLE = "chunks"


@dataclass
class Chunk:
    doc_id: str
    source: str
    title: str
    text: str
    source_url: str
    updated_at: str
    ordinal: int = 0
    topics: list[str] = field(default_factory=list)
    confidential: bool = False

    @property
    def id(self) -> str:
        return chunk_id(self.doc_id, self.ordinal)


def chunk_id(doc_id: str, ordinal: int) -> str:
    return hashlib.sha1(f"{doc_id}#{ordinal}".encode()).hexdigest()[:16]


def _quote(value: str) -> str:
    """Quote a string literal for a LanceDB filter."""
    return "'" + value.replace("'", "''") + "'"


class FactStore:
    def __init__(self, uri: str, embedder: Embedder):
        self.uri = uri
        self.embedder = embedder
        self._db = lancedb.connect(uri)
        self._table = self._open_or_create()

    def _schema(self) -> pa.Schema:
        return pa.schema(
            [
                ("id", pa.string()),
                ("doc_id", pa.string()),
                ("ordinal", pa.int32()),
                ("source", pa.string()),
                ("title", pa.string()),
                ("text", pa.string()),
                ("source_url", pa.string()),
                ("updated_at", pa.string()),
                ("topics", pa.list_(pa.string())),
                ("confidential", pa.bool_()),
                ("vector", pa.list_(pa.float32(), self.embedder.dim)),
            ],
            metadata={b"zerostore.embedder": self.embedder.name.encode()},
        )

    def _open_or_create(self):
        existing = set(self._db.list_tables().tables)
        if TABLE in existing:
            table = self._db.open_table(TABLE)
            built_with = (table.schema.metadata or {}).get(b"zerostore.embedder", b"unknown").decode()
            if built_with != self.embedder.name:
                raise RuntimeError(
                    f"the fact store at {self.uri} was built with the {built_with} embedder, but "
                    f"{self.embedder.name} is configured now, and their vectors don't mix. "
                    "Rebuild it from your sources with `zerostore reindex`."
                )
            return table
        table = self._db.create_table(TABLE, schema=self._schema())
        self._ensure_fts(table)
        return table

    @staticmethod
    def _ensure_fts(table) -> None:
        from lancedb.index import FTS

        table.create_index("text", config=FTS(), replace=True)

    # Writes (only the librarian calls these) ------------------------------

    def replace_document(self, doc_id: str, chunks: list[Chunk]) -> int:
        """Swap every chunk of `doc_id` for `chunks`. Returns the number written."""
        for c in chunks:
            if c.doc_id != doc_id:
                raise ValueError(f"chunk for {c.doc_id!r} passed to replace_document({doc_id!r})")
        rows = []
        if chunks:
            vectors = self.embedder.embed([f"{c.title}\n{c.text}" for c in chunks])
            for c, vec in zip(chunks, vectors, strict=True):
                row = asdict(c)
                row["id"] = c.id
                row["vector"] = vec
                rows.append(row)
        self._table.delete(f"doc_id = {_quote(doc_id)}")
        if rows:
            self._table.add(rows)
        return len(rows)

    def delete_document(self, doc_id: str) -> None:
        self._table.delete(f"doc_id = {_quote(doc_id)}")

    def optimize(self) -> None:
        """Fold new rows into the indexes and compact files. Run after a sync.

        Rows added since the last optimize are still searchable (LanceDB scans
        them directly); this only keeps search fast as the store grows.
        """
        self._table.optimize()

    # Reads ----------------------------------------------------------------

    def count(self) -> int:
        return self._table.count_rows()

    def get(self, fact_id: str, *, include_confidential: bool) -> dict[str, Any] | None:
        where = f"id = {_quote(fact_id)}"
        if not include_confidential:
            where += " AND confidential = false"
        rows = self._table.search().where(where).limit(1).to_list()
        return _public(rows[0]) if rows else None

    def search(
        self,
        query: str,
        *,
        include_confidential: bool,
        topics: list[str] | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Hybrid vector + keyword search, pre-filtered by access level and topics.

        Results carry `_relevance_score` from LanceDB's reciprocal-rank fusion;
        the librarian's reranker turns candidates into a 0-1 answer score.
        """
        query = query.strip()
        if not query or self.count() == 0:
            return []
        filters = []
        if not include_confidential:
            filters.append("confidential = false")
        if topics:
            filters.append("array_has_any(topics, [" + ", ".join(_quote(t) for t in topics) + "])")
        vector = self.embedder.embed([query])[0]
        builder = self._table.search(query_type="hybrid").vector(vector).text(query)
        if filters:
            builder = builder.where(" AND ".join(filters), prefilter=True)
        return [_public(r) for r in builder.limit(limit).to_list()]

    def sources(self, *, include_confidential: bool) -> list[dict[str, Any]]:
        """One entry per document: id, title, source, link, last updated, chunk count."""
        builder = self._table.search().select(["doc_id", "title", "source", "source_url", "updated_at"])
        if not include_confidential:
            builder = builder.where("confidential = false")
        docs: dict[str, dict[str, Any]] = {}
        for row in builder.limit(max(self.count(), 1)).to_list():
            doc = docs.setdefault(
                row["doc_id"],
                {
                    "doc_id": row["doc_id"],
                    "title": row["title"],
                    "source": row["source"],
                    "source_url": row["source_url"],
                    "updated_at": row["updated_at"],
                    "chunks": 0,
                },
            )
            doc["chunks"] += 1
        return sorted(docs.values(), key=lambda d: (d["source"], d["title"]))


def _public(row: dict[str, Any]) -> dict[str, Any]:
    """Drop internal columns (the vector) before a row leaves the store."""
    out = {k: v for k, v in row.items() if k != "vector" and not k.startswith("_")}
    if "_relevance_score" in row:
        out["search_score"] = row["_relevance_score"]
    return out
