"""State database (SQLite): tokens, access log, audit table, review queue, sync cursors.

Postgres support for split deployments comes later; everything here goes
through `StateDB` so that swap stays contained.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 5

# Columns added after a table first shipped: (table, column, declaration).
# CREATE TABLE IF NOT EXISTS leaves older files alone, so these are added on open.
_ADDED_COLUMNS = [
    ("review_queue", "doc_id", "TEXT"),
    ("documents", "rejected_fingerprint", "TEXT"),
    ("documents", "source_deleted_at", "TEXT"),
    ("access_log", "duration_ms", "INTEGER"),
]

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
    decided_by TEXT,
    doc_id TEXT
);
CREATE TABLE IF NOT EXISTS sync_cursors (
    source TEXT PRIMARY KEY,
    cursor TEXT,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS documents (
    doc_id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    title TEXT NOT NULL,
    url TEXT NOT NULL,
    version TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    updated_at TEXT,
    location TEXT,
    confidential INTEGER NOT NULL DEFAULT 0,
    chunks INTEGER NOT NULL DEFAULT 0,
    synced_at TEXT NOT NULL,
    rejected_fingerprint TEXT,
    source_deleted_at TEXT
);
CREATE TABLE IF NOT EXISTS judgments (
    engine TEXT NOT NULL,
    key TEXT NOT NULL,
    answer TEXT NOT NULL,
    used_at TEXT NOT NULL,
    PRIMARY KEY (engine, key)
);
CREATE TABLE IF NOT EXISTS locks (
    name TEXT PRIMARY KEY,
    holder TEXT NOT NULL,
    expires_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS documents_source ON documents (source);
CREATE INDEX IF NOT EXISTS audit_at ON audit (at);
CREATE INDEX IF NOT EXISTS access_log_at ON access_log (at);
"""

_INDEXES_ON_ADDED = "CREATE INDEX IF NOT EXISTS review_doc ON review_queue (doc_id, status);"


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
            for table, column, decl in _ADDED_COLUMNS:
                have = {row[1] for row in cur.execute(f"PRAGMA table_info({table})")}
                if column not in have:
                    cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
            cur.executescript(_INDEXES_ON_ADDED)
            cur.execute(
                "INSERT INTO meta (key, value) VALUES ('schema_version', ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (str(SCHEMA_VERSION),),
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

    # Meta values ----------------------------------------------------------

    def get_meta(self, key: str) -> str | None:
        rows = self.query("SELECT value FROM meta WHERE key = ?", (key,))
        return rows[0]["value"] if rows else None

    def set_meta(self, key: str, value: str) -> None:
        with self.tx() as cur:
            cur.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    # Locks (several processes may share one state file) --------------------

    def acquire_lock(self, name: str, holder: str, ttl_seconds: float) -> bool:
        """Take the named lock unless someone else holds an unexpired one."""
        now = time.time()
        with self.tx() as cur:
            cur.execute("DELETE FROM locks WHERE name = ? AND expires_at < ?", (name, now))
            cur.execute(
                "INSERT OR IGNORE INTO locks (name, holder, expires_at) VALUES (?, ?, ?)",
                (name, holder, now + ttl_seconds),
            )
            if cur.rowcount == 1:
                return True
            cur.execute(
                "UPDATE locks SET expires_at = ? WHERE name = ? AND holder = ?",
                (now + ttl_seconds, name, holder),
            )
            return cur.rowcount == 1

    def release_lock(self, name: str, holder: str) -> None:
        with self.tx() as cur:
            cur.execute("DELETE FROM locks WHERE name = ? AND holder = ?", (name, holder))

    # Documents (what the fact store holds, per source) -----------------------

    def documents(self, source: str | None = None) -> dict[str, dict[str, Any]]:
        sql, params = "SELECT * FROM documents", ()
        if source:
            sql, params = sql + " WHERE source = ?", (source,)
        return {r["doc_id"]: dict(r) for r in self.query(sql, params)}

    def upsert_document(self, record: dict[str, Any]) -> None:
        cols = (
            "doc_id",
            "source",
            "title",
            "url",
            "version",
            "fingerprint",
            "updated_at",
            "location",
            "confidential",
            "chunks",
        )
        with self.tx() as cur:
            cur.execute(
                f"INSERT INTO documents ({', '.join(cols)}, synced_at)"
                f" VALUES ({', '.join('?' for _ in cols)}, ?)"
                " ON CONFLICT(doc_id) DO UPDATE SET "
                + ", ".join(f"{c} = excluded.{c}" for c in cols[1:])
                + ", synced_at = excluded.synced_at, rejected_fingerprint = NULL, source_deleted_at = NULL",
                (*(record[c] for c in cols), utcnow()),
            )

    def touch_document(self, doc_id: str, version: str) -> None:
        """The item changed version but not content: remember the new version."""
        with self.tx() as cur:
            cur.execute(
                "UPDATE documents SET version = ?, synced_at = ? WHERE doc_id = ?",
                (version, utcnow(), doc_id),
            )

    def delete_document(self, doc_id: str) -> None:
        with self.tx() as cur:
            cur.execute("DELETE FROM documents WHERE doc_id = ?", (doc_id,))

    def reject_version(self, doc_id: str, fingerprint: str) -> None:
        """A person turned this version down: don't propose it again until the source changes."""
        with self.tx() as cur:
            cur.execute(
                "UPDATE documents SET rejected_fingerprint = ? WHERE doc_id = ?", (fingerprint, doc_id)
            )

    def mark_source_deleted(self, doc_id: str, deleted: bool) -> None:
        """The source no longer has this document, but its facts are kept (or it came back)."""
        with self.tx() as cur:
            cur.execute(
                "UPDATE documents SET source_deleted_at = ? WHERE doc_id = ?",
                (utcnow() if deleted else None, doc_id),
            )

    def forget_versions(self) -> None:
        """Mark every document as holding nothing, so the next sync fetches and stores each one again.

        For reindex. Rejected versions are remembered, so they aren't proposed again.
        """
        with self.tx() as cur:
            cur.execute(
                "UPDATE documents SET version = '', fingerprint = '', chunks = 0, source_deleted_at = NULL"
            )

    # Remembered judgments -------------------------------------------------

    def judgments(self, engine: str, keys: list[str]) -> dict[str, Any]:
        """The answers `engine` already gave for these keys; marks them as used today."""
        found: dict[str, Any] = {}
        now = utcnow()
        with self.tx() as cur:
            for start in range(0, len(keys), 500):  # SQLite caps the number of parameters
                batch = keys[start : start + 500]
                marks = ", ".join("?" * len(batch))
                rows = cur.execute(
                    f"SELECT key, answer FROM judgments WHERE engine = ? AND key IN ({marks})",
                    (engine, *batch),
                ).fetchall()
                found.update((row["key"], json.loads(row["answer"])) for row in rows)
                cur.execute(
                    f"UPDATE judgments SET used_at = ? WHERE engine = ? AND key IN ({marks})",
                    (now, engine, *batch),
                )
        return found

    def save_judgments(self, engine: str, answers: dict[str, Any]) -> None:
        now = utcnow()
        with self.tx() as cur:
            cur.executemany(
                "INSERT INTO judgments (engine, key, answer, used_at) VALUES (?, ?, ?, ?)"
                " ON CONFLICT(engine, key)"
                " DO UPDATE SET answer = excluded.answer, used_at = excluded.used_at",
                [(engine, key, json.dumps(answer), now) for key, answer in answers.items()],
            )

    def forget_judgments(self, unused_since: str) -> int:
        """Drop answers nobody has needed since `unused_since` (an ISO time)."""
        with self.tx() as cur:
            cur.execute("DELETE FROM judgments WHERE used_at < ?", (unused_since,))
            return cur.rowcount

    # Access log -----------------------------------------------------------

    def log_access(self, token_id: str | None, action: str, detail: str | None, allowed: bool) -> int:
        with self.tx() as cur:
            cur.execute(
                "INSERT INTO access_log (at, token_id, action, detail, allowed) VALUES (?, ?, ?, ?, ?)",
                (utcnow(), token_id, action, detail, int(allowed)),
            )
            return int(cur.lastrowid or 0)

    def access_took(self, row_id: int, duration_ms: int) -> None:
        """How long a logged request took to answer (searches, for the status charts)."""
        with self.tx() as cur:
            cur.execute("UPDATE access_log SET duration_ms = ? WHERE id = ?", (duration_ms, row_id))

    def delete_meta(self, key: str) -> bool:
        with self.tx() as cur:
            cur.execute("DELETE FROM meta WHERE key = ?", (key,))
            return cur.rowcount > 0

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
        doc_id: str | None = None,
    ) -> int:
        with self.tx() as cur:
            cur.execute(
                "INSERT INTO review_queue (kind, payload, confidence, created_at, created_by, doc_id)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (kind, json.dumps(payload), confidence, utcnow(), created_by, doc_id),
            )
            return int(cur.lastrowid)

    def pending_doc_ids(self, kind: str) -> set[str]:
        rows = self.query(
            "SELECT DISTINCT doc_id FROM review_queue WHERE kind = ? AND status = 'pending'"
            " AND doc_id IS NOT NULL",
            (kind,),
        )
        return {r["doc_id"] for r in rows}

    def pending_for_document(self, doc_id: str) -> list[dict[str, Any]]:
        rows = self.query(
            "SELECT * FROM review_queue WHERE doc_id = ? AND status = 'pending' ORDER BY id", (doc_id,)
        )
        return [_review_row(r) for r in rows]

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

    def reopen_review_item(self, item_id: int) -> None:
        """Undo a decision whose follow-up failed, so a person can try again."""
        with self.tx() as cur:
            cur.execute(
                "UPDATE review_queue SET status = 'pending', decided_at = NULL, decided_by = NULL"
                " WHERE id = ?",
                (item_id,),
            )

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
