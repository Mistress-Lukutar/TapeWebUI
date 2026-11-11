"""
File:   test_migration_0006.py
Brief:  Migration 0006 test: sessions.end_position column.
Author: Mistress-Lukutar
Date:   2026-11-12
Version: v0.11.1
"""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa

from tests.unit.test_migration_0002 import _load_migration, _run_migration

m0006 = _load_migration("0006_session_end_position")


def test_migration_0006_adds_nullable_end_position(tmp_path: Path) -> None:
    """Existing session rows read as unrecorded (NULL end_position)."""
    db_path = tmp_path / "catalog.db"
    _run_migration(db_path, _load_migration("0001_initial"))
    _run_migration(db_path, _load_migration("0002_tape_absolute_index"))
    _run_migration(db_path, _load_migration("0003_tape_last_verified"))
    _run_migration(db_path, _load_migration("0004_session_kind"))
    _run_migration(db_path, _load_migration("0005_file_kind"))
    engine = sa.create_engine(f"sqlite:///{db_path}")
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO tapes (uuid, label, created_at, status, "
                "bytes_used, notes) VALUES ('u1', '', CURRENT_TIMESTAMP, "
                "'active', 0, '')"
            )
        )
        conn.execute(
            sa.text(
                "INSERT INTO sessions (tape_id, tape_seq, job_name, "
                "started_at, status, files_count, bytes_total, "
                "format_version, file_base, kind) VALUES "
                "(1, 0, 'j', CURRENT_TIMESTAMP, 'ok', 0, 0, 1, 0, 'full')"
            )
        )
    _run_migration(db_path, m0006)

    with engine.begin() as conn:
        row = conn.execute(sa.text("SELECT end_position FROM sessions")).one()
        assert row.end_position is None
        # The column accepts the recorded position.
        conn.execute(sa.text("UPDATE sessions SET end_position = 7"))
        row = conn.execute(sa.text("SELECT end_position FROM sessions")).one()
        assert row.end_position == 7
