"""
File:   test_scheduler.py
Brief:  Scheduler tests: next_due, catch-up policies, tick behavior.
Author: Mistress-Lukutar
Date:   2026-11-06
Version: v0.6.0
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

from tapevaultui.infrastructure.itdt_transport import FakeItDtTransport
from tapevaultui.services.backup_engine import BackupEngine
from tapevaultui.services.catalog import CatalogRepository
from tapevaultui.services.event_bus import EventBus
from tapevaultui.services.job_defs import JobDefinition, JobDefStore
from tapevaultui.services.job_runtime import JobRuntime
from tapevaultui.services.scheduler_service import (
    SchedulerService,
    last_missed_fire,
    next_due,
)


class FakeClock:
    """Injectable clock for scheduler tests."""

    def __init__(self, start: datetime) -> None:
        """Initialize the clock.

        Args:
            start: Initial time.
        """
        self.current = start

    def now(self) -> datetime:
        """Return the current fake time.

        Returns:
            The set datetime.
        """
        return self.current

    def advance_to(self, moment: datetime) -> None:
        """Move the clock forward.

        Args:
            moment: New time (must not go backwards).
        """
        self.current = moment


def _write_job(tmp_path: Path, schedule_yaml: str) -> JobDefStore:
    """Write a job definition with the given schedule section.

    Args:
        tmp_path: Temp dir for job YAML files.
        schedule_yaml: Schedule section YAML text.

    Returns:
        The job definition store with the job saved.
    """
    store = JobDefStore(tmp_path / "jobs")
    content = (
        "name: night\nsources:\n  - path: Z:/missing\n"
        f"options:\n  verify: none\nschedule:\n{schedule_yaml}\n"
    )
    store.save(store.parse_yaml(content))
    return store


def _backdate_job(store: JobDefStore, moment: datetime) -> None:
    """Set the job YAML mtime to simulate an earlier creation.

    Args:
        store: Job definition store.
        moment: Creation moment to simulate (UTC).
    """
    path = store.jobs_dir / "night.yaml"
    stamp = moment.timestamp()
    path.touch()
    import os

    os.utime(path, (stamp, stamp))


def _make_scheduler(
    tmp_path: Path, clock: FakeClock
) -> tuple[SchedulerService, CatalogRepository]:
    """Build a scheduler with an injectable clock.

    Args:
        tmp_path: Temp dir for job YAML files.
        clock: Fake clock.

    Returns:
        (scheduler, catalog) tuple.
    """
    store = JobDefStore(tmp_path / "jobs")
    repo = CatalogRepository("sqlite://")
    repo.create_all()
    engine = BackupEngine(FakeItDtTransport(), repo, tmp_path / "work", EventBus())
    runtime = JobRuntime(engine, repo, EventBus())
    scheduler = SchedulerService(
        store, repo, runtime, EventBus(), tick_interval_s=0.01, now_fn=clock.now
    )
    return scheduler, repo


def test_next_due_daily(tmp_path: Path) -> None:
    """Daily jobs are due at the next occurrence of the configured time."""
    definition = JobDefinition.model_validate(
        {
            "name": "night",
            "sources": [{"path": "Z:/missing"}],
            "schedule": {"kind": "daily", "time": "03:00"},
        }
    )
    now = datetime(2026, 11, 6, 10, 0, tzinfo=UTC)
    due = next_due(definition, now)
    assert (due.hour, due.minute) == (3, 0)
    assert due.day == 7  # tomorrow at 03:00

    before = datetime(2026, 11, 6, 1, 0, tzinfo=UTC)
    assert next_due(definition, before).day == 6  # today at 03:00


def test_next_due_weekly(tmp_path: Path) -> None:
    """Weekly jobs are due on the next configured weekday."""
    definition = JobDefinition.model_validate(
        {
            "name": "night",
            "sources": [{"path": "Z:/missing"}],
            "schedule": {"kind": "weekly", "days": ["sat"], "time": "03:00"},
        }
    )
    now = datetime(2026, 11, 6, 10, 0, tzinfo=UTC)  # Friday
    due = next_due(definition, now)
    assert due.weekday() == 5  # Saturday
    assert due.day == 7


def test_last_missed_fire(tmp_path: Path) -> None:
    """A daily job missed its 03:00 fire when the app was off."""
    definition = JobDefinition.model_validate(
        {
            "name": "night",
            "sources": [{"path": "Z:/missing"}],
            "schedule": {"kind": "daily", "time": "03:00"},
        }
    )
    last_run = datetime(2026, 11, 5, 3, 0, tzinfo=UTC)  # ran yesterday
    now = datetime(2026, 11, 6, 10, 0, tzinfo=UTC)
    missed = last_missed_fire(definition, last_run, now)
    assert missed is not None
    assert missed.day == 6

    ran_today = datetime(2026, 11, 6, 8, 0, tzinfo=UTC)
    assert last_missed_fire(definition, ran_today, now) is None


async def test_catchup_once_runs_missed_job(tmp_path: Path) -> None:
    """catch_up: once — a missed run fires once on startup."""
    clock = FakeClock(datetime(2026, 11, 6, 10, 0, tzinfo=UTC))
    store = _write_job(tmp_path, '  kind: daily\n  time: "03:00"')
    # The job existed before the missed 03:00 fire.
    _backdate_job(store, datetime(2026, 11, 4, 0, 0, tzinfo=UTC))
    scheduler, repo = _make_scheduler(tmp_path, clock)
    await scheduler._process_due()

    job = next(iter(repo.list_jobs()))
    assert job.name == "night"
    assert job.last_run_at is not None
    assert job.last_status is None  # run enqueued, not yet finished


async def test_catchup_skip_records_decision(tmp_path: Path) -> None:
    """catch_up: skip — a missed run is skipped, not executed."""
    clock = FakeClock(datetime(2026, 11, 6, 10, 0, tzinfo=UTC))
    store = _write_job(tmp_path, '  kind: daily\n  time: "03:00"\n  catch_up: skip\n')
    _backdate_job(store, datetime(2026, 11, 4, 0, 0, tzinfo=UTC))
    scheduler, repo = _make_scheduler(tmp_path, clock)
    await scheduler._process_due()

    job = next(iter(repo.list_jobs()))
    assert job.name == "night"
    assert job.last_status == "skipped"

    # No run was enqueued: the runtime has no active or queued work.
    assert scheduler.schedules_overview()[0]["overdue"] is False


async def test_tick_runs_job_at_fire_time(tmp_path: Path) -> None:
    """A due job fires when the clock passes its scheduled time."""
    clock = FakeClock(datetime(2026, 11, 6, 2, 0, tzinfo=UTC))
    store = _write_job(tmp_path, '  kind: daily\n  time: "03:00"')
    # Created today after midnight: yesterday's fire predates the job.
    _backdate_job(store, datetime(2026, 11, 6, 0, 0, tzinfo=UTC))
    scheduler, repo = _make_scheduler(tmp_path, clock)

    await scheduler._process_due()
    assert repo.list_jobs() == []  # not due yet

    clock.advance_to(datetime(2026, 11, 6, 3, 1, tzinfo=UTC))
    await scheduler._process_due()
    job = next(iter(repo.list_jobs()))
    assert job.name == "night"
    assert job.last_run_at is not None


async def test_scheduler_loop_enqueues(tmp_path: Path) -> None:
    """The background loop processes due jobs until stopped."""
    clock = FakeClock(datetime(2026, 11, 6, 2, 59, tzinfo=UTC))
    store = _write_job(tmp_path, '  kind: daily\n  time: "03:00"')
    _backdate_job(store, datetime(2026, 11, 6, 0, 0, tzinfo=UTC))
    scheduler, repo = _make_scheduler(tmp_path, clock)

    scheduler.start()
    await asyncio.sleep(0.05)
    assert repo.list_jobs() == []  # before fire time

    clock.advance_to(datetime(2026, 11, 6, 3, 2, tzinfo=UTC))
    for _ in range(50):
        await asyncio.sleep(0.02)
        if repo.list_jobs():
            break
    await scheduler.stop()

    assert repo.list_jobs() != []  # the loop picked up the due job
