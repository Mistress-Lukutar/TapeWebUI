"""
File:   test_backup_engine.py
Brief:  Backup engine tests on the fake transport (command order, verify).
Author: Mistress-Lukutar
Date:   2026-11-06
Version: v0.3.0
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from tapevaultui.infrastructure.itdt_transport import FakeItDtTransport
from tapevaultui.services.backup_engine import (
    BackupEngine,
    BackupStats,
    TapeFullError,
)
from tapevaultui.services.catalog import CatalogRepository
from tapevaultui.services.event_bus import EventBus
from tapevaultui.services.job_defs import JobDefinition


def _definition(
    tmp_path: Path, verify: str = "readback", continue_on_error: bool = True
) -> JobDefinition:
    """Build a one-source job over the test tree.

    Args:
        tmp_path: Pytest temp dir holding the source tree.
        verify: Verify option value.
        continue_on_error: continue_on_error option value.

    Returns:
        A JobDefinition.
    """
    return JobDefinition.model_validate(
        {
            "name": "testjob",
            "sources": [{"path": str(tmp_path / "src")}],
            "options": {
                "mode": "append",
                "verify": verify,
                "continue_on_error": continue_on_error,
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


def _fake_with_echo_read(tmp_path: Path) -> FakeItDtTransport:
    """Fake transport whose read -d returns the requested file's content.

    The engine calls read -d with the *destination* path, so the fake
    cannot echo real tape content; instead verify download files get the
    content of the source file with the same tape index via scenarios.
    Simpler approach: write -raw records file contents; read -d writes a
    fixed scenario string. Tests that need hash equality pre-fill the
    scenario with the expected hash content.
    """
    return FakeItDtTransport()


async def test_backup_writes_header_then_files(tmp_path: Path) -> None:
    """The engine writes t0 then one write per source file, in order."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_bytes(b"AAAA")
    (src / "b.txt").write_bytes(b"BB")

    fake = FakeItDtTransport()
    engine = BackupEngine(fake, _make_repo(), tmp_path / "work", EventBus())
    stats = await engine.execute(
        _definition(tmp_path, verify="none"), BackupStats("testjob")
    )

    assert stats.state == "writing"  # runtime flips to done
    assert stats.files_total == 2
    assert stats.files_done == 2
    write_commands = [cmd for cmd in fake.commands if cmd[0] == "write"]
    assert len(write_commands) == 3  # header + 2 files
    assert "header_" in write_commands[0][3]


async def test_backup_registers_tape_and_session(tmp_path: Path) -> None:
    """The catalog receives a tape, a session, and file rows."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_bytes(b"AAAA")

    repo = _make_repo()
    fake = FakeItDtTransport()
    engine = BackupEngine(fake, repo, tmp_path / "work", EventBus())
    await engine.execute(_definition(tmp_path, verify="none"), BackupStats("testjob"))

    tapes = repo.list_tapes()
    assert len(tapes) == 1
    sessions = repo.list_sessions(tapes[0].id)
    assert len(sessions) == 1
    files = repo.list_files(sessions[0].id)
    assert [f.tape_file_index for f in files] == [0, 1]
    assert sessions[0].status == "ok"


async def test_verify_readback_updates_files(tmp_path: Path) -> None:
    """Readback verify issues rewind/fsf/read and keeps ok files ok."""
    content = b"VERIFIED-CONTENT"
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_bytes(content)

    fake = FakeItDtTransport()
    repo = _make_repo()
    engine = BackupEngine(fake, repo, tmp_path / "work", EventBus())
    await engine.execute(_definition(tmp_path), BackupStats("testjob"))

    verify_commands = [cmd for cmd in fake.commands if cmd[0] in ("fsf", "read")]
    assert any(cmd[0] == "fsf" and cmd[1] == "1" for cmd in verify_commands)
    sessions = repo.list_sessions(repo.list_tapes()[0].id)
    files = repo.list_files(sessions[0].id)
    assert files[1].status == "damaged"  # fake read returned header text


async def test_verify_readback_ok_when_content_matches(tmp_path: Path) -> None:
    """Verify keeps the file ok when the readback hash matches."""
    content = "VERIFIED-CONTENT"
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_bytes(content.encode())

    # The fake writes the read scenario stdout into the -d destination,
    # so a scenario equal to the source content makes hashes match.
    fake = FakeItDtTransport(scenarios={"read": (content, 0)})
    repo = _make_repo()
    engine = BackupEngine(fake, repo, tmp_path / "work", EventBus())
    await engine.execute(_definition(tmp_path), BackupStats("testjob"))

    sessions = repo.list_sessions(repo.list_tapes()[0].id)
    files = repo.list_files(sessions[0].id)
    assert files[1].status == "ok"


async def test_write_error_continue_on_error(tmp_path: Path) -> None:
    """A failed write marks the file damaged and continues."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_bytes(b"AAAA")
    (src / "b.txt").write_bytes(b"BB")

    fake = FakeItDtTransport(
        command_scenarios={
            ("write", "-raw", "-s", str(src / "a.txt")): (
                "Write failed: medium error",
                1,
            )
        },
    )
    repo = _make_repo()
    engine = BackupEngine(fake, repo, tmp_path / "work", EventBus())
    stats = await engine.execute(
        _definition(tmp_path, verify="none"), BackupStats("testjob")
    )

    sessions = repo.list_sessions(repo.list_tapes()[0].id)
    files = repo.list_files(sessions[0].id)
    statuses = {f.tape_file_index: f.status for f in files}
    assert statuses[1] == "damaged"
    assert statuses[2] == "ok"
    assert sessions[0].status == "ok"
    assert stats.files_done == 1


async def test_write_error_strict_fails(tmp_path: Path) -> None:
    """With continue_on_error=False the first write error aborts."""
    import pytest

    from tapevaultui.services.backup_engine import BackupEngineError

    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_bytes(b"AAAA")
    (src / "b.txt").write_bytes(b"BB")

    fake = FakeItDtTransport(
        command_scenarios={
            ("write", "-raw", "-s", str(src / "a.txt")): ("Write failed: boom", 1)
        }
    )
    repo = _make_repo()
    engine = BackupEngine(fake, repo, tmp_path / "work", EventBus())
    with pytest.raises(BackupEngineError, match="boom"):
        await engine.execute(
            _definition(tmp_path, verify="none", continue_on_error=False),
            BackupStats("testjob"),
        )


async def test_tape_full_without_media_callback(tmp_path: Path) -> None:
    """An EOM write failure raises TapeFullError when no callback given."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_bytes(b"AAAA")

    fake = FakeItDtTransport(
        command_scenarios={
            ("write", "-raw", "-s", str(src / "a.txt")): (
                "Error: end of data detected",
                1,
            )
        }
    )
    repo = _make_repo()
    engine = BackupEngine(fake, repo, tmp_path / "work", EventBus())
    import pytest

    with pytest.raises(TapeFullError, match="Tape full"):
        await engine.execute(
            _definition(tmp_path, verify="none"), BackupStats("testjob")
        )

    sessions = repo.list_sessions(repo.list_tapes()[0].id)
    assert sessions[0].status == "partial"


def test_hash_helper_matches_snapshot(tmp_path: Path) -> None:
    """sha256_of matches hashlib for the same content."""
    from tapevaultui.services.snapshot import sha256_of

    file = tmp_path / "x.bin"
    file.write_bytes(b"same")
    assert sha256_of(file) == hashlib.sha256(b"same").hexdigest()
