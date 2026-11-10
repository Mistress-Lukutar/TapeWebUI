"""
File:   archive_builder.py
Brief:  Archive sources: per-run zip build, skip/rebuild, tape-safe extract.
Author: Mistress-Lukutar
Date:   2026-11-09
Version: v0.11.3
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import shutil
import time
import zipfile
from pathlib import Path
from typing import BinaryIO

import pathspec
import structlog

from tapevaultui.services.job_defs import SourceDef
from tapevaultui.services.snapshot import (
    READ_CHUNK,
    PlannedFile,
    SnapshotError,
    _iter_unignored,
    _matches_any,
    sha256_of,
)

logger = structlog.get_logger(__name__)

# Archive entries are stored uncompressed: the goal is collapsing the
# file count, not shrinking bytes (zero CPU cost, no tape-write slowdown).
ZIP_COMPRESSION = zipfile.ZIP_STORED
# Lowest/greatest seconds representable by zip's MS-DOS date fields
# (1980-01-01 .. 2107-12-31), used to clamp source mtimes.
ZIP_TIME_MIN = 315532800
ZIP_TIME_MAX = 4354819199


class ArchiveError(RuntimeError):
    """Raised when an archive cannot be planned, built, or extracted."""


class _HashingWriter:
    """O(1)-memory streaming SHA-256 hasher for zip file bytes.

    Wraps a raw ``BinaryIO`` and feeds every ``write()`` through
    ``hashlib.sha256.update()`` so the digest covers all bytes emitted
    by ``zipfile`` (local headers, data, central directory, EOCD).
    Hashing during the build write pass removes the second full read
    of the built zip — previously ``_build_zip`` wrote to a temp file,
    closed it, then re-read the entire file for ``sha256_of()``.

    ``tell()`` raises ``OSError``, which CPython 3.11 and 3.13's
    ``ZipFile.__init__`` both catch in the same branch:
    ``self.fp = _Tellable(self.fp); self.start_dir = 0; self._seekable = False``.
    (CPython 3.13.14 additionally probes ``seek()`` and also raises,
    but that is a no-op since ``_seekable`` is already ``False``.)

    The ``_Tellable`` proxy counts write lengths for tell(), so the
    write stream is purely sequential with data-descriptor layout
    (flag bit 0x8): local headers carry CRC/size placeholders and
    descriptors are appended after each entry's data. No seek-back
    rewrite occurs, so digest matches ``sha256_of(final file)`` exactly.

    Memory usage is O(1): a single ``hashlib.sha256`` context plus the
    raw stream buffer — no per-byte accumulation.

    ``close`` forwards to the underlying stream (zipfile calls it in its
    ``__exit__``).

    Attributes:
        hexdigest: SHA-256 hex string of all bytes written.
    """

    def __init__(self, raw: BinaryIO) -> None:
        """Wrap *raw* and initialise a fresh SHA-256 digest.

        Args:
            raw: Underlying binary stream receiving the zip bytes.
        """
        self._raw = raw
        self._digest = hashlib.sha256()

    def write(self, data: bytes) -> int:
        """Digest *data* and forward it to the wrapped stream.

        Args:
            data: Bytes to write through the wrapper.

        Returns:
            Number of bytes written (from the underlying stream).
        """
        self._digest.update(data)
        return self._raw.write(data)

    def tell(self) -> int:
        """Raise — triggers zipfile's non-seekable probe in ``__init__``.

        Returns:
            Never returns; raises ``OSError`` on every call.

        Raises:
            OSError: Always — detected by ``ZipFile.__init__`` which
                wraps the stream in ``_Tellable`` (data-descriptor mode).
        """
        raise OSError("HashingWriter is non-seekable")

    def seek(self, offset: int, whence: int = 0) -> int:
        """Raise — safety net; never reached in data-descriptor mode.

        Args:
            offset: Byte offset.
            whence: Seek mode (default 0 = absolute).

        Returns:
            New position in the underlying stream.

        Raises:
            OSError: Always.
        """
        raise OSError("HashingWriter is non-seekable")

    def seekable(self) -> bool:
        """Return ``False`` — documentation of non-seekable intent."""
        return False

    def flush(self) -> None:
        """Forward ``flush()`` to the wrapped stream."""
        self._raw.flush()

    def close(self) -> None:
        """Close the underlying raw stream (forwarded from zipfile)."""
        self._raw.close()

    @property
    def hexdigest(self) -> str:
        """SHA-256 hex digest of all bytes written through this wrapper.

        Returns:
            64-character lowercase hex string.
        """
        return self._digest.hexdigest()


def manifest_digest(entries: dict[str, tuple[int, float, str]]) -> str:
    """Hash the manifest content (path, size, mtime, sha256 per file).

    Args:
        entries: Mapping relative path -> (size, mtime, sha256).

    Returns:
        Hex sha256 identifying the exact source content.
    """
    digest = hashlib.sha256()
    for path in sorted(entries):
        size, mtime, file_hash = entries[path]
        digest.update(path.encode("utf-8"))
        digest.update(f"\0{size}\0{mtime!r}\0{file_hash}".encode())
    return digest.hexdigest()


def _source_slug(source_path: str) -> str:
    """Build a filesystem-safe unique cache name for a source path.

    Args:
        source_path: Absolute source directory path.

    Returns:
        ``<leaf>_<12 hex of sha256(path)>`` — distinct for two sources
        with the same leaf name on different roots.
    """
    leaf = re.sub(r"[^A-Za-z0-9_-]+", "_", Path(source_path).name) or "source"
    return f"{leaf}_{hashlib.sha256(source_path.encode('utf-8')).hexdigest()[:12]}"


def _walk_source(root: Path, source: SourceDef) -> dict[str, tuple[int, float, str]]:
    """Walk one archive source and hash every included file.

    Excludes/patterns behave exactly like the plain snapshot walk
    (gitignore-style pruning). Every file is fully read and hashed —
    silent bit-rot with unchanged size/mtime cannot slip through.

    Args:
        root: Source root directory.
        source: Source definition (excludes, patterns).

    Returns:
        Mapping relative posix path -> (size, mtime, sha256).
    """
    spec = (
        pathspec.GitIgnoreSpec.from_lines(source.excludes) if source.excludes else None
    )
    entries: dict[str, tuple[int, float, str]] = {}
    for file_path in sorted(_iter_unignored(root, spec)):
        if not file_path.is_file():
            continue
        relative = file_path.relative_to(root).as_posix()
        if source.patterns and not _matches_any(relative, source.patterns):
            continue
        try:
            stat = file_path.stat()
            file_hash = sha256_of(file_path)
        except (OSError, SnapshotError) as exc:
            # A file deleted (or locked) between listing and read — run 42
            # lost the whole C:/_Source archive to this race. Warn with the
            # path and skip; the next run's manifest picks the change up.
            logger.warning(
                "archive_file_skipped", path=str(file_path), error=repr(exc)
            )
            continue
        entries[relative] = (stat.st_size, stat.st_mtime, file_hash)
    return entries


def _zip_date_time(mtime: float) -> tuple[int, int, int, int, int, int]:
    """Convert a unix mtime into a zip date_time tuple (clamped).

    Args:
        mtime: Source modification time (unix seconds).

    Returns:
        (year, month, day, hour, minute, second) in local time.
    """
    ts = min(max(int(mtime), ZIP_TIME_MIN), ZIP_TIME_MAX)
    parts = time.localtime(ts)
    return (
        parts.tm_year,
        parts.tm_mon,
        parts.tm_mday,
        parts.tm_hour,
        parts.tm_min,
        min(parts.tm_sec, 59),
    )


def _build_zip(
    root: Path,
    entries: dict[str, tuple[int, float, str]],
    zip_path: Path,
) -> tuple[int, str]:
    """Write the staged zip for the given content set.

    Entries are written in sorted order with per-file mtimes restored
    (deterministic bytes for identical content). File bytes are STREAMED
    into the entry — a source may contain single files far larger than
    RAM (model blobs), so reading one into memory is not an option. The
    temp file is published atomically (os.replace), so nothing ever
    reads a partially written zip; a failed build deletes its partial
    temp file (the tmp sweeps remove crash leftovers).

    A ``_HashingWriter`` (non-seekable) wraps the output stream, forcing
    zipfile's data-descriptor layout (flag 0x8): CRC and sizes are written
    as placeholders in local headers then appended after each entry's data.
    The write stream is purely sequential so SHA-256 over every ``write()``
    equals ``sha256_of(final file)`` with O(1) memory. The digest is
    returned alongside the file size so callers avoid a second full read.

    Args:
        root: Source root directory the relative paths resolve against.
        entries: Mapping relative path -> (size, mtime, sha256).
        zip_path: Destination zip file path.

    Returns:
        Tuple of (zip file size in bytes, sha256 hex of the written zip).
    """
    tmp_path = zip_path.with_suffix(".zip.tmp")
    try:
        raw = tmp_path.open("wb")
        writer = _HashingWriter(raw)
        with zipfile.ZipFile(writer, "w", compression=ZIP_COMPRESSION) as zf:
            for relative in sorted(entries):
                size, mtime, _sha = entries[relative]
                info = zipfile.ZipInfo(
                    filename=relative, date_time=_zip_date_time(mtime)
                )
                info.external_attr = 0o644 << 16
                # Publish the manifest size BEFORE opening the entry:
                # zf.open("w") sizes the local header up front and only
                # enables Zip64 when file_size predicts an overflow —
                # otherwise closing an entry whose data exceeds 4 GiB
                # raises "File size too large" (writestr, the previous
                # whole-in-RAM writer, derived this from the buffer).
                info.file_size = size
                with (
                    (root / relative).open("rb") as handle,
                    zf.open(info, mode="w") as entry,
                ):
                    shutil.copyfileobj(handle, entry, length=16 * 1024 * 1024)
        raw.close()
        os.replace(tmp_path, zip_path)
    except BaseException:
        with contextlib.suppress(Exception):
            raw.close()
        tmp_path.unlink(missing_ok=True)
        raise
    return (zip_path.stat().st_size, writer.hexdigest)


def plan_archive_source(
    source: SourceDef,
    *,
    work_dir: Path,
    written_manifest_sha: str | None = None,
    write_even_if_unchanged: bool = False,
) -> PlannedFile | None:
    """Plan one archive source, building its zip fresh into work_dir.

    Hash-all semantics: every included file is read and hashed, so the
    manifest hash reflects true content. When the manifest hash matches
    ``written_manifest_sha`` (the archive already on tape) and the run
    is incremental, the source is skipped entirely (returns None)
    BEFORE any zip is built. On a full run an unchanged source is
    always rebuilt: the new tape needs the data.

    There is no persistent zip cache — the built zip is a per-run temp
    file (``archive_<slug>.zip`` in work_dir) that the engine deletes
    once the entry is written. On a cache hit nothing is kept between
    runs; on a crash the leftover is removed by the tmp sweeps.

    Args:
        source: Archive source definition (archive=True).
        work_dir: Directory for the freshly built zip (``<data>/tmp``).
        written_manifest_sha: manifest_sha of the newest archive version
            already backed up (None = never backed up).
        write_even_if_unchanged: True for full runs — unchanged content
            still produces an entry (new tape needs the data).

    Returns:
        PlannedFile with path = source root (the catalog/header path),
        staging_path = the built zip, sha256 = zip hash, and kind
        "archive"; or None when the incremental run can skip the source
        or the source has no included files.

    Raises:
        SnapshotError: If the source root does not exist (the engine
            applies continue_on_error around this).
    """
    root = Path(source.path)
    if not root.is_dir():
        raise SnapshotError(f"Source directory does not exist: {root}")

    work_dir.mkdir(parents=True, exist_ok=True)
    # Deterministic per-source name: distinct slugs let concurrent
    # planning build several sources at once without collisions, and a
    # re-planned run overwrites its own leftover instead of accumulating.
    slug = _source_slug(str(root))
    zip_path = work_dir / f"archive_{slug}.zip"

    entries = _walk_source(root, source)
    if not entries:
        logger.warning("archive_source_empty", path=str(root))
        return None
    manifest_sha = manifest_digest(entries)

    if (
        not write_even_if_unchanged
        and written_manifest_sha is not None
        and manifest_sha == written_manifest_sha
    ):
        logger.info("archive_unchanged_skipped", path=str(root))
        return None

    try:
        zip_size, zip_sha256 = _build_zip(root, entries, zip_path)
    except OSError as exc:
        # A file vanished after the walk hashed it, so the build's
        # open() failed mid-stream. Re-walk (the skip guard drops the
        # vanished file) and rebuild once so the manifest and the zip
        # stay consistent; a second failure propagates and the engine
        # applies continue_on_error.
        logger.warning(
            "archive_rebuild_after_vanish", path=str(root), error=repr(exc)
        )
        entries = _walk_source(root, source)
        if not entries:
            logger.warning("archive_source_empty", path=str(root))
            return None
        manifest_sha = manifest_digest(entries)
        zip_size, zip_sha256 = _build_zip(root, entries, zip_path)

    logger.info("archive_built", path=str(root), size=zip_size, files=len(entries))

    stat = zip_path.stat()
    return PlannedFile(
        path=str(root),
        size=zip_size,
        mtime=stat.st_mtime,
        sha256=zip_sha256,
        staging_path=str(zip_path),
        kind="archive",
        inner_count=len(entries),
        manifest_sha=manifest_sha,
    )


def extract_archive(zip_path: Path, target_dir: Path) -> int:
    """Extract a staged archive into a target directory.

    Restores per-file mtimes recorded in the zip and rejects unsafe
    entry names (absolute or parent-relative) as defense in depth —
    archive bytes come from tape.

    Args:
        zip_path: The archive file to extract.
        target_dir: Directory to extract into (created as needed).

    Returns:
        Number of extracted files.

    Raises:
        ArchiveError: If the zip contains an unsafe entry name or the
            extraction fails.
    """
    target_dir.mkdir(parents=True, exist_ok=True)
    extracted = 0
    try:
        with zipfile.ZipFile(zip_path) as zf:
            for info in zf.infolist():
                name = info.filename
                if (
                    name.startswith("/")
                    or ".." in Path(name).parts
                    or (len(name) >= 2 and name[1] == ":")
                ):
                    raise ArchiveError(f"Unsafe archive entry name: {name!r}")
                dest = target_dir / name
                if info.is_dir():
                    dest.mkdir(parents=True, exist_ok=True)
                    continue
                dest.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, dest.open("wb") as out:
                    while chunk := src.read(READ_CHUNK):
                        out.write(chunk)
                if info.date_time[0] >= 1980:
                    # zip stores local timestamps; mktime interprets them
                    # as local time (DST flag -1 = let it decide).
                    epoch = time.mktime((*info.date_time, 0, 0, -1))
                    if epoch > 0:
                        os.utime(dest, (epoch, epoch))
                extracted += 1
    except (OSError, zipfile.BadZipFile) as exc:
        raise ArchiveError(f"Cannot extract {zip_path}: {exc}") from exc
    return extracted
