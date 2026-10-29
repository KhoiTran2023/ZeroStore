"""Folders on this computer: every text file in them, and in the folders inside them.

Made for trying ZeroStore on a laptop: point it at a folder of notes, docs or
exported pages, and agents can ask about them. Files may start with the same
small `---` header the demo uses (title, updated, labels); without one, the
file name is the title and the modified time is the date.

Hidden files and folders (starting with `.`) are skipped, and so is anything
that isn't valid text. Only files matching `include` are read.
"""

from __future__ import annotations

import fnmatch
import os
from datetime import datetime, timezone
from pathlib import Path

from zerostore.connectors.base import Connector, ConnectorError, ItemRef, SourceDocument
from zerostore.connectors.demo import split_header

SOURCE = "folders"
MAX_BYTES = 2_000_000  # larger files are almost never notes; skip rather than chunk a dump


class FolderConnector(Connector):
    name = SOURCE

    def __init__(self, paths: list[str], include: list[str]):
        self.paths = [Path(p).expanduser() for p in paths]
        self.include = include

    def _wanted(self, name: str) -> bool:
        return any(fnmatch.fnmatch(name.lower(), pattern.lower()) for pattern in self.include)

    def _files(self) -> list[tuple[Path, Path]]:
        """Every file in scope, with the folder it was found under."""
        found = []
        for base in self.paths:
            if not base.is_dir():
                # Like a Drive folder that was renamed: say so rather than retire everything in it.
                raise ConnectorError(
                    f"the folder {base} isn't there (moved, renamed or on a disk that's off?)"
                )
            for root, dirs, files in os.walk(base, onerror=None):
                dirs[:] = sorted(d for d in dirs if not d.startswith("."))
                for name in sorted(files):
                    path = Path(root) / name
                    if name.startswith(".") or not self._wanted(name) or path.is_symlink():
                        continue
                    found.append((path, base))
        return found

    def list_items(self) -> list[ItemRef]:
        refs = []
        for path, base in self._files():
            try:
                info = path.stat()
            except OSError as exc:
                raise ConnectorError(f"couldn't read {path}: {exc}") from None
            if info.st_size > MAX_BYTES:
                continue
            refs.append(
                ItemRef(
                    f"{SOURCE}:{path}",
                    f"{info.st_mtime_ns}:{info.st_size}",
                    path.stem,
                    {"path": path, "base": base, "mtime": info.st_mtime},
                )
            )
        return refs

    def fetch(self, ref: ItemRef) -> SourceDocument | None:
        path: Path = ref.extra["path"]
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise ConnectorError(f"couldn't read {path}: {exc}") from None
        try:
            meta, text = split_header(raw.decode("utf-8"))
        except UnicodeDecodeError:
            return None  # not text, whatever its name says
        if not text.strip():
            return None
        updated = meta.get("updated") or datetime.fromtimestamp(ref.extra["mtime"], tz=timezone.utc)
        return SourceDocument(
            doc_id=ref.doc_id,
            source=SOURCE,
            title=str(meta.get("title") or path.stem),
            text=text,
            url=path.resolve().as_uri(),
            updated_at=updated.isoformat() if hasattr(updated, "isoformat") else str(updated),
            version=ref.version,
            location=str(path.parent),
            labels=[str(label) for label in meta.get("labels") or []],
        )
