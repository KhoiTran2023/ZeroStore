"""What every connector provides.

A connector only reads. Each sync it lists what the source holds right now
(cheaply: ids and a version marker), and fetches full text only for items
whose version changed since the last sync. Anything the listing no longer
contains was deleted or moved out of scope.
"""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


class ConnectorError(Exception):
    """A source could not be read. The sync skips that source and changes nothing for it."""


@dataclass
class ItemRef:
    """One item as the listing sees it, before its text is fetched."""

    doc_id: str
    version: str  # changes whenever the item changes (modified time, blob sha, ...)
    title: str
    # Connector-private data needed to fetch the item (file id, API payload, ...).
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class SourceDocument:
    doc_id: str
    source: str  # google_drive, github, linear, folders, decisions or written
    title: str
    text: str
    url: str
    updated_at: str  # ISO 8601
    version: str
    location: str = ""  # folder path, repo or team: what confidentiality rules match on
    labels: list[str] = field(default_factory=list)
    # Source-specific details the sync needs (the written source's tag and confidential flag).
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def fingerprint(self) -> str:
        """Content hash, so an edit that changes nothing (a re-save) is skipped."""
        return hashlib.sha256(f"{self.title}\n{self.text}".encode()).hexdigest()


class Connector(ABC):
    #: matches the key in the `sources` section of zerostore.yaml
    name: str

    @abstractmethod
    def list_items(self) -> list[ItemRef]:
        """Everything in scope right now. Raise ConnectorError rather than return a partial list."""

    @abstractmethod
    def fetch(self, ref: ItemRef) -> SourceDocument | None:
        """Full text of one item, or None when it has no readable text (images, PDFs for now)."""

    def close(self) -> None:  # noqa: B027 - optional hook
        """Release network clients."""
