"""
File:   test_catalog.py
Brief:  Catalog repository unit tests on in-memory SQLite.
Author: Mistress-Lukutar
Date:   2026-11-06
Version: v0.6.0
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from tapevaultui.services.catalog import SESSION_HEADER_PATH, CatalogRepository


@pytest.fixture
def repo() -> CatalogRepository:
    """Repository on a fresh in-memory database.

    Returns:
        CatalogRepository with all tables created.
    """
    repository = CatalogRepository("sqlite://")
    repository.create_all()
    return repository


def test_register_tape_is_idempotent(repo: CatalogRepository) -> None:
    """register_tape returns the same row for the same uuid."""
    first = repo.register_tape("uuid-1", label="Vault 1")
    second = repo.register_tape("uuid-1", label="Vault 1")
    assert first.id == second.id
    assert len(repo.list_tapes()) == 1


def test_update_tape_status_and_bytes(repo: CatalogRepository) -> None:
    """update_tape applies label/status/bytes deltas."""
    tape = repo.register_tape("uuid-1")
    updated = repo.update_tape(
        tape.id, label="Renamed", status="full", bytes_used_delta=1000
    )
    assert updated is not None
    assert updated.label == "Renamed"
    assert updated.status == "full"
    assert updated.bytes_used == 1000


def test_update_tape_rejects_bad_status(repo: CatalogRepository) -> None:
    """Unknown tape statuses raise ValueError."""
    tape = repo.register_tape("uuid-1")
    with pytest.raises(ValueError, match="Invalid tape status"):
        repo.update_tape(tape.id, status="broken")


def test_session_lifecycle_with_files(repo: CatalogRepository) -> None:
    """A session aggregates file stats on finish (header excluded)."""
    tape = repo.register_tape("uuid-1")
    session = repo.create_session(tape.id, job_name="photos")

    mtime = datetime(2026, 11, 6, tzinfo=UTC)
    repo.add_file(
        session.id,
        tape_file_index=0,
        path=SESSION_HEADER_PATH,
        size=100,
        mtime=mtime,
        sha256="0" * 64,
    )
    repo.add_file(
        session.id,
        tape_file_index=1,
        path="D:/a.jpg",
        size=500,
        mtime=mtime,
        sha256="1" * 64,
    )
    repo.add_file(
        session.id,
        tape_file_index=2,
        path="D:/b.jpg",
        size=700,
        mtime=mtime,
        sha256="2" * 64,
    )

    finished = repo.finish_session(session.id, status="ok")
    assert finished is not None
    assert finished.status == "ok"
    assert finished.files_count == 2
    assert finished.bytes_total == 1200
    assert finished.finished_at is not None

    files = repo.list_files(session.id)
    assert [f.tape_file_index for f in files] == [0, 1, 2]


def test_tape_seq_increments(repo: CatalogRepository) -> None:
    """Sessions on one tape get sequential tape_seq numbers."""
    tape = repo.register_tape("uuid-1")
    first = repo.create_session(tape.id)
    second = repo.create_session(tape.id)
    assert (first.tape_seq, second.tape_seq) == (0, 1)


def test_second_session_file_base_is_absolute(repo: CatalogRepository) -> None:
    """A session appended to a tape continues after the previous session.

    Regression guard: the restore engine positions with ``fsf`` from
    BOP, so the second session on one tape must not reuse indexes
    0..N — its header sits at the previous session's footprint.
    """
    tape = repo.register_tape("uuid-1")
    mtime = datetime.now(UTC)
    first = repo.create_session(tape.id, job_name="a")
    repo.add_file(
        first.id,
        tape_file_index=first.file_base,
        path=SESSION_HEADER_PATH,
        size=1,
        mtime=mtime,
        sha256="0" * 64,
    )
    for index in range(3):
        repo.add_file(
            first.id,
            tape_file_index=first.file_base + 1 + index,
            path=f"D:/a{index}.jpg",
            size=10,
            mtime=mtime,
            sha256="1" * 64,
        )
    repo.finish_session(first.id, status="ok")

    second = repo.create_session(tape.id, job_name="a")
    assert second.tape_seq == 1
    assert second.file_base == 4  # 1 header + 3 files of the first session

    # After a full erase the tape restarts at position 0.
    repo.mark_tape_erased(tape.id)
    third = repo.create_session(tape.id, job_name="a")
    assert third.file_base == 0


def test_mark_tape_erased(repo: CatalogRepository) -> None:
    """Erase marks sessions erased, resets bytes, keeps history rows."""
    tape = repo.register_tape("uuid-1")
    session = repo.create_session(tape.id, job_name="photos")
    repo.add_file(
        session.id,
        tape_file_index=1,
        path="D:/a.jpg",
        size=500,
        mtime=datetime.now(UTC),
        sha256="1" * 64,
    )
    repo.finish_session(session.id, status="ok")

    count = repo.mark_tape_erased(tape.id)
    assert count == 1

    sessions = repo.list_sessions(tape.id)
    assert sessions[0].status == "erased"  # history preserved
    updated_tape = repo.get_tape(tape.id)
    assert updated_tape is not None
    assert updated_tape.bytes_used == 0
    assert updated_tape.status == "active"


def test_jobs_and_runs(repo: CatalogRepository) -> None:
    """Job upsert and run records work end to end."""
    job = repo.upsert_job("photos", sources_json={"path": "D:/Photos"})
    again = repo.upsert_job("photos", enabled=False)
    assert job.id == again.id
    assert again.enabled is False

    run = repo.create_job_run(job.id)
    repo.update_job_run(run.id, status="done", stats_json={"bytes": 100}, finished=True)
    fetched = repo.get_job(job.id)
    assert fetched is not None
    assert fetched.name == "photos"


def test_settings_roundtrip(repo: CatalogRepository) -> None:
    """Settings are a simple JSON key-value store."""
    assert repo.get_setting("ui_state") is None
    repo.set_setting("ui_state", {"tab": "dashboard"})
    repo.set_setting("ui_state", {"tab": "jobs"})  # update path
    assert repo.get_setting("ui_state") == {"tab": "jobs"}
