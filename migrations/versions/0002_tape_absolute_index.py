"""Absolute tape file indexes for the catalog explorer.

Backfills ``files.tape_file_index`` from session-local numbering (0 =
header, files restart at 1 in every session) to tape-absolute physical
positioning (what ``fsf`` uses from BOP). Previously a second session
appended to the same tape reused indexes 0..N, so restoring it read the
wrong physical files. Also adds ``sessions.file_base`` (the session
header's absolute position) and the indexes used by the catalog tree
browser.

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-06
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | None = None
depends_on: str | None = None

_HEADER_PATH = "<session-header>"


def upgrade() -> None:
    op.add_column(
        "sessions",
        sa.Column("file_base", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_index("ix_files_session_path", "files", ["session_id", "path"])
    op.create_index("ix_sessions_tape_started", "sessions", ["tape_id", "started_at"])

    _backfill_absolute_indexes()


def downgrade() -> None:
    # Revert absolute indexes to session-local numbering (best effort:
    # the header keeps index 0 and source files restart at 1).
    conn = op.get_bind()
    conn.execute(
        sa.text(
            "UPDATE files SET tape_file_index = tape_file_index -"
            " (SELECT file_base FROM sessions WHERE sessions.id = files.session_id)"
        )
    )
    op.drop_index("ix_sessions_tape_started", table_name="sessions")
    op.drop_index("ix_files_session_path", table_name="files")
    op.drop_column("sessions", "file_base")


def _backfill_absolute_indexes() -> None:
    """Rewrite tape_file_index to tape-absolute positions.

    For every tape, sessions are processed in chronological order and
    erased sessions are skipped (their data is physically gone after an
    erase, and ``mark_tape_erased`` always erases whole tapes, so
    non-erased sessions never precede erased ones on the same media).
    Each surviving session consumes one position for its t0 header plus
    one per recorded source file (including skipped/damaged entries, so
    the numbering matches what was written before this migration).
    """
    conn = op.get_bind()
    tape_ids = [row[0] for row in conn.execute(sa.text("SELECT id FROM tapes"))]
    for tape_id in tape_ids:
        sessions = conn.execute(
            sa.text(
                "SELECT id, status FROM sessions WHERE tape_id = :tape_id"
                " ORDER BY started_at, id"
            ),
            {"tape_id": tape_id},
        ).fetchall()
        base = 0
        for session_id, status in sessions:
            if status == "erased":
                continue
            files_count = conn.execute(
                sa.text(
                    "SELECT COUNT(*) FROM files WHERE session_id = :session_id"
                    " AND path <> :header_path"
                ),
                {"session_id": session_id, "header_path": _HEADER_PATH},
            ).scalar()
            conn.execute(
                sa.text("UPDATE sessions SET file_base = :base WHERE id = :session_id"),
                {"base": base, "session_id": session_id},
            )
            conn.execute(
                sa.text(
                    "UPDATE files SET tape_file_index = tape_file_index + :base"
                    " WHERE session_id = :session_id"
                ),
                {"base": base, "session_id": session_id},
            )
            base += 1 + int(files_count or 0)
