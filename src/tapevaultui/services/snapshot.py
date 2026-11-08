"""
File:   snapshot.py
Brief:  Source file snapshotting: walk, pattern filter, sizes and sha256.
Author: Mistress-Lukutar
Date:   2026-11-08
Version: v0.8.0
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from fnmatch import fnmatch
from pathlib import Path
from typing import Literal

import pathspec
import structlog
from pydantic import BaseModel, Field

from tapevaultui.services.job_defs import SourceDef

logger = structlog.get_logger(__name__)

READ_CHUNK = 4 * 1024 * 1024

# Size+mtime equality tolerance for incremental baselines: absorbs the
# float -> datetime -> float round-trip and coarse filesystem timestamps.
MTIME_TOLERANCE_S = 2.0


class PlannedFile(BaseModel):
    """One source file selected for backup.

    Plain files carry the source file path. Archive sources produce a
    single entry per source: ``path`` stays the source root (the
    catalog/header identity), ``staging_path`` points at the cached zip
    that is actually written to tape, and ``kind`` is "archive".
    """

    path: str = Field(description="Absolute source path (archive: source root)")
    size: int
    mtime: float
    sha256: str = Field(description="Hex sha256 of the content")
    kind: Literal["file", "archive"] = "file"
    staging_path: str | None = Field(
        default=None,
        description="Local file written to tape when it differs from path",
    )
    inner_count: int = Field(default=0, description="Files inside an archive")
    manifest_sha: str = Field(
        default="", description="Archive content manifest hash (archives only)"
    )


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


def _iter_unignored(
    root: Path, spec: pathspec.GitIgnoreSpec | None
) -> Iterator[Path]:
    """Yield files under root, pruning directories the spec ignores.

    Args:
        root: Source root directory.
        spec: Compiled gitignore spec, or None for no exclusions.

    Yields:
        File paths not matched by the ignore spec. Ignored directories
        are skipped entirely, so negated (``!``) rules cannot re-include
        files beneath an ignored parent (same as real gitignore).
    """
    stack = [root]
    while stack:
        directory = stack.pop()
        for entry in sorted(directory.iterdir()):
            if spec is not None:
                relative = entry.relative_to(root).as_posix()
                probe = f"{relative}/" if entry.is_dir() else relative
                if spec.match_file(probe):
                    continue
            if entry.is_dir():
                stack.append(entry)
            else:
                yield entry


def snapshot_sources(
    sources: list[SourceDef],
    *,
    continue_on_error: bool = True,
    baseline: dict[str, tuple[int, float]] | None = None,
) -> list[PlannedFile]:
    """Walk all sources and build the file plan with hashes.

    Each source may carry gitignore-style ``excludes`` patterns; ignored
    directories are pruned whole. When a baseline is given (incremental
    runs), files whose size and mtime match the baseline are dropped
    before hashing — they cost no hash I/O. Files are deduplicated by
    absolute path; hashing failures either skip the file
    (continue_on_error) or abort the snapshot.

    Args:
        sources: Source definitions from the job.
        continue_on_error: Skip unreadable files instead of failing.
        baseline: Mapping absolute path -> (size, mtime epoch) of the
            last backed-up version; matching files are not re-planned.

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
        spec = (
            pathspec.GitIgnoreSpec.from_lines(source.excludes)
            if source.excludes
            else None
        )
        for file_path in sorted(_iter_unignored(root, spec)):
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
            except OSError as exc:
                if continue_on_error:
                    logger.warning(
                        "snapshot_file_skipped", path=str(file_path), error=str(exc)
                    )
                    continue
                raise SnapshotError(f"Cannot stat {file_path}: {exc}") from exc
            if baseline is not None:
                known = baseline.get(key)
                if (
                    known is not None
                    and known[0] == stat.st_size
                    and abs(known[1] - stat.st_mtime) <= MTIME_TOLERANCE_S
                ):
                    continue
            try:
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
