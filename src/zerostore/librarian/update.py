"""The update layer: the librarian's judgment on every change a sync finds.

For a new or changed document:
1. `keep` drops noise chunks ("Lunch at noon?") and `route` tags the rest
   with topics, so retrieval can narrow its search.
2. `compare` checks each kept chunk against the closest fact from another
   document. When they disagree, the newer one wins and the older chunk is
   hidden (superseded) rather than deleted.
3. The change's confidence is the lowest confidence among those judgments.
   At or above `review.auto_apply_at` it goes live; below it the change waits
   in the review queue and the current version stays live.

For a document that disappeared from its source, `on_delete` decides whether
it was moved, should be retired, or kept; unsure or low-confidence answers go
to the review queue and the facts stay live meanwhile.

With no AI set up there is no judgment: every change is applied directly.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import asdict, dataclass, field
from typing import Any

from zerostore.app import ZeroStore
from zerostore.connectors.base import SourceDocument
from zerostore.decisions.base import (
    KEEP_QUESTION,
    MOVED_PREFIX,
    NO_TOPIC,
    ROUTE_QUESTION,
    Candidate,
    DecisionEngine,
    DeletedFile,
    Pick,
    YesNo,
    topics_from,
)
from zerostore.store.facts import Chunk

log = logging.getLogger("zerostore.update")

LIBRARIAN = "librarian"  # decided_by / created_by for the librarian's own review actions
EXCERPT = 3000  # characters of a document shown to on_delete
MAX_CANDIDATES = 5


@dataclass
class Supersede:
    """Hide chunk `chunk_id` (from `doc_id`) because `by_doc` holds the newer answer."""

    chunk_id: str
    doc_id: str
    title: str
    by_doc: str
    by_title: str
    relation: str  # changed or conflict
    text_hash: str = ""  # the hidden chunk's text when it was judged

    def still_holds(self, row: dict[str, Any] | None) -> bool:
        return row is not None and (not self.text_hash or text_hash(row["text"]) == self.text_hash)


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def classify_key(text: str, topics: list[str]) -> str:
    """What a keep-and-topic answer depends on: the questions, the topic list and the text."""
    asked = json.dumps([KEEP_QUESTION, ROUTE_QUESTION, NO_TOPIC, topics, text])
    return hashlib.sha256(asked.encode()).hexdigest()


def _answer_to(answer: tuple[YesNo, Pick]) -> dict[str, Any]:
    keep, route = answer
    return {"keep": keep.probability, "route": asdict(route)}


def _answer_from(data: dict[str, Any]) -> tuple[YesNo, Pick]:
    return YesNo(data["keep"]), Pick(**data["route"])


@dataclass
class ChangePlan:
    action: str  # added or updated
    record: dict[str, Any]  # the documents-table row to write
    chunks: list[Chunk]
    supersedes: list[Supersede] = field(default_factory=list)
    confidence: float | None = None
    engine: str | None = None
    dropped: int = 0  # noise chunks left out

    @property
    def doc_id(self) -> str:
        return self.record["doc_id"]

    @property
    def title(self) -> str:
        return self.record["title"]

    @property
    def is_noise(self) -> bool:
        """Every chunk was judged noise: nothing to serve from this document."""
        return not self.chunks and self.dropped > 0

    def replaces(self) -> list[str]:
        """Titles of the other documents whose facts this change hides."""
        return sorted({s.title for s in self.supersedes if s.by_doc == self.doc_id})

    def note(self) -> str:
        parts = []
        hidden = self.replaces()
        if hidden:
            parts.append("replaces facts in " + ", ".join(hidden))
        older = sorted({s.by_title for s in self.supersedes if s.doc_id == self.doc_id})
        if older:
            parts.append("older than " + ", ".join(older))
        if self.dropped:
            parts.append(f"{self.dropped} noise chunk{'s' if self.dropped != 1 else ''} left out")
        return "; ".join(parts)

    def to_payload(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "record": self.record,
            "chunks": [asdict(c) for c in self.chunks],
            "supersedes": [asdict(s) for s in self.supersedes],
            "confidence": self.confidence,
            "engine": self.engine,
            "dropped": self.dropped,
            "title": self.title,
            "source": self.record["source"],
            "url": self.record["url"],
            "note": self.note(),
            "preview": "\n\n".join(c.text for c in self.chunks)[:1500],
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> ChangePlan:
        return cls(
            action=payload["action"],
            record=payload["record"],
            chunks=[Chunk(**c) for c in payload["chunks"]],
            supersedes=[Supersede(**s) for s in payload["supersedes"]],
            confidence=payload.get("confidence"),
            engine=payload.get("engine"),
            dropped=payload.get("dropped", 0),
        )


@dataclass
class DeleteDecision:
    action: str  # retire, moved, keep or review
    confidence: float | None = None
    engine: str | None = None
    moved_to: dict[str, Any] | None = None  # the candidate it moved to
    suggestion: str | None = None  # what the engine leaned towards, for a review item


class Librarian:
    def __init__(self, zs: ZeroStore, judge: DecisionEngine | None):
        self.zs = zs
        self.judge = judge
        self.remembered = 0  # chunks whose keep and topic answers were reused, not asked again

    @property
    def threshold(self) -> float:
        return self.zs.config.review.auto_apply_at

    # Changes -----------------------------------------------------------------

    def plan(
        self, doc: SourceDocument, confidential: bool, record: dict[str, Any] | None, judged: bool = True
    ) -> ChangePlan:
        """Work out what this version of `doc` would change. Raises EngineError if judging fails."""
        from zerostore.librarian.ingestion import to_chunks

        chunks = to_chunks(doc, confidential)
        plan = ChangePlan(
            action="updated" if record and record["chunks"] else "added",
            record={
                "doc_id": doc.doc_id,
                "source": doc.source,
                "title": doc.title,
                "url": doc.url,
                "version": doc.version,
                "fingerprint": doc.fingerprint,
                "updated_at": doc.updated_at,
                "location": doc.location,
                "confidential": int(confidential),
                "chunks": 0,
            },
            chunks=chunks,
        )
        if self.judge is None or not judged or not chunks:
            plan.record["chunks"] = len(chunks)
            return plan

        confidences: list[float] = []
        topics = self.zs.config.retrieval.topics
        kept: list[Chunk] = []
        for chunk, (keep, route) in zip(chunks, self.classify([c.text for c in chunks], topics), strict=True):
            confidences.append(keep.confidence)
            if keep.yes:
                chunk.topics = topics_from(route)
                kept.append(chunk)
        plan.dropped = len(chunks) - len(kept)
        # Keep the original positions so chunk ids stay stable across versions.
        plan.chunks = kept
        plan.record["chunks"] = len(kept)

        pairs: list[tuple[Chunk, dict[str, Any]]] = []
        for chunk in kept:
            existing = self._closest_other(chunk)
            if existing is not None:
                pairs.append((chunk, existing))
        if pairs:
            answers = self.judge.compare([(c.text, e["text"]) for c, e in pairs])
            for (chunk, existing), pick in zip(pairs, answers, strict=True):
                confidences.append(pick.confidence)
                if pick.choice not in ("changed", "conflict"):
                    continue
                # Newer wins. Equal dates: the document being synced now is the newer word.
                if (doc.updated_at or "") >= (existing["updated_at"] or ""):
                    plan.supersedes.append(
                        Supersede(
                            existing["id"],
                            existing["doc_id"],
                            existing["title"],
                            doc.doc_id,
                            doc.title,
                            pick.choice,
                            text_hash(existing["text"]),
                        )
                    )
                else:
                    plan.supersedes.append(
                        Supersede(
                            chunk.id,
                            doc.doc_id,
                            doc.title,
                            existing["doc_id"],
                            existing["title"],
                            pick.choice,
                            text_hash(chunk.text),
                        )
                    )
        plan.confidence = min(confidences) if confidences else 1.0
        plan.engine = self.judge.label
        return plan

    def classify(self, texts: list[str], topics: list[str]) -> list[tuple[YesNo, Pick]]:
        """`keep` and `route` for each text, asking the engine only about texts it hasn't seen.

        An edit to a long document changes a few chunks; the rest get the answers the
        same engine gave before, for the same questions and topics.
        """
        assert self.judge is not None
        keys = [classify_key(text, topics) for text in texts]
        engine = self.judge.cache_id
        saved = self.zs.state.judgments(engine, sorted(set(keys)))
        known = {key: _answer_from(answer) for key, answer in saved.items()}
        ask = {key: text for key, text in zip(keys, texts, strict=True) if key not in known}
        if ask:
            answers = self.judge.classify(list(ask.values()), topics)
            fresh = dict(zip(ask, answers, strict=True))
            self.zs.state.save_judgments(engine, {key: _answer_to(a) for key, a in fresh.items()})
            known.update(fresh)
        self.remembered += len(texts) - len(ask)
        return [known[key] for key in keys]

    def _closest_other(self, chunk: Chunk) -> dict[str, Any] | None:
        """The nearest live fact from another document that this chunk may compare against."""
        rows = self.zs.facts.search(
            chunk.text,
            include_confidential=True,
            limit=4,
            exclude_doc=chunk.doc_id,
            include_superseded=True,
        )
        for row in rows:
            # Chunks hidden by some other document are out of play; ones hidden by this
            # document's previous version get judged again.
            if row["superseded_by"] not in ("", chunk.doc_id):
                continue
            # Only facts with the same access compete: a confidential document must never
            # hide a fact default tokens can see, nor be named in their audit rows, and a
            # public chunk is never hidden behind a confidential one.
            if bool(row["confidential"]) != chunk.confidential:
                continue
            return row
        return None

    def apply(self, plan: ChangePlan, *, decided_by: str | None = None) -> None:
        """Make the change live, write the audit row, and close older pending reviews."""
        facts, state = self.zs.facts, self.zs.state
        # Embedding happens first inside replace_document, so if it fails nothing has changed.
        facts.replace_document(plan.doc_id, plan.chunks)
        facts.restore_superseded(plan.doc_id)  # undo what the previous version hid
        by_doc: dict[str, list[str]] = {}
        for s in plan.supersedes:
            row = facts.get(s.chunk_id, include_confidential=True)
            # Skip a judgment that no longer applies: the chunk changed, or someone else hid it.
            if s.still_holds(row) and row["superseded_by"] in ("", s.by_doc):
                by_doc.setdefault(s.by_doc, []).append(s.chunk_id)
        for winner, ids in by_doc.items():
            facts.supersede(ids, winner)
        state.upsert_document(plan.record)
        self.close_pending(plan.doc_id, "a newer version was applied")
        if plan.is_noise and plan.action == "added":
            state.audit(
                "document_skipped",
                doc_id=plan.doc_id,
                source=plan.record["source"],
                detail=f"{plan.title} (only noise)",
                confidence=plan.confidence,
                engine=plan.engine,
                confidential=bool(plan.record["confidential"]),
            )
            return
        detail = plan.title + (f" ({plan.note()})" if plan.note() else "")
        if decided_by:
            detail += f"; approved by {decided_by}"
        state.audit(
            f"document_{plan.action}",
            doc_id=plan.doc_id,
            source=plan.record["source"],
            detail=detail,
            confidence=plan.confidence,
            engine=plan.engine,
            confidential=bool(plan.record["confidential"]),
        )

    def queue(self, plan: ChangePlan, *, rules: str | None = None) -> int:
        """Send a low-confidence change to the review queue; the live version stays.

        `rules` is the confidentiality rules' fingerprint at the time, so an approval
        after the rules change can't publish a document under the old access level.
        """
        self.close_pending(plan.doc_id, "a newer version arrived", kinds=("change",))
        payload = {**plan.to_payload(), "rules": rules}
        item = self.zs.state.add_review_item(
            "change", payload, confidence=plan.confidence, created_by=LIBRARIAN, doc_id=plan.doc_id
        )
        self.zs.state.audit(
            "change_queued",
            doc_id=plan.doc_id,
            source=plan.record["source"],
            detail=f"{plan.title} (review item {item})",
            confidence=plan.confidence,
            engine=plan.engine,
            confidential=bool(plan.record["confidential"]),
        )
        return item

    def waiting(self, doc_id: str, fingerprint: str, confidential: bool) -> bool:
        """This exact version, with this access level, is already waiting for a person."""
        return any(
            item["kind"] == "change"
            and item["payload"]["record"]["fingerprint"] == fingerprint
            and bool(item["payload"]["record"]["confidential"]) == confidential
            for item in self.zs.state.pending_for_document(doc_id)
        )

    def close_pending(self, doc_id: str, why: str, kinds: tuple[str, ...] = ("change", "delete")) -> None:
        for item in self.zs.state.pending_for_document(doc_id):
            if item["kind"] in kinds:
                self.zs.state.decide_review_item(item["id"], approve=False, decided_by=f"{LIBRARIAN}: {why}")

    # Deletions ---------------------------------------------------------------

    def decide_delete(self, record: dict[str, Any], added: list[dict[str, Any]]) -> DeleteDecision:
        """What to do about a document its source no longer lists. Raises EngineError."""
        if self.judge is None:
            return DeleteDecision("retire")
        chunks = self.zs.facts.chunks_of(record["doc_id"])
        excerpt = "\n\n".join(c["text"] for c in chunks)[:EXCERPT]
        candidates: list[dict[str, Any]] = []
        seen = {record["doc_id"]}
        confidential = bool(record["confidential"])
        # Candidates share the deleted file's access level, so a public audit row never names a
        # confidential document.
        for doc in added:  # documents that appeared in this same sync come first
            if doc["doc_id"] in seen or doc["confidential"] != confidential:
                continue
            if len(candidates) < MAX_CANDIDATES:
                seen.add(doc["doc_id"])
                candidates.append({**doc, "added_this_sync": True})
        for row in self.zs.facts.search(excerpt or record["title"], include_confidential=True, limit=10):
            if row["doc_id"] in seen or len(candidates) >= MAX_CANDIDATES:
                continue
            if bool(row["confidential"]) != confidential:
                continue
            seen.add(row["doc_id"])
            candidates.append(
                {
                    "doc_id": row["doc_id"],
                    "title": row["title"],
                    "source": row["source"],
                    "url": row["source_url"],
                    "excerpt": row["text"],
                    "added_this_sync": False,
                }
            )
        for c in candidates:
            if "excerpt" not in c:
                c["excerpt"] = "\n\n".join(x["text"] for x in self.zs.facts.chunks_of(c["doc_id"]))[:EXCERPT]
        pick = self.judge.on_delete(
            DeletedFile(record["title"], record["source"], record["location"] or "", excerpt),
            [Candidate(c["title"], c["source"], c["excerpt"], c["added_this_sync"]) for c in candidates],
        )
        moved_to = None
        action = pick.choice
        if action.startswith(MOVED_PREFIX):
            index = int(action[len(MOVED_PREFIX) :])
            moved_to = {k: candidates[index][k] for k in ("doc_id", "title", "source", "url")}
            action = "moved"
        if action == "unsure" or pick.confidence < self.threshold:
            return DeleteDecision(
                "review", pick.confidence, self.judge.label, moved_to=moved_to, suggestion=action
            )
        return DeleteDecision(action, pick.confidence, self.judge.label, moved_to=moved_to)

    def retire(
        self,
        record: dict[str, Any],
        reason: str,
        *,
        action: str = "document_retired",
        confidence: float | None = None,
        engine: str | None = None,
    ) -> None:
        self.zs.facts.delete_document(record["doc_id"])
        self.zs.facts.restore_superseded(record["doc_id"])
        self.zs.state.delete_document(record["doc_id"])
        self.close_pending(record["doc_id"], "the document was retired")
        self.zs.state.audit(
            action,
            doc_id=record["doc_id"],
            source=record["source"],
            detail=f"{record['title']} ({reason})",
            confidence=confidence,
            engine=engine,
            confidential=bool(record["confidential"]),
        )

    def keep_deleted(self, record: dict[str, Any], *, confidence: float | None, engine: str | None) -> None:
        self.zs.state.mark_source_deleted(record["doc_id"], True)
        self.zs.state.audit(
            "document_kept",
            doc_id=record["doc_id"],
            source=record["source"],
            detail=f"{record['title']} (deleted from the source; its facts are kept)",
            confidence=confidence,
            engine=engine,
            confidential=bool(record["confidential"]),
        )

    def queue_delete(self, record: dict[str, Any], decision: DeleteDecision) -> int:
        payload = {
            "record": record,
            "title": record["title"],
            "source": record["source"],
            "url": record["url"],
            "suggestion": decision.suggestion,
            "moved_to": decision.moved_to,
            "confidence": decision.confidence,
            "engine": decision.engine,
        }
        item = self.zs.state.add_review_item(
            "delete", payload, confidence=decision.confidence, created_by=LIBRARIAN, doc_id=record["doc_id"]
        )
        self.zs.state.audit(
            "delete_queued",
            doc_id=record["doc_id"],
            source=record["source"],
            detail=f"{record['title']} (review item {item})",
            confidence=decision.confidence,
            engine=decision.engine,
            confidential=bool(record["confidential"]),
        )
        return item
