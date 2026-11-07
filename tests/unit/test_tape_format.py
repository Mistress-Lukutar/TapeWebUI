"""
File:   test_tape_format.py
Brief:  Unit tests for the tape session header (t0) format.
Author: Mistress-Lukutar
Date:   2026-11-07
Version: v0.7.0
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from tapevaultui.services.tape_format import (
    FORMAT_VERSION,
    HeaderFileEntry,
    SessionHeader,
    TapeFormatError,
    build_session_header,
    parse_header,
    serialize_header,
)


def _sample_header() -> SessionHeader:
    """Build a representative session header for tests.

    Returns:
        A SessionHeader with one file entry.
    """
    return build_session_header(
        tape_id="tape-uuid-1",
        session_id="session-uuid-1",
        job_name="photos",
        tape_label="Vault 1",
        started_at=datetime(2026, 11, 6, 12, 0, 0, tzinfo=UTC),
        files=[
            HeaderFileEntry(
                seq=0,
                path="D:/Photos/a.jpg",
                size=12345,
                mtime=1790000000.0,
                sha256="ab" * 32,
                tape_file_index=1,
            )
        ],
    )


def test_serialize_parse_roundtrip() -> None:
    """A serialized header parses back to the same content."""
    header = _sample_header()
    raw = serialize_header(header)
    parsed = parse_header(raw)
    assert parsed == header


def test_parse_current_format_version_accepted() -> None:
    """The current format version roundtrips and is stamped correctly."""
    header = _sample_header()
    assert header.format_version == FORMAT_VERSION


def test_parse_garbage_returns_none() -> None:
    """Non-JSON and foreign JSON both yield None (foreign tape)."""
    assert parse_header(b"\x00\x01\x02 not json") is None
    assert parse_header(b'{"something": "else"}') is None
    assert parse_header(b"") is None


def test_parse_newer_format_raises_tape_format_error() -> None:
    """A future format version raises TapeFormatError, not None."""
    payload = _sample_header().model_dump(mode="json")
    payload["format_version"] = FORMAT_VERSION + 1
    raw = __import__("json").dumps(payload).encode()
    with pytest.raises(TapeFormatError, match="newer than supported"):
        parse_header(raw)


def test_parse_invalid_fields_returns_none() -> None:
    """A header with invalid field types is treated as foreign data."""
    raw = b'{"format_version": 1, "files": "not-a-list"}'
    assert parse_header(raw) is None


def test_started_at_is_iso_string() -> None:
    """started_at serializes to an ISO-8601 string."""
    raw = serialize_header(_sample_header()).decode()
    assert '"started_at": "2026-11-06T12:00:00+00:00"' in raw
