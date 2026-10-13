"""A connector that serves documents from a dict, for sync tests."""

from __future__ import annotations

from zerostore.connectors.base import Connector, ConnectorError, ItemRef, SourceDocument


class FakeConnector(Connector):
    def __init__(self, name: str = "google_drive"):
        self.name = name
        self.docs: dict[str, SourceDocument] = {}
        self.fetched: list[str] = []
        self.fail_listing = False
        self.fail_fetch: set[str] = set()
        self.closed = False

    def put(self, key, title, text, *, version="v1", location="Company", labels=(), updated="2026-10-01"):
        self.docs[key] = SourceDocument(
            doc_id=f"{self.name}:{key}",
            source=self.name,
            title=title,
            text=text,
            url=f"https://example.com/{key}",
            updated_at=updated,
            version=version,
            location=location,
            labels=list(labels),
        )

    def list_items(self):
        if self.fail_listing:
            raise ConnectorError("source unreachable")
        return [ItemRef(d.doc_id, d.version, d.title, {"key": k}) for k, d in self.docs.items()]

    def fetch(self, ref):
        self.fetched.append(ref.extra["key"])
        if ref.extra["key"] in self.fail_fetch:
            raise ConnectorError("timeout")
        doc = self.docs[ref.extra["key"]]
        return doc if doc.text.strip() else None

    def close(self):
        self.closed = True
