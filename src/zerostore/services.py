"""The four parts of a running ZeroStore, and whether each one is up.

`zerostore serve --http` runs all four in one program:

- the web console (the admin pages at /),
- the MCP server (agents connect at /mcp),
- the vector DB (LanceDB, where the facts and their embeddings live),
- the ingestion agent (the librarian's timed syncs from Google Drive, GitHub and Linear).

The serving process writes a heartbeat to the state file every few seconds, so
`zerostore status` in another terminal can tell whether the server is up
without calling it. The vector DB is checked directly by reading it.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from zerostore.app import ZeroStore

log = logging.getLogger("zerostore.services")

HEARTBEAT_KEY = "server_heartbeat"
BEAT_SECONDS = 15.0
STALE_AFTER = 3  # beats missed before a server counts as stopped

RUNNING, STOPPED, OFF, ERROR = "running", "stopped", "off", "error"


@dataclass
class Here:
    """What this process serves, when the check runs inside `zerostore serve --http`."""

    web: bool
    mcp_url: str | None
    scheduler: Callable[[], bool] | None  # whether the timed-sync thread is alive; None when off


def _now() -> float:
    return time.time()


def beat(zs: ZeroStore, here: Here, base: str, now: float | None = None) -> None:
    zs.state.set_meta(
        HEARTBEAT_KEY,
        json.dumps(
            {
                "at": now if now is not None else _now(),
                "pid": os.getpid(),
                "base": base,
                "web": here.web,
                "mcp_url": here.mcp_url,
                "scheduler": None if here.scheduler is None else here.scheduler(),
                "every": BEAT_SECONDS,
            }
        ),
    )


class Heartbeat:
    """Writes `beat` every BEAT_SECONDS from a background thread while the server runs."""

    def __init__(self, zs: ZeroStore, here: Here, base: str, every: float = BEAT_SECONDS):
        self.zs, self.here, self.base, self.every = zs, here, base, every
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                beat(self.zs, self.here, self.base)
            except Exception:  # a locked or moved state file shows as "stopped"; keep serving
                log.exception("could not write the server heartbeat")
            self._stop.wait(self.every)

    def start(self) -> Heartbeat:
        self._thread = threading.Thread(target=self._loop, name="zerostore-heartbeat", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        try:
            self.zs.state.set_meta(HEARTBEAT_KEY, json.dumps({"at": 0, "stopped": True}))
        except Exception:
            pass


def last_beat(zs: ZeroStore, now: float | None = None) -> dict[str, Any] | None:
    """The serving process's latest heartbeat, or None when no server is up."""
    raw = zs.state.get_meta(HEARTBEAT_KEY)
    if not raw:
        return None
    try:
        info = json.loads(raw)
    except ValueError:
        return None
    age = (now if now is not None else _now()) - float(info.get("at", 0))
    return info if age <= STALE_AFTER * float(info.get("every", BEAT_SECONDS)) else None


def syncing(zs: ZeroStore, now: float | None = None) -> bool:
    from zerostore.librarian.ingestion import SYNC_LOCK

    rows = zs.state.query(
        "SELECT 1 FROM locks WHERE name = ? AND expires_at >= ?", (SYNC_LOCK, now if now else _now())
    )
    return bool(rows)


def _vector_db(zs: ZeroStore) -> dict[str, Any]:
    where = zs.config.servers.vectordb_uri()
    started = time.perf_counter()
    try:
        facts = zs.facts.count()
    except Exception as exc:
        return {"state": ERROR, "detail": f"can't read it: {exc}", "address": where}
    ms = round((time.perf_counter() - started) * 1000)
    return {"state": RUNNING, "detail": f"{facts} facts · answered in {ms} ms", "address": where}


def _ingestion(zs: ZeroStore, alive: bool | None, *, known: bool) -> dict[str, Any]:
    from zerostore.insights import last_sync, next_sync
    from zerostore.librarian.ingestion import source_names

    cfg = zs.config
    sources = source_names(cfg)
    last = last_sync(zs)
    parts = [f"reads {', '.join(sources)}" if sources else "no sources yet"]
    if syncing(zs):
        parts.append("syncing now")
    elif last:
        when = last["finished_at"][5:16].replace("T", " ")
        parts.append(f"last sync {when} {'ok' if last['ok'] else 'had problems'}")
    if alive is None:
        state = OFF if known else STOPPED
        parts.append("timed syncs are off (started with --no-schedule)" if known else "start the server")
    elif alive:
        state = RUNNING
        if (upcoming := next_sync(zs)) and sources:
            parts.append(f"next {upcoming[5:16].replace('T', ' ')}")
    else:
        state = ERROR
        parts.append("the timed-sync thread stopped; restart the server")
    return {"state": state, "detail": " · ".join(parts), "address": f"schedule '{cfg.sync.schedule}'"}


def check(zs: ZeroStore, here: Here | None = None, now: float | None = None) -> list[dict[str, Any]]:
    """One row per part: name, state (running, stopped, off or error), detail and address.

    Inside the server, pass `here` (what this process serves). From another process,
    the server's heartbeat says what is up.
    """
    if here is not None:
        web, mcp_url = here.web, here.mcp_url
        alive = here.scheduler() if here.scheduler else None
        up, base = True, (mcp_url[: -len("/mcp")] if mcp_url and mcp_url.endswith("/mcp") else None)
    else:
        info = last_beat(zs, now)
        up = info is not None
        web, mcp_url = bool(info and info.get("web")), info and info.get("mcp_url")
        alive, base = (info or {}).get("scheduler"), (info or {}).get("base")
    how = "run `zerostore serve --http`"
    if not up:
        web_row = {"state": STOPPED, "detail": how, "address": "-"}
        mcp_row = {"state": STOPPED, "detail": f"{how} (or an agent starts it over stdio)", "address": "-"}
    else:
        if not zs.config.web_ui:
            web_row = {"state": OFF, "detail": "web_ui is off in zerostore.yaml", "address": "-"}
        elif web:
            web_row = {"state": RUNNING, "detail": "admin pages", "address": base or "-"}
        else:
            web_row = {"state": OFF, "detail": "this server has no web console", "address": "-"}
        mcp_row = {"state": RUNNING, "detail": "agents connect here", "address": mcp_url or "-"}
    return [
        {"name": "web console", **web_row},
        {"name": "mcp server", **mcp_row},
        {"name": "vector db", **_vector_db(zs)},
        {"name": "ingestion agent", **_ingestion(zs, alive, known=up)},
    ]
