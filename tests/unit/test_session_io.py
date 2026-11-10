"""
File:   test_session_io.py
Brief:  Session header I/O helper tests on the fake transport.
Author: Mistress-Lukutar
Date:   2026-11-10
Version: v0.9.2
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import ValidationError

from tapevaultui.infrastructure.itdt_transport import (
    FakeItDtTransport,
    ItDtResult,
)
from tapevaultui.services.session_io import (
    NoMediaError,
    SessionIoError,
    erase_tape_short,
    identify_tape,
    new_session_id,
    read_header_at_current_position,
    read_session_header,
    space_forward_files,
    space_to_end_of_data,
    timeout_for_size,
    write_filemarks,
    write_session_header,
)
from tapevaultui.services.tape_format import (
    HeaderFileEntry,
    SessionHeader,
    build_session_header,
    parse_header,
    serialize_header,
)


def _header() -> SessionHeader:
    """Build a representative session header.

    Returns:
        A SessionHeader instance.
    """
    return build_session_header(
        tape_id="tape-1",
        session_id=new_session_id(),
        job_name="photos",
        started_at=datetime.now(UTC),
    )


# Real ITDT output observed on this machine for a read at BOP of a
# never-written cartridge (blank-check sense, errno 1117, zero records).
BLANK_READ_OUTPUT = (
    "Initializing device...\n"
    "Read Exerciser...\n"
    "Reading file from tape...\n"
    "Read complete, 0 records 0 total bytes read...\n"
    "Operation failed with errno 1117: The request could not be performed "
    "because of an I/O device error\n"
    "Exit with code: 1117"
)

# Real ITDT output observed for a rewind with the cartridge ejected but
# loadable (sense key 0x02 not-ready, ASC/ASCQ 04/02).
NO_MEDIA_REWIND_OUTPUT = (
    "Rewinding tape...\n"
    "Operation FAILED with errno 2 The system cannot find the file specified"
)


class CapturingFakeItDtTransport(FakeItDtTransport):
    """Fake transport that snapshots bytes written via write -raw -s.

    Records the content of every temp file passed to a ``write -raw -s``
    command in ``captured_write_bytes`` so tests can assert on the exact
    payload without needing the file after cleanup.
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.captured_write_bytes: list[bytes] = []  # type: ignore[assignment]

    async def run(self, args: list[str], timeout_s: int | None = None) -> ItDtResult:
        """Capture bytes from write -raw -s before delegating."""
        if (
            len(args) >= 4
            and args[0] == "write"
            and "-raw" in args
            and "-s" in args
        ):
            idx = args.index("-s")
            path = Path(args[idx + 1])
            self.captured_write_bytes.append(path.read_bytes())
        return await super().run(args, timeout_s)


async def test_write_header_issues_single_write_command(tmp_path: Path) -> None:
    """Writing the header issues one write -raw followed by a filemark."""
    header = _header()
    fake = CapturingFakeItDtTransport()
    await write_session_header(fake, header, tmp_path)

    assert [cmd[0] for cmd in fake.commands] == ["write", "weof"]
    command = fake.commands[0]
    assert command[:3] == ("write", "-raw", "-s")

    # Exactly one write -raw was issued and its payload matches.
    assert len(fake.captured_write_bytes) == 1
    snapshotted = fake.captured_write_bytes[0]
    assert snapshotted == serialize_header(header)

    # The temp file is cleaned up after the call (success path).
    header_file = Path(command[3])
    assert not header_file.exists()

    parsed = parse_header(snapshotted)
    assert parsed is not None
    assert parsed.tape_id == "tape-1"
    assert fake.commands[1] == ("weof", "1")


async def test_write_header_nonzero_exit_deletes_temp(tmp_path: Path) -> None:
    """A nonzero write exit raises SessionIoError and the temp file is deleted."""
    header = _header()
    fake = CapturingFakeItDtTransport(
        scenarios={"write": ("Write failed", 1)},
    )
    with pytest.raises(SessionIoError, match="Write failed"):
        await write_session_header(fake, header, tmp_path)

    # The finally block removes the temp regardless of exit code.
    assert len(fake.captured_write_bytes) == 1
    for cmd in fake.commands:
        if cmd[0] == "write" and "-s" in cmd:
            header_file = Path(cmd[3])
            assert not header_file.exists()


async def test_read_session_header_rewind_raises_deletes_temp(
    tmp_path: Path,
) -> None:
    """A rewind failure during read_session_header raises and no t0_*.bin remains."""
    fake = FakeItDtTransport(scenarios={"rewind": (NO_MEDIA_REWIND_OUTPUT, 2)})

    with (
        patch(
            "tapevaultui.services.session_io.rewind_tape",
            new=AsyncMock(side_effect=NoMediaError("no medium")),
        ),
        pytest.raises(NoMediaError, match=r"no loaded medium|no medium"),
    ):
        await read_session_header(fake, tmp_path)

    # No t0_*.bin should survive the finally block.
    for f in tmp_path.glob("t0_*.bin"):
        raise AssertionError(f"Leftover temp file: {f}")


async def test_read_session_header_success_position_aware(tmp_path: Path) -> None:
    """A position-aware fake returns the header and cleans up the read temp."""
    header = _header()
    serialized = serialize_header(header)
    fake = FakeItDtTransport(tape_files=[serialized])

    parsed = await read_session_header(fake, tmp_path)
    assert parsed is not None
    assert parsed.tape_id == "tape-1"
    assert parsed.job_name == "photos"

    # Verify the command sequence includes rewind + read + rewind.
    cmd_names = [cmd[0] for cmd in fake.commands]
    assert cmd_names == ["rewind", "read", "rewind"]

    # No t0_*.bin leftover after successful read.
    for f in tmp_path.glob("t0_*.bin"):
        raise AssertionError(f"Leftover temp file: {f}")


async def test_read_header_at_current_position_blank_returns_none(
    tmp_path: Path,
) -> None:
    """A blank-tape read at current position returns None with no leftover."""
    fake = FakeItDtTransport(
        scenarios={"read": (BLANK_READ_OUTPUT, 1117)},
    )
    parsed = await read_header_at_current_position(fake, tmp_path)
    assert parsed is None

    # No hdr_*.bin left after the call.
    for f in tmp_path.glob("hdr_*.bin"):
        raise AssertionError(f"Leftover temp file: {f}")


async def test_read_header_at_current_position_success_no_leftover(
    tmp_path: Path,
) -> None:
    """A successful read at current position returns the header with no leftover."""
    header = _header()
    serialized = serialize_header(header)
    fake = FakeItDtTransport(scenarios={"read": (serialized.decode("utf-8"), 0)})

    parsed = await read_header_at_current_position(fake, tmp_path)
    assert parsed is not None
    assert parsed.tape_id == "tape-1"

    # No hdr_*.bin leftover.
    for f in tmp_path.glob("hdr_*.bin"):
        raise AssertionError(f"Leftover temp file: {f}")


async def test_write_filemarks_issues_weof() -> None:
    """write_filemarks issues weof with the requested count."""
    fake = FakeItDtTransport()
    await write_filemarks(fake, 2)
    assert fake.commands == [("weof", "2")]


async def test_write_filemarks_failure_raises() -> None:
    """A failed weof raises SessionIoError with the ITDT output."""
    fake = FakeItDtTransport(scenarios={"weof": ("Filemark failed", 1)})
    with pytest.raises(SessionIoError, match="Filemark failed"):
        await write_filemarks(fake)


async def test_read_header_roundtrip_on_fake(tmp_path: Path) -> None:
    """A tape whose t0 holds our header is parsed back correctly."""
    header = _header()
    fake = FakeItDtTransport(
        scenarios={"read": (serialize_header(header).decode("utf-8"), 0)}
    )
    parsed = await read_session_header(fake, tmp_path)
    assert parsed is not None
    assert parsed.tape_id == "tape-1"
    assert parsed.job_name == "photos"
    assert [cmd[0] for cmd in fake.commands] == ["rewind", "read", "rewind"]


async def test_read_header_foreign_tape_returns_none(tmp_path: Path) -> None:
    """A tape with garbage at t0 is reported as foreign (None)."""
    fake = FakeItDtTransport(scenarios={"read": ("\x00garbage not json", 0)})
    parsed = await read_session_header(fake, tmp_path)
    assert parsed is None


async def test_read_header_read_failure_raises(tmp_path: Path) -> None:
    """A hard ITDT read failure raises SessionIoError."""
    fake = FakeItDtTransport(
        scenarios={"read": ("Boom", 1)},
    )
    with pytest.raises(SessionIoError, match="Boom"):
        await read_session_header(fake, tmp_path)


async def test_read_header_rewinds_after_read_failure(tmp_path: Path) -> None:
    """Even after a failed read the tape is left rewound."""
    fake = FakeItDtTransport(scenarios={"read": ("Boom", 1)})
    with pytest.raises(SessionIoError):
        await read_session_header(fake, tmp_path)
    assert fake.commands[-1] == ("rewind",)


async def test_read_header_blank_tape_returns_none(tmp_path: Path) -> None:
    """A never-written tape (blank-check read failure) yields None."""
    fake = FakeItDtTransport(scenarios={"read": (BLANK_READ_OUTPUT, 1117)})
    parsed = await read_session_header(fake, tmp_path)
    assert parsed is None
    assert fake.commands[-1] == ("rewind",)  # left rewound after failure


async def test_rewind_without_media_raises_no_media_error(
    tmp_path: Path,
) -> None:
    """A rewind failure with the cartridge ejected raises NoMediaError."""
    fake = FakeItDtTransport(scenarios={"rewind": (NO_MEDIA_REWIND_OUTPUT, 2)})
    with pytest.raises(NoMediaError, match="no loaded medium"):
        await read_session_header(fake, tmp_path)


def test_no_media_error_is_session_io_error() -> None:
    """NoMediaError stays catchable as a plain SessionIoError."""
    assert issubclass(NoMediaError, SessionIoError)


async def test_positioning_and_erase_helpers(tmp_path: Path) -> None:
    """seod/fsf/erase/identify issue the expected command sequences."""
    fake = FakeItDtTransport()
    await space_to_end_of_data(fake)
    await space_forward_files(fake, 3)
    await erase_tape_short(fake)
    assert [cmd[0] for cmd in fake.commands] == ["seod", "fsf", "rewind", "erase"]
    assert fake.commands[1] == ("fsf", "3")
    assert fake.commands[3] == ("erase", "-short")


async def test_identify_tape_reads_header(tmp_path: Path) -> None:
    """identify_tape rewinds, reads t0, rewinds, and parses the header."""
    header = _header()
    fake = FakeItDtTransport(
        scenarios={"read": (serialize_header(header).decode("utf-8"), 0)}
    )
    identified = await identify_tape(fake, tmp_path)
    assert identified is not None
    assert identified.tape_id == "tape-1"


async def test_failure_of_fsf_raises() -> None:
    """A failed fsf raises SessionIoError with the ITDT output."""
    fake = FakeItDtTransport(scenarios={"fsf": ("Beyond EOM", 1)})
    with pytest.raises(SessionIoError, match="Beyond EOM"):
        await space_forward_files(fake, 1)


def test_header_file_entry_validation() -> None:
    """Negative sizes and indexes are rejected by the schema."""
    with pytest.raises(ValidationError):
        HeaderFileEntry(
            seq=0, path="x", size=-1, mtime=0.0, sha256="0" * 64, tape_file_index=0
        )


def test_timeout_for_size_scales_with_payload() -> None:
    """Small payloads keep the flat default; large ones get extra budget."""
    base = 600
    assert timeout_for_size(0, base) == base  # nothing to transfer
    assert timeout_for_size(1024, base) == base  # far below the estimate
    # 10 MiB at 5 MiB/s = 2 s transfer + 120 s slack, still under base.
    assert timeout_for_size(10 * 1024 * 1024, base) == base
    # 10 GiB at 5 MiB/s = 2048 s transfer + 120 s slack.
    assert timeout_for_size(10 * 1024 * 1024 * 1024, base) == 2168
    # A custom base above the estimate is kept.
    assert timeout_for_size(1024, 3000) == 3000
