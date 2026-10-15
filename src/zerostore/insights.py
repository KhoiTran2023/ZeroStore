"""What ZeroStore is doing, for the status page and the owner's `status` and
`activity` MCP tools: sources, last sync, who is asking, recent changes, and
the numbers behind the charts. Everything here only reads.
"""

from __future__ import annotations

import statistics
from datetime import date, datetime, timedelta, timezone
from typing import Any

from zerostore import __version__
from zerostore.app import ZeroStore
from zerostore.config import Config

APPLIED_ACTIONS = (
    "document_added",
    "document_updated",
    "document_retired",
    "document_moved",
    "document_kept",
)
OUTCOMES = {
    "applied": APPLIED_ACTIONS,
    "left out as noise": ("document_skipped",),
    "sent to review": ("change_queued", "delete_queued"),
    "approved": ("review_approved",),
    "rejected": ("review_rejected",),
}
CHART_DAYS = 14


def engine_label(cfg: Config) -> str:
    ai = cfg.ai
    if ai.decision_engine == "demo":
        return "the demo judge (scripted rules for `zerostore demo`)"
    if ai.decision_engine == "jev" and ai.has_jev:
        return "Jev"
    if ai.has_llm:
        return f"your LLM ({ai.llm_provider})"
    if ai.has_jev:
        return "Jev"
    return "none (every change is applied directly; add an AI key to turn on judgment)"


def last_sync(zs: ZeroStore) -> dict[str, Any] | None:
    from zerostore.librarian.ingestion import SyncReport

    raw = zs.state.get_meta("last_sync")
    if not raw:
        return None
    report = SyncReport.from_json(raw)
    return {
        "started_at": report.started_at,
        "finished_at": report.finished_at,
        "summary": report.summary(),
        "ok": report.ok,
        "applied": len(report.applied),
        "waiting": len(report.waiting),
        "engine": report.engine,
        "judged": report.judged,
        "judge_seconds": round(report.judge_seconds, 2),
        "changes": [
            {
                "action": c.action,
                "document": c.title,
                "source": c.source,
                "confidence": c.confidence,
                "note": c.note,
            }
            for c in report.changes
        ],
        "problems": [
            {"source": r.source, "error": r.error, "warning": r.warning, "failed": r.failed}
            for r in report.sources
            if r.error or r.warning or r.failed
        ],
    }


def next_sync(zs: ZeroStore, now: datetime | None = None) -> str | None:
    from zerostore.scheduler.runner import next_run

    try:
        return next_run(zs.config.sync.schedule, now or datetime.now().astimezone()).isoformat()
    except Exception:  # an odd cron line is reported by the config check, not here
        return None


def _days(today: date, n: int = CHART_DAYS) -> list[str]:
    return [(today - timedelta(days=n - 1 - i)).isoformat() for i in range(n)]


def charts(zs: ZeroStore, today: date | None = None) -> dict[str, Any]:
    """Daily series for the last two weeks, plus outcome counts and a confidence histogram."""
    today = today or datetime.now(timezone.utc).date()
    days = _days(today)
    since = days[0]
    q = zs.state.query

    def per_day(rows) -> list[int]:
        counts = {r["day"]: r["n"] for r in rows}
        return [counts.get(d, 0) for d in days]

    marks = ", ".join("?" for _ in APPLIED_ACTIONS)
    applied = per_day(
        q(
            f"SELECT substr(at, 1, 10) AS day, COUNT(*) AS n FROM audit WHERE at >= ? AND action IN ({marks})"
            " GROUP BY day",
            (since, *APPLIED_ACTIONS),
        )
    )
    queued = per_day(
        q(
            "SELECT substr(at, 1, 10) AS day, COUNT(*) AS n FROM audit WHERE at >= ?"
            " AND action IN ('change_queued', 'delete_queued') GROUP BY day",
            (since,),
        )
    )
    outcomes = {}
    for label, actions in OUTCOMES.items():
        marks = ", ".join("?" for _ in actions)
        outcomes[label] = q(
            f"SELECT COUNT(*) AS n FROM audit WHERE at >= ? AND action IN ({marks})", (since, *actions)
        )[0]["n"]
    confidence = [0] * 10
    for row in q("SELECT confidence FROM audit WHERE at >= ? AND confidence IS NOT NULL", (since,)):
        confidence[min(9, int(float(row["confidence"]) * 10))] += 1

    searches = q(
        "SELECT substr(at, 1, 10) AS day, duration_ms FROM access_log"
        " WHERE at >= ? AND action = 'search' AND allowed = 1",
        (since,),
    )
    by_day: dict[str, list[int]] = {d: [] for d in days}
    counts = dict.fromkeys(days, 0)
    for row in searches:
        if row["day"] in counts:
            counts[row["day"]] += 1
            if row["duration_ms"] is not None:
                by_day[row["day"]].append(int(row["duration_ms"]))
    return {
        "days": days,
        "changes_applied": applied,
        "changes_queued": queued,
        "outcomes": outcomes,
        "confidence": confidence,
        "questions": [counts[d] for d in days],
        "median_ms": [int(statistics.median(by_day[d])) if by_day[d] else None for d in days],
    }


def status(zs: ZeroStore, now: datetime | None = None) -> dict[str, Any]:
    from zerostore.librarian.ingestion import source_names

    cfg = zs.config
    docs = zs.state.documents()
    sources = []
    for name in source_names(cfg):
        mine = [d for d in docs.values() if d["source"] == name]
        sources.append(
            {
                "name": name,
                "documents": sum(1 for d in mine if d["chunks"]),
                "chunks": sum(d["chunks"] for d in mine),
                "confidential": sum(1 for d in mine if d["confidential"] and d["chunks"]),
            }
        )
    day_ago = ((now or datetime.now(timezone.utc)) - timedelta(days=1)).isoformat(timespec="seconds")
    asked = {
        r["token_id"]: r["n"]
        for r in zs.state.query(
            "SELECT token_id, COUNT(*) AS n FROM access_log"
            " WHERE at >= ? AND allowed = 1 AND action = 'search' GROUP BY token_id",
            (day_ago,),
        )
    }
    tokens = [
        {
            "id": t.id,
            "name": t.name,
            "level": t.level.value,
            "last_used_at": t.last_used_at,
            "questions_24h": asked.get(t.id, 0),
        }
        for t in zs.tokens.list()
    ]
    return {
        "version": __version__,
        "sources": sources,
        "documents": sum(s["documents"] for s in sources),
        "chunks": zs.facts.count(),
        "waiting": zs.state.query("SELECT COUNT(*) AS n FROM review_queue WHERE status = 'pending'")[0]["n"],
        "engine": engine_label(cfg),
        "embedder": zs.facts.embedder.name,
        "schedule": cfg.sync.schedule,
        "next_sync": next_sync(zs),
        "last_sync": last_sync(zs),
        "tokens": tokens,
        "charts": charts(zs),
    }


# Read-only owner tools the web UI calls on every page: logged, but left out of the activity list.
UI_READS = ("status", "activity", "review_list", "token_list")


def activity(zs: ZeroStore, limit: int = 50, kind: str = "all") -> list[dict[str, Any]]:
    """Recent changes (the audit table) and requests (the access log), newest first.

    The owner's own page views (`UI_READS`) are left out, so agents' requests stay visible.
    """
    limit = max(1, min(limit, 500))
    rows: list[dict[str, Any]] = []
    if kind in ("all", "changes"):
        for r in zs.state.query(
            "SELECT at, action, source, detail, confidence, engine FROM audit"
            " ORDER BY at DESC, id DESC LIMIT ?",
            (limit,),
        ):
            rows.append({"kind": "change", **dict(r)})
    if kind in ("all", "questions"):
        names = {t.id: t.name for t in zs.tokens.list(include_revoked=True)}
        for r in zs.state.query(
            "SELECT at, token_id, action, detail, allowed, duration_ms FROM access_log"
            f" WHERE NOT (allowed = 1 AND action IN ({', '.join('?' for _ in UI_READS)}))"
            " ORDER BY id DESC LIMIT ?",
            (*UI_READS, limit),
        ):
            rows.append(
                {
                    "kind": "request",
                    "at": r["at"],
                    "action": r["action"] if r["allowed"] else f"{r['action']} (refused)",
                    "token": names.get(r["token_id"], "unknown token"),
                    "detail": r["detail"],
                    "duration_ms": r["duration_ms"],
                }
            )
    rows.sort(key=lambda r: r["at"], reverse=True)
    return rows[:limit]
