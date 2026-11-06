"""
File:   scheduler_service.py
Brief:  In-app asyncio scheduler with missed-run catch-up (SPEC §7).
Author: Mistress-Lukutar
Date:   2026-11-06
Version: v0.6.3
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import structlog

from tapevaultui.services.catalog import CatalogRepository
from tapevaultui.services.event_bus import EventBus
from tapevaultui.services.job_defs import JobDefinition, JobDefStore
from tapevaultui.services.job_runtime import JobRuntime

logger = structlog.get_logger(__name__)

SCHEDULE_TOPIC = "schedule"

_WEEKDAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def _parse_time(value: str) -> tuple[int, int]:
    """Parse an HH:MM schedule time.

    Args:
        value: Time string.

    Returns:
        (hour, minute) tuple.
    """
    hour_str, minute_str = value.split(":")
    return int(hour_str), int(minute_str)


def next_due(definition: JobDefinition, now: datetime) -> datetime:
    """Compute the next due moment for a job schedule.

    Args:
        definition: The job definition.
        now: Current UTC time.

    Returns:
        The next due UTC datetime.

    Raises:
        ValueError: On an unknown schedule kind.
    """
    hour, minute = _parse_time(definition.schedule.time)
    today_at = now.replace(hour=hour, minute=minute, second=0, microsecond=0)

    if definition.schedule.kind == "daily":
        return today_at if today_at > now else today_at + timedelta(days=1)
    if definition.schedule.kind == "weekly":
        target_days = {_WEEKDAY_NAMES.index(day) for day in definition.schedule.days}
        for offset in range(0, 8):
            candidate = today_at + timedelta(days=offset)
            if candidate.weekday() in target_days and candidate > now:
                return candidate
        raise ValueError("No next due time within a week")  # pragma: no cover
    raise ValueError(
        f"Job {definition.name} is not schedulable ({definition.schedule.kind})"
    )


def last_missed_fire(
    definition: JobDefinition, last_run: datetime | None, now: datetime
) -> datetime | None:
    """Find the most recent fire time that was missed, if any.

    A fire is missed when the app was off (or asleep) at the scheduled
    moment: last_run < fire <= now (spec §7 catch-up rule). While the
    machine sleeps asyncio does not tick; after wake the same window
    logic applies.

    Args:
        definition: The job definition.
        last_run: Last actual run time (None = never ran).
        now: Current UTC time.

    Returns:
        The missed fire datetime, or None.
    """
    if definition.schedule.kind not in ("daily", "weekly"):
        return None
    floor = last_run or datetime.min.replace(tzinfo=UTC)
    hour, minute = _parse_time(definition.schedule.time)
    today_at = now.replace(hour=hour, minute=minute, second=0, microsecond=0)

    if definition.schedule.kind == "daily":
        candidate = today_at if today_at <= now else today_at - timedelta(days=1)
        return candidate if floor < candidate <= now else None

    target_days = {_WEEKDAY_NAMES.index(day) for day in definition.schedule.days}
    for offset in range(0, 8):
        candidate = today_at - timedelta(days=offset)
        if candidate.weekday() in target_days and floor < candidate <= now:
            return candidate
    return None


class SchedulerService:
    """Runs enabled scheduled jobs and reports upcoming schedules.

    The loop ticks every 30 seconds (spec §7). Missed runs — app was off
    or the machine slept — follow the per-job catch_up policy: ``once``
    runs a single catch-up, ``skip`` waits for the next due time.
    """

    def __init__(
        self,
        job_defs: JobDefStore,
        catalog: CatalogRepository,
        runtime: JobRuntime,
        event_bus: EventBus,
        *,
        tick_interval_s: float = 30.0,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        """Initialize the scheduler.

        Args:
            job_defs: Job definition store.
            catalog: Catalog repository (last_run tracking).
            runtime: Job runtime used to enqueue runs.
            event_bus: Bus for schedule publications.
            tick_interval_s: Loop tick interval.
            now_fn: Injectable clock (tests); defaults to UTC now.
        """
        self._job_defs = job_defs
        self._catalog = catalog
        self._runtime = runtime
        self._event_bus = event_bus
        self._tick_interval_s = tick_interval_s
        self._now_fn = now_fn or (lambda: datetime.now(UTC))
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()

    def start(self) -> None:
        """Start the scheduler loop (idempotent)."""
        if self._task is None or self._task.done():
            self._stopping.clear()
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        """Stop the scheduler loop."""
        self._stopping.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    def schedules_overview(self) -> list[dict[str, object]]:
        """Build the schedules overview for the UI.

        Returns:
            Per-job schedule info with next_due and overdue flags.
        """
        now = self._now_fn()
        overview: list[dict[str, object]] = []
        for definition in self._job_defs.list_jobs():
            schedule = definition.schedule
            last_run = self._last_run(definition.name)
            entry: dict[str, object] = {
                "name": definition.name,
                "kind": schedule.kind,
                "days": schedule.days,
                "time": schedule.time,
                "catch_up": schedule.catch_up,
                "last_run_at": last_run.isoformat() if last_run else None,
                "next_due": None,
                "overdue": False,
            }
            if schedule.kind in ("daily", "weekly"):
                due = next_due(definition, now)
                entry["next_due"] = due.isoformat()
                entry["overdue"] = (
                    last_missed_fire(definition, last_run, now) is not None
                )
            overview.append(entry)
        return overview

    async def _loop(self) -> None:
        """Process due jobs immediately, then tick forever."""
        await self._process_due()
        while not self._stopping.is_set():
            try:
                await asyncio.sleep(self._tick_interval_s)
                await self._process_due()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("scheduler_tick_failed")

    async def _process_due(self) -> None:
        """Enqueue due jobs per their catch_up policy and publish state.

        This covers both the startup catch-up (app was off at fire time)
        and wake-from-sleep: asyncio skips ticks during sleep, so the
        first tick after wake sees the overdue job through the same
        last_run < fire <= now window.
        """
        now = self._now_fn()
        for definition in self._job_defs.list_jobs():
            if definition.schedule.kind not in ("daily", "weekly"):
                continue
            floor = self._job_floor(definition.name)
            if floor is None:
                continue
            last_run = self._last_run(definition.name)
            effective_last = max(last_run, floor) if last_run else floor
            missed = last_missed_fire(definition, effective_last, now)
            if missed is None:
                continue
            if definition.schedule.catch_up == "once":
                logger.info(
                    "scheduler_run_due", job=definition.name, fire=missed.isoformat()
                )
                entry = self._runtime.enqueue(definition)
                self._catalog.update_job_run(
                    entry.run_id,
                    stats_json={"trigger": "catch-up" if last_run else "scheduled"},
                )
            else:
                logger.info("scheduler_skip_due", job=definition.name)
                # Record the decision so this fire is not re-detected.
                self._catalog.mark_job_ran(definition.name, at=now, status="skipped")
        await self._publish_overview()

    @property
    def job_defs(self) -> JobDefStore:
        """The job definition store (used by the schedules API)."""
        return self._job_defs

    def _job_floor(self, name: str) -> datetime | None:
        """Return the earliest moment a job can be missed from.

        A brand-new job must not fire catch-ups for fires that happened
        before it existed: the floor is the catalog creation time, or the
        YAML file mtime for jobs that never ran in this catalog.

        Args:
            name: Job name.

        Returns:
            Floor datetime (UTC), or None if it cannot be determined.
        """
        job = next(
            (item for item in self._catalog.list_jobs() if item.name == name), None
        )
        if job is not None and job.created_at is not None:
            created = job.created_at
            return created if created.tzinfo else created.replace(tzinfo=UTC)
        path = self._job_defs.jobs_dir / f"{name}.yaml"
        if path.exists():
            return datetime.fromtimestamp(path.stat().st_mtime, UTC)
        return None

    def _last_run(self, name: str) -> datetime | None:
        """Read the last run time for a job name.

        SQLite drops tzinfo, so naive values are normalized to UTC.

        Args:
            name: Job name.

        Returns:
            Last run datetime or None.
        """
        job = next(
            (item for item in self._catalog.list_jobs() if item.name == name), None
        )
        if job is None or job.last_run_at is None:
            return None
        value = job.last_run_at
        return value if value.tzinfo else value.replace(tzinfo=UTC)

    async def _publish_overview(self) -> None:
        """Publish the schedules overview on the schedule topic."""
        await self._event_bus.publish(
            SCHEDULE_TOPIC, {"jobs": self.schedules_overview()}
        )
