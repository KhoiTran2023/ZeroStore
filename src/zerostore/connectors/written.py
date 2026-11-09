"""Written knowledge: what agents added or changed, saved as files ZeroStore reads back.

Agents never write into the fact store. A change from an agent (allowed by its
role, see librarian/writes.py) becomes one Markdown file in the written folder
(default `~/.zerostore/written/`), and that folder is a source like any other.
So every sync reads the change back in instead of undoing it.

Each file is one entry with a small `---` header:

    ---
    tag: sales              # the tag its facts carry (none: untagged)
    confidential: false
    written_by: cursor-laptop
    corrects: 3f2a...       # optional: the fact from another source this replaces
    corrects_hash: 9b1c...  # that fact's text when it was corrected
    corrects_title: Pricing 2026
    ---
    The Pro plan costs $29 a month.

A correction hides the fact it names (the "newer wins" rule) for as long as that
fact's text is unchanged. When someone edits the original document, the source is
the newer word again and the old fact's hiding ends.
"""

from __future__ import annotations

import os
import re
import secrets
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from zerostore.connectors.base import Connector, ConnectorError, ItemRef, SourceDocument
from zerostore.connectors.demo import split_header

SOURCE = "written"
PREFIX = f"{SOURCE}:"
ENTRY_ID = re.compile(r"[0-9a-f]{12}")
MAX_CHARS = 8000


@dataclass
class Entry:
    id: str
    text: str
    written_by: str
    tag: str | None = None
    confidential: bool = False
    corrects: str | None = None
    corrects_hash: str | None = None
    corrects_title: str | None = None

    @property
    def doc_id(self) -> str:
        return PREFIX + self.id

    @property
    def removes(self) -> bool:
        """A removal: hides the fact it names and says nothing itself."""
        return bool(self.corrects) and not self.text.strip()

    @property
    def title(self) -> str:
        if self.removes:
            return f"Removed from {self.corrects_title or 'a document'} (by {self.written_by})"
        if self.corrects_title:
            return f"Correction to {self.corrects_title} (by {self.written_by})"
        return f"Written by {self.written_by}"

    def header(self) -> dict[str, Any]:
        head: dict[str, Any] = {
            "tag": self.tag,
            "confidential": self.confidential,
            "written_by": self.written_by,
        }
        if self.corrects:
            head.update(
                corrects=self.corrects, corrects_hash=self.corrects_hash, corrects_title=self.corrects_title
            )
        return head


def new_id() -> str:
    return secrets.token_hex(6)


def entry_id_of(doc_id: str) -> str | None:
    """The entry id behind a written document id, or None for other sources."""
    if doc_id.startswith(PREFIX) and ENTRY_ID.fullmatch(doc_id[len(PREFIX) :]):
        return doc_id[len(PREFIX) :]
    return None


def entry_path(folder: Path, entry_id: str) -> Path:
    if not ENTRY_ID.fullmatch(entry_id):
        raise ValueError(f"not a written entry id: {entry_id!r}")
    return folder / f"{entry_id}.md"


def write_entry(folder: Path, entry: Entry) -> Path:
    """Save an entry in one step (a reader never sees half a file)."""
    folder.mkdir(parents=True, exist_ok=True)
    target = entry_path(folder, entry.id)
    text = entry.text.strip()
    body = (
        "---\n" + yaml.safe_dump(entry.header(), sort_keys=False) + "---\n" + (f"\n{text}\n" if text else "")
    )
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(body)
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return target


def read_entry(folder: Path, entry_id: str) -> Entry | None:
    path = entry_path(folder, entry_id)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as exc:
        raise ConnectorError(f"couldn't read {path}: {exc}") from exc
    return _parse(entry_id, raw)


def delete_entry(folder: Path, entry_id: str) -> bool:
    try:
        entry_path(folder, entry_id).unlink()
    except FileNotFoundError:
        return False
    return True


def _text(value: Any) -> str | None:
    if value is None:
        return None
    return str(value).strip() or None


def _parse(entry_id: str, raw: str) -> Entry:
    meta, body = split_header(raw)
    return Entry(
        id=entry_id,
        text=body.strip(),
        written_by=_text(meta.get("written_by")) or "an agent",
        tag=_text(meta.get("tag")),
        confidential=meta.get("confidential") is True,
        corrects=_text(meta.get("corrects")),
        corrects_hash=_text(meta.get("corrects_hash")),
        corrects_title=_text(meta.get("corrects_title")),
    )


def entries(folder: Path) -> list[Entry]:
    """Every readable entry in the folder."""
    found = []
    for path in sorted(folder.glob("*.md")) if folder.is_dir() else []:
        if ENTRY_ID.fullmatch(path.stem):
            try:
                found.append(_parse(path.stem, path.read_text(encoding="utf-8")))
            except (OSError, UnicodeDecodeError):
                continue
    return found


class WrittenConnector(Connector):
    name = SOURCE

    def __init__(self, folder: Path):
        self.folder = folder

    def list_items(self) -> list[ItemRef]:
        if not self.folder.is_dir():
            return []
        refs = []
        try:
            paths = sorted(self.folder.glob("*.md"))
            for path in paths:
                if not ENTRY_ID.fullmatch(path.stem):
                    continue
                stat = path.stat()
                refs.append(
                    ItemRef(
                        doc_id=PREFIX + path.stem,
                        version=f"{stat.st_mtime_ns}:{stat.st_size}",
                        title=path.stem,
                        extra={"path": str(path)},
                    )
                )
        except OSError as exc:
            raise ConnectorError(f"couldn't read {self.folder}: {exc}") from exc
        return refs

    def fetch(self, ref: ItemRef) -> SourceDocument | None:
        path = Path(ref.extra["path"])
        try:
            raw = path.read_text(encoding="utf-8")
            modified = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
        except FileNotFoundError:
            return None
        except (OSError, UnicodeDecodeError) as exc:
            raise ConnectorError(f"couldn't read {path}: {exc}") from exc
        entry = _parse(path.stem, raw)
        if not entry.text:
            return None
        return SourceDocument(
            doc_id=entry.doc_id,
            source=SOURCE,
            title=entry.title,
            text=entry.text,
            url=path.resolve().as_uri(),
            updated_at=modified.isoformat(timespec="seconds"),
            version=ref.version,
            location=str(self.folder),
            labels=[entry.tag] if entry.tag else [],
            meta={"confidential": entry.confidential, "tag": entry.tag},
        )


def reapply_corrections(zs: Any) -> int:
    """Hide every fact a live correction or removal replaces, when its text is still the
    one that was corrected, and bring back facts whose removal file is gone.

    Run at the end of each sync, so a correction keeps holding after the original
    document is re-read with that fact unchanged. Returns how many facts were hidden.
    """
    from zerostore.librarian.update import text_hash

    folder = zs.config.agent_writes.folder_path()
    live = zs.state.documents(SOURCE)
    found = entries(folder)
    present = {e.doc_id for e in found}
    for by_doc in zs.facts.superseding_docs(PREFIX):
        if by_doc not in present and by_doc not in live:
            zs.facts.restore_superseded(by_doc)  # its removal or correction was deleted
    hidden = 0
    for entry in found:
        if not entry.corrects:
            continue
        if not entry.removes and not (live.get(entry.doc_id) or {}).get("chunks"):
            continue
        row = zs.facts.get(entry.corrects, include_confidential=True)
        if row is None or row["superseded_by"] or row["doc_id"] == entry.doc_id:
            continue
        if entry.corrects_hash and text_hash(row["text"]) != entry.corrects_hash:
            continue  # the original changed since: the source is the newer word
        zs.facts.supersede([entry.corrects], entry.doc_id)
        hidden += 1
    return hidden
