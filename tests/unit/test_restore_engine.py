"""
File:   test_restore_engine.py
Brief:  Restore engine tests on the fake transport and in-memory catalog.
Author: Mistress-Lukutar
Date:   2026-11-08
Version: v0.9.1
"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tapevaultui.db.models import FileEntry
from tapevaultui.infrastructure.itdt_transport import FakeItDtTransport
from tapevaultui.services.catalog import CatalogRepository
from tapevaultui.services.event_bus import EventBus
from tapevaultui.services.restore_engine import (
    PlanSelection,
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


def _header_text(tape_uuid: str) -> str:
    """Build the serialized t0 header a fake tape shows at BOP.

    Args:
        tape_uuid: Tape identity to embed in the header.

    Returns:
        Header JSON as text (what ``read -d`` writes to disk).
    """
    from tapevaultui.services.tape_format import serialize_header

    return serialize_header(
        build_session_header(
            tape_id=tape_uuid,
            session_id=f"s-{tape_uuid[:4]}",
            job_name="restorejob",
            started_at=datetime(2026, 11, 1, tzinfo=UTC),
        )
    ).decode()


_TAPE_A_HEADER = _header_text("tape-uuid-restore")
_TAPE_B_HEADER = _header_text("tape-uuid-2")


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
    assert len(plan.tapes) == 1
    assert [item.tape_file_index for item in plan.tapes[0].items] == [1, 2, 3]
    assert plan.tapes[0].tape_id == "tape-uuid-restore"
    assert plan.bytes_total == 100 + 200 + 300


def test_build_plan_rejects_header(tmp_path: Path) -> None:
    """The session header entry cannot be restored."""
    engine, repo = _make_engine(tmp_path, FakeItDtTransport())
    _seed_tape_with_files(repo)
    header_id = repo.list_files(repo.list_sessions(repo.list_tapes()[0].id)[0].id)[0].id
    with pytest.raises(RestoreEngineError, match="header"):
        engine.build_plan([header_id])


def test_build_plan_groups_multiple_tapes(tmp_path: Path) -> None:
    """Files spanning two tapes are planned as two tape groups."""
    engine, repo = _make_engine(tmp_path, FakeItDtTransport())
    tape_a_id, files_a = _seed_tape_with_files(repo, count=1)
    tape_b = repo.register_tape("tape-uuid-2", label="Vault B")
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
    plan = engine.build_plan([files_a[0], entry_b.id])
    assert len(plan.tapes) == 2
    assert {group.tape_id for group in plan.tapes} == {
        "tape-uuid-restore",
        "tape-uuid-2",
    }
    assert plan.files_count == 2


async def test_restore_waits_for_expected_tape(tmp_path: Path) -> None:
    """A wrong/blank tape enters waiting_media instead of failing."""
    from tapevaultui.services.restore_engine import RestoreCancelled
    from tapevaultui.services.tape_format import serialize_header

    engine, repo = _make_engine(tmp_path, FakeItDtTransport())
    _, file_ids = _seed_tape_with_files(repo)
    foreign = build_session_header(
        tape_id="foreign-uuid",
        session_id="s-1",
        job_name="other",
        started_at=datetime.now(UTC),
    )
    fake = FakeItDtTransport(
        scenarios={"read": (serialize_header(foreign).decode(), 0)}
    )
    engine = RestoreEngine(fake, repo, tmp_path / "work", EventBus(), asyncio.Lock())
    plan = engine.build_plan(file_ids)
    request = RestoreRequest(file_ids=file_ids)
    stats = RestoreStats(1, plan.tapes[0].tape_id)
    cancel = asyncio.Event()

    task = asyncio.create_task(engine.execute(request, stats, plan, cancel))
    for _ in range(100):
        if stats.state == "waiting_media":
            break
        await asyncio.sleep(0.01)
    assert stats.state == "waiting_media"
    cancel.set()
    with pytest.raises(RestoreCancelled):
        await task


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
    stats = RestoreStats(1, plan.tapes[0].tape_id)
    final = await engine.execute(request, stats, plan, asyncio.Event())

    assert final.files_done == 1  # second file hash-mismatches by design
    assert final.hash_mismatches == 1
    restored = tmp_path / "out" / "Photos" / "file1.jpg"
    assert restored.read_text() == tape_content
    # fsf issued for each file index, rewind before/after
    assert any(cmd[:2] == ("fsf", "1") for cmd in fake.commands)
    assert fake.commands[-1] == ("rewind",)


def test_build_plan_expands_prefix_selections(tmp_path: Path) -> None:
    """Session/tape prefix selections expand into plan items."""
    engine, repo = _make_engine(tmp_path, FakeItDtTransport())
    tape_row_id, file_ids = _seed_tape_with_files(repo, count=3)
    tape_row = repo.get_tape(tape_row_id)
    assert tape_row is not None

    session = repo.list_sessions(tape_row.id)[0]
    plan = engine.build_plan(
        [],
        [
            PlanSelection(
                session_id=session.id,
                prefix="D:/Photos/file2.jpg",
                latest_only=True,
            )
        ],
    )
    assert plan.files_count == 1
    assert plan.tapes[0].items[0].path == "D:/Photos/file2.jpg"

    subtree = engine.build_plan(
        [], [PlanSelection(session_id=session.id, prefix="D:/Photos/")]
    )
    assert subtree.files_count == 3

    merged = engine.build_plan(
        [],
        [
            PlanSelection(
                tape_id=tape_row.id,
                prefix="D:/Photos/",
                latest_only=True,
            )
        ],
    )
    assert {item.file_id for item in merged.tapes[0].items} == set(file_ids)


def test_build_plan_prefix_latest_picks_newest_version(tmp_path: Path) -> None:
    """A merged prefix selection takes the newest version per path."""
    from datetime import timedelta

    engine, repo = _make_engine(tmp_path, FakeItDtTransport())
    tape = repo.register_tape("tape-uuid-versions", label="Vault V")
    old_session = repo.create_session(
        tape.id,
        job_name="old",
        started_at=datetime.now(UTC) - timedelta(hours=2),
    )
    old_entry = repo.add_file(
        old_session.id,
        tape_file_index=1,
        path="D:/Photos/file.jpg",
        size=100,
        mtime=datetime.now(UTC),
        sha256=_sha("old"),
    )
    new_session = repo.create_session(
        tape.id,
        job_name="new",
        started_at=datetime.now(UTC),
    )
    new_entry = repo.add_file(
        new_session.id,
        tape_file_index=1,
        path="D:/Photos/file.jpg",
        size=200,
        mtime=datetime.now(UTC),
        sha256=_sha("new"),
    )

    plan = engine.build_plan(
        [],
        [PlanSelection(tape_id=tape.id, prefix="D:/Photos/", latest_only=True)],
    )
    items = [item for group in plan.tapes for item in group.items]
    assert [item.file_id for item in items] == [new_entry.id]
    assert old_entry.id not in {item.file_id for item in items}

    everything = engine.build_plan(
        [],
        [PlanSelection(tape_id=tape.id, prefix="D:/Photos/", latest_only=False)],
    )
    assert {item.file_id for group in everything.tapes for item in group.items} == {
        old_entry.id,
        new_entry.id,
    }


async def test_restore_swaps_tapes_between_groups(tmp_path: Path) -> None:
    """Multi-tape plans wait for each tape and process both groups."""
    engine, repo = _make_engine(tmp_path, FakeItDtTransport())
    _tape_a_id, files_a = _seed_tape_with_files(repo, count=2)
    tape_b = repo.register_tape("tape-uuid-2", label="Vault B")
    session_b = repo.create_session(tape_b.id, job_name="other")
    entry_b = repo.add_file(
        session_b.id,
        tape_file_index=1,
        path="C:/x.txt",
        size=1,
        mtime=datetime.now(UTC),
        sha256=_sha(_TAPE_B_HEADER),
    )
    # The fake writes the scenario text for every read, so catalog
    # hashes must match the header text for the files to verify.
    with repo._session_factory() as db:
        for file_id in files_a:
            entry = db.get(FileEntry, file_id)
            entry.sha256 = _sha(_TAPE_A_HEADER)
            db.commit()

    plan = engine.build_plan([*files_a, entry_b.id])
    assert len(plan.tapes) == 2

    # The fake starts with tape A's header at BOP; swapping the scenario
    # simulates the operator inserting the next cartridge.
    fake = FakeItDtTransport(scenarios={"read": (_TAPE_A_HEADER, 0)})
    engine = RestoreEngine(fake, repo, tmp_path / "work", EventBus(), asyncio.Lock())

    request = RestoreRequest(
        file_ids=[*files_a, entry_b.id],
        destination_mode="alternate",
        destination_dir=str(tmp_path / "out"),
    )
    stats = RestoreStats(1, plan.tapes[0].tape_id, tapes_total=len(plan.tapes))
    cancel = asyncio.Event()

    task = asyncio.create_task(engine.execute(request, stats, plan, cancel))

    async def _wait_for_tape(label: str) -> None:
        for _ in range(200):
            if stats.state == "running" and stats.tape_label == label:
                return
            await asyncio.sleep(0.01)
        raise AssertionError(f"never started running on tape {label}")

    await _wait_for_tape("Vault R")

    # waiting_media for tape B implies group A has fully processed.
    async def _wait_for_waiting_media() -> None:
        for _ in range(200):
            if stats.state == "waiting_media":
                return
            await asyncio.sleep(0.01)
        raise AssertionError("never entered waiting_media")

    await _wait_for_waiting_media()
    # The operator inserts tape B: the drive header changes.
    fake.scenarios["read"] = (_TAPE_B_HEADER, 0)
    await _wait_for_tape("Vault B")
    await task

    assert stats.state == "done"
    assert stats.files_done == 3
    fsf_targets = [cmd[1] for cmd in fake.commands if cmd[0] == "fsf"]
    assert fsf_targets == ["1", "2", "1"]


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


# -- archive entries -------------------------------------------------------


async def test_restore_archive_entry_extracts_tree(tmp_path: Path) -> None:
    """An archive entry is hash-checked, then extracted into the target."""
    from tapevaultui.services.archive_builder import plan_archive_source
    from tapevaultui.services.job_defs import SourceDef
    from tapevaultui.services.snapshot import sha256_of
    from tapevaultui.services.tape_format import serialize_header

    # Real source tree -> staged zip (as the backup engine would build).
    src = tmp_path / "src"
    (src / "nested").mkdir(parents=True)
    (src / "a.txt").write_bytes(b"AAAA")
    (src / "nested" / "b.txt").write_bytes(b"BB")
    staged = plan_archive_source(
        SourceDef.model_validate({"path": str(src), "archive": True}),
        staging_dir=tmp_path / "staging",
    )
    assert staged is not None
    zip_path = Path(staged.staging_path)
    assert zip_path is not None

    engine, repo = _make_engine(tmp_path, FakeItDtTransport())
    tape = repo.register_tape("tape-uuid-archive", label="Archive R")
    session = repo.create_session(tape.id, job_name="archivejob")
    mtime = datetime.now(UTC)
    header_entry = repo.add_file(
        session.id,
        tape_file_index=0,
        path="<session-header>",
        size=10,
        mtime=mtime,
        sha256="0" * 64,
    )
    assert header_entry is not None
    archive_entry = repo.add_file(
        session.id,
        tape_file_index=1,
        path=str(src),
        size=len(zip_path.read_bytes()),
        mtime=mtime,
        sha256=sha256_of(zip_path),
        kind="archive",
        inner_count=staged.inner_count,
        manifest_sha=staged.manifest_sha,
    )
    assert archive_entry is not None

    tape_header = serialize_header(
        build_session_header(
            tape_id="tape-uuid-archive",
            session_id="s-1",
            job_name="archivejob",
            started_at=datetime.now(UTC),
        )
    )
    # Position-aware fake: BOP read sees the header, index 1 the zip.
    fake = FakeItDtTransport(tape_files=[tape_header, zip_path.read_bytes()])
    engine = RestoreEngine(fake, repo, tmp_path / "work", EventBus(), asyncio.Lock())

    request = RestoreRequest(
        file_ids=[archive_entry.id],
        destination_mode="alternate",
        destination_dir=str(tmp_path / "out"),
        collision="overwrite",
    )
    plan = engine.build_plan([archive_entry.id])
    stats = RestoreStats(1, plan.tapes[0].tape_id)
    final = await engine.execute(request, stats, plan, asyncio.Event())

    assert final.files_done == 1
    assert final.hash_mismatches == 0
    # The tree was recreated under the alternate destination, keeping
    # the hierarchy below the drive root.
    restored_root = tmp_path / "out" / Path(*Path(src).parts[1:])
    assert (restored_root / "a.txt").read_bytes() == b"AAAA"
    assert (restored_root / "nested" / "b.txt").read_bytes() == b"BB"


def test_build_plan_keeps_archive_kind(tmp_path: Path) -> None:
    """Plan items carry the archive kind and inner count."""
    engine, repo = _make_engine(tmp_path, FakeItDtTransport())
    tape = repo.register_tape("tape-uuid-archive2", label="Archive 2")
    session = repo.create_session(tape.id, job_name="archivejob")
    entry = repo.add_file(
        session.id,
        tape_file_index=1,
        path="C:/Data/toolset",
        size=100,
        mtime=datetime.now(UTC),
        sha256=_sha("zip-bytes"),
        kind="archive",
        inner_count=42,
    )
    assert entry is not None
    plan = engine.build_plan([entry.id])
    item = plan.tapes[0].items[0]
    assert item.kind == "archive"
    assert item.inner_count == 42


async def test_restore_recreates_empty_files_without_tape(tmp_path: Path) -> None:
    """A zero-byte catalog entry is written out without a tape read."""
    # The scenario read is the t0 header download: without a matching
    # header the engine waits for the expected tape forever.
    fake = FakeItDtTransport(scenarios={"read": (_TAPE_A_HEADER, 0)})
    engine, repo = _make_engine(tmp_path, fake)
    tape_row_id, _ = _seed_tape_with_files(repo, count=1)
    session = repo.list_sessions(tape_row_id)[0]
    empty_entry = repo.add_file(
        session.id,
        tape_file_index=2,
        path="D:/Photos/placeholder/.gitkeep",
        size=0,
        mtime=datetime.now(UTC),
        sha256=hashlib.sha256(b"").hexdigest(),
    )
    assert empty_entry is not None

    request = RestoreRequest(
        file_ids=[empty_entry.id],
        destination_mode="alternate",
        destination_dir=str(tmp_path / "out"),
        collision="rename",
    )
    plan = engine.build_plan([empty_entry.id])
    stats = RestoreStats(1, plan.tapes[0].tape_id)
    final = await engine.execute(request, stats, plan, asyncio.Event())

    assert final.files_done == 1
    assert final.hash_mismatches == 0
    restored = tmp_path / "out" / "Photos" / "placeholder" / ".gitkeep"
    assert restored.exists()
    assert restored.stat().st_size == 0
    # No positioning or data read was issued for the empty file: only
    # the tape-identification header read touches the drive.
    assert not [cmd for cmd in fake.commands if cmd[0] == "fsf"]
