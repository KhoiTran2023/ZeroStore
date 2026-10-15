"""decisions.md: facts agents proposed and a person approved.

Approved proposals are appended to this file, which is read back like any
other source, so they flow through the same sync, audit table and search.
Facts proposed through a confidential token go to a second file next to it
(decisions-confidential.md), whose facts only confidential and owner tokens see.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from pathlib import Path

from zerostore.connectors.base import Connector, ConnectorError, ItemRef, SourceDocument

SOURCE = "decisions"
DOC_ID = "decisions:decisions.md"
CONFIDENTIAL_DOC_ID = "decisions:decisions-confidential.md"
TITLE = "Approved decisions"
CONFIDENTIAL_TITLE = "Approved decisions (confidential)"
HEADER = (
    "# {title}\n\n"
    "Facts that agents proposed and a person approved. ZeroStore reads this file as a source;\n"
    "edit or delete lines here to change what agents are told.\n"
)

_write_lock = threading.Lock()


def confidential_path(path: Path) -> Path:
    return path.with_name(f"{path.stem}-confidential{path.suffix or '.md'}")


def append_decision(
    path: Path, fact: str, approved_by: str, quote: str = "", *, confidential: bool = False
) -> Path:
    """Add one approved fact as a dated section. Returns the file written."""
    target = confidential_path(path) if confidential else path
    target.parent.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).date().isoformat()
    entry = f"\n## {today}\n\n{fact.strip()}\n"
    if quote.strip():
        entry += f"\n> Context: {' '.join(quote.split())}\n"
    entry += f"\nApproved by {approved_by}.\n"
    with _write_lock:
        try:  # create with the header exactly once, even if two approvals race
            with open(target, "x", encoding="utf-8") as fh:
                fh.write(HEADER.format(title=CONFIDENTIAL_TITLE if confidential else TITLE))
        except FileExistsError:
            pass
        # Append-only, so a concurrent writer (another process) can't erase this entry.
        with open(target, "a", encoding="utf-8") as fh:
            fh.write(entry)
    return target


class DecisionsConnector(Connector):
    name = SOURCE

    def __init__(self, path: Path):
        self.files = {
            DOC_ID: (path, TITLE),
            CONFIDENTIAL_DOC_ID: (confidential_path(path), CONFIDENTIAL_TITLE),
        }

    def list_items(self) -> list[ItemRef]:
        refs = []
        for doc_id, (path, title) in self.files.items():
            if not path.exists():
                continue
            try:
                stat = path.stat()
            except OSError as exc:
                raise ConnectorError(f"couldn't read {path}: {exc}") from exc
            refs.append(ItemRef(doc_id=doc_id, version=f"{stat.st_mtime_ns}:{stat.st_size}", title=title))
        return refs

    def fetch(self, ref: ItemRef) -> SourceDocument | None:
        path, title = self.files[ref.doc_id]
        try:
            text = path.read_text(encoding="utf-8")
            modified = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
        except OSError as exc:
            raise ConnectorError(f"couldn't read {path}: {exc}") from exc
        if not text.strip():
            return None
        return SourceDocument(
            doc_id=ref.doc_id,
            source=SOURCE,
            title=title,
            text=text,
            url=path.resolve().as_uri(),
            updated_at=modified.isoformat(timespec="seconds"),
            version=ref.version,
            location=str(path.parent),
        )
