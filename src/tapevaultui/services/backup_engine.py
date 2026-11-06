"""
File:   backup_engine.py
Brief:  Job execution: snapshot -> write session (t0 + files) -> verify.
Author: Mistress-Lukutar
Date:   2026-11-06
Version: v0.5.0
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path

import structlog

from tapevaultui.db.models import Tape
from tapevaultui.infrastructure.itdt_transport import (
    FakeItDtTransport,
    ItDtTransport,
)
from tapevaultui.services.catalog import CatalogRepository
from tapevaultui.services.event_bus import EventBus
from tapevaultui.services.job_defs import JobDefinition
from tapevaultui.services.session_io import (
    SessionIoError,
    erase_tape_short,
    new_session_id,
    read_session_header,
    space_to_end_of_data,
    write_session_header,
)
from tapevaultui.services.snapshot import PlannedFile, sha256_of, snapshot_sources
from tapevaultui.services.tape_format import (
    HeaderFileEntry,
    SessionHeader,
    TapeFormatError,
    build_session_header,
    serialize_header,
)

logger = structlog.get_logger(__name__)

JOB_TOPIC = "job"
PROGRESS_INTERVAL_S = 1.0

# Callback invoked when the tape is full and a new cartridge is needed.
# Returns True when a (new) tape is ready, False on cancel.
MediaWaitCallback = Callable[[], Awaitable[bool]]
# Callback invoked cooperatively between files; returns True to cancel.
CancelCheckCallback = Callable[[], bool]


class BackupEngineError(RuntimeError):
    """Raised when a backup cannot continue."""


class TapeFullError(BackupEngineError):
    """Raised when the drive reports end of medium mid-session."""


class JobCancelled(BackupEngineError):
    """Raised when the job was cancelled between files."""


def _looks_like_eom(stdout: str) -> bool:
    """Check whether ITDT output indicates end of medium.

    Args:
        stdout: ITDT output text.

    Returns:
        True if the output suggests the tape is full.
    """
    lowered = stdout.lower()
    return "end of data" in lowered or "eom" in lowered or "end of medium" in lowered


class BackupStats:
    """Mutable progress accumulator for one backup job execution."""

    def __init__(self, job_name: str, run_id: int | None = None) -> None:
        """Initialize zeroed stats.

        Args:
            job_name: Job name for events.
            run_id: Catalog job_runs id (None before it is created).
        """
        self.job_name = job_name
        self.run_id = run_id
        self.state = "queued"
        self.files_total = 0
        self.files_done = 0
        self.bytes_total = 0
        self.bytes_done = 0
        self.current_file = ""
        self.current_file_started: float | None = None
        self.tape_id = ""
        self.session_id = ""
        self.started_monotonic = time.monotonic()
        self.error = ""

    def snapshot(self) -> dict[str, object]:
        """Return a JSON-serializable progress payload.

        ``bytes_done`` includes the estimated position within the current
        file: elapsed-since-current-file-start times the average speed
        (spec §4.3 — no intra-file byte counter in v1).

        Returns:
            Dict with all progress fields.
        """
        elapsed = time.monotonic() - self.started_monotonic
        speed = self.bytes_done / elapsed if elapsed > 0 else 0.0
        estimated = float(self.bytes_done)
        if self.current_file_started is not None and speed > 0:
            in_file = time.monotonic() - self.current_file_started
            estimated = min(estimated + speed * in_file, float(self.bytes_total))
        remaining_bytes = self.bytes_total - estimated
        eta = remaining_bytes / speed if speed > 0 else None
        return {
            "job_name": self.job_name,
            "run_id": self.run_id,
            "state": self.state,
            "files_total": self.files_total,
            "files_done": self.files_done,
            "bytes_total": self.bytes_total,
            "bytes_done": round(estimated),
            "bytes_confirmed": self.bytes_done,
            "current_file": self.current_file,
            "tape_id": self.tape_id,
            "session_id": self.session_id,
            "speed_bps": round(speed, 1),
            "eta_s": round(eta) if eta is not None else None,
            "elapsed_s": round(elapsed, 1),
            "error": self.error,
        }


class BackupEngine:
    """Executes backup jobs end to end through the ITDT transport.

    Tape passes: when the medium fills mid-job, the engine publishes
    waiting_media, awaits the media callback, and continues the remaining
    files as a NEW session on the next tape (spec §8). A failed file is
    marked damaged/skipped in the catalog; the session header keeps the
    planned file list, the catalog tracks what actually landed (spec §4.4).
    """

    def __init__(
        self,
        transport: ItDtTransport | FakeItDtTransport,
        catalog: CatalogRepository,
        work_dir: Path,
        event_bus: EventBus,
    ) -> None:
        """Initialize the engine.

        Args:
            transport: ITDT transport (real or fake).
            catalog: Catalog repository.
            work_dir: Directory for temp files (headers, verify downloads).
            event_bus: Bus for job progress publications.
        """
        self._transport = transport
        self._catalog = catalog
        self._work_dir = work_dir
        self._event_bus = event_bus

    async def publish(self, stats: BackupStats) -> None:
        """Publish the current progress snapshot on the job topic.

        Args:
            stats: Progress accumulator to publish.
        """
        await self._event_bus.publish(JOB_TOPIC, stats.snapshot())

    async def execute(
        self,
        definition: JobDefinition,
        stats: BackupStats,
        *,
        wait_for_media: MediaWaitCallback | None = None,
        cancel_check: CancelCheckCallback | None = None,
    ) -> BackupStats:
        """Run a backup job to completion.

        Args:
            definition: The job to execute.
            stats: Progress accumulator (run_id may be preset).
            wait_for_media: Callback awaiting new tape insertion.
            cancel_check: Cooperative cancel check.

        Returns:
            The final progress stats.

        Raises:
            JobCancelled: If cancelled via cancel_check.
            TapeFullError: If the tape filled and no media callback given.
            BackupEngineError: On unrecoverable ITDT/snapshot failures.
        """
        stats.state = "preparing"
        await self.publish(stats)

        plan = await asyncio.to_thread(
            snapshot_sources,
            definition.sources,
            continue_on_error=definition.options.continue_on_error,
        )
        if cancel_check and cancel_check():
            raise JobCancelled("cancelled during preparation")

        stats.files_total = len(plan)
        stats.bytes_total = sum(file.size for file in plan)
        await self.publish(stats)

        remaining = list(plan)
        while remaining:
            processed = await self._write_tape_pass(
                definition,
                remaining,
                stats,
                cancel_check=cancel_check,
                wait_for_media=wait_for_media,
            )
            remaining = remaining[processed:]
        return stats

    async def _write_tape_pass(
        self,
        definition: JobDefinition,
        files: list[PlannedFile],
        stats: BackupStats,
        *,
        cancel_check: CancelCheckCallback | None,
        wait_for_media: MediaWaitCallback | None,
    ) -> int:
        """Write one session covering as many files as fit on the tape.

        Args:
            definition: Job definition.
            files: Files still to write (consumed in order).
            stats: Progress accumulator.
            cancel_check: Cooperative cancel check.
            wait_for_media: Callback awaiting new tape insertion.

        Returns:
            Number of files processed in this pass (skipped and damaged
            files count as processed, so the caller never retries them;
            a file cut off by end-of-medium is NOT processed and will be
            retried on the next tape).

        Raises:
            JobCancelled: On cancellation.
            TapeFullError: When the tape filled with files still pending
                and no media callback is available.
            BackupEngineError: On hard ITDT failures.
        """
        tape_id, tape_row = await self._prepare_tape()
        stats.tape_id = tape_id

        session = self._catalog.create_session(tape_row.id, job_name=definition.name)
        stats.session_id = new_session_id()

        if definition.options.mode == "overwrite":
            await erase_tape_short(self._transport)
        else:
            await space_to_end_of_data(self._transport)

        header = build_session_header(
            tape_id=tape_id,
            session_id=stats.session_id,
            job_name=definition.name,
            tape_label=tape_row.label,
            started_at=datetime.now(UTC),
            files=[
                HeaderFileEntry(
                    seq=seq,
                    path=file.path,
                    size=file.size,
                    mtime=file.mtime,
                    sha256=file.sha256,
                    tape_file_index=seq + 1,
                )
                for seq, file in enumerate(files)
            ],
        )
        await write_session_header(self._transport, header, self._work_dir)
        self._catalog.add_file(
            session.id,
            tape_file_index=0,
            path="<session-header>",
            size=len(serialize_header(header)),
            mtime=datetime.now(UTC),
            sha256="0" * 64,
        )

        stats.state = "writing"
        await self.publish(stats)
        files_processed = 0
        bytes_written_in_pass = 0
        hard_failure: str | None = None

        for offset, file in enumerate(files):
            if cancel_check and cancel_check():
                self._catalog.finish_session(session.id, status="failed")
                raise JobCancelled(f"cancelled before {file.path}")

            stats.current_file = file.path
            stats.current_file_started = time.monotonic()
            await self.publish(stats)
            ticker = self._start_ticker(stats)
            try:
                result = await self._transport.run(["write", "-raw", "-s", file.path])
            finally:
                _stop_ticker(ticker)

            catalog_mtime = datetime.fromtimestamp(file.mtime, UTC)
            if result.exit_code != 0:
                stdout = result.stdout.strip()
                if _looks_like_eom(stdout):
                    # Tape full: this file was NOT written; the next pass
                    # retries it on the following tape as a new session.
                    self._catalog.add_file(
                        session.id,
                        tape_file_index=offset + 1,
                        path=file.path,
                        size=file.size,
                        mtime=catalog_mtime,
                        sha256=file.sha256,
                        status="skipped",
                    )
                    self._catalog.finish_session(session.id, status="partial")
                    if wait_for_media is None:
                        raise TapeFullError(f"Tape full: {stdout}")
                    await self._await_new_tape(stats, wait_for_media)
                    return files_processed
                self._catalog.add_file(
                    session.id,
                    tape_file_index=offset + 1,
                    path=file.path,
                    size=file.size,
                    mtime=catalog_mtime,
                    sha256=file.sha256,
                    status="damaged",
                )
                if definition.options.continue_on_error:
                    logger.warning("backup_file_damaged", path=file.path, output=stdout)
                    files_processed += 1
                    continue
                hard_failure = stdout
                break

            self._catalog.add_file(
                session.id,
                tape_file_index=offset + 1,
                path=file.path,
                size=file.size,
                mtime=catalog_mtime,
                sha256=file.sha256,
                status="ok",
            )
            files_processed += 1
            stats.files_done += 1
            stats.bytes_done += file.size
            bytes_written_in_pass += file.size
            await self.publish(stats)

        if hard_failure is not None:
            self._catalog.finish_session(session.id, status="failed")
            raise BackupEngineError(f"ITDT write failed: {hard_failure}")

        self._catalog.update_tape(tape_row.id, bytes_used_delta=bytes_written_in_pass)
        if definition.options.verify == "readback":
            stats.state = "verifying"
            await self.publish(stats)
            await self._verify_pass(session.id, files, stats, cancel_check)
        await self.publish(stats)

        self._catalog.finish_session(session.id, status="ok")
        return files_processed

    async def _verify_pass(
        self,
        session_id: int,
        files: list[PlannedFile],
        stats: BackupStats,
        cancel_check: CancelCheckCallback | None,
    ) -> None:
        """Re-read written files and compare sha256 against the plan.

        Args:
            session_id: Catalog session id for status updates.
            files: Files written in this pass (tape order).
            stats: Progress accumulator (unused, kept for event symmetry).
            cancel_check: Cooperative cancel check.

        Raises:
            JobCancelled: On cancellation.
        """
        entries = {
            entry.tape_file_index: entry
            for entry in self._catalog.list_files(session_id)
        }
        await self._transport.run(["rewind"])
        for offset, file in enumerate(files):
            tape_index = offset + 1
            entry = entries.get(tape_index)
            if entry is None or entry.status != "ok":
                continue
            if cancel_check and cancel_check():
                raise JobCancelled("cancelled during verification")
            dest = self._work_dir / f"verify_{tape_index}.bin"
            await self._transport.run(["fsf", str(tape_index)])
            result = await self._transport.run(["read", "-d", str(dest)])
            if result.exit_code != 0:
                self._catalog.mark_file_status(entry.id, "damaged")
                logger.warning("verify_read_failed", path=file.path)
                continue
            actual = await asyncio.to_thread(sha256_of, dest)
            dest.unlink(missing_ok=True)
            if actual != file.sha256:
                self._catalog.mark_file_status(entry.id, "damaged")
                logger.warning("verify_hash_mismatch", path=file.path)
        await self._transport.run(["rewind"])

    async def read_tape_header_quiet(self) -> SessionHeader | None:
        """Read the inserted tape's header, swallowing device errors.

        Used by the waiting_media poll: an empty drive or a read failure
        simply means "no TapeWebUI tape inserted yet".

        Returns:
            Parsed SessionHeader or None.
        """

        try:
            return await read_session_header(self._transport, self._work_dir)
        except (SessionIoError, TapeFormatError):
            return None

    async def _prepare_tape(self) -> tuple[str, Tape]:
        """Identify the inserted tape and ensure it exists in the catalog.

        A blank/foreign tape gets a fresh tape_id and is registered as a
        new TapeWebUI tape (it will be claimed by the header written next).

        Returns:
            Tuple of (tape_id, catalog Tape row).
        """
        header = await read_session_header(self._transport, self._work_dir)
        if header is None:
            tape_id = str(uuid.uuid4())
            tape_row = self._catalog.register_tape(tape_id)
            logger.info("new_tape_registered", tape_id=tape_id)
            return tape_id, tape_row
        tape_row = self._catalog.register_tape(header.tape_id, label=header.tape_label)
        return header.tape_id, tape_row

    async def _await_new_tape(
        self,
        stats: BackupStats,
        wait_for_media: MediaWaitCallback,
    ) -> None:
        """Enter waiting_media until the callback reports a new tape.

        Args:
            stats: Progress accumulator.
            wait_for_media: Callback awaiting tape insertion.
        """
        stats.state = "waiting_media"
        await self.publish(stats)
        ready = await wait_for_media()
        if not ready:
            raise JobCancelled("cancelled while waiting for media")
        await self.publish(stats)

    def _start_ticker(self, stats: BackupStats) -> asyncio.Task[None] | None:
        """Start a periodic progress publisher for the current file.

        The in-file byte position is estimated from elapsed time and the
        average speed so far (spec §4.3); the ticker simply republishes
        the stats snapshot while a write blocks in the transport.

        Args:
            stats: Progress accumulator.

        Returns:
            The ticker task, or None if no loop is running.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return None

        async def _tick() -> None:
            while True:
                await asyncio.sleep(PROGRESS_INTERVAL_S)
                await self.publish(stats)

        return loop.create_task(_tick())


def _stop_ticker(task: asyncio.Task[None] | None) -> None:
    """Stop a progress ticker task.

    Args:
        task: The ticker task or None.
    """
    if task is not None:
        task.cancel()
