"""
File:   snapshot.py
Brief:  Source file snapshotting: walk, pattern filter, sizes and sha256.
Author: Mistress-Lukutar
Date:   2026-11-06
Version: v0.5.0
"""

from __future__ import annotations

import hashlib
from fnmatch import fnmatch
from pathlib import Path

import structlog
from pydantic import BaseModel, Field

from tapevaultui.services.job_defs import SourceDef

logger = structlog.get_logger(__name__)

READ_CHUNK = 4 * 1024 * 1024


class PlannedFile(BaseModel):
    """One source file selected for backup."""

    path: str = Field(description="Absolute source path")
    size: int
    mtime: float
    sha256: str = Field(description="Hex sha256 of the content")


class SnapshotError(RuntimeError):
    """Raised when a source cannot be snapshotted."""


def sha256_of(path: Path) -> str:
    """Compute the sha256 hex digest of a file.

    Args:
        path: File to hash.

    Returns:
        Lowercase hex digest.

    Raises:
        SnapshotError: If the file cannot be read.
    """
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(READ_CHUNK):
                digest.update(chunk)
    except OSError as exc:
        raise SnapshotError(f"Cannot read {path}: {exc}") from exc
    return digest.hexdigest()


def _matches_any(name: str, patterns: list[str]) -> bool:
    """Check a relative posix path against include patterns.

    ``**/`` is optional: a pattern like ``**/*.jpg`` matches ``a.jpg`` in
    the root as well as ``sub/deep/b.jpg`` (spec example semantics).

    Args:
        name: Relative path in posix form.
        patterns: glob-style patterns (matched on the full relative path).

    Returns:
        True if the file matches at least one pattern.
    """
    for pattern in patterns:
        if fnmatch(name, pattern):
            return True
        if pattern.startswith("**/") and fnmatch(name, pattern[3:]):
            return True
    return False


def snapshot_sources(
    sources: list[SourceDef],
    *,
    continue_on_error: bool = True,
) -> list[PlannedFile]:
    """Walk all sources and build the file plan with hashes.

    Files are deduplicated by absolute path; hashing failures either skip
    the file (continue_on_error) or abort the snapshot.

    Args:
        sources: Source definitions from the job.
        continue_on_error: Skip unreadable files instead of failing.

    Returns:
        Planned files sorted by path.

    Raises:
        SnapshotError: If a source root is missing or a file is unreadable
            while continue_on_error is False.
    """
    planned: dict[str, PlannedFile] = {}
    for source in sources:
        root = Path(source.path)
        if not root.is_dir():
            message = f"Source directory does not exist: {root}"
            if continue_on_error:
                logger.warning("snapshot_source_missing", path=str(root))
                continue
            raise SnapshotError(message)
        for file_path in sorted(root.rglob("*")):
            if not file_path.is_file():
                continue
            relative = file_path.relative_to(root).as_posix()
            if source.patterns and not _matches_any(relative, source.patterns):
                continue
            key = str(file_path.resolve())
            if key in planned:
                continue
            try:
                stat = file_path.stat()
                digest = sha256_of(file_path)
            except (OSError, SnapshotError) as exc:
                if continue_on_error:
                    logger.warning(
                        "snapshot_file_skipped", path=str(file_path), error=str(exc)
                    )
                    continue
                raise
            planned[key] = PlannedFile(
                path=str(file_path),
                size=stat.st_size,
                mtime=stat.st_mtime,
                sha256=digest,
            )
    return sorted(planned.values(), key=lambda item: item.path)
