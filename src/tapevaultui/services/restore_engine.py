"""
File:   restore_engine.py
Brief:  Restore engine: plan, single-pass tape read, hash-checked write-out.
Author: Mistress-Lukutar
Date:   2026-11-06
Version: v0.5.3
"""

from __future__ import annotations

import asyncio
import shutil
import time
import uuid
from pathlib import Path
from typing import Any, Literal

import structlog
from pydantic import BaseModel, Field

from tapevaultui.db.models import FileEntry, Tape
from tapevaultui.infrastructure.itdt_transport import (
    FakeItDtTransport,
    ItDtTransport,
)
from tapevaultui.services.catalog import CatalogRepository
from tapevaultui.services.event_bus import EventBus
from tapevaultui.services.session_io import read_session_header
from tapevaultui.services.snapshot import sha256_of

logger = structlog.get_logger(__name__)

RESTORE_TOPIC = "restore"

CollisionPolicy = Literal["skip", "overwrite", "rename"]
DestinationMode = Literal["original", "alternate"]


class RestoreItem(BaseModel):
    """One file selected for restore."""

    file_id: int
    session_id: int
    tape_file_index: int
    path: str
    size: int
    sha256: str


class RestorePlan(BaseModel):
    """A restore plan: one tape pass in storage order."""

    tape_id: str
    tape_label: str = ""
    items: list[RestoreItem]
    bytes_total: int
    files_count: int


class RestoreRequest(BaseModel):
    """Restore run request built from a plan."""

    file_ids: list[int] = Field(min_length=1)
    destination_mode: DestinationMode = "original"
    destination_dir: str | None = None
    collision: CollisionPolicy = "rename"


class RestoreStats:
    """Mutable progress accumulator for one restore run."""

    def __init__(self, run_id: int, tape_id: str) -> None:
        """Initialize zeroed stats.

        Args:
            run_id: Catalog job_runs id.
            tape_id: Expected tape identity.
        """
        self.run_id = run_id
        self.tape_id = tape_id
        self.state = "queued"
        self.files_total = 0
        self.files_done = 0
        self.bytes_total = 0
        self.bytes_done = 0
        self.current_file = ""
        self.hash_mismatches = 0
        self.started_monotonic = time.monotonic()
        self.error = ""

    def snapshot(self) -> dict[str, object]:
        """Return a JSON-serializable progress payload.

        Returns:
            Dict with all restore progress fields.
        """
        elapsed = time.monotonic() - self.started_monotonic
        speed = self.bytes_done / elapsed if elapsed > 0 else 0.0
        return {
            "run_id": self.run_id,
            "kind": "restore",
            "state": self.state,
            "tape_id": self.tape_id,
            "files_total": self.files_total,
            "files_done": self.files_done,
            "bytes_total": self.bytes_total,
            "bytes_done": self.bytes_done,
            "current_file": self.current_file,
            "hash_mismatches": self.hash_mismatches,
            "speed_bps": round(speed, 1),
            "elapsed_s": round(elapsed, 1),
            "error": self.error,
        }


class RestoreEngineError(RuntimeError):
    """Raised when a restore cannot continue."""


class RestoreCancelled(RestoreEngineError):
    """Raised when the restore was cancelled by the user."""


class RestoreEngine:
    """Executes restore plans through the ITDT transport.

    The plan is executed in ascending tape_file_index order (single
    optimal tape pass, spec §9.2). Every file is hash-checked after
    read; only matching content is written out.
    """

    def __init__(
        self,
        transport: ItDtTransport | FakeItDtTransport,
        catalog: CatalogRepository,
        work_dir: Path,
        event_bus: EventBus,
        drive_lock: asyncio.Lock,
    ) -> None:
        """Initialize the engine.

        Args:
            transport: ITDT transport (real or fake).
            catalog: Catalog repository.
            work_dir: Directory for temp download files.
            event_bus: Bus for restore progress publications.
            drive_lock: Shared lock serializing drive operations with the
                backup runtime.
        """
        self._transport = transport
        self._catalog = catalog
        self._work_dir = work_dir
        self._event_bus = event_bus
        self._drive_lock = drive_lock
        self._active_task: asyncio.Task[RestoreStats] | None = None
        self._active_stats: RestoreStats | None = None
        self._cancel_event = asyncio.Event()

    def is_running(self) -> bool:
        """Whether a restore run is currently active.

        Returns:
            True if a run is in flight.
        """
        return self._active_task is not None and not self._active_task.done()

    def status(self) -> dict[str, object] | None:
        """Current run progress, or None when idle.

        Returns:
            Progress snapshot of the active (or last) run.
        """
        return self._active_stats.snapshot() if self._active_stats else None

    def request_cancel(self) -> bool:
        """Signal cooperative cancellation of the active run.

        Returns:
            True if a run was active.
        """
        if not self.is_running():
            return False
        self._cancel_event.set()
        return True

    def start_run(
        self, request: RestoreRequest, plan: RestorePlan, run_id: int
    ) -> None:
        """Launch a restore run as a background task.

        Args:
            request: Destination and collision options.
            plan: The plan to execute.
            run_id: Catalog job_runs id.
        """
        stats = RestoreStats(run_id, plan.tape_id)
        self._active_stats = stats
        self._cancel_event = asyncio.Event()
        self._active_task = asyncio.create_task(
            self._run_and_record(request, plan, stats)
        )

    async def _run_and_record(
        self,
        request: RestoreRequest,
        plan: RestorePlan,
        stats: RestoreStats,
    ) -> RestoreStats:
        """Execute a run and record its terminal state in the catalog.

        Args:
            request: Destination and collision options.
            plan: The plan to execute.
            stats: Progress accumulator.

        Returns:
            The final stats.
        """
        try:
            final_stats = await self.execute(request, stats, plan, self._cancel_event)
        except RestoreCancelled as exc:
            stats.state = "cancelled"
            stats.error = str(exc)
            self._catalog.update_job_run(
                stats.run_id, status="cancelled", error=str(exc), finished=True
            )
        except RestoreEngineError as exc:
            stats.state = "failed"
            stats.error = str(exc)
            self._catalog.update_job_run(
                stats.run_id, status="failed", error=str(exc), finished=True
            )
        except Exception as exc:
            logger.exception("restore_run_crashed")
            stats.state = "failed"
            stats.error = str(exc)
            self._catalog.update_job_run(
                stats.run_id, status="failed", error=str(exc), finished=True
            )
        else:
            self._catalog.update_job_run(
                stats.run_id,
                status="done",
                stats_json=final_stats.snapshot(),
                finished=True,
            )
            return final_stats
        await self._publish(stats)
        return stats

    def build_plan(self, file_ids: list[int]) -> RestorePlan:
        """Build a restore plan from catalog file ids.

        Args:
            file_ids: Catalog file entry ids.

        Returns:
            The ordered RestorePlan (single tape, ascending index).

        Raises:
            RestoreEngineError: If ids are empty, unknown, or span
                multiple tapes.
        """
        items: list[RestoreItem] = []
        tape: Tape | None = None
        for file_id in file_ids:
            entry = self._get_file_entry(file_id)
            if entry.tape_file_index == 0 or entry.path == "<session-header>":
                raise RestoreEngineError("The session header cannot be restored")
            session = self._catalog.get_session(entry.session_id)
            if session is None:
                raise RestoreEngineError(f"Session missing for file {file_id}")
            entry_tape = self._catalog.get_tape(session.tape_id)
            if entry_tape is None:
                raise RestoreEngineError(f"Tape missing for file {file_id}")
            if tape is None:
                tape = entry_tape
            elif tape.id != entry_tape.id:
                raise RestoreEngineError(
                    "Selected files span multiple tapes; run separate restores"
                )
            if entry.status == "damaged":
                logger.warning("restore_damaged_file_selected", file_id=file_id)
            items.append(
                RestoreItem(
                    file_id=entry.id,
                    session_id=entry.session_id,
                    tape_file_index=entry.tape_file_index,
                    path=entry.path,
                    size=entry.size,
                    sha256=entry.sha256,
                )
            )
        if tape is None:
            raise RestoreEngineError("No files selected")
        items.sort(key=lambda item: item.tape_file_index)
        return RestorePlan(
            tape_id=tape.uuid,
            tape_label=tape.label,
            items=items,
            bytes_total=sum(item.size for item in items),
            files_count=len(items),
        )

    async def execute(
        self,
        request: RestoreRequest,
        stats: RestoreStats,
        plan: RestorePlan,
        cancel_event: asyncio.Event,
    ) -> RestoreStats:
        """Run a restore plan to completion.

        Args:
            request: Destination and collision options.
            stats: Progress accumulator.
            plan: The plan to execute.
            cancel_event: Cooperative cancel signal.

        Returns:
            The final restore stats.

        Raises:
            RestoreCancelled: On cancellation.
            RestoreEngineError: On wrong tape, hard ITDT failure.
        """
        async with self._drive_lock:
            stats.files_total = plan.files_count
            stats.bytes_total = plan.bytes_total
            stats.state = "preparing"
            await self._publish(stats)

            header = await read_session_header(self._transport, self._work_dir)
            if header is None or header.tape_id != plan.tape_id:
                raise RestoreEngineError(
                    f"Wrong tape inserted: expected {plan.tape_id}, "
                    "insert the requested tape and try again"
                )

            stats.state = "running"
            await self._publish(stats)
            await self._transport.run(["rewind"])

            for item in plan.items:
                if cancel_event.is_set():
                    raise RestoreCancelled("restore cancelled")
                await self._restore_item(item, request, stats)
                await self._publish(stats)

            await self._transport.run(["rewind"])
            stats.state = "done"
            await self._publish(stats)
            return stats

    async def _restore_item(
        self,
        item: RestoreItem,
        request: RestoreRequest,
        stats: RestoreStats,
    ) -> None:
        """Read one tape file, hash-check it, and write it out.

        Args:
            item: The plan item to restore.
            request: Destination and collision options.
            stats: Progress accumulator.
        """
        stats.current_file = item.path
        await self._publish(stats)

        dest = self._work_dir / f"restore_{uuid.uuid4().hex[:8]}.bin"
        # Absolute positioning: space from BOP over the file index.
        # Relative spacing after a previous read drifts whenever a
        # skipped/failed file shifts the position.
        await self._transport.run(["rewind"])
        await self._transport.run(["fsf", str(item.tape_file_index)])
        result = await self._transport.run(["read", "-d", str(dest)])
        if result.exit_code != 0:
            stats.hash_mismatches += 1
            logger.warning("restore_read_failed", path=item.path)
            return

        try:
            actual = await asyncio.to_thread(sha256_of, dest)
            if item.sha256 != "0" * 64 and actual != item.sha256:
                stats.hash_mismatches += 1
                logger.warning("restore_hash_mismatch", path=item.path)
                return
            target = self._target_path(item.path, request)
            if target is None:
                return  # collision policy: skip
            await asyncio.to_thread(shutil.copyfile, dest, target)
        finally:
            dest.unlink(missing_ok=True)

        stats.files_done += 1
        stats.bytes_done += item.size

    def _target_path(self, original_path: str, request: RestoreRequest) -> Path | None:
        """Resolve the output path for a restored file.

        Args:
            original_path: The absolute source path recorded in the catalog.
            request: Destination and collision options.

        Returns:
            The target path, or None when the collision policy says skip.
        """
        original = Path(original_path)
        if request.destination_mode == "original" or not request.destination_dir:
            target = original
        else:
            # Keep the hierarchy below the drive root to preserve relative
            # structure without colliding across source roots.
            relative = (
                Path(*original.parts[1:]) if len(original.parts) > 1 else original
            )
            target = Path(request.destination_dir) / relative

        if not target.exists() or request.collision == "overwrite":
            target.parent.mkdir(parents=True, exist_ok=True)
            return target
        if request.collision == "skip":
            logger.info("restore_collision_skipped", path=str(target))
            return None
        # rename: append a numeric suffix before the extension.
        stem, suffix = target.stem, target.suffix
        for counter in range(1, 1000):
            candidate = target.with_name(f"{stem}({counter}){suffix}")
            if not candidate.exists():
                candidate.parent.mkdir(parents=True, exist_ok=True)
                return candidate
        raise RestoreEngineError(f"Cannot find free name for {target}")

    async def _publish(self, stats: RestoreStats) -> None:
        """Publish restore progress on the restore topic.

        Args:
            stats: Progress accumulator.
        """
        payload: dict[str, Any] = stats.snapshot()
        await self._event_bus.publish(RESTORE_TOPIC, payload)

    def _get_file_entry(self, file_id: int) -> FileEntry:
        """Fetch a file entry or raise.

        Args:
            file_id: FileEntry primary key.

        Returns:
            The FileEntry row.

        Raises:
            RestoreEngineError: If the entry does not exist.
        """
        entry = self._catalog.get_file(file_id)
        if entry is None:
            raise RestoreEngineError(f"Unknown file id: {file_id}")
        return entry
