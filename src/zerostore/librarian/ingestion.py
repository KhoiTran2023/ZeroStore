"""Ingestion: bring the fact store in line with the sources.

For each source: list what it holds, fetch only what changed, and hand each
changed document to the update layer (`update.py`), which judges it and
either applies it or sends it to the review queue. Documents a source no
longer lists go through the same judgment after every source is read, so a
file moved from one source to another can be recognised. Every change is
written to the audit table.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import socket
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from zerostore.access.roles import access_key, matches, tags_for
from zerostore.app import ZeroStore
from zerostore.config import ConfidentialRule, Config
from zerostore.connectors.base import Connector, ConnectorError, SourceDocument
from zerostore.decisions.base import DecisionEngine, EngineError
from zerostore.librarian.chunking import chunk_text
from zerostore.store.embeddings import EmbeddingError
from zerostore.store.facts import Chunk
from zerostore.store.state import utcnow

log = logging.getLogger("zerostore.sync")

# What a failed judgment looks like: the engine or the embedding service couldn't answer.
JUDGE_ERRORS = (EngineError, EmbeddingError)

SYNC_LOCK = "sync"
SYNC_LOCK_TTL = 2 * 60 * 60  # a sync that takes longer than this is presumed dead
JUDGMENTS_KEPT_DAYS = 180  # remembered keep answers nobody reused in this long are dropped


class SyncBusy(Exception):
    """Another process is already syncing."""


# Change actions that took effect, and ones waiting for a person.
APPLIED = ("added", "updated", "retired", "moved", "kept")
WAITING = ("review", "review_delete")


@dataclass
class DocChange:
    action: str  # one of APPLIED or WAITING
    doc_id: str
    title: str
    source: str
    confidential: bool
    confidence: float | None = None
    engine: str | None = None
    note: str = ""
    replaces: list[str] = field(default_factory=list)  # documents whose facts this change hid


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
    engine: str | None = None  # who judged the changes, e.g. "Jev"
    judged: int = 0  # documents and deletions the engine judged
    judge_seconds: float = 0.0  # time spent waiting on the engine for them
    remembered: int = 0  # chunks whose keep and topic answers were reused instead of asked again

    @property
    def ok(self) -> bool:
        return all(r.error is None for r in self.sources)

    @property
    def applied(self) -> list[DocChange]:
        return [c for c in self.changes if c.action in APPLIED]

    @property
    def waiting(self) -> list[DocChange]:
        return [c for c in self.changes if c.action in WAITING]

    def summary(self) -> str:
        """One line, e.g. '1 change · Pricing 2026 · Jev confidence 0.97' or 'No changes'."""
        applied, waiting = self.applied, self.waiting
        if not applied:
            text = "No changes"
        else:
            n = len(applied)
            titles = ", ".join(c.title for c in applied[:3]) + (", ..." if n > 3 else "")
            text = f"{n} change{'s' if n != 1 else ''} · {titles}"
            judged = [c for c in applied if c.confidence is not None]
            if judged:
                lowest = min(judged, key=lambda c: c.confidence or 0.0)
                prefix = "" if len(judged) == 1 else "lowest "
                text += f" · {prefix}{lowest.engine or 'engine'} confidence {lowest.confidence:.2f}"
        if waiting:
            text += f" · {len(waiting)} waiting for review"
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
            engine=data.get("engine"),
            judged=data.get("judged", 0),
            judge_seconds=data.get("judge_seconds", 0.0),
            remembered=data.get("remembered", 0),
        )


ConnectorFactory = Callable[[str, Config], Connector]


def source_names(config: Config) -> list[str]:
    """Every source a sync reads: the configured ones, plus decisions.md once it exists."""
    from zerostore.connectors.decisions import confidential_path

    path = config.decisions_path()
    decisions = config.agent_proposals and (path.exists() or confidential_path(path).exists())
    return config.sources.enabled() + (["decisions"] if decisions else [])


def make_connector(name: str, config: Config) -> Connector:
    if name == "decisions":
        from zerostore.connectors.decisions import DecisionsConnector

        return DecisionsConnector(config.decisions_path())
    if name == "folders":  # real folders on this computer, in the demo too
        from zerostore.connectors.folder import FolderConnector

        c = config.sources.folders
        return FolderConnector(c.paths, c.include)
    if config.demo:
        from zerostore.connectors.demo import DemoConnector

        return DemoConnector.from_config(name, config)
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
    if doc.source == "decisions":
        from zerostore.connectors.decisions import CONFIDENTIAL_DOC_ID

        # Facts proposed through confidential tokens live in their own file.
        return doc.doc_id == CONFIDENTIAL_DOC_ID
    return any(matches(doc, rule) for rule in rules)


def access_of(doc: SourceDocument, config: Config) -> str:
    """The document's tags as stored with its facts (see access/roles.py)."""
    if doc.source == "decisions":
        return ""  # approved proposals carry no tags; confidential ones have their own file
    return access_key(tags_for(doc, config))


def rules_fingerprint(config: Config) -> str:
    """Changes when the confidential places or the tags change, so every document is checked again."""
    rules: Any = [r.model_dump() for r in config.confidential]
    if config.tags:
        tags = {name: [r.model_dump() for r in place] for name, place in config.tags.items()}
        rules = {"confidential": rules, "tags": tags}
    return hashlib.sha256(json.dumps(rules, sort_keys=True).encode()).hexdigest()


def to_chunks(doc: SourceDocument, confidential: bool, access: str = "") -> list[Chunk]:
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
            access=access,
        )
        for i, piece in enumerate(chunk_text(doc.text))
    ]


_UNSET: Any = object()


class Ingestor:
    def __init__(
        self,
        zs: ZeroStore,
        connector_factory: ConnectorFactory | None = None,
        judge: DecisionEngine | None = _UNSET,
    ):
        from zerostore.decisions.select import build_judge
        from zerostore.librarian.update import Librarian

        self.zs = zs
        self.make_connector = connector_factory or (lambda name, config: make_connector(name, config))
        self.holder = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        self.judge = build_judge(zs.config) if judge is _UNSET else judge
        self.librarian = Librarian(zs, self.judge)

    def sync(self, only: list[str] | None = None) -> SyncReport:
        """Sync every source (or just `only`). Raises SyncBusy if one is already running."""
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
        report = SyncReport(started_at=utcnow(), engine=self.judge.label if self.judge else None)
        configured = source_names(config)
        names = [n for n in configured if not only or n in only]
        unknown = set(only or []) - set(configured)
        if unknown:
            raise ValueError(f"not configured in sources: {', '.join(sorted(unknown))}")

        # The confidential places or the tags changed: re-check every document, not only changed ones.
        rules_fp = rules_fingerprint(config)
        recheck_all = self.zs.state.get_meta("confidential_rules") != rules_fp
        remembered_before = self.librarian.remembered

        added: list[dict[str, Any]] = []
        missing: list[tuple[dict[str, Any], SourceResult]] = []
        for name in names:
            result = SourceResult(source=name)
            report.sources.append(result)
            try:
                connector = self.make_connector(name, config)
            except ConnectorError as exc:
                result.error = str(exc)
            else:
                try:
                    gone = self._sync_source(connector, result, report, recheck_all, added)
                    missing.extend((record, result) for record in gone)
                except ConnectorError as exc:
                    result.error = str(exc)
                finally:
                    connector.close()
            if result.error:
                log.warning("sync of %s failed: %s", name, result.error)
                self.zs.state.audit("sync_failed", source=name, detail=result.error)

        # After every source is read, so a file that moved between sources can be recognised.
        for record, result in missing:
            self._handle_missing(record, result, report, added)

        if not only:
            self._retire_unconfigured(set(configured), report)
        if not only and report.ok:
            self.zs.state.set_meta("confidential_rules", rules_fp)
        if report.changes:
            self.zs.facts.optimize()
        report.remembered = self.librarian.remembered - remembered_before
        cutoff = datetime.now(timezone.utc) - timedelta(days=JUDGMENTS_KEPT_DAYS)
        self.zs.state.forget_judgments(cutoff.isoformat(timespec="seconds"))
        report.finished_at = utcnow()
        return report

    def _sync_source(
        self,
        connector: Connector,
        result: SourceResult,
        report: SyncReport,
        recheck_all: bool,
        added: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Bring one source's documents up to date. Returns the documents it no longer lists."""
        state, config, librarian = self.zs.state, self.zs.config, self.librarian
        known = state.documents(connector.name)
        refs = {ref.doc_id: ref for ref in connector.list_items()}
        result.listed = len(refs)

        awaiting_delete = state.pending_doc_ids("delete")
        awaiting_change = state.pending_doc_ids("change")
        for doc_id, ref in refs.items():
            record = known.get(doc_id)
            if record and record["source_deleted_at"]:
                # A file we kept after it was deleted is back in the source.
                state.mark_source_deleted(doc_id, False)
            if doc_id in awaiting_delete:
                librarian.close_pending(doc_id, "the file is back in the source", kinds=("delete",))
            if record and record["version"] == ref.version and not recheck_all:
                result.unchanged += 1
                continue
            try:
                doc = connector.fetch(ref)
            except ConnectorError as exc:
                result.failed.append(f"{ref.title}: {exc}")
                continue  # keep whatever the store already has for it
            if doc is None:
                if record and record["chunks"]:
                    librarian.retire(record, "no readable text any more")
                    report.changes.append(_change("retired", record))
                else:
                    result.skipped += 1
                continue
            confidential = is_confidential(doc, config.confidential)
            access = access_of(doc, config)
            same_content = record and record["fingerprint"] == doc.fingerprint
            same_access = record and bool(record["confidential"]) == confidential
            same_access = same_access and record["access"] == access
            if same_content and same_access:
                state.touch_document(doc_id, doc.version)
                if doc_id in awaiting_change:
                    # The source went back to the live version: the waiting change is moot.
                    librarian.close_pending(doc_id, "the source went back to the live version", ("change",))
                result.unchanged += 1
                continue
            if record and record["rejected_fingerprint"] == doc.fingerprint:
                state.touch_document(doc_id, doc.version)  # a person already turned this version down
                result.unchanged += 1
                continue
            # When the confidentiality rules changed, a waiting change is judged again (and replaced).
            if not recheck_all and librarian.waiting(doc_id, doc.fingerprint, confidential, access):
                if record:
                    state.touch_document(doc_id, doc.version)
                result.unchanged += 1
                continue
            judged = connector.name != "decisions" and self.judge is not None
            started = time.perf_counter()
            try:
                plan = librarian.plan(doc, confidential, record, judged=judged, access=access)
            except JUDGE_ERRORS as exc:
                result.failed.append(f"{ref.title}: couldn't be judged, kept as it was ({exc})")
                continue
            finally:
                if judged:
                    report.judged += 1
                    report.judge_seconds += time.perf_counter() - started
            if plan.confidence is not None and plan.confidence < librarian.threshold:
                librarian.queue(plan, rules=rules_fingerprint(config))
                if record:  # don't fetch this version again while it waits
                    state.touch_document(doc_id, doc.version)
                report.changes.append(
                    DocChange(
                        "review", doc_id, doc.title, doc.source, confidential, plan.confidence, plan.engine
                    )
                )
                continue
            try:
                librarian.apply(plan)
            except EmbeddingError as exc:
                result.failed.append(f"{ref.title}: couldn't be stored, kept as it was ({exc})")
                continue
            if plan.is_noise and plan.action == "added":
                result.skipped += 1
                continue
            report.changes.append(
                DocChange(
                    plan.action,
                    doc_id,
                    doc.title,
                    doc.source,
                    confidential,
                    plan.confidence,
                    plan.engine,
                    plan.note(),
                    plan.replaces(),
                )
            )
            if plan.action == "added":
                added.append(
                    {
                        "doc_id": doc_id,
                        "title": doc.title,
                        "source": doc.source,
                        "url": doc.url,
                        "confidential": confidential,
                        "access": access,
                    }
                )

        missing = [record for doc_id, record in known.items() if doc_id not in refs]
        if missing and not refs:
            # An empty listing for a source that had documents is far more likely a lost
            # permission or renamed folder than everything being deleted at once.
            result.warning = (
                f"{connector.name} listed nothing, but ZeroStore holds {len(missing)} of its documents; "
                "kept them. Check the folder names and permissions."
            )
            return []
        return missing

    def _handle_missing(
        self, record: dict[str, Any], result: SourceResult, report: SyncReport, added: list[dict[str, Any]]
    ) -> None:
        state, librarian = self.zs.state, self.librarian
        if not record["chunks"]:
            # Nothing was served from it (noise, or a version a person turned down).
            state.delete_document(record["doc_id"])
            return
        if record["source_deleted_at"]:
            return  # already decided: keep its facts
        if record["source"] == "decisions":
            # A person removed approved facts from decisions.md on purpose.
            librarian.retire(record, "removed from the decisions file")
            report.changes.append(_change("retired", record))
            return
        if any(item["kind"] == "delete" for item in state.pending_for_document(record["doc_id"])):
            return  # already waiting for a person
        started = time.perf_counter()
        try:
            decision = librarian.decide_delete(record, added)
        except JUDGE_ERRORS as exc:
            result.failed.append(
                f"{record['title']}: deletion couldn't be judged, facts kept for now ({exc})"
            )
            return
        finally:
            if self.judge is not None:
                report.judged += 1
                report.judge_seconds += time.perf_counter() - started
        conf, engine = decision.confidence, decision.engine
        if decision.action == "retire":
            librarian.retire(record, "no longer in the source", confidence=conf, engine=engine)
            report.changes.append(_change("retired", record, conf, engine))
        elif decision.action == "moved" and decision.moved_to:
            note = f"moved to {decision.moved_to['title']}"
            librarian.retire(record, note, action="document_moved", confidence=conf, engine=engine)
            report.changes.append(_change("moved", record, conf, engine, note))
        elif decision.action == "keep":
            librarian.keep_deleted(record, confidence=conf, engine=engine)
            report.changes.append(
                _change("kept", record, conf, engine, "deleted from the source; facts kept")
            )
        else:
            librarian.queue_delete(record, decision)
            report.changes.append(_change("review_delete", record, conf, engine))

    def _retire_unconfigured(self, configured: set[str], report: SyncReport) -> None:
        """Sources removed from zerostore.yaml stop being served."""
        for record in self.zs.state.documents().values():
            if record["source"] not in configured:
                if record["chunks"]:
                    reason = (
                        "decisions.md is gone or agent proposals are off"
                        if record["source"] == "decisions"
                        else f"{record['source']} was removed from the settings"
                    )
                    self.librarian.retire(record, reason)
                    report.changes.append(_change("retired", record))
                else:
                    self.zs.state.delete_document(record["doc_id"])


def _change(
    action: str,
    record: dict[str, Any],
    confidence: float | None = None,
    engine: str | None = None,
    note: str = "",
) -> DocChange:
    return DocChange(
        action,
        record["doc_id"],
        record["title"],
        record["source"],
        bool(record["confidential"]),
        confidence,
        engine,
        note,
    )
