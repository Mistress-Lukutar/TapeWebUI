"""
File:   test_migration_0002.py
Brief:  Backfill test for migration 0002 (tape-absolute file indexes).
Author: Mistress-Lukutar
Date:   2026-11-06
Version: v0.6.0
"""

from __future__ import annotations

import importlib.util
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

_MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations" / "versions"


def _load_migration(name: str) -> ModuleType:
    """Import a migration module by file name.

    Args:
        name: File stem, e.g. ``0001_initial``.

    Returns:
        The loaded migration module.
    """
    spec = importlib.util.spec_from_file_location(
        f"test_migration_{name}", _MIGRATIONS_DIR / f"{name}.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


m0001 = _load_migration("0001_initial")
m0002 = _load_migration("0002_tape_absolute_index")


def _run_migration(db_path: Path, module: object) -> None:
    """Run one migration module's upgrade() against a database file.

    Args:
        db_path: SQLite database file path.
        module: Imported alembic migration module.
    """
    engine = sa.create_engine(f"sqlite:///{db_path}")
    with engine.begin() as conn:
        context = MigrationContext.configure(conn)
        with Operations.context(context):
            module.upgrade()  # type: ignore[attr-defined]


def test_backfill_absolute_indexes(tmp_path: Path) -> None:
    """Old session-local indexes become tape-absolute positions."""
    db_path = tmp_path / "catalog.db"
    _run_migration(db_path, m0001)

    engine = sa.create_engine(f"sqlite:///{db_path}")
    start = datetime(2026, 11, 1, tzinfo=UTC)
    with engine.begin() as conn:
        tape = conn.execute(
            sa.text(
                "INSERT INTO tapes (uuid, label, created_at, status, bytes_used,"
                " notes) VALUES ('t1', '', :created, 'active', 0, '')"
            ),
            {"created": start},
        )
        tape_id = tape.lastrowid
        inserted: list[int] = []
        for seq, files in enumerate([(2, "ok"), (3, "ok"), (2, "erased")]):
            status = files[1]
            count = files[0]
            result = conn.execute(
                sa.text(
                    "INSERT INTO sessions (tape_id, tape_seq, job_name,"
                    " started_at, status, files_count, bytes_total,"
                    " format_version)"
                    " VALUES (:tape_id, :seq, 'j', :started, :status, :count,"
                    " 0, 1)"
                ),
                {
                    "tape_id": tape_id,
                    "seq": seq,
                    "started": start + timedelta(hours=seq),
                    "status": status,
                    "count": count,
                },
            )
            inserted.append(result.lastrowid)
            for index in range(count + 1):  # header 0 + files 1..count
                path = "<session-header>" if index == 0 else f"C:/f{index}.txt"
                conn.execute(
                    sa.text(
                        "INSERT INTO files (session_id, tape_file_index, path,"
                        " size, mtime, sha256, status)"
                        " VALUES (:sid, :index, :path, 1, :mtime, :sha, 'ok')"
                    ),
                    {
                        "sid": inserted[-1],
                        "index": index,
                        "path": path,
                        "mtime": start,
                        "sha": "0" * 64,
                    },
                )

    _run_migration(db_path, m0002)

    with engine.connect() as conn:
        bases = dict(
            conn.execute(
                sa.text("SELECT id, file_base FROM sessions ORDER BY id")
            ).fetchall()
        )
        assert bases[inserted[0]] == 0
        assert bases[inserted[1]] == 3  # 1 header + 2 files of session 1
        # Erased session keeps the default base (excluded from backfill).
        assert bases[inserted[2]] == 0

        indexes = dict(
            conn.execute(
                sa.text(
                    "SELECT tape_file_index, path FROM files"
                    " WHERE session_id = :sid ORDER BY tape_file_index"
                ),
                {"sid": inserted[1]},
            ).fetchall()
        )
        assert indexes == {
            3: "<session-header>",
            4: "C:/f1.txt",
            5: "C:/f2.txt",
            6: "C:/f3.txt",
        }

        erased_indexes = [
            row[0]
            for row in conn.execute(
                sa.text(
                    "SELECT tape_file_index FROM files"
                    " WHERE session_id = :sid ORDER BY tape_file_index"
                ),
                {"sid": inserted[2]},
            )
        ]
        assert erased_indexes == [0, 1, 2]
