"""Timed syncs, run inside `zerostore serve`. No system cron needed.

Several ZeroStore processes can share one state file (each agent may start
its own stdio server), so a sync only runs when it is due and the shared
sync lock is free. A run the computer slept through happens once on waking.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import datetime, timedelta, timezone, tzinfo

from apscheduler.triggers.cron import CronTrigger
from tzlocal import get_localzone

from zerostore.librarian.ingestion import Ingestor, SyncBusy
from zerostore.store.state import StateDB

log = logging.getLogger("zerostore.scheduler")

ANCHOR_KEY = "schedule_anchor"  # when the last scheduled sync ran, or scheduling began
_ONE_SECOND = timedelta(seconds=1)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def next_run(schedule: str, after: datetime, tz: tzinfo | None = None) -> datetime:
    """The first time at or after `after` that the cron line fires, in local time by default."""
    trigger = CronTrigger.from_crontab(schedule, timezone=tz or get_localzone())
    return trigger.get_next_fire_time(None, after)


def is_due(state: StateDB, schedule: str, now: datetime, tz: tzinfo | None = None) -> bool:
    anchor_text = state.get_meta(ANCHOR_KEY)
    if anchor_text is None:
        state.set_meta(ANCHOR_KEY, now.isoformat())
        return False
    anchor = datetime.fromisoformat(anchor_text)
    # Strictly after the anchor, so the run that set it doesn't count again.
    return now >= next_run(schedule, anchor.replace(microsecond=0) + _ONE_SECOND, tz)


class SyncScheduler:
    """Runs syncs on a cron schedule.

    With `schedule=None` the schedule is read from the live config on every tick,
    and `renew` (when given) builds a fresh Ingestor for each run, so sources,
    AI keys and the schedule saved from the web UI apply without a restart.
    """

    def __init__(
        self,
        ingestor: Ingestor,
        schedule: str | None,
        tick_seconds: float = 60,
        clock: Callable[[], datetime] = _now,
        tz: tzinfo | None = None,
        renew: Callable[[], Ingestor] | None = None,
    ):
        self.tz = tz
        self.ingestor = ingestor
        self._schedule = schedule
        self.renew = renew
        self.tick = tick_seconds
        self.clock = clock
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def schedule(self) -> str:
        return self._schedule or self.ingestor.zs.config.sync.schedule

    def run_if_due(self) -> bool:
        """Run one scheduled sync if it is due. Returns True if a sync ran."""
        from zerostore.librarian.ingestion import source_names

        zs = self.ingestor.zs
        now = self.clock()
        if not is_due(zs.state, self.schedule, now, self.tz):
            return False
        if not source_names(zs.config):
            zs.state.set_meta(ANCHOR_KEY, now.isoformat())  # nothing to read yet; wait for the next slot
            return False
        if self.renew:
            self.ingestor = self.renew()
        state = zs.state
        try:
            report = self.ingestor.sync()
        except SyncBusy:
            return False
        state.set_meta(ANCHOR_KEY, now.isoformat())
        log.info("scheduled sync: %s", report.summary())
        return True

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_if_due()
            except Exception:  # keep the server alive; the next tick tries again
                log.exception("scheduled sync failed")
            self._stop.wait(self.tick)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="zerostore-scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
