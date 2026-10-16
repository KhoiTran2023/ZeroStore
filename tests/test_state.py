"""The state database: upgrades from older files, and the review markers on documents."""

from __future__ import annotations

import sqlite3

from zerostore.store.state import SCHEMA_VERSION, StateDB

# The review_queue and documents tables as stage 2 shipped them.
STAGE_2 = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE review_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, payload TEXT NOT NULL, confidence REAL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected')),
    created_at TEXT NOT NULL, created_by TEXT, decided_at TEXT, decided_by TEXT
);
CREATE TABLE documents (
    doc_id TEXT PRIMARY KEY, source TEXT NOT NULL, title TEXT NOT NULL, url TEXT NOT NULL,
    version TEXT NOT NULL, fingerprint TEXT NOT NULL, updated_at TEXT, location TEXT,
    confidential INTEGER NOT NULL DEFAULT 0, chunks INTEGER NOT NULL DEFAULT 0, synced_at TEXT NOT NULL
);
INSERT INTO review_queue (kind, payload, created_at) VALUES ('agent_proposal', '{"fact": "f"}', '2026-10-14');
INSERT INTO documents VALUES ('d', 'github', 'D', 'u', 'v1', 'fp', NULL, 'acme/app', 0, 1, '2026-10-14');
"""

RECORD = {
    "doc_id": "d",
    "source": "github",
    "title": "D",
    "url": "u",
    "version": "v2",
    "fingerprint": "fp2",
    "updated_at": None,
    "location": "acme/app",
    "confidential": 0,
    "chunks": 1,
}


def test_a_stage_2_file_is_upgraded_in_place(tmp_path):
    path = tmp_path / "state.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript(STAGE_2)
    conn.close()
    state = StateDB(path)
    assert state.get_meta("schema_version") == str(SCHEMA_VERSION)
    [item] = state.review_items()
    assert item["payload"] == {"fact": "f"} and item["doc_id"] is None
    doc = state.documents()["d"]
    assert doc["rejected_fingerprint"] is None and doc["source_deleted_at"] is None
    state.close()
    StateDB(path).close()  # opening again changes nothing


def test_review_markers_reset_when_a_new_version_lands(tmp_path):
    state = StateDB(tmp_path / "s.sqlite")
    state.upsert_document(RECORD)
    state.reject_version("d", "fp3")
    state.mark_source_deleted("d", True)
    doc = state.documents()["d"]
    assert doc["rejected_fingerprint"] == "fp3" and doc["source_deleted_at"]
    state.upsert_document({**RECORD, "version": "v4", "fingerprint": "fp4"})
    doc = state.documents()["d"]
    assert doc["rejected_fingerprint"] is None and doc["source_deleted_at"] is None


def test_pending_items_by_document_and_reopening(tmp_path):
    state = StateDB(tmp_path / "s.sqlite")
    first = state.add_review_item("change", {"x": 1}, 0.5, created_by="librarian", doc_id="d")
    state.add_review_item("change", {"x": 2}, 0.5, created_by="librarian", doc_id="other")
    assert [i["id"] for i in state.pending_for_document("d")] == [first]
    assert state.decide_review_item(first, True, "khoi")
    assert state.pending_for_document("d") == []
    state.reopen_review_item(first)
    assert state.get_review_item(first)["status"] == "pending"
    state.upsert_document(RECORD)
    state.reject_version("d", "fp9")
    state.forget_versions()
    doc = state.documents()["d"]
    assert doc["chunks"] == 0 and doc["version"] == "" and doc["rejected_fingerprint"] == "fp9"


def test_judgments_are_kept_per_engine_and_dropped_when_unused(tmp_path):
    state = StateDB(tmp_path / "state.sqlite")
    keys = [f"k{i}" for i in range(1200)]  # more than one batch of SQLite parameters
    state.save_judgments("jev:jev-latest", {k: {"keep": 0.9} for k in keys})
    state.save_judgments("jev:jev-latest", {"k0": {"keep": 0.1}})  # a new answer replaces the old
    found = state.judgments("jev:jev-latest", [*keys, "missing"])
    assert len(found) == 1200 and found["k0"] == {"keep": 0.1} and "missing" not in found
    assert state.judgments("llm:openai:gpt-6-sol", keys) == {}
    with state.tx() as cur:
        cur.execute("UPDATE judgments SET used_at = '2026-01-01T00:00:00+00:00'")
    state.judgments("jev:jev-latest", ["k1"])  # reading one marks it as used
    assert state.forget_judgments("2026-06-01T00:00:00+00:00") == 1199
    assert list(state.judgments("jev:jev-latest", keys)) == ["k1"]
    state.close()
