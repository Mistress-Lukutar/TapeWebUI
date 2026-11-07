"""
File:   restore_engine.py
Brief:  Restore engine: plan, single-pass tape read, hash-checked write-out.
Author: Mistress-Lukutar
Date:   2026-11-07
Version: v0.7.0
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
from tapevaultui.services.catalog import SESSION_HEADER_PATH, CatalogRepository
from tapevaultui.services.event_bus import EventBus
from tapevaultui.services.session_io import (
    SessionIoError,
    read_session_header,
)
from tapevaultui.services.snapshot import sha256_of
from tapevaultui.services.tape_format import TapeFormatError

logger = structlog.get_logger(__name__)

RESTORE_TOPIC = "restore"

CollisionPolicy = Literal["skip", "overwrite", "rename"]
DestinationMode = Literal["original", "alternate"]

# How often the waiting_media poll re-reads the inserted tape header.
MEDIA_POLL_INTERVAL_S = 3.0
# Granularity of the cancel-responsive sleep between polls.
CANCEL_SLICE_S = 0.5


class RestoreItem(BaseModel):
    """One file selected for restore."""

    file_id: int
    session_id: int
    tape_file_index: int
    path: str
    size: int
    sha256: str


class PlanSelection(BaseModel):
    """A subtree selection expanded into file ids at plan time.

    Exactly one of ``session_id`` (single-session scope) or ``tape_id``
    (merged scope across the tape's live sessions) must be set.
    """

    session_id: int | None = None
    tape_id: int | None = None
    prefix: str = ""
    latest_only: bool = True


class TapeGroup(BaseModel):
    """The plan items of one tape, in storage order."""

    tape_id: str
    tape_label: str = ""
    items: list[RestoreItem]


class RestorePlan(BaseModel):
    """A restore plan: one tape pass per tape, grouped by media."""

    tapes: list[TapeGroup]
    bytes_total: int
    files_count: int


class RestoreRequest(BaseModel):
    """Restore run request built from a plan."""

    file_ids: list[int] = Field(default_factory=list)
    selections: list[PlanSelection] = Field(default_factory=list)
    destination_mode: DestinationMode = "original"
    destination_dir: str | None = None
    collision: CollisionPolicy = "rename"


class RestoreStats:
    """Mutable progress accumulator for one restore run."""

    def __init__(self, run_id: int, tape_id: str, tapes_total: int = 1) -> None:
        """Initialize zeroed stats.

        Args:
            run_id: Catalog job_runs id.
            tape_id: Expected tape identity (first planned tape).
            tapes_total: Number of tapes in the plan.
        """
        self.run_id = run_id
        self.tape_id = tape_id
        self.tapes_total = tapes_total
        self.tape_label = ""
        self.waiting_for_tape = ""
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
            "tape_label": self.tape_label,
            "tapes_total": self.tapes_total,
            "waiting_for_tape": self.waiting_for_tape,
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

    The plan is executed one tape pass per tape group, in storage order
    (absolute tape_file_index from BOP). When the wrong tape is inserted
    the engine publishes ``waiting_media`` and polls until the expected
    cartridge is loaded. Every file is hash-checked after read; only
    matching content is written out.
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
        first_tape = plan.tapes[0].tape_id if plan.tapes else ""
        stats = RestoreStats(run_id, first_tape, tapes_total=len(plan.tapes))
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

    def build_plan(
        self, file_ids: list[int], selections: list[PlanSelection] | None = None
    ) -> RestorePlan:
        """Build a restore plan from file ids and subtree selections.

        Selections are expanded first (in request order), then explicit
        file ids are appended; duplicate ids are dropped. The items are
        grouped per tape and ordered by absolute tape position within
        each group.

        Args:
            file_ids: Catalog file entry ids.
            selections: Subtree selections to expand (session or tape
                scope).

        Returns:
            The RestorePlan with one group per tape.

        Raises:
            RestoreEngineError: If the selection is empty or invalid,
                an id is unknown, a session was erased, or the session
                header is selected.
        """
        ordered_ids: list[int] = []
        seen: set[int] = set()
        for selection in selections or []:
            for file_id in self._expand_selection(selection):
                if file_id not in seen:
                    seen.add(file_id)
                    ordered_ids.append(file_id)
        for file_id in file_ids:
            if file_id not in seen:
                seen.add(file_id)
                ordered_ids.append(file_id)
        if not ordered_ids:
            raise RestoreEngineError("No files selected")

        groups: dict[int, tuple[Tape, list[RestoreItem]]] = {}
        for file_id in ordered_ids:
            entry = self._get_file_entry(file_id)
            if entry.path == SESSION_HEADER_PATH:
                raise RestoreEngineError("The session header cannot be restored")
            session = self._catalog.get_session(entry.session_id)
            if session is None:
                raise RestoreEngineError(f"Session missing for file {file_id}")
            if session.status == "erased":
                raise RestoreEngineError(
                    f"Session {session.id} was erased; its data is no longer "
                    "on the tape"
                )
            entry_tape = self._catalog.get_tape(session.tape_id)
            if entry_tape is None:
                raise RestoreEngineError(f"Tape missing for file {file_id}")
            if entry.status == "damaged":
                logger.warning("restore_damaged_file_selected", file_id=file_id)
            item = RestoreItem(
                file_id=entry.id,
                session_id=entry.session_id,
                tape_file_index=entry.tape_file_index,
                path=entry.path,
                size=entry.size,
                sha256=entry.sha256,
            )
            bucket = groups.get(entry_tape.id)
            if bucket is None:
                groups[entry_tape.id] = (entry_tape, [item])
            else:
                bucket[1].append(item)

        tape_groups: list[TapeGroup] = []
        for tape, items in groups.values():
            items.sort(key=lambda item: item.tape_file_index)
            tape_groups.append(
                TapeGroup(tape_id=tape.uuid, tape_label=tape.label, items=items)
            )
        return RestorePlan(
            tapes=tape_groups,
            bytes_total=sum(item.size for group in tape_groups for item in group.items),
            files_count=sum(len(group.items) for group in tape_groups),
        )

    def _expand_selection(self, selection: PlanSelection) -> list[int]:
        """Expand one subtree selection into catalog file ids.

        Args:
            selection: The selection scope and prefix.

        Returns:
            Catalog file ids under the selection.

        Raises:
            RestoreEngineError: On ambiguous scope, unknown session or
                tape, or an erased session.
        """
        if (selection.session_id is None) == (selection.tape_id is None):
            raise RestoreEngineError(
                "Selection must set exactly one of session_id or tape_id"
            )
        if selection.session_id is not None:
            session = self._catalog.get_session(selection.session_id)
            if session is None:
                raise RestoreEngineError(f"Session {selection.session_id} not found")
            if session.status == "erased":
                raise RestoreEngineError(
                    f"Session {session.id} was erased; its data is no longer "
                    "on the tape"
                )
            return [
                entry.id
                for entry in self._catalog.files_under_prefix(
                    selection.session_id, selection.prefix
                )
            ]
        assert selection.tape_id is not None
        tape = self._catalog.get_tape(selection.tape_id)
        if tape is None:
            raise RestoreEngineError(f"Tape {selection.tape_id} not found")
        if selection.latest_only:
            return self._catalog.latest_file_ids_under_prefix(
                selection.tape_id, selection.prefix
            )
        return [
            entry.id
            for entry, _ in self._catalog.tape_files_under_prefix(
                selection.tape_id, selection.prefix
            )
        ]

    async def execute(
        self,
        request: RestoreRequest,
        stats: RestoreStats,
        plan: RestorePlan,
        cancel_event: asyncio.Event,
    ) -> RestoreStats:
        """Run a restore plan to completion, tape group by tape group.

        Before each group the inserted medium is checked; a wrong or
        missing tape enters ``waiting_media`` (drive lock released so
        backups can proceed) and polls until the expected cartridge is
        loaded or the restore is cancelled.

        Args:
            request: Destination and collision options.
            stats: Progress accumulator.
            plan: The plan to execute.
            cancel_event: Cooperative cancel signal.

        Returns:
            The final restore stats.

        Raises:
            RestoreCancelled: On cancellation.
            RestoreEngineError: On incompatible media or hard ITDT
                failure.
        """
        stats.files_total = plan.files_count
        stats.bytes_total = plan.bytes_total
        stats.tapes_total = len(plan.tapes)
        stats.state = "preparing"
        await self._publish(stats)

        for group in plan.tapes:
            if cancel_event.is_set():
                raise RestoreCancelled("restore cancelled")
            stats.tape_id = group.tape_id
            stats.tape_label = group.tape_label
            await self._await_tape(group, stats, cancel_event)
            async with self._drive_lock:
                stats.state = "running"
                await self._publish(stats)
                await self._transport.run(["rewind"])
                for item in group.items:
                    if cancel_event.is_set():
                        raise RestoreCancelled("restore cancelled")
                    await self._restore_item(item, request, stats)
                    await self._publish(stats)
                await self._transport.run(["rewind"])

        stats.state = "done"
        await self._publish(stats)
        return stats

    async def _await_tape(
        self,
        group: TapeGroup,
        stats: RestoreStats,
        cancel_event: asyncio.Event,
    ) -> None:
        """Wait until the tape of a plan group is inserted.

        Args:
            group: The tape group awaiting its medium.
            stats: Progress accumulator (waiting_media is published).
            cancel_event: Cooperative cancel signal.

        Raises:
            RestoreCancelled: On cancellation while waiting.
            RestoreEngineError: When the inserted medium holds an
                incompatible future header format.
        """
        while True:
            if cancel_event.is_set():
                raise RestoreCancelled("restore cancelled")
            try:
                async with self._drive_lock:
                    header = await read_session_header(self._transport, self._work_dir)
            except TapeFormatError as exc:
                raise RestoreEngineError(
                    f"Inserted tape is incompatible: {exc}"
                ) from exc
            except SessionIoError:
                header = None  # no medium / transient error: keep waiting
            if header is not None and header.tape_id == group.tape_id:
                return
            stats.state = "waiting_media"
            stats.waiting_for_tape = group.tape_label or group.tape_id
            await self._publish(stats)
            await self._sleep_cancelable(cancel_event)

    async def _sleep_cancelable(self, cancel_event: asyncio.Event) -> None:
        """Sleep one media poll interval, checking cancellation.

        Args:
            cancel_event: Cooperative cancel signal.

        Raises:
            RestoreCancelled: On cancellation during the wait.
        """
        deadline = time.monotonic() + MEDIA_POLL_INTERVAL_S
        while True:
            if cancel_event.is_set():
                raise RestoreCancelled("restore cancelled")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            await asyncio.sleep(min(CANCEL_SLICE_S, remaining))

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
