"""Ingestion: bring the fact store in line with the sources.

For each source: list what it holds, fetch only what changed, chunk it and
swap the document's old chunks for the new ones. Documents a source no
longer lists are retired. Every change is written to the audit table.

This stage applies every change directly. The update layer adds the
librarian's judgment on top (is this lasting knowledge, did a decision
change, was a deleted file moved) and sends low-confidence changes to the
review queue instead.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import socket
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field

from zerostore.app import ZeroStore
from zerostore.config import ConfidentialRule, Config
from zerostore.connectors.base import Connector, ConnectorError, SourceDocument
from zerostore.librarian.chunking import chunk_text
from zerostore.store.facts import Chunk
from zerostore.store.state import utcnow

log = logging.getLogger("zerostore.sync")

SYNC_LOCK = "sync"
SYNC_LOCK_TTL = 2 * 60 * 60  # a sync that takes longer than this is presumed dead


class SyncBusy(Exception):
    """Another process is already syncing."""


@dataclass
class DocChange:
    action: str  # added, updated, retired
    doc_id: str
    title: str
    source: str
    confidential: bool


@dataclass
class SourceResult:
    source: str
    listed: int = 0
    unchanged: int = 0
    skipped: int = 0
    failed: list[str] = field(default_factory=list)
    error: str | None = None
    warning: str | None = None


@dataclass
class SyncReport:
    started_at: str
    finished_at: str = ""
    sources: list[SourceResult] = field(default_factory=list)
    changes: list[DocChange] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(r.error is None for r in self.sources)

    def summary(self) -> str:
        """One line, e.g. '2 changes · Pricing 2026, Roadmap' or 'No changes'."""
        if not self.changes:
            text = "No changes"
        else:
            n = len(self.changes)
            titles = ", ".join(c.title for c in self.changes[:3]) + (", ..." if n > 3 else "")
            text = f"{n} change{'s' if n != 1 else ''} · {titles}"
        errors = [r for r in self.sources if r.error]
        if errors:
            text += f" · {len(errors)} source{'s' if len(errors) != 1 else ''} failed"
        return text

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, raw: str) -> SyncReport:
        data = json.loads(raw)
        return cls(
            started_at=data["started_at"],
            finished_at=data["finished_at"],
            sources=[SourceResult(**r) for r in data["sources"]],
            changes=[DocChange(**c) for c in data["changes"]],
        )


ConnectorFactory = Callable[[str, Config], Connector]


def make_connector(name: str, config: Config) -> Connector:
    if name == "google_drive":
        from zerostore.connectors.gdrive import GoogleDriveConnector

        c = config.sources.google_drive
        return GoogleDriveConnector(c.folders, c.credentials)
    if name == "github":
        from zerostore.connectors.github import GitHubConnector

        c = config.sources.github
        return GitHubConnector(c.repos, c.token, c.include)
    if name == "linear":
        from zerostore.connectors.linear import LinearConnector

        c = config.sources.linear
        return LinearConnector(c.teams, c.api_key)
    raise ValueError(f"unknown source {name}")


def is_confidential(doc: SourceDocument, rules: list[ConfidentialRule]) -> bool:
    for rule in rules:
        if rule.google_drive and doc.source == "google_drive":
            folder = rule.google_drive.strip("/")
            if doc.location == folder or doc.location.startswith(folder + "/"):
                return True
        if rule.github and doc.source == "github" and doc.location.lower() == rule.github.lower():
            return True
        if rule.linear_label and doc.source == "linear":
            if rule.linear_label.lower() in (label.lower() for label in doc.labels):
                return True
    return False


def _rules_fingerprint(config: Config) -> str:
    return hashlib.sha256(json.dumps([r.model_dump() for r in config.confidential]).encode()).hexdigest()


def to_chunks(doc: SourceDocument, confidential: bool) -> list[Chunk]:
    return [
        Chunk(
            doc_id=doc.doc_id,
            source=doc.source,
            title=doc.title,
            text=piece.with_heading(),
            source_url=doc.url,
            updated_at=doc.updated_at,
            ordinal=i,
            confidential=confidential,
        )
        for i, piece in enumerate(chunk_text(doc.text))
    ]


class Ingestor:
    def __init__(self, zs: ZeroStore, connector_factory: ConnectorFactory | None = None):
        self.zs = zs
        self.make_connector = connector_factory or (lambda name, config: make_connector(name, config))
        self.holder = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"

    def sync(self, only: list[str] | None = None) -> SyncReport:
        """Sync every configured source (or just `only`). Raises SyncBusy if one is already running."""
        state = self.zs.state
        if not state.acquire_lock(SYNC_LOCK, self.holder, SYNC_LOCK_TTL):
            raise SyncBusy("another sync is already running")
        try:
            report = self._sync(only)
            state.set_meta("last_sync", report.to_json())
            return report
        finally:
            state.release_lock(SYNC_LOCK, self.holder)

    def _sync(self, only: list[str] | None) -> SyncReport:
        config = self.zs.config
        report = SyncReport(started_at=utcnow())
        configured = config.sources.enabled()
        names = [n for n in configured if not only or n in only]
        unknown = set(only or []) - set(configured)
        if unknown:
            raise ValueError(f"not configured in sources: {', '.join(sorted(unknown))}")

        # Confidentiality rules changed: re-check every document, not only changed ones.
        rules_fp = _rules_fingerprint(config)
        recheck_all = self.zs.state.get_meta("confidential_rules") != rules_fp

        for name in names:
            result = SourceResult(source=name)
            report.sources.append(result)
            try:
                connector = self.make_connector(name, config)
            except ConnectorError as exc:
                result.error = str(exc)
                continue
            try:
                self._sync_source(connector, result, report, recheck_all)
            except ConnectorError as exc:
                result.error = str(exc)
            finally:
                connector.close()
            if result.error:
                log.warning("sync of %s failed: %s", name, result.error)
                self.zs.state.audit("sync_failed", source=name, detail=result.error)

        if not only:
            self._retire_unconfigured(set(configured), report)
        if not only and report.ok:
            self.zs.state.set_meta("confidential_rules", rules_fp)
        if report.changes:
            self.zs.facts.optimize()
        report.finished_at = utcnow()
        return report

    def _sync_source(
        self, connector: Connector, result: SourceResult, report: SyncReport, recheck_all: bool
    ) -> None:
        state, facts, config = self.zs.state, self.zs.facts, self.zs.config
        known = state.documents(connector.name)
        refs = {ref.doc_id: ref for ref in connector.list_items()}
        result.listed = len(refs)

        for doc_id, ref in refs.items():
            record = known.get(doc_id)
            if record and record["version"] == ref.version and not recheck_all:
                result.unchanged += 1
                continue
            try:
                doc = connector.fetch(ref)
            except ConnectorError as exc:
                result.failed.append(f"{ref.title}: {exc}")
                continue  # keep whatever the store already has for it
            if doc is None:
                if record:
                    self._retire(record, "no readable text any more", report)
                else:
                    result.skipped += 1
                continue
            confidential = is_confidential(doc, config.confidential)
            if (
                record
                and record["fingerprint"] == doc.fingerprint
                and bool(record["confidential"]) == confidential
            ):
                state.touch_document(doc_id, doc.version)
                result.unchanged += 1
                continue
            chunks = to_chunks(doc, confidential)
            facts.replace_document(doc_id, chunks)
            state.upsert_document(
                {
                    "doc_id": doc_id,
                    "source": doc.source,
                    "title": doc.title,
                    "url": doc.url,
                    "version": doc.version,
                    "fingerprint": doc.fingerprint,
                    "updated_at": doc.updated_at,
                    "location": doc.location,
                    "confidential": int(confidential),
                    "chunks": len(chunks),
                }
            )
            action = "updated" if record else "added"
            state.audit(
                f"document_{action}",
                doc_id=doc_id,
                source=doc.source,
                detail=doc.title,
                confidential=confidential,
            )
            report.changes.append(DocChange(action, doc_id, doc.title, doc.source, confidential))

        missing = [record for doc_id, record in known.items() if doc_id not in refs]
        if missing and not refs:
            # An empty listing for a source that had documents is far more likely a lost
            # permission or renamed folder than everything being deleted at once.
            result.warning = (
                f"{connector.name} listed nothing, but ZeroStore holds {len(missing)} of its documents; "
                "kept them. Check the folder names and permissions."
            )
            return
        for record in missing:
            self._retire(record, "no longer in the source", report)

    def _retire(self, record: dict, reason: str, report: SyncReport) -> None:
        self.zs.facts.delete_document(record["doc_id"])
        self.zs.state.delete_document(record["doc_id"])
        self.zs.state.audit(
            "document_retired",
            doc_id=record["doc_id"],
            source=record["source"],
            detail=f"{record['title']} ({reason})",
            confidential=bool(record["confidential"]),
        )
        report.changes.append(
            DocChange(
                "retired", record["doc_id"], record["title"], record["source"], bool(record["confidential"])
            )
        )

    def _retire_unconfigured(self, configured: set[str], report: SyncReport) -> None:
        """Sources removed from zerostore.yaml stop being served."""
        for record in self.zs.state.documents().values():
            if record["source"] not in configured:
                self._retire(record, f"{record['source']} was removed from the settings", report)
