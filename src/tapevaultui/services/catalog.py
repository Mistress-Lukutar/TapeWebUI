"""
File:   catalog.py
Brief:  SQLite catalog repository on SQLAlchemy 2.0 (sync, thread-offloaded).
Author: Mistress-Lukutar
Date:   2026-11-06
Version: v0.5.0
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import structlog
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from tapevaultui.db.models import (
    BackupSession,
    Base,
    FileEntry,
    Job,
    JobRun,
    Setting,
    Tape,
)

logger = structlog.get_logger(__name__)

# Conservative native capacity of an LTO-3 cartridge (bytes), used until
# real consumption is tracked by the catalog.
LTO3_NATIVE_CAPACITY = 400 * 1024**3


def sqlite_url_for_path(db_path: Path) -> str:
    """Build a sqlite URL for a catalog file path.

    Args:
        db_path: Path to catalog.db.

    Returns:
        SQLAlchemy sqlite URL string.
    """
    return f"sqlite:///{db_path}"


class CatalogRepository:
    """Repository for the backup catalog.

    All methods are synchronous; callers that need non-blocking behavior
    must wrap them in ``asyncio.to_thread`` (the drive is the bottleneck,
    so sync DB calls are acceptable and much simpler).
    """

    def __init__(self, db_url: str, *, echo: bool = False) -> None:
        """Initialize the repository and its engine.

        Args:
            db_url: SQLAlchemy database URL (sqlite catalog).
            echo: Log all SQL statements (debugging).
        """
        self._engine = create_engine(db_url, echo=echo)
        self._session_factory = sessionmaker(self._engine, expire_on_commit=False)

    def create_all(self) -> None:
        """Create all tables (dev/test shortcut; use alembic in production)."""
        Base.metadata.create_all(self._engine)

    # -- tapes ------------------------------------------------------------

    def register_tape(self, uuid: str, label: str = "") -> Tape:
        """Register a new tape or return the existing one by uuid.

        Args:
            uuid: Stable tape identity (generated on first write).
            label: Human-readable label.

        Returns:
            The Tape row.
        """
        with self._session_factory() as session:
            tape = session.scalar(select(Tape).where(Tape.uuid == uuid))
            if tape is None:
                tape = Tape(uuid=uuid, label=label, created_at=_now())
                session.add(tape)
                session.commit()
                logger.info("catalog_tape_registered", uuid=uuid, label=label)
            return tape

    def get_tape_by_uuid(self, uuid: str) -> Tape | None:
        """Find a tape by uuid.

        Args:
            uuid: Tape identity.

        Returns:
            The Tape row or None.
        """
        with self._session_factory() as session:
            return session.scalar(select(Tape).where(Tape.uuid == uuid))

    def get_tape(self, tape_id: int) -> Tape | None:
        """Find a tape by id.

        Args:
            tape_id: Tape primary key.

        Returns:
            The Tape row or None.
        """
        with self._session_factory() as session:
            return session.get(Tape, tape_id)

    def list_tapes(self) -> list[Tape]:
        """List all tapes ordered by creation time.

        Returns:
            List of Tape rows.
        """
        with self._session_factory() as session:
            return list(session.scalars(select(Tape).order_by(Tape.created_at)))

    def update_tape(
        self,
        tape_id: int,
        *,
        label: str | None = None,
        status: str | None = None,
        notes: str | None = None,
        bytes_used_delta: int = 0,
    ) -> Tape | None:
        """Update mutable tape fields.

        Args:
            tape_id: Tape primary key.
            label: New label (unchanged if None).
            status: New status: active | full | retired.
            notes: New notes (unchanged if None).
            bytes_used_delta: Value added to bytes_used.

        Returns:
            The updated Tape row or None if not found.

        Raises:
            ValueError: If the status value is not allowed.
        """
        if status is not None and status not in ("active", "full", "retired"):
            raise ValueError(f"Invalid tape status: {status!r}")
        with self._session_factory() as session:
            tape = session.get(Tape, tape_id)
            if tape is None:
                return None
            if label is not None:
                tape.label = label
            if status is not None:
                tape.status = status
            if notes is not None:
                tape.notes = notes
            tape.bytes_used += bytes_used_delta
            session.commit()
            return tape

    def mark_tape_erased(self, tape_id: int) -> int:
        """Mark all sessions of a tape erased (history is kept, not deleted).

        Args:
            tape_id: Tape primary key.

        Returns:
            Number of sessions marked erased.
        """
        with self._session_factory() as session:
            sessions = list(
                session.scalars(
                    select(BackupSession).where(BackupSession.tape_id == tape_id)
                )
            )
            for backup_session in sessions:
                backup_session.status = "erased"
            tape = session.get(Tape, tape_id)
            if tape is not None:
                tape.bytes_used = 0
                tape.status = "active"
            session.commit()
            logger.info(
                "catalog_tape_marked_erased", tape_id=tape_id, sessions=len(sessions)
            )
            return len(sessions)

    # -- sessions and files ----------------------------------------------

    def create_session(
        self,
        tape_id: int,
        *,
        job_id: int | None = None,
        job_name: str = "",
        started_at: datetime | None = None,
        format_version: int = 1,
    ) -> BackupSession:
        """Create a new write session record.

        Args:
            tape_id: Tape primary key.
            job_id: Owning job id (None for manual sessions).
            job_name: Job name snapshot.
            started_at: Session start (defaults to now UTC).
            format_version: Tape format version.

        Returns:
            The created BackupSession row.
        """
        with self._session_factory() as session:
            tape_seq = _next_tape_seq(session, tape_id)
            record = BackupSession(
                tape_id=tape_id,
                tape_seq=tape_seq,
                job_id=job_id,
                job_name=job_name,
                started_at=started_at or _now(),
                format_version=format_version,
            )
            session.add(record)
            session.commit()
            return record

    def finish_session(
        self,
        session_id: int,
        *,
        status: str,
        finished_at: datetime | None = None,
    ) -> BackupSession | None:
        """Finalize a session with a terminal status and aggregate stats.

        Args:
            session_id: BackupSession primary key.
            status: Terminal status: ok | partial | failed | erased.
            finished_at: Finish time (defaults to now UTC).

        Returns:
            The updated BackupSession row or None if not found.

        Raises:
            ValueError: If the status value is not allowed.
        """
        if status not in ("ok", "partial", "failed", "erased"):
            raise ValueError(f"Invalid session status: {status!r}")
        with self._session_factory() as session:
            record = session.get(BackupSession, session_id)
            if record is None:
                return None
            record.status = status
            record.finished_at = finished_at or _now()
            record.files_count = _session_files_count(session, session_id)
            record.bytes_total = _session_bytes_total(session, session_id)
            session.commit()
            return record

    def get_session(self, session_id: int) -> BackupSession | None:
        """Find a session by id.

        Args:
            session_id: BackupSession primary key.

        Returns:
            The BackupSession row or None.
        """
        with self._session_factory() as session:
            return session.get(BackupSession, session_id)

    def list_sessions(self, tape_id: int) -> list[BackupSession]:
        """List sessions of a tape in tape order.

        Args:
            tape_id: Tape primary key.

        Returns:
            List of BackupSession rows.
        """
        with self._session_factory() as session:
            return list(
                session.scalars(
                    select(BackupSession)
                    .where(BackupSession.tape_id == tape_id)
                    .order_by(BackupSession.tape_seq)
                )
            )

    def add_file(
        self,
        session_id: int,
        *,
        tape_file_index: int,
        path: str,
        size: int,
        mtime: datetime,
        sha256: str,
        status: str = "ok",
    ) -> FileEntry:
        """Register a file within a session.

        Args:
            session_id: BackupSession primary key.
            tape_file_index: Tape file index (0 = header).
            path: Source file path.
            size: File size in bytes.
            mtime: Source modification time.
            sha256: Hex digest of the file content.
            status: ok | damaged | skipped.

        Returns:
            The created FileEntry row.

        Raises:
            ValueError: If the status value is not allowed.
        """
        if status not in ("ok", "damaged", "skipped"):
            raise ValueError(f"Invalid file status: {status!r}")
        with self._session_factory() as session:
            entry = FileEntry(
                session_id=session_id,
                tape_file_index=tape_file_index,
                path=path,
                size=size,
                mtime=mtime,
                sha256=sha256,
                status=status,
            )
            session.add(entry)
            session.commit()
            return entry

    def mark_file_status(self, file_id: int, status: str) -> FileEntry | None:
        """Update a file entry status (e.g. damaged after verify).

        Args:
            file_id: FileEntry primary key.
            status: ok | damaged | skipped.

        Returns:
            The updated FileEntry row or None if not found.

        Raises:
            ValueError: If the status value is not allowed.
        """
        if status not in ("ok", "damaged", "skipped"):
            raise ValueError(f"Invalid file status: {status!r}")
        with self._session_factory() as session:
            entry = session.get(FileEntry, file_id)
            if entry is None:
                return None
            entry.status = status
            session.commit()
            return entry

    def list_files(self, session_id: int) -> list[FileEntry]:
        """List files of a session in tape order.

        Args:
            session_id: BackupSession primary key.

        Returns:
            List of FileEntry rows.
        """
        with self._session_factory() as session:
            return list(
                session.scalars(
                    select(FileEntry)
                    .where(FileEntry.session_id == session_id)
                    .order_by(FileEntry.tape_file_index)
                )
            )

    def get_file(self, file_id: int) -> FileEntry | None:
        """Find a file entry by id.

        Args:
            file_id: FileEntry primary key.

        Returns:
            The FileEntry row or None.
        """
        with self._session_factory() as session:
            return session.get(FileEntry, file_id)

    # -- jobs and runs -----------------------------------------------------

    def upsert_job(
        self,
        name: str,
        *,
        enabled: bool = True,
        sources_json: dict[str, object] | None = None,
        options_json: dict[str, object] | None = None,
        schedule_json: dict[str, object] | None = None,
    ) -> Job:
        """Insert or update a job definition row.

        Args:
            name: Unique job name.
            enabled: Whether the job is schedulable.
            sources_json: Sources payload.
            options_json: Options payload.
            schedule_json: Schedule payload.

        Returns:
            The Job row.
        """
        with self._session_factory() as session:
            job = session.scalar(select(Job).where(Job.name == name))
            if job is None:
                job = Job(
                    name=name,
                    enabled=enabled,
                    sources_json=sources_json or {},
                    options_json=options_json or {},
                    schedule_json=schedule_json or {},
                    created_at=_now(),
                )
                session.add(job)
            else:
                job.enabled = enabled
                if sources_json is not None:
                    job.sources_json = sources_json
                if options_json is not None:
                    job.options_json = options_json
                if schedule_json is not None:
                    job.schedule_json = schedule_json
            session.commit()
            return job

    def mark_job_ran(
        self,
        name: str,
        *,
        at: datetime | None = None,
        status: str | None = None,
    ) -> Job:
        """Record that a job ran (or its run was decided).

        Creates the Job row if missing; updates last_run_at and last_status.

        Args:
            name: Unique job name.
            at: Run time (defaults to now UTC).
            status: Terminal status to record as last_status.

        Returns:
            The Job row.
        """
        with self._session_factory() as session:
            job = session.scalar(select(Job).where(Job.name == name))
            if job is None:
                job = Job(
                    name=name,
                    enabled=True,
                    sources_json={},
                    options_json={},
                    schedule_json={},
                    created_at=_now(),
                )
                session.add(job)
            job.last_run_at = at or _now()
            if status is not None:
                job.last_status = status
            session.commit()
            return job

    def get_job(self, job_id: int) -> Job | None:
        """Find a job by id.

        Args:
            job_id: Job primary key.

        Returns:
            The Job row or None.
        """
        with self._session_factory() as session:
            return session.get(Job, job_id)

    def list_jobs(self) -> list[Job]:
        """List all jobs ordered by name.

        Returns:
            List of Job rows.
        """
        with self._session_factory() as session:
            return list(session.scalars(select(Job).order_by(Job.name)))

    def create_job_run(self, job_id: int | None = None) -> JobRun:
        """Start a job run record.

        Args:
            job_id: Job primary key (None for restores without a job).

        Returns:
            The created JobRun row.
        """
        with self._session_factory() as session:
            record = JobRun(job_id=job_id, started_at=_now(), status="queued")
            session.add(record)
            session.commit()
            return record

    def get_job_run(self, run_id: int) -> JobRun | None:
        """Find a job run by id.

        Args:
            run_id: JobRun primary key.

        Returns:
            The JobRun row or None.
        """
        with self._session_factory() as session:
            return session.get(JobRun, run_id)

    def list_job_runs(self, job_id: int | None = None) -> list[JobRun]:
        """List job runs, newest first (optionally filtered by job).

        Args:
            job_id: Optional job filter.

        Returns:
            List of JobRun rows.
        """
        with self._session_factory() as session:
            statement = select(JobRun).order_by(JobRun.id.desc())
            if job_id is not None:
                statement = statement.where(JobRun.job_id == job_id)
            return list(session.scalars(statement))

    def update_job_run(
        self,
        run_id: int,
        *,
        status: str | None = None,
        error: str | None = None,
        stats_json: dict[str, object] | None = None,
        plan_json: dict[str, object] | None = None,
        finished: bool = False,
    ) -> JobRun | None:
        """Update a job run record.

        Args:
            run_id: JobRun primary key.
            status: New state-machine status.
            error: Error message for failed runs.
            stats_json: Stats payload.
            plan_json: Plan payload.
            finished: Set finished_at to now.

        Returns:
            The updated JobRun row or None if not found.
        """
        with self._session_factory() as session:
            record = session.get(JobRun, run_id)
            if record is None:
                return None
            if status is not None:
                record.status = status
            if error is not None:
                record.error = error
            if stats_json is not None:
                record.stats_json = stats_json
            if plan_json is not None:
                record.plan_json = plan_json
            if finished:
                record.finished_at = _now()
            session.commit()
            return record

    # -- settings -----------------------------------------------------------

    def get_setting(self, key: str) -> dict[str, object] | None:
        """Read a setting value.

        Args:
            key: Setting key.

        Returns:
            The stored JSON payload or None.
        """
        with self._session_factory() as session:
            record = session.get(Setting, key)
            return record.value_json if record else None

    def set_setting(self, key: str, value_json: dict[str, object]) -> None:
        """Write a setting value (insert or update).

        Args:
            key: Setting key.
            value_json: JSON payload to store.
        """
        with self._session_factory() as session:
            record = session.get(Setting, key)
            if record is None:
                record = Setting(key=key, value_json=value_json)
                session.add(record)
            else:
                record.value_json = value_json
            session.commit()


def _now() -> datetime:
    """Return the current UTC time.

    Returns:
        Timezone-aware UTC datetime.
    """
    return datetime.now(UTC)


def _next_tape_seq(session: Session, tape_id: int) -> int:
    """Compute the next session sequence number for a tape.

    Args:
        session: Active SQLAlchemy session.
        tape_id: Tape primary key.

    Returns:
        Zero-based sequence number.
    """
    rows = session.scalars(
        select(BackupSession.tape_seq).where(BackupSession.tape_id == tape_id)
    )
    return max(rows, default=-1) + 1


def _session_files_count(session: Session, session_id: int) -> int:
    """Count source files (index > 0) of a session.

    Args:
        session: Active SQLAlchemy session.
        session_id: BackupSession primary key.

    Returns:
        Number of file entries excluding the header.
    """
    rows = session.scalars(
        select(FileEntry.tape_file_index).where(FileEntry.session_id == session_id)
    )
    return sum(1 for index in rows if index > 0)


def _session_bytes_total(session: Session, session_id: int) -> int:
    """Sum sizes of source files of a session.

    Args:
        session: Active SQLAlchemy session.
        session_id: BackupSession primary key.

    Returns:
        Total bytes of source files (header excluded).
    """
    rows = session.execute(
        select(FileEntry.tape_file_index, FileEntry.size).where(
            FileEntry.session_id == session_id
        )
    )
    return sum(size for index, size in rows if index > 0)
