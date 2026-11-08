"""
File:   test_migration_0005.py
Brief:  Migration 0005 test: files.kind/inner_count/manifest_sha columns.
Author: Mistress-Lukutar
Date:   2026-11-08
Version: v0.8.0
"""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from tests.unit.test_migration_0002 import _load_migration, _run_migration

m0005 = _load_migration("0005_file_kind")


def test_migration_0005_adds_kind_columns_with_defaults(tmp_path: Path) -> None:
    """Existing file rows read as plain files after the upgrade."""
    db_path = tmp_path / "catalog.db"
    _run_migration(db_path, _load_migration("0001_initial"))
    _run_migration(db_path, _load_migration("0002_tape_absolute_index"))
    _run_migration(db_path, _load_migration("0003_tape_last_verified"))
    _run_migration(db_path, _load_migration("0004_session_kind"))
    # Seed one file row before the archive columns exist.
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
        conn.execute(
            sa.text(
                "INSERT INTO files (session_id, tape_file_index, path, "
                "size, mtime, sha256, status) VALUES "
                "(1, 1, 'C:/a.txt', 4, CURRENT_TIMESTAMP, "
                "'" + "0" * 64 + "', 'ok')"
            )
        )
    _run_migration(db_path, m0005)

    with engine.begin() as conn:
        row = conn.execute(
            sa.text("SELECT kind, inner_count, manifest_sha FROM files")
        ).one()
        columns = [r[1] for r in conn.execute(sa.text("PRAGMA table_info(files)"))]
    assert row == ("file", 0, "")
    assert {"kind", "inner_count", "manifest_sha"} <= set(columns)


def test_migration_0005_downgrade_removes_columns(tmp_path: Path) -> None:
    """The downgrade drops the archive columns again."""
    db_path = tmp_path / "catalog.db"
    _run_migration(db_path, _load_migration("0001_initial"))
    _run_migration(db_path, _load_migration("0002_tape_absolute_index"))
    _run_migration(db_path, _load_migration("0003_tape_last_verified"))
    _run_migration(db_path, _load_migration("0004_session_kind"))
    _run_migration(db_path, m0005)

    engine = sa.create_engine(f"sqlite:///{db_path}")
    with engine.begin() as conn:
        context = MigrationContext.configure(conn)
        with Operations.context(context):
            m0005.downgrade()  # type: ignore[attr-defined]
    with engine.begin() as conn:
        columns = [r[1] for r in conn.execute(sa.text("PRAGMA table_info(files)"))]
    assert "kind" not in columns
    assert "inner_count" not in columns
    assert "manifest_sha" not in columns
