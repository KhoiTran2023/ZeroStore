"""Simulated sources for `zerostore demo`: Google Drive, GitHub and Linear, read
from a local folder of made-up files instead of the real services.

The folder holds one subfolder per source, laid out like the real thing:

    company/google_drive/<folder path>/<doc>.md     location = the folder path
    company/github/<owner>/<repo>/<path>.md         location = owner/repo
    company/linear/<team>/<issue>.md                location = the team

Each file may start with a small header:

    ---
    title: Pricing 2026
    updated: 2026-09-12
    labels: [pricing]
    ---

The listing only includes the folders, repos and teams named in the demo's
zerostore.yaml, just like the real connectors, so confidentiality rules and
deletions behave the same way.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path

import yaml

from zerostore.config import Config
from zerostore.connectors.base import Connector, ConnectorError, ItemRef, SourceDocument

SUFFIXES = {".md", ".markdown", ".txt"}


def split_header(raw: str) -> tuple[dict, str]:
    """The `---` header as a dict, and the text after it."""
    raw = raw.lstrip("\ufeff").replace("\r\n", "\n")
    if raw.startswith("---\n"):
        head, sep, body = raw[4:].partition("\n---\n")
        if sep:
            try:
                meta = yaml.safe_load(head) or {}
            except yaml.YAMLError:
                meta = {}
            if isinstance(meta, dict):
                return meta, body.lstrip("\n")
    return {}, raw


class DemoConnector(Connector):
    def __init__(self, name: str, root: Path, scopes: list[str]):
        self.name = name
        self.root = root / name
        self.scopes = scopes

    @classmethod
    def from_config(cls, name: str, config: Config) -> DemoConnector:
        root = config.demo_path()
        if root is None:
            raise ConnectorError("this isn't a demo install")
        settings = getattr(config.sources, name)
        scopes = {"google_drive": "folders", "github": "repos", "linear": "teams"}[name]
        return cls(name, root, list(getattr(settings, scopes)))

    def _files(self) -> list[tuple[Path, str]]:
        """Every file in scope, with the scope it was found under."""
        if not self.root.is_dir():
            raise ConnectorError(f"the demo folder {self.root} is missing; run `zerostore demo` again")
        found = []
        for scope in self.scopes:
            base = self.root / scope.strip("/")
            if not base.is_dir():
                # Like a Drive folder that was renamed: say so rather than retire everything in it.
                raise ConnectorError(f"the demo has no {scope} under {self.root}")
            found += [
                (p, scope)
                for p in sorted(base.rglob("*"))
                if p.suffix in SUFFIXES and p.is_file() and not p.name.startswith(".")
            ]
        return found

    def list_items(self) -> list[ItemRef]:
        refs = []
        for path, scope in self._files():
            rel = path.relative_to(self.root).as_posix()
            try:
                version = hashlib.sha256(path.read_bytes()).hexdigest()[:16]
            except OSError as exc:
                raise ConnectorError(f"couldn't read {rel}: {exc}") from None
            refs.append(
                ItemRef(f"{self.name}:demo/{rel}", version, path.stem, {"path": path, "scope": scope})
            )
        return refs

    def fetch(self, ref: ItemRef) -> SourceDocument | None:
        path: Path = ref.extra["path"]
        try:
            meta, text = split_header(path.read_text(encoding="utf-8", errors="replace"))
            mtime = path.stat().st_mtime
        except OSError as exc:
            raise ConnectorError(str(exc)) from None
        if not text.strip():
            return None
        updated = meta.get("updated")
        if updated is None:
            updated = datetime.fromtimestamp(mtime, tz=timezone.utc)
        if self.name == "google_drive":
            location = path.parent.relative_to(self.root).as_posix()
        else:
            location = ref.extra["scope"]
        return SourceDocument(
            doc_id=ref.doc_id,
            source=self.name,
            title=str(meta.get("title") or path.stem),
            text=text,
            url=path.resolve().as_uri(),
            updated_at=updated.isoformat() if hasattr(updated, "isoformat") else str(updated),
            version=ref.version,
            location=location,
            labels=[str(label) for label in meta.get("labels") or []],
        )
