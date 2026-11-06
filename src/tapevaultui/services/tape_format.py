"""
File:   tape_format.py
Brief:  Tape format protocol: session header (t0) serialization/parsing.
Author: Mistress-Lukutar
Date:   2026-11-06
Version: v0.6.0
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator

from tapevaultui import __version__

# Bump when the on-tape protocol changes in an incompatible way.
FORMAT_VERSION = 1


class HeaderFileEntry(BaseModel):
    """One source file entry in a session header."""

    seq: int = Field(ge=0, description="Order of the file within the session")
    path: str = Field(description="Source file path")
    size: int = Field(ge=0, description="File size in bytes")
    mtime: float = Field(description="Source modification time (unix seconds)")
    sha256: str = Field(description="Hex sha256 of the file content")
    tape_file_index: int = Field(ge=0, description="Tape file index (tN)")


class SessionHeader(BaseModel):
    """The t0 session header written as the first tape file of a session."""

    format_version: int = Field(description="On-tape format version")
    app_version: str = Field(description="TapeWebUI version that wrote the session")
    tape_id: str = Field(description="Tape identity UUID (assigned on first write)")
    tape_label: str = Field(default="", description="Human-readable tape label")
    session_id: str = Field(description="Session UUID")
    job_name: str = Field(default="", description="Job name snapshot")
    started_at: str = Field(description="Session start (ISO-8601 UTC)")
    files: list[HeaderFileEntry] = Field(default_factory=list)

    @field_validator("format_version")
    @classmethod
    def _check_format_version(cls, value: int) -> int:
        """Reject headers written in an unknown format.

        Args:
            value: Parsed format version.

        Returns:
            The validated version.

        Raises:
            ValueError: If the version is newer than supported.
        """
        if value > FORMAT_VERSION:
            raise ValueError(
                f"Session header format v{value} is newer than "
                f"supported v{FORMAT_VERSION}"
            )
        return value


class TapeFormatError(ValueError):
    """Raised when t0 data cannot be parsed as a TapeWebUI session header."""


def build_session_header(
    *,
    tape_id: str,
    session_id: str,
    job_name: str = "",
    tape_label: str = "",
    started_at: datetime,
    files: list[HeaderFileEntry] | None = None,
) -> SessionHeader:
    """Build a session header with current format and app versions.

    Args:
        tape_id: Tape identity UUID.
        session_id: Session UUID.
        job_name: Job name snapshot.
        tape_label: Human-readable tape label.
        started_at: Session start time (UTC).
        files: Source file entries.

    Returns:
        A ready-to-serialize SessionHeader.
    """
    return SessionHeader(
        format_version=FORMAT_VERSION,
        app_version=__version__,
        tape_id=tape_id,
        tape_label=tape_label,
        session_id=session_id,
        job_name=job_name,
        started_at=started_at.isoformat(),
        files=files or [],
    )


def serialize_header(header: SessionHeader) -> bytes:
    """Serialize a session header to on-tape bytes.

    Args:
        header: The header to serialize.

    Returns:
        UTF-8 JSON document (compact, trailing newline).
    """
    payload = header.model_dump(mode="json")
    return (json.dumps(payload, indent=2) + "\n").encode("utf-8")


def parse_header(raw: bytes) -> SessionHeader | None:
    """Parse on-tape bytes into a session header.

    A tape that was not written by TapeWebUI (garbage, foreign format, or
    a failed parse) yields None — the caller reports a foreign tape.

    Args:
        raw: Raw content of tape file t0.

    Returns:
        Parsed SessionHeader, or None if the data is not a valid header.

    Raises:
        TapeFormatError: If the data is a header of a newer, incompatible
            format version (distinct from a foreign tape).
    """
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    try:
        payload: dict[str, Any] = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict) or "format_version" not in payload:
        return None
    try:
        header = SessionHeader.model_validate(payload)
    except ValueError as exc:
        message = str(exc)
        if "newer than supported" in message:
            raise TapeFormatError(message) from exc
        return None
    return header
