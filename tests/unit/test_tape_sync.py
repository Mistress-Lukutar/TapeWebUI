"""
File:   test_tape_sync.py
Brief:  Tape sync tests: verify, erase attribution, deep scan, comparison.
Author: Mistress-Lukutar
Date:   2026-11-07
Version: v0.7.0
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from tapevaultui.infrastructure.itdt_transport import FakeItDtTransport
from tapevaultui.services.catalog import CatalogRepository
from tapevaultui.services.session_io import new_session_id
from tapevaultui.services.tape_format import (
    HeaderFileEntry,
    build_session_header,
    serialize_header,
)
from tapevaultui.services.tape_sync import (
    DIFF_CATALOG_ONLY,
    DIFF_MATCH,
    DIFF_MISMATCH,
    DIFF_TAPE_ONLY,
    VERIFY_BLANK,
    VERIFY_FOREIGN,
    VERIFY_MATCH,
    VERIFY_NO_MEDIUM,
    OnTapeFile,
    OnTapeSession,
    apply_erase_attribution,
    attribute_erase,
    clear_stale_notice,
    compare_with_catalog,
    get_stale_notice,
    scan_tape_contents,
    set_stale_notice,
    verify_loaded_tape,
)

TAPE_UUID = "11111111-1111-1111-1111-111111111111"
FOREIGN_UUID = "22222222-2222-2222-2222-222222222222"

# Real ITDT output observed for a rewind with the cartridge ejected but
# loadable (sense key 0x02 not-ready, ASC/ASCQ 04/02).
NO_MEDIA_REWIND_OUTPUT = (
    "Rewinding tape...\n"
    "Operation FAILED with errno 2 The system cannot find the file specified"
)


@pytest.fixture
def repo(tmp_path: Path) -> CatalogRepository:
    """Repository on a fresh file-based database.

    A file (not in-memory) database is required: verify/attribute paths
    touch the catalog from ``asyncio.to_thread``, which an in-memory
    SQLite database does not survive across threads.

    Args:
        tmp_path: Pytest temporary directory.

    Returns:
        CatalogRepository with all tables created.
    """
    repository = CatalogRepository(f"sqlite:///{tmp_path / 'catalog.db'}")
    repository.create_all()
    return repository


def _header_bytes(
    tape_id: str = TAPE_UUID,
    *,
    files: list[tuple[str, int]] | None = None,
    job_name: str = "photos",
) -> bytes:
    """Serialize a session header for the simulated tape.

    Args:
        tape_id: Tape identity UUID in the header.
        files: (path, size) pairs recorded in the header.
        job_name: Job name snapshot.

    Returns:
        Serialized header bytes.
    """
    header = build_session_header(
        tape_id=tape_id,
        session_id=new_session_id(),
        job_name=job_name,
        started_at=datetime.now(UTC),
        files=[
            HeaderFileEntry(
                seq=seq,
                path=path,
                size=size,
                mtime=0.0,
                sha256="0" * 64,
                tape_file_index=seq + 1,
            )
            for seq, (path, size) in enumerate(files or [])
        ],
    )
    return serialize_header(header)


async def test_verify_match_stamps_tape(
    tmp_path: Path, repo: CatalogRepository
) -> None:
    """A matching header marks the catalog tape verified."""
    tape = repo.register_tape(TAPE_UUID, label="Vault 1")
    fake = FakeItDtTransport(tape_files=[_header_bytes()])
    result = await verify_loaded_tape(fake, tmp_path, repo)
    assert result.status == VERIFY_MATCH
    assert result.tape_id == tape.id
    refreshed = repo.get_tape(tape.id)
    assert refreshed is not None and refreshed.last_verified_at is not None


async def test_verify_blank_tape(tmp_path: Path, repo: CatalogRepository) -> None:
    """An empty tape (erase wipes the header) verifies as blank."""
    repo.register_tape(TAPE_UUID)
    fake = FakeItDtTransport(tape_files=[])
    result = await verify_loaded_tape(fake, tmp_path, repo)
    assert result.status == VERIFY_BLANK


async def test_verify_foreign_tape(tmp_path: Path, repo: CatalogRepository) -> None:
    """A readable header with an unknown UUID verifies as foreign."""
    repo.register_tape(TAPE_UUID)
    fake = FakeItDtTransport(tape_files=[_header_bytes(tape_id=FOREIGN_UUID)])
    result = await verify_loaded_tape(fake, tmp_path, repo)
    assert result.status == VERIFY_FOREIGN
    assert result.tape_uuid == FOREIGN_UUID


async def test_verify_no_medium(tmp_path: Path, repo: CatalogRepository) -> None:
    """A drive without a cartridge verifies as no_medium."""
    fake = FakeItDtTransport(scenarios={"rewind": (NO_MEDIA_REWIND_OUTPUT, 2)})
    result = await verify_loaded_tape(fake, tmp_path, repo)
    assert result.status == VERIFY_NO_MEDIUM


async def test_attribute_erase_found_and_blank(
    tmp_path: Path, repo: CatalogRepository
) -> None:
    """Attribution resolves the catalog tape before the erase wipes it."""
    tape = repo.register_tape(TAPE_UUID)
    fake = FakeItDtTransport(tape_files=[_header_bytes()])
    assert await attribute_erase(fake, tmp_path, repo) == tape.id
    fake = FakeItDtTransport(tape_files=[])
    assert await attribute_erase(fake, tmp_path, repo) is None


async def test_scan_tape_contents_walks_sessions(tmp_path: Path) -> None:
    """The deep scan walks headers and computes absolute positions."""
    fake = FakeItDtTransport(
        tape_files=[
            _header_bytes(files=[("a.txt", 10), ("b.txt", 20)]),
            b"data-a",
            b"data-b",
            _header_bytes(job_name="docs", files=[("c.txt", 30)]),
            b"data-c",
        ]
    )
    sessions = await scan_tape_contents(fake, tmp_path)
    assert [s.file_base for s in sessions] == [0, 3]
    assert [s.job_name for s in sessions] == ["photos", "docs"]
    assert sessions[1].files[0].path == "c.txt"
    # Absolute positioning: rewind+fsf before every header read.
    assert [cmd[0] for cmd in fake.commands] == [
        "rewind",
        "read",
        "rewind",
        "fsf",
        "read",
        "rewind",
        "fsf",
        "read",
    ]


def _catalog_with_sessions(repo: CatalogRepository) -> None:
    """Fill the repo with tape-1 sessions: 3 live, 1 erased."""
    tape = repo.register_tape(TAPE_UUID)
    first = repo.create_session(tape.id, job_name="photos")
    repo.add_file(
        first.id,
        tape_file_index=1,
        path="a.txt",
        size=10,
        mtime=datetime.now(UTC),
        sha256="0" * 64,
    )
    repo.finish_session(first.id, status="ok")
    second = repo.create_session(tape.id, job_name="docs")
    repo.add_file(
        second.id,
        tape_file_index=4,
        path="b.txt",
        size=20,
        mtime=datetime.now(UTC),
        sha256="0" * 64,
    )
    repo.finish_session(second.id, status="ok")
    third = repo.create_session(tape.id, job_name="old")
    repo.finish_session(third.id, status="ok")
    fourth = repo.create_session(tape.id, job_name="history")
    repo.finish_session(fourth.id, status="erased")


def test_compare_with_catalog_classifies_diffs(repo: CatalogRepository) -> None:
    """Comparison matches by file_base and classifies every session."""
    _catalog_with_sessions(repo)
    tape = repo.get_tape_by_uuid(TAPE_UUID)
    assert tape is not None
    on_tape = [
        OnTapeSession(
            file_base=0,
            session_id="hdr-1",
            job_name="photos",
            started_at="",
            files=[OnTapeFile(path="a.txt", size=10)],
        ),
        OnTapeSession(
            file_base=2,
            session_id="hdr-2",
            job_name="docs",
            started_at="",
            files=[OnTapeFile(path="b.txt", size=20)],
        ),
        OnTapeSession(
            file_base=9,
            session_id="hdr-3",
            job_name="ghost",
            started_at="",
            files=[OnTapeFile(path="x.txt", size=99)],
        ),
    ]
    report = compare_with_catalog(on_tape, repo, tape.id)
    assert report.tape_id == tape.id
    kinds = [(d.kind, d.file_base) for d in report.diffs]
    assert (DIFF_MATCH, 0) in kinds
    assert (DIFF_TAPE_ONLY, 9) in kinds
    catalog_only = [d for d in report.diffs if d.kind == DIFF_CATALOG_ONLY]
    assert len(catalog_only) == 1
    assert catalog_only[0].job_name == "old"
    assert all(d.kind != DIFF_MISMATCH for d in report.diffs)


def test_compare_with_catalog_detects_mismatch(repo: CatalogRepository) -> None:
    """A same-position session with different files is a mismatch."""
    repo.register_tape(TAPE_UUID)
    tape = repo.get_tape_by_uuid(TAPE_UUID)
    assert tape is not None
    session = repo.create_session(tape.id, job_name="photos")
    repo.add_file(
        session.id,
        tape_file_index=1,
        path="a.txt",
        size=10,
        mtime=datetime.now(UTC),
        sha256="0" * 64,
    )
    repo.finish_session(session.id, status="ok")
    on_tape = [
        OnTapeSession(
            file_base=0,
            session_id="hdr-1",
            job_name="photos",
            started_at="",
            files=[OnTapeFile(path="a.txt", size=11)],
        ),
    ]
    report = compare_with_catalog(on_tape, repo, tape.id)
    assert [d.kind for d in report.diffs] == [DIFF_MISMATCH]


def test_stale_notice_roundtrip(repo: CatalogRepository) -> None:
    """The stale notice is set, visible and dismissible."""
    assert get_stale_notice(repo) is None
    set_stale_notice(repo, "unattributed_erase")
    notice = get_stale_notice(repo)
    assert notice is not None and notice["reason"] == "unattributed_erase"
    clear_stale_notice(repo)
    assert get_stale_notice(repo) is None


def test_apply_erase_attribution_marks_tape(repo: CatalogRepository) -> None:
    """Attributed erase marks the tape erased in the catalog."""
    tape = repo.register_tape(TAPE_UUID)
    session = repo.create_session(tape.id, job_name="photos")
    repo.finish_session(session.id, status="ok")
    message = apply_erase_attribution(repo, tape.id)
    assert "Catalog synced" in message
    sessions = repo.list_sessions(tape.id)
    assert all(s.status == "erased" for s in sessions)
    refreshed = repo.get_tape(tape.id)
    assert refreshed is not None and refreshed.bytes_used == 0


def test_apply_erase_attribution_without_tape_sets_notice(
    repo: CatalogRepository,
) -> None:
    """An unattributed erase records the stale-catalog notice."""
    message = apply_erase_attribution(repo, None)
    assert "may be stale" in message
    assert get_stale_notice(repo) is not None
