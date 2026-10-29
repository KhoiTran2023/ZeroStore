from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fakes import FakeConnector

from zerostore.config import GoogleDriveSource
from zerostore.librarian.ingestion import Ingestor
from zerostore.scheduler.runner import ANCHOR_KEY, SyncScheduler, is_due, next_run

UTC = timezone.utc


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


def make(zs, clock):
    zs.config.sources.google_drive = GoogleDriveSource(folders=["Company"])
    drive = FakeConnector()
    drive.put("a", "A", "alpha")
    ingestor = Ingestor(zs, lambda name, config: drive)
    return SyncScheduler(ingestor, "0 6 * * *", clock=clock, tz=UTC), drive


def test_next_run_daily_at_six():
    after = datetime(2026, 10, 13, 7, 0, tzinfo=UTC)
    assert next_run("0 6 * * *", after, UTC) == datetime(2026, 10, 14, 6, 0, tzinfo=UTC)


def test_a_new_install_syncs_on_the_first_tick(zs):
    clock = Clock(datetime(2026, 10, 13, 7, 0, tzinfo=UTC))
    scheduler, _ = make(zs, clock)
    assert scheduler.run_if_due() is True  # never synced: don't wait for 06:00 tomorrow
    assert zs.state.documents()
    assert zs.state.get_meta(ANCHOR_KEY).startswith("2026-10-13T07:00")
    clock.now += timedelta(minutes=1)
    assert scheduler.run_if_due() is False  # then the schedule takes over


def test_after_the_first_sync_the_first_tick_only_starts_the_clock(zs):
    clock = Clock(datetime(2026, 10, 13, 7, 0, tzinfo=UTC))
    scheduler, _ = make(zs, clock)
    scheduler.run_if_due()
    zs.state.delete_meta(ANCHOR_KEY)  # an install that has synced before, scheduled afresh
    clock.now += timedelta(hours=1)
    assert scheduler.run_if_due() is False
    assert zs.state.get_meta(ANCHOR_KEY).startswith("2026-10-13T08:00")


def test_runs_once_when_due(zs):
    clock = Clock(datetime(2026, 10, 13, 7, 0, tzinfo=UTC))
    scheduler, drive = make(zs, clock)
    scheduler.run_if_due()
    clock.now = datetime(2026, 10, 14, 5, 59, tzinfo=UTC)
    assert scheduler.run_if_due() is False
    clock.now = datetime(2026, 10, 14, 6, 0, 30, tzinfo=UTC)
    assert scheduler.run_if_due() is True
    assert zs.state.documents()  # the sync really ran
    clock.now += timedelta(minutes=1)
    assert scheduler.run_if_due() is False  # not twice for the same slot


def test_missed_runs_while_asleep_run_once(zs):
    clock = Clock(datetime(2026, 10, 13, 7, 0, tzinfo=UTC))
    scheduler, _ = make(zs, clock)
    scheduler.run_if_due()
    clock.now = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)  # laptop closed for days
    assert scheduler.run_if_due() is True
    assert scheduler.run_if_due() is False


def test_second_process_skips_while_first_holds_the_lock(zs):
    clock = Clock(datetime(2026, 10, 13, 7, 0, tzinfo=UTC))
    scheduler, _ = make(zs, clock)
    scheduler.run_if_due()
    clock.now = datetime(2026, 10, 14, 6, 1, tzinfo=UTC)
    zs.state.acquire_lock("sync", "other-process", 600)
    assert scheduler.run_if_due() is False
    zs.state.release_lock("sync", "other-process")
    zs.state.set_meta(ANCHOR_KEY, clock.now.isoformat())  # the other process finished its run
    assert is_due(zs.state, "0 6 * * *", clock.now, UTC) is False


def test_thread_starts_and_stops(zs):
    clock = Clock(datetime(2026, 10, 13, 7, 0, tzinfo=UTC))
    scheduler, _ = make(zs, clock)
    scheduler.tick = 0.01
    scheduler.start()
    scheduler.stop()
    assert zs.state.get_meta(ANCHOR_KEY) is not None
