"""State database (SQLite): tokens, access log, audit table, review queue, sync cursors.

Postgres support for split deployments comes later; everything here goes
through `StateDB` so that swap stays contained.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tokens (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    level TEXT NOT NULL CHECK (level IN ('default', 'confidential', 'owner')),
    token_hash TEXT NOT NULL UNIQUE,
    auto_approve INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    revoked_at TEXT,
    last_used_at TEXT
);
CREATE TABLE IF NOT EXISTS access_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    token_id TEXT,
    action TEXT NOT NULL,
    detail TEXT,
    allowed INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    action TEXT NOT NULL,
    doc_id TEXT,
    source TEXT,
    detail TEXT,
    confidence REAL,
    engine TEXT,
    confidential INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS review_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    confidence REAL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected')),
    created_at TEXT NOT NULL,
    created_by TEXT,
    decided_at TEXT,
    decided_by TEXT
);
CREATE TABLE IF NOT EXISTS sync_cursors (
    source TEXT PRIMARY KEY,
    cursor TEXT,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS audit_at ON audit (at);
CREATE INDEX IF NOT EXISTS access_log_at ON access_log (at);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class StateDB:
    """A small, thread-safe wrapper around one SQLite file."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # Token hashes and the access log live here: readable only by this user.
            self.path.touch(mode=0o600, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        if str(path) != ":memory:":
            self._conn.execute("PRAGMA journal_mode = WAL")
        with self.tx() as cur:
            cur.executescript(_SCHEMA)
            cur.execute(
                "INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),)
            )

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Cursor]:
        with self._lock:
            cur = self._conn.cursor()
            try:
                yield cur
                self._conn.commit()
            except BaseException:
                self._conn.rollback()
                raise
            finally:
                cur.close()

    def query(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # Access log -----------------------------------------------------------

    def log_access(self, token_id: str | None, action: str, detail: str | None, allowed: bool) -> None:
        with self.tx() as cur:
            cur.execute(
                "INSERT INTO access_log (at, token_id, action, detail, allowed) VALUES (?, ?, ?, ?, ?)",
                (utcnow(), token_id, action, detail, int(allowed)),
            )

    # Audit table ----------------------------------------------------------

    def audit(
        self,
        action: str,
        *,
        doc_id: str | None = None,
        source: str | None = None,
        detail: str | None = None,
        confidence: float | None = None,
        engine: str | None = None,
        confidential: bool = False,
        at: str | None = None,
    ) -> None:
        with self.tx() as cur:
            cur.execute(
                "INSERT INTO audit (at, action, doc_id, source, detail, confidence, engine, confidential)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (at or utcnow(), action, doc_id, source, detail, confidence, engine, int(confidential)),
            )

    def audit_since(
        self,
        since: str,
        *,
        actions: tuple[str, ...] | None = None,
        include_confidential: bool = True,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Audit rows at or after `since` (an ISO date or timestamp), newest first."""
        sql = (
            "SELECT at, action, doc_id, source, detail, confidence, engine, confidential"
            " FROM audit WHERE at >= ?"
        )
        params: list[Any] = [since]
        if actions:
            sql += f" AND action IN ({', '.join('?' for _ in actions)})"
            params.extend(actions)
        if not include_confidential:
            sql += " AND confidential = 0"
        rows = self.query(sql + " ORDER BY at DESC, id DESC LIMIT ?", (*params, limit))
        return [{**dict(r), "confidential": bool(r["confidential"])} for r in rows]

    # Review queue ---------------------------------------------------------

    def add_review_item(
        self,
        kind: str,
        payload: dict[str, Any],
        confidence: float | None = None,
        created_by: str | None = None,
    ) -> int:
        with self.tx() as cur:
            cur.execute(
                "INSERT INTO review_queue (kind, payload, confidence, created_at, created_by)"
                " VALUES (?, ?, ?, ?, ?)",
                (kind, json.dumps(payload), confidence, utcnow(), created_by),
            )
            return int(cur.lastrowid)

    def review_items(self, status: str = "pending", limit: int = 100) -> list[dict[str, Any]]:
        rows = self.query("SELECT * FROM review_queue WHERE status = ? ORDER BY id LIMIT ?", (status, limit))
        return [_review_row(r) for r in rows]

    def get_review_item(self, item_id: int) -> dict[str, Any] | None:
        rows = self.query("SELECT * FROM review_queue WHERE id = ?", (item_id,))
        return _review_row(rows[0]) if rows else None

    def decide_review_item(self, item_id: int, approve: bool, decided_by: str | None) -> bool:
        """Mark a pending item approved or rejected. Returns False if it wasn't pending."""
        with self.tx() as cur:
            cur.execute(
                "UPDATE review_queue SET status = ?, decided_at = ?, decided_by = ?"
                " WHERE id = ? AND status = 'pending'",
                ("approved" if approve else "rejected", utcnow(), decided_by, item_id),
            )
            return cur.rowcount == 1

    # Sync cursors ---------------------------------------------------------

    def get_cursor(self, source: str) -> str | None:
        rows = self.query("SELECT cursor FROM sync_cursors WHERE source = ?", (source,))
        return rows[0]["cursor"] if rows else None

    def set_cursor(self, source: str, cursor: str | None) -> None:
        with self.tx() as cur:
            cur.execute(
                "INSERT INTO sync_cursors (source, cursor, updated_at) VALUES (?, ?, ?)"
                " ON CONFLICT(source) DO UPDATE"
                " SET cursor = excluded.cursor, updated_at = excluded.updated_at",
                (source, cursor, utcnow()),
            )


def _review_row(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["payload"] = json.loads(item["payload"])
    return item
