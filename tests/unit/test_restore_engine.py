"""
File:   test_restore_engine.py
Brief:  Restore engine tests on the fake transport and in-memory catalog.
Author: Mistress-Lukutar
Date:   2026-11-06
Version: v0.4.0
"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tapevaultui.infrastructure.itdt_transport import FakeItDtTransport
from tapevaultui.services.catalog import CatalogRepository
from tapevaultui.services.event_bus import EventBus
from tapevaultui.services.restore_engine import (
    RestoreEngine,
    RestoreEngineError,
    RestoreRequest,
    RestoreStats,
)
from tapevaultui.services.tape_format import build_session_header


def _sha(text: str) -> str:
    """Hash text with sha256 (test helper).

    Args:
        text: Input text.

    Returns:
        Hex digest.
    """
    return hashlib.sha256(text.encode()).hexdigest()


def _seed_tape_with_files(
    repo: CatalogRepository, *, count: int = 2
) -> tuple[int, list[int]]:
    """Create a tape/session/file fixture in the catalog.

    Args:
        repo: Catalog repository.
        count: Number of source files.

    Returns:
        (tape_row_id, file_ids) tuple.
    """
    tape = repo.register_tape("tape-uuid-restore", label="Vault R")
    session = repo.create_session(tape.id, job_name="restorejob")
    mtime = datetime.now(UTC)
    repo.add_file(
        session.id,
        tape_file_index=0,
        path="<session-header>",
        size=10,
        mtime=mtime,
        sha256="0" * 64,
    )
    file_ids: list[int] = []
    for index in range(1, count + 1):
        entry = repo.add_file(
            session.id,
            tape_file_index=index,
            path=f"D:/Photos/file{index}.jpg",
            size=100 * index,
            mtime=mtime,
            sha256=_sha(f"content-{index}"),
        )
        file_ids.append(entry.id)
    return tape.id, file_ids


def _make_engine(
    tmp_path: Path, fake: FakeItDtTransport
) -> tuple[RestoreEngine, CatalogRepository]:
    """Build a restore engine over an in-memory catalog.

    Args:
        tmp_path: Pytest temp dir.
        fake: Fake transport.

    Returns:
        (engine, repository) tuple.
    """
    repo = CatalogRepository("sqlite://")
    repo.create_all()
    engine = RestoreEngine(fake, repo, tmp_path / "work", EventBus(), asyncio.Lock())
    return engine, repo


def test_build_plan_orders_by_tape_index(tmp_path: Path) -> None:
    """Plan items are sorted ascending by tape_file_index."""
    engine, repo = _make_engine(tmp_path, FakeItDtTransport())
    _, file_ids = _seed_tape_with_files(repo, count=3)
    plan = engine.build_plan([file_ids[2], file_ids[0], file_ids[1]])
    assert [item.tape_file_index for item in plan.items] == [1, 2, 3]
    assert plan.tape_id == "tape-uuid-restore"
    assert plan.bytes_total == 100 + 200 + 300


def test_build_plan_rejects_header(tmp_path: Path) -> None:
    """The session header entry cannot be restored."""
    engine, repo = _make_engine(tmp_path, FakeItDtTransport())
    _seed_tape_with_files(repo)
    header_id = repo.list_files(repo.list_sessions(repo.list_tapes()[0].id)[0].id)[0].id
    with pytest.raises(RestoreEngineError, match="header"):
        engine.build_plan([header_id])


def test_build_plan_rejects_multiple_tapes(tmp_path: Path) -> None:
    """Files spanning two tapes cannot be planned together."""
    engine, repo = _make_engine(tmp_path, FakeItDtTransport())
    tape_a_id, files_a = _seed_tape_with_files(repo, count=1)
    tape_b = repo.register_tape("tape-uuid-2")
    session_b = repo.create_session(tape_b.id, job_name="other")
    entry_b = repo.add_file(
        session_b.id,
        tape_file_index=1,
        path="C:/x.txt",
        size=1,
        mtime=datetime.now(UTC),
        sha256="0" * 64,
    )
    assert tape_a_id != tape_b.id
    with pytest.raises(RestoreEngineError, match="multiple tapes"):
        engine.build_plan([files_a[0], entry_b.id])


async def test_restore_rejects_wrong_tape(tmp_path: Path) -> None:
    """Restore refuses to run when the inserted tape does not match."""
    engine, repo = _make_engine(tmp_path, FakeItDtTransport())
    _, file_ids = _seed_tape_with_files(repo)
    plan = engine.build_plan(file_ids)
    request = RestoreRequest(file_ids=file_ids)
    stats = RestoreStats(1, plan.tape_id)
    with pytest.raises(RestoreEngineError, match="Wrong tape"):
        await engine.execute(request, stats, plan, asyncio.Event())


async def test_restore_end_to_end_on_fake(tmp_path: Path) -> None:
    """Matching tape: files are read, hash-checked, and written out."""
    engine, repo = _make_engine(tmp_path, FakeItDtTransport())
    tape_row_id, file_ids = _seed_tape_with_files(repo, count=2)
    tape_row = repo.get_tape(tape_row_id)
    assert tape_row is not None

    # The fake's read -d writes one fixed text into every destination,
    # including the t0 download — so the tape matches the header below
    # and file 1 (recorded with the same hash) verifies, while file 2
    # (old hash) counts as a mismatch.
    header = build_session_header(
        tape_id=tape_row.uuid,
        session_id="s-1",
        job_name="restorejob",
        started_at=datetime.now(UTC),
    )
    from tapevaultui.services.tape_format import serialize_header

    tape_content = serialize_header(header).decode()
    fake = FakeItDtTransport(scenarios={"read": (tape_content, 0)})
    engine = RestoreEngine(fake, repo, tmp_path / "work", EventBus(), asyncio.Lock())

    first = repo.get_file(file_ids[0])
    assert first is not None
    with repo._session_factory() as db:
        first.sha256 = _sha(tape_content)
        db.add(first)
        db.commit()

    request = RestoreRequest(
        file_ids=file_ids,
        destination_mode="alternate",
        destination_dir=str(tmp_path / "out"),
        collision="rename",
    )
    plan = engine.build_plan(file_ids)
    stats = RestoreStats(1, plan.tape_id)
    final = await engine.execute(request, stats, plan, asyncio.Event())

    assert final.files_done == 1  # second file hash-mismatches by design
    assert final.hash_mismatches == 1
    restored = tmp_path / "out" / "Photos" / "file1.jpg"
    assert restored.read_text() == tape_content
    # fsf issued for each file index, rewind before/after
    assert any(cmd[:2] == ("fsf", "1") for cmd in fake.commands)
    assert fake.commands[-1] == ("rewind",)


def test_target_path_collision_policies(tmp_path: Path) -> None:
    """skip/overwrite/rename collision policies behave as specified."""
    engine, _ = _make_engine(tmp_path, FakeItDtTransport())
    existing = tmp_path / "out" / "Photos" / "a.jpg"
    existing.parent.mkdir(parents=True)
    existing.write_text("old")

    request = RestoreRequest(
        file_ids=[1],
        destination_mode="alternate",
        destination_dir=str(tmp_path / "out"),
    )
    target = engine._target_path("D:/Photos/a.jpg", request)
    assert target is not None
    assert target.name == "a(1).jpg"

    request_skip = request.model_copy(update={"collision": "skip"})
    assert engine._target_path("D:/Photos/a.jpg", request_skip) is None

    request_overwrite = request.model_copy(update={"collision": "overwrite"})
    target = engine._target_path("D:/Photos/a.jpg", request_overwrite)
    assert target == existing

    request_original = RestoreRequest(file_ids=[1], destination_mode="original")
    original_target = str(tmp_path / "orig" / "a.jpg")
    resolved = engine._target_path(original_target, request_original)
    assert resolved is not None
    assert Path(resolved) == Path(original_target)
