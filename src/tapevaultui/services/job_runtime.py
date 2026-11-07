"""
File:   job_runtime.py
Brief:  Job queue and state machine (exactly one active drive job).
Author: Mistress-Lukutar
Date:   2026-11-07
Version: v0.7.0
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

import structlog

from tapevaultui.services.backup_engine import (
    BackupEngine,
    BackupEngineError,
    BackupStats,
    JobCancelled,
    TapeFullError,
)
from tapevaultui.services.catalog import CatalogRepository
from tapevaultui.services.event_bus import EventBus
from tapevaultui.services.job_defs import JobDefinition

logger = structlog.get_logger(__name__)


class QueueEntry:
    """One queued or running job."""

    def __init__(self, definition: JobDefinition, run_id: int) -> None:
        """Initialize a queue entry.

        Args:
            definition: The job to run.
            run_id: Catalog job_runs id.
        """
        self.definition = definition
        self.run_id = run_id
        self.stats = BackupStats(definition.name, run_id)
        self.cancel_event = asyncio.Event()

    def to_public(self) -> dict[str, object]:
        """Build the UI-facing payload for this entry.

        Returns:
            Progress/stats dict including the queue position marker.
        """
        return self.stats.snapshot()


class JobRuntime:
    """Serializes drive jobs: exactly one active, the rest queued.

    States (spec §8): queued → preparing → writing → verifying → done,
    with waiting_media / failed / cancelled. All transitions are
    published on the ``job`` topic and mirrored into the catalog.
    """

    def __init__(
        self,
        engine: BackupEngine,
        catalog: CatalogRepository,
        event_bus: EventBus,
    ) -> None:
        """Initialize the runtime.

        Args:
            engine: The backup engine driving actual work.
            catalog: Catalog repository for job_runs records.
            event_bus: Bus for job publications.
        """
        self._engine = engine
        self._catalog = catalog
        self._event_bus = event_bus
        self._queue: asyncio.Queue[QueueEntry] = asyncio.Queue()
        self._pending: list[QueueEntry] = []
        self._active: QueueEntry | None = None
        self._worker_task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()

    @property
    def active(self) -> QueueEntry | None:
        """The currently active entry, or None."""
        return self._active

    def queue_length(self) -> int:
        """Number of entries waiting in the queue.

        Returns:
            Queued entry count (active job not included).
        """
        return self._queue.qsize()

    def start(self) -> None:
        """Start the worker loop (idempotent)."""
        if self._worker_task is None or self._worker_task.done():
            self._stopping.clear()
            self._worker_task = asyncio.create_task(self._worker())

    async def stop(self) -> None:
        """Stop the worker and cancel any active job."""
        self._stopping.set()
        if self._active is not None:
            self._active.cancel_event.set()
        if self._worker_task is not None:
            self._worker_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._worker_task
            self._worker_task = None

    def enqueue(self, definition: JobDefinition) -> QueueEntry:
        """Queue a job for execution.

        Args:
            definition: The job to run.

        Returns:
            The created QueueEntry (already recorded in the catalog).
        """
        run = self._catalog.create_job_run()
        entry = QueueEntry(definition, run.id)
        self._pending.append(entry)
        self._queue.put_nowait(entry)
        self.start()
        # Mirror the run into the jobs table for the scheduler/UI lists.
        self._catalog.mark_job_ran(definition.name)
        logger.info("job_enqueued", job=definition.name, run_id=run.id)
        return entry

    def request_cancel(self) -> bool:
        """Cancel the active job (cooperative) and drop queued ones.

        Returns:
            True if there was an active or queued job to cancel.
        """
        cancelled = False
        if self._active is not None:
            self._active.cancel_event.set()
            cancelled = True
        while self._pending:
            dropped = self._pending.pop(0)
            self._catalog.update_job_run(
                dropped.run_id, status="cancelled", finished=True
            )
            # Drain the matching asyncio.Queue item.
            while not self._queue.empty():
                try:
                    self._queue.get_nowait()
                    break
                except asyncio.QueueEmpty:
                    break
            cancelled = True
        return cancelled

    def status(self) -> dict[str, Any]:
        """Build the full runtime status for the UI.

        Returns:
            Dict with the active job and queued jobs.
        """
        return {
            "active": self._active.to_public() if self._active else None,
            "queued": [entry.to_public() for entry in self._pending],
            "queue_length": len(self._pending),
        }

    async def _worker(self) -> None:
        """Process queue entries one at a time."""
        while not self._stopping.is_set():
            try:
                entry = await self._queue.get()
            except asyncio.CancelledError:
                raise
            self._active = entry
            if entry in self._pending:
                self._pending.remove(entry)
            await self._run_entry(entry)
            self._active = None

    async def _run_entry(self, entry: QueueEntry) -> None:
        """Drive one entry through the state machine.

        Args:
            entry: The queue entry to execute.
        """
        stats = entry.stats
        try:
            await self._engine.execute(
                entry.definition,
                stats,
                wait_for_media=self._wait_for_media(entry),
                cancel_check=entry.cancel_event.is_set,
            )
        except JobCancelled as exc:
            stats.state = "cancelled"
            stats.error = str(exc)
            self._record_terminal(entry, "cancelled", str(exc))
        except TapeFullError as exc:
            stats.state = "failed"
            stats.error = f"{exc} (insert the next tape and retry)"
            self._record_terminal(entry, "waiting_media", str(exc))
        except BackupEngineError as exc:
            stats.state = "failed"
            stats.error = str(exc)
            self._record_terminal(entry, "failed", str(exc))
        except Exception as exc:
            logger.exception("job_run_crashed", job=entry.definition.name)
            stats.state = "failed"
            stats.error = str(exc)
            self._record_terminal(entry, "failed", str(exc))
        else:
            stats.state = "done"
            if stats.error:
                # e.g. readback verification damaged files: the job ran
                # through, but the UI must not show a clean success.
                self._record_terminal(
                    entry,
                    "done_with_errors",
                    stats.error,
                    stats_json=stats.snapshot(),
                )
            else:
                self._record_terminal(entry, "done", None, stats_json=stats.snapshot())
        await self._engine.publish(stats)

    def _record_terminal(
        self,
        entry: QueueEntry,
        status: str,
        error: str | None,
        stats_json: dict[str, object] | None = None,
    ) -> None:
        """Record a terminal run state in the catalog.

        Args:
            entry: The finished queue entry.
            status: Terminal status.
            error: Error message, if any.
            stats_json: Final stats payload for done runs.
        """
        self._catalog.update_job_run(
            entry.run_id,
            status=status,
            error=error,
            stats_json=stats_json,
            finished=True,
        )
        self._catalog.mark_job_ran(entry.definition.name, status=status)

    def _wait_for_media(self, entry: QueueEntry) -> Any:
        """Build the media-wait callback for an entry.

        v1 behavior (spec §15 decision): Windows toast notification is
        enough; the runtime polls the drive for usable media every 15
        seconds — ready when a blank/foreign tape or one with a different
        tape_id is loaded (an empty drive keeps waiting).

        Args:
            entry: The entry that hit end-of-medium or started with no
                medium loaded.

        Returns:
            Async callback returning True when media is ready.
        """

        async def _wait() -> bool:
            old_tape_id = entry.stats.tape_id
            while not entry.cancel_event.is_set():
                await asyncio.sleep(15.0)
                if await self._engine.media_ready(old_tape_id):
                    logger.info("media_ready", old=old_tape_id)
                    return True
            return False

        return _wait
