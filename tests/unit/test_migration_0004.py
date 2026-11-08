"""
File:   test_migration_0004.py
Brief:  Migration 0004 test: sessions.kind column with 'full' default.
Author: Mistress-Lukutar
Date:   2026-11-07
Version: v0.7.0
"""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from tests.unit.test_migration_0002 import (
    _load_migration,
    _run_migration,
)

m0004 = _load_migration("0004_session_kind")


def test_migration_0004_adds_kind_with_full_default(tmp_path: Path) -> None:
    """Existing sessions read as kind='full' after the upgrade."""
    db_path = tmp_path / "catalog.db"
    _run_migration(db_path, _load_migration("0001_initial"))
    _run_migration(db_path, _load_migration("0002_tape_absolute_index"))
    _run_migration(db_path, _load_migration("0003_tape_last_verified"))
    # Seed one session row before the kind column exists.
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
                "format_version, file_base) VALUES (1, 0, 'j', "
                "CURRENT_TIMESTAMP, 'ok', 0, 0, 1, 0)"
            )
        )
    _run_migration(db_path, m0004)

    with engine.begin() as conn:
        kind = conn.execute(sa.text("SELECT kind FROM sessions")).scalar_one()
        columns = [
            row[1]
            for row in conn.execute(sa.text("PRAGMA table_info(sessions)"))
        ]
    assert kind == "full"
    assert "kind" in columns
    indexes = [
        row[1]
        for row in engine.connect().execute(  # type: ignore[attr-defined]
            sa.text("PRAGMA index_list(sessions)")
        )
    ]
    assert "ix_sessions_job_started" in indexes


def test_migration_0004_downgrade_removes_kind(tmp_path: Path) -> None:
    """The downgrade drops the column and the index again."""
    db_path = tmp_path / "catalog.db"
    _run_migration(db_path, _load_migration("0001_initial"))
    _run_migration(db_path, m0004)

    engine = sa.create_engine(f"sqlite:///{db_path}")
    with engine.begin() as conn:
        context = MigrationContext.configure(conn)
        with Operations.context(context):
            m0004.downgrade()  # type: ignore[attr-defined]
    with engine.begin() as conn:
        columns = [
            row[1]
            for row in conn.execute(sa.text("PRAGMA table_info(sessions)"))
        ]
    assert "kind" not in columns
