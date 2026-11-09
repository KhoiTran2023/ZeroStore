"""Deciding review items: what approving or rejecting each kind does.

- `change`: a low-confidence edit or new document. Approve makes it live;
  reject keeps the current version and won't propose this version again.
- `delete`: a document that left its source and the librarian wasn't sure.
  Approve retires it (or records the move); reject keeps its facts.
- `agent_proposal`: a fact an agent suggested. Approve writes it to
  decisions.md, which is read back as a source; reject drops it.
- `agent_write`: a change an agent's role allows, queued because
  `agent_writes.review` is on. Approve carries it out (librarian/writes.py);
  reject drops it.

The MCP tools, the CLI and the web UI all go through `decide`.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any

from zerostore.app import ZeroStore

log = logging.getLogger("zerostore.review")

REVIEW_LOCK_TTL = 10 * 60


class ReviewError(Exception):
    pass


@dataclass
class Outcome:
    id: int
    kind: str
    status: str  # approved or rejected
    message: str


def describe(item: dict[str, Any]) -> dict[str, Any]:
    """A compact view of a review item for people and owner tools."""
    p = item["payload"]
    view: dict[str, Any] = {
        "id": item["id"],
        "kind": item["kind"],
        "created_at": item["created_at"],
        "confidence": item["confidence"],
    }
    if item["kind"] == "change":
        view.update(
            document=p["title"],
            source=p["source"],
            url=p["url"],
            change=p["action"],
            engine=p.get("engine"),
            note=p.get("note") or None,
            preview=p.get("preview"),
        )
    elif item["kind"] == "delete":
        view.update(
            document=p["title"],
            source=p["source"],
            url=p["url"],
            change="deleted from the source",
            engine=p.get("engine"),
            suggestion=p.get("suggestion"),
            moved_to=(p.get("moved_to") or {}).get("title"),
        )
    elif item["kind"] == "agent_write":
        from zerostore.librarian.writes import describe as describe_write

        view.update(describe_write(p), proposed_by=item.get("created_by"))
    else:
        view.update(
            fact=p.get("fact"),
            quote=p.get("quote"),
            proposed_by=item.get("created_by"),
            confidential=bool(p.get("confidential")),
        )
    return {k: v for k, v in view.items() if v is not None}


SHOWN_TEXTS = 3
SHOWN_CHARS = 400


def _shown(document: str, text: str) -> dict[str, str]:
    text = text.strip()
    if len(text) > SHOWN_CHARS:
        text = text[: SHOWN_CHARS - 3].rstrip() + "..."
    return {"document": document, "text": text}


def before_after(zs: ZeroStore, item: dict[str, Any]) -> dict[str, list[dict[str, str]]]:
    """What agents are told now, and what they'd be told if the item is approved.

    For a change: the document's text that would go away plus other documents'
    facts it would hide, against its new text. For a delete: the document's
    facts. For a proposal: the proposed fact.
    """
    p = item["payload"]
    before: list[dict[str, str]] = []
    after: list[dict[str, str]] = []
    if item["kind"] == "change":
        title, doc_id = p["title"], p["record"]["doc_id"]
        live = [r["text"] for r in zs.facts.chunks_of(doc_id) if not r.get("superseded_by")]
        new = [c["text"] for c in p["chunks"]]
        before = [_shown(title, t) for t in live if t not in set(new)]
        for s in p.get("supersedes", []):
            if s.get("by_doc") != doc_id:
                continue
            row = zs.facts.get(s["chunk_id"], include_confidential=True)
            if row and not row.get("superseded_by"):
                before.append(_shown(s["title"], row["text"]))
        after = [_shown(title, t) for t in new if t not in set(live)]
    elif item["kind"] == "delete":
        live = zs.facts.chunks_of(item["doc_id"] or "")
        before = [_shown(p["title"], r["text"]) for r in live if not r.get("superseded_by")]
    elif item["kind"] == "agent_write":
        if p.get("before"):
            before = [_shown(p.get("corrects_title") or "written fact", p["before"])]
        if p.get("text"):
            after = [_shown("changed fact", p["text"])]
    else:
        after = [_shown("proposed fact", p.get("fact") or "")]
    return {"before": before[:SHOWN_TEXTS], "after": after[:SHOWN_TEXTS]}


def decide(
    zs: ZeroStore,
    item_id: int,
    approve: bool,
    decided_by: str,
    *,
    fact: str | None = None,
) -> Outcome:
    """Approve or reject one pending item and carry out what that means."""
    from zerostore.librarian.ingestion import SYNC_LOCK

    item = zs.state.get_review_item(item_id)
    if item is None or item["status"] != "pending":
        raise ReviewError(f"no pending review item with id {item_id}")
    kind = item["kind"]
    if kind in ("change", "delete"):
        # Changes to the fact store wait for a running sync, which may be touching the same document.
        holder = f"review:{item_id}:{uuid.uuid4().hex[:8]}"
        if not zs.state.acquire_lock(SYNC_LOCK, holder, REVIEW_LOCK_TTL):
            raise ReviewError("a sync is running right now; try again when it finishes")
        try:
            return _decide(zs, item, approve, decided_by, fact)
        finally:
            zs.state.release_lock(SYNC_LOCK, holder)
    return _decide(zs, item, approve, decided_by, fact)


def _decide(zs: ZeroStore, item: dict[str, Any], approve: bool, decided_by: str, fact: str | None) -> Outcome:
    from zerostore.librarian.ingestion import rules_fingerprint
    from zerostore.librarian.update import ChangePlan, Librarian

    item_id, kind, payload = item["id"], item["kind"], item["payload"]
    if kind == "change" and approve and payload.get("rules") not in (None, rules_fingerprint(zs.config)):
        raise ReviewError(
            "the confidentiality rules changed since this was queued; the next sync checks it again"
        )
    if not zs.state.decide_review_item(item_id, approve, decided_by=decided_by):
        raise ReviewError(f"review item {item_id} was decided by someone else just now")

    librarian = Librarian(zs, judge=None)
    try:
        if kind == "change":
            plan = ChangePlan.from_payload(payload)
            if approve:
                librarian.apply(plan, decided_by=decided_by)
                message = f"{plan.title} is live now"
            else:
                if plan.doc_id not in zs.state.documents(plan.record["source"]):
                    zs.state.upsert_document({**plan.record, "chunks": 0})
                zs.state.reject_version(plan.doc_id, plan.record["fingerprint"])
                message = f"kept the current version of {plan.title}"
        elif kind == "delete":
            record = zs.state.documents().get(item["doc_id"] or "")
            if record is None:
                message = "that document is already gone"
            elif approve and payload.get("moved_to"):
                note = f"moved to {payload['moved_to']['title']}; approved by {decided_by}"
                librarian.retire(record, note, action="document_moved")
                message = f"{record['title']} is retired; its facts live in {payload['moved_to']['title']}"
            elif approve:
                librarian.retire(record, f"deleted from the source; approved by {decided_by}")
                message = f"{record['title']} is retired"
            else:
                librarian.keep_deleted(record, confidence=None, engine=None)
                message = f"kept the facts from {record['title']}"
        elif kind == "agent_proposal":
            message = _decide_proposal(zs, payload, approve, decided_by, fact)
        elif kind == "agent_write":
            message = _decide_write(zs, item, approve, fact)
        else:
            raise ReviewError(f"review item {item_id} has an unknown kind {kind!r}")
    except Exception:
        zs.state.reopen_review_item(item_id)
        raise

    status = "approved" if approve else "rejected"
    zs.state.audit(f"review_{status}", doc_id=item["doc_id"], detail=f"{item_id} ({kind}) by {decided_by}")
    return Outcome(item_id, kind, status, message)


def _decide_write(zs: ZeroStore, item: dict[str, Any], approve: bool, fact: str | None) -> str:
    from zerostore.librarian.writes import WriteRefused, carry_out

    if not approve:
        return "the change was dropped"
    payload = dict(item["payload"])
    if fact and fact.strip() and payload.get("op") != "remove":
        payload["text"] = fact.strip()
    try:
        return carry_out(zs, payload, item.get("created_by") or "an agent").message
    except WriteRefused as exc:
        raise ReviewError(str(exc)) from None


def _decide_proposal(
    zs: ZeroStore, payload: dict[str, Any], approve: bool, decided_by: str, fact: str | None
) -> str:
    from zerostore.connectors.decisions import append_decision
    from zerostore.librarian.ingestion import Ingestor, SyncBusy

    if not approve:
        return "the proposal was dropped"
    text = (fact or payload["fact"]).strip()
    if not text:
        raise ReviewError("the fact is empty")
    path = append_decision(
        zs.config.decisions_path(),
        text,
        decided_by,
        payload.get("quote", ""),
        confidential=bool(payload.get("confidential")),
    )
    if not zs.config.agent_proposals:
        return f"saved to {path}"
    try:
        Ingestor(zs, judge=None).sync(only=["decisions"])
    except SyncBusy:
        return f"saved to {path}; agents will see it after the sync that's running now"
    return "saved; agents can find it now"
