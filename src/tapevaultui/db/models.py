"""
File:   models.py
Brief:  SQLAlchemy 2.0 catalog models (tapes, sessions, files, jobs, runs).
Author: Mistress-Lukutar
Date:   2026-11-06
Version: v0.4.0
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Index, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Declarative base for all catalog models."""


class Tape(Base):
    """A physical cartridge known to the catalog."""

    __tablename__ = "tapes"

    id: Mapped[int] = mapped_column(primary_key=True)
    uuid: Mapped[str] = mapped_column(String(36), unique=True, index=True)
    label: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # active | full | retired
    status: Mapped[str] = mapped_column(String(16), default="active")
    bytes_used: Mapped[int] = mapped_column(default=0)
    notes: Mapped[str] = mapped_column(String(1024), default="")


class BackupSession(Base):
    """One write session on a tape (t0 header + t1..tN files)."""

    __tablename__ = "sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    tape_id: Mapped[int] = mapped_column(ForeignKey("tapes.id"), index=True)
    tape_seq: Mapped[int] = mapped_column(default=0)  # session number on the tape
    job_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id"), nullable=True)
    job_name: Mapped[str] = mapped_column(String(255), default="")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # ok | partial | failed | erased
    status: Mapped[str] = mapped_column(String(16), default="ok")
    files_count: Mapped[int] = mapped_column(default=0)
    bytes_total: Mapped[int] = mapped_column(default=0)
    format_version: Mapped[int] = mapped_column(default=1)


class FileEntry(Base):
    """A source file stored in a session, addressable by tape file index."""

    __tablename__ = "files"
    __table_args__ = (Index("ix_files_session_index", "session_id", "tape_file_index"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("sessions.id"), index=True)
    tape_file_index: Mapped[int]  # 0 = session header, source files from 1
    path: Mapped[str] = mapped_column(String(1024))
    size: Mapped[int] = mapped_column(default=0)
    mtime: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    sha256: Mapped[str] = mapped_column(String(64))
    # ok | damaged | skipped
    status: Mapped[str] = mapped_column(String(16), default="ok")


class Job(Base):
    """A backup job definition (YAML file mirrored into the catalog)."""

    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), unique=True)
    enabled: Mapped[bool] = mapped_column(default=True)
    sources_json: Mapped[dict[str, object]] = mapped_column(JSON)
    options_json: Mapped[dict[str, object]] = mapped_column(JSON)
    schedule_json: Mapped[dict[str, object]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_run_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # last job run status for the UI list
    last_status: Mapped[str | None] = mapped_column(String(32), nullable=True)


class JobRun(Base):
    """One execution of a job (backup or restore)."""

    __tablename__ = "job_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int | None] = mapped_column(
        ForeignKey("jobs.id"), nullable=True, index=True
    )
    session_id: Mapped[int | None] = mapped_column(
        ForeignKey("sessions.id"), nullable=True
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # queued | preparing | writing | verifying | done | waiting_media
    # | failed | cancelled
    status: Mapped[str] = mapped_column(String(32), default="queued")
    error: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    stats_json: Mapped[dict[str, object] | None] = mapped_column(JSON, nullable=True)
    plan_json: Mapped[dict[str, object] | None] = mapped_column(JSON, nullable=True)


class Setting(Base):
    """A non-env setting (UI state, misc key-value data)."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(255), primary_key=True)
    value_json: Mapped[dict[str, object]] = mapped_column(JSON)
