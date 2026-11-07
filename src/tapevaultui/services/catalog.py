"""
File:   catalog.py
Brief:  SQLite catalog repository on SQLAlchemy 2.0 (sync, thread-offloaded).
Author: Mistress-Lukutar
Date:   2026-11-07
Version: v0.7.0
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import structlog
from sqlalchemy import create_engine, func, select
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

# Path of the t0 session header pseudo-entry in the files table.
SESSION_HEADER_PATH = "<session-header>"
# Upper bound for path prefix range scans: every valid UTF-8 string that
# extends ``prefix`` sorts strictly below ``prefix + _PREFIX_SENTINEL``
# and at or above ``prefix``, so the (session_id, path) index can be
# used instead of a case-insensitive LIKE.
_PREFIX_SENTINEL = "\U0010ffff"

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

    def mark_tape_erased(
        self, tape_id: int, *, exclude_session_id: int | None = None
    ) -> int:
        """Mark all sessions of a tape erased (history is kept, not deleted).

        Args:
            tape_id: Tape primary key.
            exclude_session_id: Session to leave untouched (the freshly
                created session of an overwrite-mode backup that just
                erased the tape itself).

        Returns:
            Number of sessions marked erased.
        """
        with self._session_factory() as session:
            sessions = list(
                session.scalars(
                    select(BackupSession).where(BackupSession.tape_id == tape_id)
                )
            )
            marked = 0
            for backup_session in sessions:
                if backup_session.id == exclude_session_id:
                    continue
                backup_session.status = "erased"
                marked += 1
            tape = session.get(Tape, tape_id)
            if tape is not None:
                tape.bytes_used = 0
                tape.status = "active"
                tape.last_verified_at = None
            session.commit()
            logger.info("catalog_tape_marked_erased", tape_id=tape_id, sessions=marked)
            return marked

    def set_tape_verified(self, tape_id: int) -> bool:
        """Stamp the tape as verified against the physical cartridge.

        Args:
            tape_id: Tape primary key.

        Returns:
            True if the tape exists and was stamped.
        """
        with self._session_factory() as session:
            tape = session.get(Tape, tape_id)
            if tape is None:
                return False
            tape.last_verified_at = _now()
            session.commit()
            return True

    def mark_sessions_erased(self, session_ids: list[int]) -> int:
        """Mark specific sessions erased (deep-scan apply).

        Args:
            session_ids: Session primary keys to mark erased.

        Returns:
            Number of sessions actually marked (unknown ids are skipped).
        """
        if not session_ids:
            return 0
        marked = 0
        with self._session_factory() as session:
            for session_id in session_ids:
                backup_session = session.get(BackupSession, session_id)
                if backup_session is None or backup_session.status == "erased":
                    continue
                backup_session.status = "erased"
                marked += 1
                tape = session.get(Tape, backup_session.tape_id)
                if tape is not None:
                    tape.last_verified_at = None
            session.commit()
        logger.info("catalog_sessions_marked_erased", count=marked)
        return marked

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
            file_base = _next_tape_file_base(session, tape_id)
            record = BackupSession(
                tape_id=tape_id,
                tape_seq=tape_seq,
                job_id=job_id,
                job_name=job_name,
                started_at=started_at or _now(),
                format_version=format_version,
                file_base=file_base,
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
            tape_file_index: Tape-absolute file position (fsf steps from
                BOP; the session header sits at ``sessions.file_base``).
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

    # -- catalog tree browsing --------------------------------------------

    def files_under_prefix(self, session_id: int, prefix: str) -> list[FileEntry]:
        """Source files of a session whose path starts with ``prefix``.

        The t0 header pseudo-entry is always excluded.

        Args:
            session_id: BackupSession primary key.
            prefix: Directory prefix ("" for the whole session).

        Returns:
            FileEntry rows ordered by path.
        """
        with self._session_factory() as session:
            return list(
                session.scalars(
                    select(FileEntry)
                    .where(
                        FileEntry.session_id == session_id,
                        FileEntry.path != SESSION_HEADER_PATH,
                        FileEntry.path >= prefix,
                        FileEntry.path < prefix + _PREFIX_SENTINEL,
                    )
                    .order_by(FileEntry.path)
                )
            )

    def tape_files_under_prefix(
        self, tape_id: int, prefix: str
    ) -> list[tuple[FileEntry, BackupSession]]:
        """Files of all live sessions of a tape under a path prefix.

        Args:
            tape_id: Tape primary key.
            prefix: Directory prefix ("" for the whole tape).

        Returns:
            (FileEntry, BackupSession) pairs ordered by path; erased
            sessions and header pseudo-entries are excluded.
        """
        with self._session_factory() as session:
            rows = session.execute(
                select(FileEntry, BackupSession)
                .join(BackupSession, FileEntry.session_id == BackupSession.id)
                .where(
                    BackupSession.tape_id == tape_id,
                    BackupSession.status != "erased",
                    FileEntry.path != SESSION_HEADER_PATH,
                    FileEntry.path >= prefix,
                    FileEntry.path < prefix + _PREFIX_SENTINEL,
                )
                .order_by(FileEntry.path)
            )
            return [(entry, record) for entry, record in rows.all()]

    def latest_file_ids_under_prefix(self, tape_id: int, prefix: str) -> list[int]:
        """Catalog ids of the newest version of each path under a prefix.

        Versions are ranked by session start time (then session id, then
        file id) across all non-erased sessions of the tape.

        Args:
            tape_id: Tape primary key.
            prefix: Directory prefix ("" for the whole tape).

        Returns:
            FileEntry ids, one per distinct path.
        """
        pairs = self.tape_files_under_prefix(tape_id, prefix)
        latest: dict[str, tuple[FileEntry, BackupSession]] = {}
        for entry, record in pairs:
            best = latest.get(entry.path)
            if best is None or (record.started_at, record.id, entry.id) > (
                best[1].started_at,
                best[1].id,
                best[0].id,
            ):
                latest[entry.path] = (entry, record)
        return [entry.id for entry, _ in latest.values()]

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
        session_id: int | None = None,
        stats_json: dict[str, object] | None = None,
        plan_json: dict[str, object] | None = None,
        finished: bool = False,
    ) -> JobRun | None:
        """Update a job run record.

        Args:
            run_id: JobRun primary key.
            status: New state-machine status.
            error: Error message for failed runs.
            session_id: Related session id (first session of a restore).
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
            if session_id is not None:
                record.session_id = session_id
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


def _next_tape_file_base(session: Session, tape_id: int) -> int:
    """Compute the absolute tape position for a new session's header.

    The position is the sum of the physical footprints of the tape's
    live (non-erased) sessions: one filemark-delimited position for each
    t0 header plus one per recorded source file. Erased sessions are
    skipped because an erase wipes the whole tape, so the next session
    starts over at position 0.

    Args:
        session: Active SQLAlchemy session.
        tape_id: Tape primary key.

    Returns:
        Zero-based absolute tape file index for the new t0 header.
    """
    live_sessions = session.scalars(
        select(BackupSession.id).where(
            BackupSession.tape_id == tape_id,
            BackupSession.status != "erased",
        )
    ).all()
    base = 0
    for live_id in live_sessions:
        files_count = session.scalar(
            select(func.count(FileEntry.id)).where(
                FileEntry.session_id == live_id,
                FileEntry.path != SESSION_HEADER_PATH,
            )
        )
        base += 1 + int(files_count or 0)
    return base


def _session_files_count(session: Session, session_id: int) -> int:
    """Count source files of a session (header pseudo-entry excluded).

    Args:
        session: Active SQLAlchemy session.
        session_id: BackupSession primary key.

    Returns:
        Number of file entries excluding the header.
    """
    return int(
        session.scalar(
            select(func.count(FileEntry.id)).where(
                FileEntry.session_id == session_id,
                FileEntry.path != SESSION_HEADER_PATH,
            )
        )
        or 0
    )


def _session_bytes_total(session: Session, session_id: int) -> int:
    """Sum sizes of source files of a session (header excluded).

    Args:
        session: Active SQLAlchemy session.
        session_id: BackupSession primary key.

    Returns:
        Total bytes of source files (header excluded).
    """
    return int(
        session.scalar(
            select(func.coalesce(func.sum(FileEntry.size), 0)).where(
                FileEntry.session_id == session_id,
                FileEntry.path != SESSION_HEADER_PATH,
            )
        )
        or 0
    )
