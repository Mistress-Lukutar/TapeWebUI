"""
File:   test_direct_verify.py
Brief:  Direct-reader verify path tests (hash-on-the-fly, empty-file dance, fallback).
Author: Mistress-Lukutar
Date:   2026-11-09
Version: v0.10.0
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

from tapevaultui.infrastructure.itdt_transport import FakeItDtTransport
from tapevaultui.infrastructure.tape_device import (
    DirectReadUnavailableError,
    TapeDeviceError,
)
from tapevaultui.services.backup_engine import BackupEngine, BackupStats
from tapevaultui.services.catalog import CatalogRepository
from tapevaultui.services.event_bus import EventBus
from tapevaultui.services.job_defs import JobDefinition
from tapevaultui.services.session_io import new_session_id
from tapevaultui.services.tape_format import (
    build_session_header,
    serialize_header,
)


def _definition(
    tmp_path: Path,
    verify: str = "readback",
    continue_on_error: bool = True,
    mode: str = "append",
    backup_type: str = "full",
    full_every_days: int = 30,
) -> JobDefinition:
    """Build a one-source job over the test tree.

    Args:
        tmp_path: Pytest temp dir holding the source tree.
        verify: Verify option value.
        continue_on_error: continue_on_error option value.
        mode: Backup mode (append | overwrite).
        backup_type: full | incremental.
        full_every_days: Force-full period for incremental jobs.

    Returns:
        A JobDefinition.
    """
    return JobDefinition.model_validate(
        {
            "name": "testjob",
            "sources": [{"path": str(tmp_path / "src")}],
            "options": {
                "mode": mode,
                "verify": verify,
                "continue_on_error": continue_on_error,
                "backup_type": backup_type,
                "full_every_days": full_every_days,
            },
        }
    )


def _make_repo() -> CatalogRepository:
    """Create an in-memory catalog with all tables.

    Returns:
        CatalogRepository.
    """
    repo = CatalogRepository("sqlite://")
    repo.create_all()
    return repo


class FakeDirectReader:
    """Hardware-free stand-in for DirectTapeReader backed by a FakeItDtTransport.

    Shares the transport's ``position`` so that direct reads and ITDT
    commands (fsf/rewind) walk the same virtual tape.  Empty-file reads
    leave the position untouched so that the subsequent ITDT ``fsf 1``
    correctly advances past the bare filemark.

    Args:
        fake: The FakeItDtTransport to drive.
        fail_position: If not None, raise TapeDeviceError on this tape index.
        fail_open: If True, raise DirectReadUnavailableError on open().
    """

    def __init__(
        self,
        fake: FakeItDtTransport,
        fail_position: int | None = None,
        fail_open: bool = False,
    ) -> None:
        """Initialize the fake reader.

        Args:
            fake: Shared FakeItDtTransport.
            fail_position: Tape index at which read_file_hashed fails.
            fail_open: Whether open() should raise immediately.
        """
        self.fake = fake
        self.fail_position = fail_position
        self.fail_open = fail_open
        self.open_calls = 0
        self.close_calls = 0
        self.reads = 0

    def open(self) -> None:
        """Open the reader (optionally failing)."""
        if self.fail_open:
            raise DirectReadUnavailableError("simulated unavailable")
        self.open_calls += 1

    def close(self) -> None:
        """Close the reader."""
        self.close_calls += 1

    def read_file_hashed(self) -> tuple[int, str]:
        """Simulate a direct hash-on-the-fly read.

        Returns:
            (bytes_read, sha256_hex) for the current tape position.
        """
        self.reads += 1
        if self.fake.position >= len(self.fake.tape_files):
            raise TapeDeviceError("simulated end of data")
        if self.fake.position == self.fail_position:
            raise TapeDeviceError("simulated read failure")
        data = self.fake.tape_files[self.fake.position]
        # Non-empty files consume one tape position; empty files leave the
        # head so that a subsequent ITDT fsf 1 advances past the filemark.
        if len(data) > 0:
            self.fake.position += 1
        return len(data), hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _build_tape(
    content_a: bytes = b"AAAA",
    content_b: bytes = b"BBBB",
    *,
    fail_position: int | None = None,
    fail_open: bool = False,
) -> tuple[FakeItDtTransport, FakeDirectReader]:
    """Create a fake transport with a valid session header and two content files.

    Args:
        content_a: Bytes for the first tape file (a.txt).
        content_b: Bytes for the second tape file (b.txt).
        fail_position: Tape index at which read_file_hashed fails.
        fail_open: Whether open() should raise immediately.

    Returns:
        Tuple of (FakeItDtTransport, FakeDirectReader) ready for use.
    """
    header = serialize_header(
        build_session_header(
            tape_id="direct-tape",
            session_id=new_session_id(),
            started_at=datetime.now(UTC),
        )
    )
    fake = FakeItDtTransport(tape_files=[header, content_a, content_b])
    reader = FakeDirectReader(fake, fail_position=fail_position, fail_open=fail_open)
    return fake, reader


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_direct_verify_hashes_on_the_fly(tmp_path: Path) -> None:
    """Direct-reader verify hashes on-the-fly: no ITDT read commands issued."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_bytes(b"AAAA")
    (src / "b.txt").write_bytes(b"BBBB")

    fake, reader = _build_tape()
    repo = _make_repo()
    engine = BackupEngine(
        fake,
        repo,
        tmp_path / "work",
        EventBus(),
        direct_reader_factory=lambda _d: reader,
    )
    stats = await engine.execute(_definition(tmp_path), BackupStats("testjob"))

    sessions = repo.list_sessions(repo.list_tapes()[0].id)
    files = repo.list_files(sessions[0].id)
    # Both files verified successfully.
    assert files[1].status == "ok"  # a.txt
    assert files[2].status == "ok"  # b.txt
    assert stats.hash_mismatches == 0
    assert reader.reads == 2

    # No ITDT read command in the verify phase (proves direct path used).
    last_weof = len(fake.commands) - fake.commands[::-1].index(("weof", "1"))
    verify_commands = fake.commands[last_weof:]
    reads = [cmd for cmd in verify_commands if cmd[0] == "read"]
    assert len(reads) == 0

    # Verify phase: one rewind-enter, one fsf (position), one rewind-leave.
    rewinds = [cmd for cmd in verify_commands if cmd[0] == "rewind"]
    fsfs = [cmd for cmd in verify_commands if cmd[0] == "fsf"]
    assert len(rewinds) == 2
    assert fsfs == [("fsf", "1")]


async def test_direct_verify_hash_mismatch_continues(tmp_path: Path) -> None:
    """A hash mismatch marks the file damaged but continues to the next file."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_bytes(b"AAAA")
    (src / "b.txt").write_bytes(b"BBBB")

    tape_files = [
        serialize_header(
            build_session_header(
                tape_id="direct-tape",
                session_id=new_session_id(),
                started_at=datetime.now(UTC),
            )
        ),
        b"AA_ACORRUPTED",  # corrupted a.txt content
        b"BBBB",
    ]
    fake = FakeItDtTransport(tape_files=tape_files)
    reader = FakeDirectReader(fake)
    repo = _make_repo()
    engine = BackupEngine(
        fake,
        repo,
        tmp_path / "work",
        EventBus(),
        direct_reader_factory=lambda _d: reader,
    )
    stats = await engine.execute(_definition(tmp_path), BackupStats("testjob"))

    sessions = repo.list_sessions(repo.list_tapes()[0].id)
    files = repo.list_files(sessions[0].id)
    # a.txt is damaged due to hash mismatch; b.txt verified ok.
    assert files[1].status == "damaged"
    assert files[2].status == "ok"
    assert stats.hash_mismatches == 1
    assert "verification failed" in stats.error


async def test_direct_read_failure_stops_pass(tmp_path: Path) -> None:
    """A direct read failure marks the file damaged and stops the pass."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_bytes(b"AAAA")
    (src / "b.txt").write_bytes(b"BBBB")

    fake, reader = _build_tape(fail_position=1)  # fail on first content read
    repo = _make_repo()
    engine = BackupEngine(
        fake,
        repo,
        tmp_path / "work",
        EventBus(),
        direct_reader_factory=lambda _d: reader,
    )
    stats = await engine.execute(_definition(tmp_path), BackupStats("testjob"))

    sessions = repo.list_sessions(repo.list_tapes()[0].id)
    files = repo.list_files(sessions[0].id)
    # a.txt damaged due to read failure; b.txt not read (pass broke).
    assert files[1].status == "damaged"
    assert reader.reads == 1

    last_weof = len(fake.commands) - fake.commands[::-1].index(("weof", "1"))
    verify_commands = fake.commands[last_weof:]
    rewinds = [cmd for cmd in verify_commands if cmd[0] == "rewind"]
    # Final rewind still issued after the finally block closes the reader.
    assert len(rewinds) == 2
    assert reader.close_calls >= 1
    assert stats.hash_mismatches == 1


async def test_direct_unavailable_falls_back_to_itdt(tmp_path: Path) -> None:
    """When direct reader open fails, all files fall back to ITDT read."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_bytes(b"AAAA")
    (src / "b.txt").write_bytes(b"BBBB")

    fake, reader = _build_tape(fail_open=True)
    repo = _make_repo()
    engine = BackupEngine(
        fake,
        repo,
        tmp_path / "work",
        EventBus(),
        direct_reader_factory=lambda _d: reader,
    )
    # Should not crash; ITDT fallback handles everything.
    stats = await engine.execute(_definition(tmp_path), BackupStats("testjob"))

    sessions = repo.list_sessions(repo.list_tapes()[0].id)
    files = repo.list_files(sessions[0].id)
    assert files[1].status == "ok"
    assert files[2].status == "ok"
    assert stats.hash_mismatches == 0

    # ITDT read -d commands present in the verify phase.
    last_weof = len(fake.commands) - fake.commands[::-1].index(("weof", "1"))
    verify_commands = fake.commands[last_weof:]
    reads = [cmd for cmd in verify_commands if cmd[0] == "read"]
    assert len(reads) >= 2  # one per content file

    # Reader never successfully opened or read.
    assert reader.reads == 0


async def test_direct_verify_spaces_empty_file_and_reopens(tmp_path: Path) -> None:
    """Empty-file exclusive-device dance: close -> ITDT fsf 1 -> reopen."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "empty.txt").touch()  # 0 bytes
    (src / "b.txt").write_bytes(b"CONTENT-B")

    header = serialize_header(
        build_session_header(
            tape_id="direct-tape",
            session_id=new_session_id(),
            started_at=datetime.now(UTC),
        )
    )
    # Files sorted by path: b.txt < empty.txt, so tape_files must match.
    fake = FakeItDtTransport(tape_files=[header, b"CONTENT-B", b""])
    reader = FakeDirectReader(fake)
    repo = _make_repo()
    engine = BackupEngine(
        fake,
        repo,
        tmp_path / "work",
        EventBus(),
        direct_reader_factory=lambda _d: reader,
    )
    stats = await engine.execute(_definition(tmp_path), BackupStats("testjob"))

    sessions = repo.list_sessions(repo.list_tapes()[0].id)
    files = repo.list_files(sessions[0].id)
    # Both files verified ok: b.txt via direct (non-empty), empty.txt via dance.
    assert files[1].status == "ok"  # b.txt (first alphabetically)
    assert files[2].status == "ok"  # empty.txt
    assert stats.hash_mismatches == 0

    last_weof = len(fake.commands) - fake.commands[::-1].index(("weof", "1"))
    verify_commands = fake.commands[last_weof:]

    # Two fsf commands: positioning + empty-file spacing.
    fsfs = [cmd for cmd in verify_commands if cmd[0] == "fsf"]
    assert fsfs == [("fsf", "1"), ("fsf", "1")]

    # No ITDT read command (direct path used for the non-empty file).
    reads = [cmd for cmd in verify_commands if cmd[0] == "read"]
    assert len(reads) == 0

    # Reader opened twice (initial + reopen after empty file), closed at least once.
    assert reader.open_calls == 2
    assert reader.close_calls >= 1
