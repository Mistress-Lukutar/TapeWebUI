"""Initial catalog schema: tapes, sessions, files, jobs, job_runs, settings.

Revision ID: 0001
Revises:
Create Date: 2026-11-06
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "tapes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("uuid", sa.String(length=36), nullable=False),
        sa.Column("label", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("bytes_used", sa.Integer(), nullable=False),
        sa.Column("notes", sa.String(length=1024), nullable=False),
    )
    op.create_index("ix_tapes_uuid", "tapes", ["uuid"], unique=True)

    op.create_table(
        "jobs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("sources_json", sa.JSON(), nullable=False),
        sa.Column("options_json", sa.JSON(), nullable=False),
        sa.Column("schedule_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_status", sa.String(length=32), nullable=True),
    )
    op.create_index("ix_jobs_name", "jobs", ["name"], unique=True)

    op.create_table(
        "sessions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tape_id", sa.Integer(), sa.ForeignKey("tapes.id"), nullable=False),
        sa.Column("tape_seq", sa.Integer(), nullable=False),
        sa.Column("job_id", sa.Integer(), sa.ForeignKey("jobs.id"), nullable=True),
        sa.Column("job_name", sa.String(length=255), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("files_count", sa.Integer(), nullable=False),
        sa.Column("bytes_total", sa.Integer(), nullable=False),
        sa.Column("format_version", sa.Integer(), nullable=False),
    )
    op.create_index("ix_sessions_tape_id", "sessions", ["tape_id"])

    op.create_table(
        "files",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "session_id",
            sa.Integer(),
            sa.ForeignKey("sessions.id"),
            nullable=False,
        ),
        sa.Column("tape_file_index", sa.Integer(), nullable=False),
        sa.Column("path", sa.String(length=1024), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("mtime", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
    )
    op.create_index("ix_files_session_id", "files", ["session_id"])
    op.create_index(
        "ix_files_session_index", "files", ["session_id", "tape_file_index"]
    )

    op.create_table(
        "job_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("job_id", sa.Integer(), sa.ForeignKey("jobs.id"), nullable=True),
        sa.Column(
            "session_id", sa.Integer(), sa.ForeignKey("sessions.id"), nullable=True
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("error", sa.String(length=2048), nullable=True),
        sa.Column("stats_json", sa.JSON(), nullable=True),
        sa.Column("plan_json", sa.JSON(), nullable=True),
    )
    op.create_index("ix_job_runs_job_id", "job_runs", ["job_id"])

    op.create_table(
        "settings",
        sa.Column("key", sa.String(length=255), primary_key=True),
        sa.Column("value_json", sa.JSON(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("settings")
    op.drop_table("job_runs")
    op.drop_table("files")
    op.drop_table("sessions")
    op.drop_table("jobs")
    op.drop_table("tapes")
