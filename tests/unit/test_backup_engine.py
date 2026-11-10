"""
File:   test_backup_engine.py
Brief:  Backup engine tests on the fake transport (command order, verify).
Author: Mistress-Lukutar
Date:   2026-11-08
Version: v0.9.1
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tapevaultui.infrastructure.itdt_transport import FakeItDtTransport
from tapevaultui.services.backup_engine import (
    BackupEngine,
    BackupEngineError,
    BackupStats,
    TapeFullError,
)
from tapevaultui.services.catalog import (
    TAPE_ADOPTION_KEY,
    CatalogRepository,
)
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
    # Every written tape file is delimited by a filemark (header + 2 files)
    # so BOP header reads and fsf positioning stay O(1), not O(tape).
    weof_commands = [cmd for cmd in fake.commands if cmd[0] == "weof"]
    assert len(weof_commands) == 3
    assert weof_commands[0] == ("weof", "1")


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


async def test_overwrite_marks_prior_sessions_erased(tmp_path: Path) -> None:
    """Overwrite erases the tape and syncs the catalog in the right order.

    Prior sessions become erased and the fresh session's file_base
    restarts at 0, matching the physical position after the erase.
    """
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_bytes(b"AAAA")

    repo = _make_repo()
    tape = repo.register_tape("tape-uuid-1")
    old_session = repo.create_session(tape.id, job_name="old")
    repo.finish_session(old_session.id, status="ok")

    header = build_session_header(
        tape_id="tape-uuid-1",
        session_id=new_session_id(),
        started_at=datetime.now(UTC),
    )
    fake = FakeItDtTransport(tape_files=[serialize_header(header)])
    engine = BackupEngine(fake, repo, tmp_path / "work", EventBus())
    stats = await engine.execute(
        _definition(tmp_path, verify="none", mode="overwrite"), BackupStats("testjob")
    )

    assert stats.files_done == 1
    sessions = repo.list_sessions(tape.id)
    by_seq = {s.tape_seq: s for s in sessions}
    assert by_seq[0].status == "erased"  # the pre-overwrite session
    assert by_seq[1].status == "ok"  # the fresh overwrite session
    assert by_seq[1].file_base == 0
    # Erase must precede the session creation: identify (rewind+read+
    # rewind), then erase (rewind+erase), then the header write.
    assert [cmd[0] for cmd in fake.commands][:5] == [
        "rewind",
        "read",
        "rewind",
        "rewind",
        "erase",
    ]


async def test_verify_readback_updates_files(tmp_path: Path) -> None:
    """Readback verify issues rewind/fsf/read and keeps ok files ok."""
    content = b"VERIFIED-CONTENT"
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_bytes(content)

    fake = FakeItDtTransport()
    repo = _make_repo()
    engine = BackupEngine(fake, repo, tmp_path / "work", EventBus())
    stats = BackupStats("testjob")
    await engine.execute(_definition(tmp_path), stats)

    verify_commands = [cmd for cmd in fake.commands if cmd[0] in ("fsf", "read")]
    assert any(cmd[0] == "fsf" and cmd[1] == "1" for cmd in verify_commands)
    sessions = repo.list_sessions(repo.list_tapes()[0].id)
    files = repo.list_files(sessions[0].id)
    assert files[1].status == "damaged"  # fake read returned header text
    # Verify damage must be visible in the stats (no silent "done").
    assert stats.hash_mismatches == 1
    assert "verification failed" in stats.error


async def test_backup_adopts_pending_tape(tmp_path: Path) -> None:
    """A pending adoption reuses the old catalog record for a blank tape.

    The re-erased cartridge keeps its catalog row (label, erased
    history) while the fresh uuid is what the new session header and
    the record carry; the adoption setting is consumed.
    """
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_bytes(b"AAAA")

    repo = _make_repo()
    tape = repo.register_tape("old-tape-uuid", label="Shelf A")
    old_session = repo.create_session(tape.id, job_name="old")
    repo.finish_session(old_session.id, status="ok")
    repo.mark_tape_erased(tape.id)
    repo.set_setting(TAPE_ADOPTION_KEY, {"tape_id": tape.id})

    fake = FakeItDtTransport()  # reads as blank after the erase
    engine = BackupEngine(fake, repo, tmp_path / "work", EventBus())
    await engine.execute(_definition(tmp_path, verify="none"), BackupStats("testjob"))

    tapes = repo.list_tapes()
    assert len(tapes) == 1  # no duplicate record for the same cartridge
    adopted = tapes[0]
    assert adopted.id == tape.id
    assert adopted.uuid != "old-tape-uuid"  # fresh identity on the header
    assert adopted.label == "Shelf A"  # label and history preserved
    sessions = repo.list_sessions(tape.id)
    assert [s.status for s in sessions] == ["erased", "ok"]
    assert sessions[1].file_base == 0  # erased sessions do not offset
    assert repo.get_setting(TAPE_ADOPTION_KEY) is None  # consumed


async def test_backup_without_adoption_registers_new_tape(tmp_path: Path) -> None:
    """A stale adoption payload falls back to plain registration."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_bytes(b"AAAA")

    repo = _make_repo()
    repo.set_setting(TAPE_ADOPTION_KEY, {"tape_id": 424242})  # missing record

    fake = FakeItDtTransport()
    engine = BackupEngine(fake, repo, tmp_path / "work", EventBus())
    await engine.execute(_definition(tmp_path, verify="none"), BackupStats("testjob"))

    tapes = repo.list_tapes()
    assert len(tapes) == 1
    assert repo.get_setting(TAPE_ADOPTION_KEY) is None


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


async def test_verify_is_one_sequential_pass(tmp_path: Path) -> None:
    """Verify positions once, then reads file after file without rewinds."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_bytes(b"AAAA")
    (src / "b.txt").write_bytes(b"BB")
    (src / "c.txt").write_bytes(b"CC")

    fake = FakeItDtTransport()
    engine = BackupEngine(fake, _make_repo(), tmp_path / "work", EventBus())
    await engine.execute(_definition(tmp_path), BackupStats("testjob"))

    verify_started = fake.commands.index(("weof", "1")) + 1
    # The write phase ends with the last weof; verify follows.
    verify_commands = fake.commands[verify_started:]
    rewinds = [cmd for cmd in verify_commands if cmd[0] == "rewind"]
    fsfs = [cmd for cmd in verify_commands if cmd[0] == "fsf"]
    reads = [cmd for cmd in verify_commands if cmd[0] == "read"]
    assert len(rewinds) == 2  # enter + leave the pass
    assert fsfs == [("fsf", "1")]  # first session file, once
    assert len(reads) == 3  # one per written file, in tape order


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
    statuses = {f.path: f.status for f in files}
    assert statuses[str(src / "a.txt")] == "damaged"
    assert statuses[str(src / "b.txt")] == "ok"
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


async def test_empty_file_written_as_filemark_only(tmp_path: Path) -> None:
    """A zero-byte source becomes a bare-filemark tape file, status ok."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_bytes(b"AAAA")
    (src / "empty.txt").touch()

    repo = _make_repo()
    fake = FakeItDtTransport()
    engine = BackupEngine(fake, repo, tmp_path / "work", EventBus())
    stats = await engine.execute(
        _definition(tmp_path, verify="none"), BackupStats("testjob")
    )

    # ITDT rejects data-less writes: no write command may target the
    # empty file; its tape file is the filemark itself.
    write_paths = [cmd[3] for cmd in fake.commands if cmd[0] == "write"]
    assert all("empty.txt" not in path for path in write_paths)
    weof_commands = [cmd for cmd in fake.commands if cmd[0] == "weof"]
    assert len(weof_commands) == 3  # header + a.txt + empty.txt
    sessions = repo.list_sessions(repo.list_tapes()[0].id)
    files = repo.list_files(sessions[0].id)
    by_path = {f.path: f for f in files}
    empty = by_path[str(src / "empty.txt")]
    assert empty.status == "ok"
    assert empty.size == 0
    assert empty.tape_file_index == 2
    assert by_path[str(src / "a.txt")].tape_file_index == 1
    assert stats.files_done == 2


async def test_verify_spaces_over_empty_files(tmp_path: Path) -> None:
    """Verify reads real files and spaces over bare-filemark files."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_bytes(b"AAAA")
    (src / "empty.txt").touch()

    fake = FakeItDtTransport(scenarios={"read": ("AAAA", 0)})
    repo = _make_repo()
    engine = BackupEngine(fake, repo, tmp_path / "work", EventBus())
    await engine.execute(_definition(tmp_path), BackupStats("testjob"))

    verify_started = len(fake.commands) - fake.commands[::-1].index(("weof", "1"))
    verify_commands = fake.commands[verify_started:]
    assert verify_commands[0] == ("rewind",)
    assert verify_commands[1] == ("fsf", "1")  # first session file
    assert verify_commands[2][0] == "read"  # a.txt readback
    assert verify_commands[3] == ("fsf", "1")  # over empty.txt's filemark
    assert verify_commands[4] == ("rewind",)  # leave the pass
    reads = [cmd for cmd in verify_commands if cmd[0] == "read"]
    assert len(reads) == 1
    files = repo.list_files(repo.list_sessions(repo.list_tapes()[0].id)[0].id)
    assert {f.status for f in files} == {"ok"}


async def test_damaged_write_shares_next_physical_index(tmp_path: Path) -> None:
    """A failed write consumes no tape position; the next file takes it."""
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
    await engine.execute(_definition(tmp_path, verify="none"), BackupStats("testjob"))

    tape = repo.list_tapes()[0]
    sessions = repo.list_sessions(tape.id)
    files = repo.list_files(sessions[0].id)
    by_path = {f.path: f for f in files}
    damaged = by_path[str(src / "a.txt")]
    assert damaged.status == "damaged"
    assert damaged.tape_file_index == 1  # shares b.txt's position
    assert by_path[str(src / "b.txt")].status == "ok"
    assert by_path[str(src / "b.txt")].tape_file_index == 1
    # The next session starts after the header and the one physical file.
    second = repo.create_session(tape.id, job_name="next")
    assert second.file_base == 2


async def test_large_write_gets_scaled_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Whole-file writes size their timeout from the payload."""
    from tapevaultui.services import session_io

    src = tmp_path / "src"
    src.mkdir()
    (src / "big.bin").write_bytes(b"x" * 1000)

    # Shrink the throughput floor so a tiny file still exceeds the flat
    # 600 s default: 1000 B at 2 B/s = 500 s + 120 s slack.
    monkeypatch.setattr(session_io, "MIN_FILE_THROUGHPUT_BPS", 2)
    fake = FakeItDtTransport()
    engine = BackupEngine(fake, _make_repo(), tmp_path / "work", EventBus())
    await engine.execute(_definition(tmp_path, verify="none"), BackupStats("testjob"))

    big_index = fake.commands.index(("write", "-raw", "-s", str(src / "big.bin")))
    assert fake.call_timeouts[big_index] == 620
    # The header write keeps the flat default (no override passed).
    header_index = fake.commands.index(
        next(cmd for cmd in fake.commands if cmd[0] == "write")
    )
    assert fake.call_timeouts[header_index] is None


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


async def test_no_media_waits_then_proceeds(tmp_path: Path) -> None:
    """An empty drive enters waiting_media and resumes once media is in."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_bytes(b"AAAA")

    no_media_rewind = (
        "Rewinding tape...\n"
        "Operation FAILED with errno 2 The system cannot find the file specified"
    )
    fake = FakeItDtTransport(command_scenarios={("rewind",): (no_media_rewind, 2)})
    repo = _make_repo()
    engine = BackupEngine(fake, repo, tmp_path / "work", EventBus())
    stats = BackupStats("testjob")

    async def wait_for_media() -> bool:
        # Inside the callback the engine has published waiting_media and
        # the drive is still empty; the poll must report "not ready".
        assert stats.state == "waiting_media"
        assert not await engine.media_ready("")
        # The operator loads a (blank) cartridge; retry succeeds.
        fake.command_scenarios.clear()
        return True

    stats = await engine.execute(
        _definition(tmp_path, verify="none"),
        stats,
        wait_for_media=wait_for_media,
    )
    assert stats.state == "writing"
    assert stats.files_done == 1
    assert repo.list_tapes()[0].id  # blank cartridge was claimed


def test_hash_helper_matches_snapshot(tmp_path: Path) -> None:
    """sha256_of matches hashlib for the same content."""
    from tapevaultui.services.snapshot import sha256_of

    file = tmp_path / "x.bin"
    file.write_bytes(b"same")
    assert sha256_of(file) == hashlib.sha256(b"same").hexdigest()


async def test_incremental_first_run_is_full(tmp_path: Path) -> None:
    """The first run of an incremental job records a full session."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_bytes(b"AAAA")

    repo = _make_repo()
    engine = BackupEngine(FakeItDtTransport(), repo, tmp_path / "work", EventBus())
    await engine.execute(
        _definition(tmp_path, backup_type="incremental", verify="none"),
        BackupStats("testjob"),
    )

    sessions = repo.list_sessions(repo.list_tapes()[0].id)
    assert len(sessions) == 1
    assert sessions[0].kind == "full"


async def test_incremental_second_run_unchanged_skips_tape(tmp_path: Path) -> None:
    """With no changes the incremental run writes nothing at all."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_bytes(b"AAAA")

    fake = FakeItDtTransport()
    repo = _make_repo()
    engine = BackupEngine(fake, repo, tmp_path / "work", EventBus())
    await engine.execute(
        _definition(tmp_path, backup_type="incremental", verify="none"),
        BackupStats("testjob"),
    )

    stats = await engine.execute(
        _definition(tmp_path, backup_type="incremental", verify="none"),
        BackupStats("testjob"),
    )
    assert stats.files_total == 0
    tape_ids = [t.id for t in repo.list_tapes()]
    total_sessions = sum(len(repo.list_sessions(t)) for t in tape_ids)
    assert total_sessions == 1  # only the first (full) session exists
    writes = [cmd for cmd in fake.commands if cmd[0] == "write"]
    assert len(writes) == 2  # header + one file from the first run only


async def test_incremental_writes_only_changed_files(tmp_path: Path) -> None:
    """A changed file is re-written as a new incremental session."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_bytes(b"AAAA")
    (src / "b.txt").write_bytes(b"BBBB")

    repo = _make_repo()
    fake = FakeItDtTransport()
    engine = BackupEngine(fake, repo, tmp_path / "work", EventBus())
    await engine.execute(
        _definition(tmp_path, backup_type="incremental", verify="none"),
        BackupStats("testjob"),
    )

    # Change one file's content (mtime changes with it).
    (src / "b.txt").write_bytes(b"CHANGED")
    # Run 2 on a fake whose BOP read returns the existing tape's header,
    # so the engine matches tape 1 instead of registering a new one.
    tape_uuid = repo.list_tapes()[0].uuid
    fake2 = FakeItDtTransport(
        tape_files=[
            serialize_header(
                build_session_header(
                    tape_id=tape_uuid,
                    session_id=new_session_id(),
                    started_at=datetime.now(UTC),
                )
            )
        ]
    )
    engine2 = BackupEngine(fake2, repo, tmp_path / "work", EventBus())
    stats = BackupStats("testjob")
    await engine2.execute(
        _definition(tmp_path, backup_type="incremental", verify="none"), stats
    )

    tape = repo.list_tapes()[0]
    sessions = repo.list_sessions(tape.id)
    assert len(sessions) == 2
    assert sessions[1].kind == "incremental"
    files = repo.list_files(sessions[1].id)
    names = [f.path.rsplit("\\", 1)[-1].rsplit("/", 1)[-1] for f in files]
    assert names == ["<session-header>", "b.txt"]
    assert stats.files_total == 1


async def test_incremental_forces_full_after_interval(tmp_path: Path) -> None:
    """A stale full session forces the next run back to full."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_bytes(b"AAAA")

    repo = _make_repo()
    engine = BackupEngine(FakeItDtTransport(), repo, tmp_path / "work", EventBus())
    await engine.execute(
        _definition(tmp_path, backup_type="incremental", verify="none"),
        BackupStats("testjob"),
    )

    # Backdate the full session beyond full_every_days.
    tape = repo.list_tapes()[0]
    session = repo.list_sessions(tape.id)[0]
    stale = datetime.now(UTC) - timedelta(days=31)
    with repo._session_factory() as db:
        row = db.get(type(session), session.id)
        assert row is not None
        row.started_at = stale
        db.commit()

    tape_uuid = tape.uuid
    fake2 = FakeItDtTransport(
        tape_files=[
            serialize_header(
                build_session_header(
                    tape_id=tape_uuid,
                    session_id=new_session_id(),
                    started_at=datetime.now(UTC),
                )
            )
        ]
    )
    engine2 = BackupEngine(fake2, repo, tmp_path / "work", EventBus())
    await engine2.execute(
        _definition(
            tmp_path, backup_type="incremental", verify="none", full_every_days=30
        ),
        BackupStats("testjob"),
    )
    sessions = repo.list_sessions(tape.id)
    assert len(sessions) == 2
    assert sessions[1].kind == "full"


async def test_incremental_overwrite_mode_is_full(tmp_path: Path) -> None:
    """Overwrite mode always records a full session."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_bytes(b"AAAA")

    repo = _make_repo()
    engine = BackupEngine(FakeItDtTransport(), repo, tmp_path / "work", EventBus())
    await engine.execute(
        _definition(tmp_path, backup_type="incremental", mode="overwrite"),
        BackupStats("testjob"),
    )
    sessions = repo.list_sessions(repo.list_tapes()[0].id)
    assert sessions[0].kind == "full"


async def test_incremental_baseline_ignores_erased_sessions(tmp_path: Path) -> None:
    """After the baseline tape is erased the next run is a full."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_bytes(b"AAAA")

    repo = _make_repo()
    engine = BackupEngine(FakeItDtTransport(), repo, tmp_path / "work", EventBus())
    await engine.execute(
        _definition(tmp_path, backup_type="incremental", verify="none"),
        BackupStats("testjob"),
    )

    tape = repo.list_tapes()[0]
    repo.mark_tape_erased(tape.id)
    fake = FakeItDtTransport()
    engine2 = BackupEngine(fake, repo, tmp_path / "work", EventBus())
    stats = BackupStats("testjob")
    await engine2.execute(
        _definition(
            tmp_path,
            backup_type="incremental",
            verify="none",
            mode="overwrite",
        ),
        stats,
    )
    assert stats.files_total == 1  # file re-planned despite unchanged mtime


# -- archive sources -------------------------------------------------------


def _archive_definition(
    tmp_path: Path,
    *,
    backup_type: str = "full",
    verify: str = "none",
    extra_plain: bool = False,
    extra_archive: str | None = None,
) -> JobDefinition:
    """Build a job with one archive source (+ optional plain/archive sources).

    Args:
        tmp_path: Pytest temp dir holding the source tree.
        backup_type: full | incremental.
        verify: Verify option value.
        extra_plain: Add a second plain (non-archive) source.
        extra_archive: Path string for an additional archive source.

    Returns:
        A JobDefinition.
    """
    sources: list[dict[str, object]] = [
        {"path": str(tmp_path / "src"), "archive": True}
    ]
    if extra_plain:
        sources.append({"path": str(tmp_path / "plain")})
    if extra_archive is not None:
        sources.append({"path": extra_archive, "archive": True})
    return JobDefinition.model_validate(
        {
            "name": "testjob",
            "sources": sources,
            "options": {
                "mode": "append",
                "verify": verify,
                "continue_on_error": True,
                "backup_type": backup_type,
                "full_every_days": 30,
            },
        }
    )


async def test_archive_source_writes_one_tape_file(tmp_path: Path) -> None:
    """An archive source lands on tape as one file, cataloged by source root."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_bytes(b"AAAA")
    (src / "sub").mkdir(parents=True)
    (src / "sub" / "b.txt").write_bytes(b"BB")

    repo = _make_repo()
    fake = FakeItDtTransport()
    engine = BackupEngine(fake, repo, tmp_path / "work", EventBus())
    stats = BackupStats("testjob")
    await engine.execute(_archive_definition(tmp_path), stats)

    assert stats.files_total == 1  # the zip, not the two inner files
    assert stats.archives_total == 1
    assert stats.archived_files == 2
    writes = [cmd for cmd in fake.commands if cmd[0] == "write"]
    assert len(writes) == 2  # header + one archive tape file
    assert "header_" in writes[0][3]
    staged = writes[1][3]
    assert staged != str(src)  # the staged zip, not the source root
    assert staged.endswith(".zip")

    session = repo.list_sessions(repo.list_tapes()[0].id)[0]
    files = repo.list_files(session.id)
    entry = files[1]
    assert entry.path == str(src)  # source-root identity in the catalog
    assert entry.kind == "archive"
    assert entry.inner_count == 2
    assert entry.manifest_sha
    assert entry.sha256  # zip digest

    # The BOP header read (tape identification) happened.
    assert any(cmd[0] == "read" for cmd in fake.commands)


async def test_archive_incremental_unchanged_skips_tape(tmp_path: Path) -> None:
    """An incremental run skips an archive whose content did not change."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_bytes(b"AAAA")

    repo = _make_repo()
    engine = BackupEngine(FakeItDtTransport(), repo, tmp_path / "work", EventBus())
    await engine.execute(
        _archive_definition(tmp_path, backup_type="incremental"),
        BackupStats("testjob"),
    )
    assert len(repo.list_sessions(repo.list_tapes()[0].id)) == 1

    stats = BackupStats("testjob")
    await engine.execute(
        _archive_definition(tmp_path, backup_type="incremental"),
        stats,
    )
    assert stats.files_total == 0
    assert len(repo.list_sessions(repo.list_tapes()[0].id)) == 1


async def test_archive_incremental_changed_rewrites(tmp_path: Path) -> None:
    """Changed archive content is rebuilt and written as a new session."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_bytes(b"AAAA")

    repo = _make_repo()
    engine = BackupEngine(FakeItDtTransport(), repo, tmp_path / "work", EventBus())
    await engine.execute(
        _archive_definition(tmp_path, backup_type="incremental"),
        BackupStats("testjob"),
    )

    (src / "a.txt").write_bytes(b"CHANGED")
    # Run 2 on a fake whose BOP read returns the existing tape's header,
    # so the engine matches tape 1 instead of registering a new one.
    tape_uuid = repo.list_tapes()[0].uuid
    fake2 = FakeItDtTransport(
        tape_files=[
            serialize_header(
                build_session_header(
                    tape_id=tape_uuid,
                    session_id=new_session_id(),
                    started_at=datetime.now(UTC),
                )
            )
        ]
    )
    engine2 = BackupEngine(fake2, repo, tmp_path / "work", EventBus())
    stats = BackupStats("testjob")
    await engine2.execute(
        _archive_definition(tmp_path, backup_type="incremental"),
        stats,
    )
    tape = repo.list_tapes()[0]
    sessions = repo.list_sessions(tape.id)
    assert len(sessions) == 2
    written = repo.list_files(sessions[1].id)
    assert written[1].kind == "archive"
    assert written[1].manifest_sha

    versions = repo.latest_archive_versions("testjob")
    assert str(src) in versions
    assert versions[str(src)][1] == written[1].manifest_sha


async def test_mixed_sources_write_plain_and_archive(tmp_path: Path) -> None:
    """Plain sources write per-file; archive sources one zip; both cataloged."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_bytes(b"AAAA")
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "p.txt").write_bytes(b"P")

    repo = _make_repo()
    fake = FakeItDtTransport()
    engine = BackupEngine(fake, repo, tmp_path / "work", EventBus())
    stats = BackupStats("testjob")
    await engine.execute(_archive_definition(tmp_path, extra_plain=True), stats)

    assert stats.files_total == 2  # 1 plain file + 1 archive zip
    assert stats.archives_total == 1
    assert stats.archived_files == 1  # the archive source holds a.txt only
    writes = [cmd for cmd in fake.commands if cmd[0] == "write"]
    assert len(writes) == 3  # header + plain file + archive zip
    session = repo.list_sessions(repo.list_tapes()[0].id)[0]
    files = repo.list_files(session.id)
    kinds = {entry.path: entry.kind for entry in files}
    assert kinds[str(plain / "p.txt")] == "file"
    assert kinds[str(src)] == "archive"


async def test_archive_missing_source_continues_on_error(tmp_path: Path) -> None:
    """A missing archive source is skipped when continue_on_error=True."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_bytes(b"AAAA")

    repo = _make_repo()
    fake = FakeItDtTransport()
    engine = BackupEngine(fake, repo, tmp_path / "work", EventBus())
    stats = BackupStats("testjob")
    await engine.execute(
        _archive_definition(tmp_path, extra_archive=str(tmp_path / "nonexistent")),
        stats,
    )

    assert stats.state == "writing"  # completed without raising
    assert stats.archives_total == 1  # only the existing source planned


async def test_archive_missing_source_aborts_without_continue(
    tmp_path: Path,
) -> None:
    """A missing archive source raises when continue_on_error=False."""
    import re

    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_bytes(b"AAAA")

    repo = _make_repo()
    fake = FakeItDtTransport()
    engine = BackupEngine(fake, repo, tmp_path / "work", EventBus())

    missing_path = str(tmp_path / "nonexistent")
    with pytest.raises(BackupEngineError, match=re.escape(missing_path)):
        await engine.execute(
            JobDefinition.model_validate(
                {
                    "name": "testjob",
                    "sources": [
                        {"path": str(tmp_path / "src"), "archive": True},
                        {"path": missing_path, "archive": True},
                    ],
                    "options": {
                        "mode": "append",
                        "verify": "none",
                        "continue_on_error": False,
                        "backup_type": "full",
                        "full_every_days": 30,
                    },
                }
            ),
            BackupStats("testjob"),
        )

    # No session was written because planning failed before tape I/O.
    assert len(repo.list_tapes()) == 0


def test_looks_like_eom_word_boundaries() -> None:
    """Only explicit end-of-medium wording classifies as tape-full.

    A substring match also fired inside unrelated media-error text,
    misreporting a failing write on a worn cartridge as "tape full"
    (v0.9.0 incident report); ``eom`` must match as a whole word only.
    """
    from tapevaultui.services.backup_engine import _looks_like_eom

    assert _looks_like_eom("Error: end of data detected")
    assert _looks_like_eom("Error: End of Medium detected")
    assert _looks_like_eom("Error: EOM detected")
    assert _looks_like_eom("Early warning zone reached")
    assert not _looks_like_eom("Write failed: medium error 0x31090002")
    assert not _looks_like_eom("Write failed: pseudoeom garbage")


# -- archive zip lifecycle ---------------------------------------------------


async def test_archive_zip_removed_from_work_dir_after_write(tmp_path: Path) -> None:
    """Archive zips are deleted from the work dir after a successful write.

    The engine builds a fresh ``archive_*.zip`` during planning, writes it
    to tape via ``write -raw -s <staging_path>``, then calls
    ``_discard_staged_zip`` on success. After execute returns the work dir
    must contain no archive zips.
    """
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_bytes(b"AAAA")

    repo = _make_repo()
    fake = FakeItDtTransport()
    engine = BackupEngine(fake, repo, tmp_path / "work", EventBus())
    stats = BackupStats("testjob")
    await engine.execute(_archive_definition(tmp_path), stats)

    # The write command targeted a staging path matching archive_*.zip.
    writes = [cmd for cmd in fake.commands if cmd[0] == "write"]
    assert any(w[3].endswith(".zip") for w in writes)
    # After execute returns no archive zip survives in the work dir.
    zips = list((tmp_path / "work").glob("archive_*.zip"))
    assert zips == [], f"expected no archive zips, found {[p.name for p in zips]}"


async def test_archive_zip_removed_after_damaged_continue(tmp_path: Path) -> None:
    """A failed archive write with continue_on_error deletes the zip.

    When the ITDT ``write`` command returns a non-zero exit code and
    ``continue_on_error=True``, the engine marks the file damaged, calls
    ``_discard_staged_zip(file)``, and continues to the next file. The
    archive zip must not survive in the work dir.
    """
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_bytes(b"AAAA")

    repo = _make_repo()
    fake = FakeItDtTransport()
    engine = BackupEngine(fake, repo, tmp_path / "work", EventBus())
    stats = BackupStats("testjob")
    await engine.execute(_archive_definition(tmp_path), stats)

    # Identify the archive write command to build a failing scenario.
    writes = [cmd for cmd in fake.commands if cmd[0] == "write"]
    archive_write = next(w for w in writes if w[3].endswith(".zip"))
    archive_key = tuple(archive_write)

    # Run again with the archive write failing (full run so zip is rebuilt).
    fake2 = FakeItDtTransport(
        command_scenarios={archive_key: ("Write failed: medium error", 1)},
    )
    engine2 = BackupEngine(fake2, repo, tmp_path / "work", EventBus())
    await engine2.execute(
        _archive_definition(tmp_path, backup_type="full"),
        BackupStats("testjob"),
    )

    # Catalog shows the archive as damaged.
    tape = repo.list_tapes()[-1]  # latest tape from second execute
    sessions = repo.list_sessions(tape.id)
    files = repo.list_files(sessions[-1].id)  # latest session on that tape
    archive_entry = next(f for f in files if f.kind == "archive")
    assert archive_entry.status == "damaged"
    # No archive zip left in the work dir.
    zips = list((tmp_path / "work").glob("archive_*.zip"))
    assert zips == [], f"expected no archive zips, found {[p.name for p in zips]}"


async def test_archive_zip_kept_across_tape_full_then_written(
    tmp_path: Path,
) -> None:
    """The archive zip survives EOM and is deleted after retry succeeds.

    When an archive write hits end-of-medium the engine calls
    ``wait_for_media`` and returns from ``_write_tape_pass`` **without**
    calling ``_discard_staged_zip``, so the cut-off file keeps its zip
    and is retried on the next tape within the same execute. The callback
    must see the zip, then a subsequent write succeeds and the zip is
    finally deleted.
    """
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_bytes(b"AAAA")

    repo = _make_repo()
    fake = FakeItDtTransport()
    engine = BackupEngine(fake, repo, tmp_path / "work", EventBus())
    stats = BackupStats("testjob")
    await engine.execute(_archive_definition(tmp_path), stats)

    # Identify the archive write command to build an EOM scenario.
    writes = [cmd for cmd in fake.commands if cmd[0] == "write"]
    archive_write = next(w for w in writes if w[3].endswith(".zip"))
    archive_key = tuple(archive_write)

    call_count = 0

    fake2 = FakeItDtTransport(
        command_scenarios={
            archive_key: ("Error: end of data detected", 1),
        },
    )

    async def wait_for_media() -> bool:
        nonlocal call_count
        call_count += 1
        work_dir = tmp_path / "work"
        if call_count == 1:
            # First wait: the archive zip must still exist (retention point).
            zips = list(work_dir.glob("archive_*.zip"))
            assert zips, "archive zip must survive EOM until next write"
            # Clear fake2's scenarios so the retry succeeds with default response.
            fake2.command_scenarios.clear()
            return True
        return False

    engine2 = BackupEngine(fake2, repo, tmp_path / "work", EventBus())
    await engine2.execute(
        _archive_definition(tmp_path, backup_type="full"),
        BackupStats("testjob"),
        wait_for_media=wait_for_media,
    )

    # The archive was eventually written successfully.
    tape = repo.list_tapes()[-1]  # latest tape from second execute
    sessions = repo.list_sessions(tape.id)
    files = repo.list_files(sessions[-1].id)  # latest session on that tape
    archive_entry = next(f for f in files if f.kind == "archive")
    assert archive_entry.status == "ok"
    # No archive zip left after the successful retry write.
    zips = list((tmp_path / "work").glob("archive_*.zip"))
    assert zips == [], f"expected no archive zips, found {[p.name for p in zips]}"


async def test_verify_buffer_deleted_when_hashing_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The verify read-back buffer is deleted even when sha256_of raises.

    With the default ``direct_reader_factory=None`` the engine uses the
    legacy ITDT fallback path: it reads into ``verify_<index>.bin``, calls
    ``sha256_of(dest)`` via ``asyncio.to_thread``, then unlinks ``dest`` in a
    ``finally`` block. Patching ``sha256_of`` to raise must not leave the
    buffer behind.
    """
    content = b"verify-buffer-test-content"
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_bytes(content)

    # Fake read returns the same content so the ITDT fallback read succeeds.
    fake = FakeItDtTransport(scenarios={"read": (content.decode(), 0)})
    repo = _make_repo()
    engine = BackupEngine(fake, repo, tmp_path / "work", EventBus())

    def _sha256_raises(_path: Path) -> str:
        buf = list((tmp_path / "work").glob("verify_*.bin"))
        assert buf, "verify buffer must exist when sha256_of is called"
        raise RuntimeError("hashing failed")

    # Patch the module-level attribute used by _verify_pass ITDT branch.
    from tapevaultui.services import backup_engine as be_mod

    monkeypatch.setattr(be_mod, "sha256_of", _sha256_raises)

    stats = BackupStats("testjob")
    with pytest.raises(RuntimeError, match="hashing failed"):
        await engine.execute(
            # Force full run and readback verify so sha256_of is called.
            _archive_definition(tmp_path, backup_type="full", verify="readback"),
            stats,
        )

    # The finally block must have deleted the buffer.
    bufs = list((tmp_path / "work").glob("verify_*.bin"))
    assert bufs == [], f"expected no verify buffers, found {[p.name for p in bufs]}"
