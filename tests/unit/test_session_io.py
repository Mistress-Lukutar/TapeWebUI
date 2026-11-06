"""
File:   test_session_io.py
Brief:  Session header I/O helper tests on the fake transport.
Author: Mistress-Lukutar
Date:   2026-11-06
Version: v0.5.3
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from tapevaultui.infrastructure.itdt_transport import FakeItDtTransport
from tapevaultui.services.session_io import (
    NoMediaError,
    SessionIoError,
    erase_tape_short,
    identify_tape,
    new_session_id,
    read_session_header,
    space_forward_files,
    space_to_end_of_data,
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


async def test_write_header_issues_single_write_command(tmp_path: Path) -> None:
    """Writing the header issues one write -raw followed by a filemark."""
    fake = FakeItDtTransport()
    await write_session_header(fake, _header(), tmp_path)  # type: ignore[arg-type]
    assert [cmd[0] for cmd in fake.commands] == ["write", "weof"]
    command = fake.commands[0]
    assert command[:3] == ("write", "-raw", "-s")
    header_file = Path(command[3])
    assert header_file.exists()
    parsed = parse_header(header_file.read_bytes())
    assert parsed is not None
    assert parsed.tape_id == "tape-1"
    assert fake.commands[1] == ("weof", "1")


async def test_write_filemarks_issues_weof() -> None:
    """write_filemarks issues weof with the requested count."""
    fake = FakeItDtTransport()
    await write_filemarks(fake, 2)  # type: ignore[arg-type]
    assert fake.commands == [("weof", "2")]


async def test_write_filemarks_failure_raises() -> None:
    """A failed weof raises SessionIoError with the ITDT output."""
    fake = FakeItDtTransport(scenarios={"weof": ("Filemark failed", 1)})
    with pytest.raises(SessionIoError, match="Filemark failed"):
        await write_filemarks(fake)  # type: ignore[arg-type]


async def test_read_header_roundtrip_on_fake(tmp_path: Path) -> None:
    """A tape whose t0 holds our header is parsed back correctly."""
    header = _header()
    fake = FakeItDtTransport(
        scenarios={"read": (serialize_header(header).decode("utf-8"), 0)}
    )
    parsed = await read_session_header(fake, tmp_path)  # type: ignore[arg-type]
    assert parsed is not None
    assert parsed.tape_id == "tape-1"
    assert parsed.job_name == "photos"
    assert [cmd[0] for cmd in fake.commands] == ["rewind", "read", "rewind"]


async def test_read_header_foreign_tape_returns_none(tmp_path: Path) -> None:
    """A tape with garbage at t0 is reported as foreign (None)."""
    fake = FakeItDtTransport(scenarios={"read": ("\x00garbage not json", 0)})
    parsed = await read_session_header(fake, tmp_path)  # type: ignore[arg-type]
    assert parsed is None


async def test_read_header_read_failure_raises(tmp_path: Path) -> None:
    """A hard ITDT read failure raises SessionIoError."""
    fake = FakeItDtTransport(
        scenarios={"read": ("Boom", 1)},
    )
    with pytest.raises(SessionIoError, match="Boom"):
        await read_session_header(fake, tmp_path)  # type: ignore[arg-type]


async def test_read_header_rewinds_after_read_failure(tmp_path: Path) -> None:
    """Even after a failed read the tape is left rewound."""
    fake = FakeItDtTransport(scenarios={"read": ("Boom", 1)})
    with pytest.raises(SessionIoError):
        await read_session_header(fake, tmp_path)  # type: ignore[arg-type]
    assert fake.commands[-1] == ("rewind",)


async def test_read_header_blank_tape_returns_none(tmp_path: Path) -> None:
    """A never-written tape (blank-check read failure) yields None."""
    fake = FakeItDtTransport(scenarios={"read": (BLANK_READ_OUTPUT, 1117)})
    parsed = await read_session_header(fake, tmp_path)  # type: ignore[arg-type]
    assert parsed is None
    assert fake.commands[-1] == ("rewind",)  # left rewound after failure


async def test_rewind_without_media_raises_no_media_error(
    tmp_path: Path,
) -> None:
    """A rewind failure with the cartridge ejected raises NoMediaError."""
    fake = FakeItDtTransport(scenarios={"rewind": (NO_MEDIA_REWIND_OUTPUT, 2)})
    with pytest.raises(NoMediaError, match="no loaded medium"):
        await read_session_header(fake, tmp_path)  # type: ignore[arg-type]


def test_no_media_error_is_session_io_error() -> None:
    """NoMediaError stays catchable as a plain SessionIoError."""
    assert issubclass(NoMediaError, SessionIoError)


async def test_positioning_and_erase_helpers(tmp_path: Path) -> None:
    """seod/fsf/erase/identify issue the expected command sequences."""
    fake = FakeItDtTransport()
    await space_to_end_of_data(fake)  # type: ignore[arg-type]
    await space_forward_files(fake, 3)  # type: ignore[arg-type]
    await erase_tape_short(fake)  # type: ignore[arg-type]
    assert [cmd[0] for cmd in fake.commands] == ["seod", "fsf", "rewind", "erase"]
    assert fake.commands[1] == ("fsf", "3")
    assert fake.commands[3] == ("erase", "-short")


async def test_identify_tape_reads_header(tmp_path: Path) -> None:
    """identify_tape rewinds, reads t0, rewinds, and parses the header."""
    header = _header()
    fake = FakeItDtTransport(
        scenarios={"read": (serialize_header(header).decode("utf-8"), 0)}
    )
    identified = await identify_tape(fake, tmp_path)  # type: ignore[arg-type]
    assert identified is not None
    assert identified.tape_id == "tape-1"


async def test_failure_of_fsf_raises() -> None:
    """A failed fsf raises SessionIoError with the ITDT output."""
    fake = FakeItDtTransport(scenarios={"fsf": ("Beyond EOM", 1)})
    with pytest.raises(SessionIoError, match="Beyond EOM"):
        await space_forward_files(fake, 1)  # type: ignore[arg-type]


def test_header_file_entry_validation() -> None:
    """Negative sizes and indexes are rejected by the schema."""
    with pytest.raises(ValidationError):
        HeaderFileEntry(
            seq=0, path="x", size=-1, mtime=0.0, sha256="0" * 64, tape_file_index=0
        )
