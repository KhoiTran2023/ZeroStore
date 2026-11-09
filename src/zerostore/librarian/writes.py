"""Agents changing company knowledge, within what their role allows.

Off unless `agent_writes.enabled` is true. `agent_writes.roles` lists, per role,
the tags whose information its agents may change (`untagged` for information with
no tag); owner tokens may change everything. Every write is checked against the
information it touches: a new fact's tag, or the tags and confidential flag of the
fact being changed. Anything outside the role's list is refused (and logged by the
MCP server, like every call).

Nothing here writes into the fact store. A change becomes a file in the written
folder (connectors/written.py), which the sync reads like any other source, so the
next timed sync keeps it instead of undoing it:

- add: a new entry with the chosen tag.
- change a written fact: its entry is rewritten.
- change a fact from another source (Drive, GitHub, ...): a correction entry with
  the same tags, which hides the original fact while its text is unchanged.
- remove a written fact: its entry is deleted. Facts from other sources can't be
  removed here, only corrected, because ZeroStore never edits sources.

With `agent_writes.review: true` the change waits in the review queue (kind
`agent_write`) and is carried out when a person approves it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from zerostore.access.roles import key_tags, write_problem
from zerostore.access.tokens import TokenInfo
from zerostore.app import ZeroStore
from zerostore.connectors import written as w


class WriteRefused(Exception):
    """The change isn't allowed, or doesn't make sense."""


@dataclass
class Result:
    status: str  # saved, waiting for approval
    message: str
    fact_ids: list[str]
    review_id: int | None = None


def _check(zs: ZeroStore, caller: TokenInfo, tags: list[str], confidential: bool) -> None:
    problem = write_problem(
        zs.config,
        caller.role,
        tags,
        confidential,
        owner=caller.level.is_owner,
        sees_confidential=caller.level.sees_confidential,
    )
    if problem:
        raise WriteRefused(problem)


def _clean(text: str) -> str:
    text = (text or "").strip()
    if not text:
        raise WriteRefused("the text is empty")
    if len(text) > w.MAX_CHARS:
        raise WriteRefused(f"keep each fact under {w.MAX_CHARS} characters; split longer text into several")
    return text


def _target(zs: ZeroStore, fact_id: str, hidden: list[str], caller: TokenInfo) -> dict[str, Any]:
    row = zs.facts.get(fact_id, include_confidential=caller.level.sees_confidential, hidden_tags=hidden)
    if row is None:
        raise WriteRefused(f"no fact with id {fact_id}")
    if row.get("superseded_by"):
        raise WriteRefused(
            "this fact is outdated (a newer one replaced it); search again and change that one"
        )
    return row


def plan_add(
    zs: ZeroStore, caller: TokenInfo, text: str, tag: str | None, confidential: bool
) -> dict[str, Any]:
    tag = (tag or "").strip().lower() or None
    if tag is None and not caller.level.is_owner:
        # No tag given: when the role may change exactly one tag (and not untagged), use it.
        scope = set(zs.config.agent_writes.roles.get(caller.role or "", []))
        if len(scope) == 1 and "untagged" not in scope:
            tag = next(iter(scope))
    if tag == "untagged":
        tag = None
    if tag and tag not in zs.config.tags:
        names = ", ".join(zs.config.tags) or "none yet"
        raise WriteRefused(f"there is no tag called {tag!r} (tags: {names})")
    _check(zs, caller, [tag] if tag else [], confidential)
    return {"op": "add", "text": _clean(text), "tag": tag, "confidential": confidential}


def plan_change(
    zs: ZeroStore, caller: TokenInfo, fact_id: str, text: str, hidden: list[str]
) -> dict[str, Any]:
    row = _target(zs, fact_id, hidden, caller)
    tags, confidential = key_tags(row["access"]), bool(row["confidential"])
    _check(zs, caller, tags, confidential)
    text = _clean(text)
    entry_id = w.entry_id_of(row["doc_id"])
    if entry_id:
        return {"op": "change", "entry": entry_id, "text": text, "before": row["text"]}
    from zerostore.librarian.update import text_hash

    if len(tags) > 1:
        raise WriteRefused("this fact has several tags; change it in its source instead")
    return {
        "op": "correct",
        "text": text,
        "tag": tags[0] if tags else None,
        "confidential": confidential,
        "corrects": row["id"],
        "corrects_hash": text_hash(row["text"]),
        "corrects_title": row["title"],
        "before": row["text"],
    }


def plan_remove(zs: ZeroStore, caller: TokenInfo, fact_id: str, hidden: list[str]) -> dict[str, Any]:
    row = _target(zs, fact_id, hidden, caller)
    _check(zs, caller, key_tags(row["access"]), bool(row["confidential"]))
    entry_id = w.entry_id_of(row["doc_id"])
    if entry_id:
        return {"op": "remove", "entry": entry_id, "before": row["text"]}
    from zerostore.librarian.update import text_hash

    # A fact from a document: ZeroStore never edits the source, so it hides the fact instead.
    return {
        "op": "hide",
        "text": "",
        "confidential": bool(row["confidential"]),
        "corrects": row["id"],
        "corrects_hash": text_hash(row["text"]),
        "corrects_title": row["title"],
        "before": row["text"],
    }


def submit(zs: ZeroStore, caller: TokenInfo, payload: dict[str, Any]) -> Result:
    """Carry out a checked change now, or queue it when the settings ask for review."""
    if zs.config.agent_writes.review and not caller.level.is_owner:
        item_id = zs.state.add_review_item("agent_write", payload, created_by=caller.name)
        return Result(
            "waiting for approval",
            "A person will review this change. Until then agents are told what they were told before.",
            [],
            item_id,
        )
    return carry_out(zs, payload, caller.name)


def carry_out(zs: ZeroStore, payload: dict[str, Any], written_by: str) -> Result:
    """Write the change to the written folder and read it back so agents see it now."""
    from zerostore.librarian.ingestion import Ingestor, SyncBusy
    from zerostore.store.facts import chunk_id

    folder = zs.config.agent_writes.folder_path()
    op = payload["op"]
    if op in ("add", "correct", "hide"):
        entry = w.Entry(
            id=w.new_id(),
            text=payload["text"],
            written_by=written_by,
            tag=payload.get("tag"),
            confidential=bool(payload.get("confidential")),
            corrects=payload.get("corrects"),
            corrects_hash=payload.get("corrects_hash"),
            corrects_title=payload.get("corrects_title"),
        )
        w.write_entry(folder, entry)
        verb = {"correct": "corrected", "hide": "removed"}.get(op, "added")
    elif op == "change":
        entry = w.read_entry(folder, payload["entry"])
        if entry is None:
            raise WriteRefused("that fact was removed meanwhile")
        entry.text, entry.written_by = payload["text"], written_by
        w.write_entry(folder, entry)
        verb = "changed"
    elif op == "remove":
        if not w.delete_entry(folder, payload["entry"]):
            raise WriteRefused("that fact was already removed")
        entry, verb = None, "removed"
    else:
        raise WriteRefused(f"unknown change {op!r}")

    try:
        Ingestor(zs).sync(only=["written"])
    except SyncBusy:
        return Result("saved", f"{verb}; agents will see it after the sync that's running now", [])
    ids = [r["id"] for r in zs.facts.chunks_of(entry.doc_id)] if entry and not entry.removes else []
    if (
        entry and not entry.removes and not ids
    ):  # one chunk at least, by position, if the store was slow to show it
        ids = [chunk_id(entry.doc_id, 0)]
    return Result("saved", f"{verb}; agents see it now", ids)


def describe(payload: dict[str, Any]) -> dict[str, Any]:
    """A compact view of a queued change for the review queue."""
    view = {
        "change": {
            "add": "add a fact",
            "change": "change a fact",
            "correct": "correct a fact",
            "remove": "remove a fact",
            "hide": "remove a fact",
        }.get(payload.get("op", ""), payload.get("op")),
        "fact": payload.get("text"),
        "tag": payload.get("tag"),
        "document": payload.get("corrects_title"),
        "confidential": bool(payload.get("confidential")) or None,
    }
    return {k: v for k, v in view.items() if v is not None}
