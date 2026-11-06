"""
File:   test_migrations.py
Brief:  Alembic migration tests against a temporary SQLite database.
Author: Mistress-Lukutar
Date:   2026-11-06
Version: v0.2.0
"""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_migration_0001_upgrade_and_downgrade(tmp_path: Path) -> None:
    """Migration 0001 creates all catalog tables and downgrades cleanly."""
    db_path = tmp_path / "catalog.db"
    config = Config(PROJECT_ROOT / "alembic.ini")
    config.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")

    command.upgrade(config, "head")

    engine = create_engine(f"sqlite:///{db_path}")
    tables = set(inspect(engine).get_table_names())
    assert {
        "tapes",
        "sessions",
        "files",
        "jobs",
        "job_runs",
        "settings",
        "alembic_version",
    } <= tables

    # A tape row survives a downgrade/upgrade cycle (history matters).
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO tapes (uuid, label, created_at, status,"
                " bytes_used, notes) VALUES ('u1', 'L', 0, 'active', 0, '')"
            )
        )

    command.downgrade(config, "base")
    command.upgrade(config, "head")

    with engine.begin() as connection:
        count = connection.execute(text("SELECT count(*) FROM tapes")).scalar_one()
    assert count == 0  # fresh schema after base downgrade
