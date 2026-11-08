"""
File:   archive_builder.py
Brief:  Archive sources: content manifest, staged zip cache, skip/rebuild.
Author: Mistress-Lukutar
Date:   2026-11-08
Version: v0.8.0
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import zipfile
from pathlib import Path

import pathspec
import structlog

from tapevaultui.services.job_defs import SourceDef
from tapevaultui.services.snapshot import (
    READ_CHUNK,
    PlannedFile,
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


class ArchiveManifest:
    """Content manifest of one archive source.

    Maps relative posix paths to (size, mtime, sha256); the manifest
    hash identifies the exact content, so an unchanged manifest means
    the previously written archive is still current.
    """

    def __init__(
        self,
        entries: dict[str, tuple[int, float, str]],
        manifest_sha: str,
    ) -> None:
        """Store the entries and their content hash.

        Args:
            entries: Mapping relative path -> (size, mtime, sha256).
            manifest_sha: Content hash over the sorted entries.
        """
        self.entries = entries
        self.manifest_sha = manifest_sha

    @classmethod
    def from_bytes(cls, raw: bytes) -> ArchiveManifest:
        """Parse a serialized manifest.

        Args:
            raw: UTF-8 JSON bytes as written by ``serialize``.

        Returns:
            The parsed manifest.

        Raises:
            ArchiveError: If the manifest is corrupt.
        """
        try:
            payload = json.loads(raw.decode("utf-8"))
            entries = payload["entries"]
            return cls(
                {k: (v[0], v[1], v[2]) for k, v in entries.items()},
                payload["manifest_sha"],
            )
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise ArchiveError(f"Corrupt archive manifest: {exc}") from exc

    def serialize(self) -> bytes:
        """Serialize to canonical UTF-8 JSON bytes.

        Returns:
            Compact JSON with sorted keys (stable manifest_sha).
        """
        return json.dumps(
            {"entries": self.entries, "manifest_sha": self.manifest_sha},
            sort_keys=True,
        ).encode("utf-8")


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


def _walk_source(
    root: Path, source: SourceDef
) -> dict[str, tuple[int, float, str]]:
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
        pathspec.GitIgnoreSpec.from_lines(source.excludes)
        if source.excludes
        else None
    )
    entries: dict[str, tuple[int, float, str]] = {}
    for file_path in sorted(_iter_unignored(root, spec)):
        if not file_path.is_file():
            continue
        relative = file_path.relative_to(root).as_posix()
        if source.patterns and not _matches_any(relative, source.patterns):
            continue
        stat = file_path.stat()
        entries[relative] = (stat.st_size, stat.st_mtime, sha256_of(file_path))
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
) -> int:
    """Write the staged zip for the given content set.

    Entries are written in sorted order with per-file mtimes restored
    (deterministic bytes for identical content). The temp file replaces
    the cache atomically so a crashed build never corrupts the cache.

    Args:
        root: Source root directory the relative paths resolve against.
        entries: Mapping relative path -> (size, mtime, sha256).
        zip_path: Destination zip file path.

    Returns:
        The zip file size in bytes.
    """
    tmp_path = zip_path.with_suffix(".zip.tmp")
    with zipfile.ZipFile(tmp_path, "w", compression=ZIP_COMPRESSION) as zf:
        for relative in sorted(entries):
            info = zipfile.ZipInfo(
                filename=relative, date_time=_zip_date_time(entries[relative][1])
            )
            info.external_attr = 0o644 << 16
            with (root / relative).open("rb") as handle:
                zf.writestr(info, handle.read())
    os.replace(tmp_path, zip_path)
    return zip_path.stat().st_size


def plan_archive_source(
    source: SourceDef,
    *,
    staging_dir: Path,
    written_manifest_sha: str | None = None,
    write_even_if_unchanged: bool = False,
) -> PlannedFile | None:
    """Plan one archive source into a single staged zip file entry.

    Hash-all semantics: every included file is read and hashed, so the
    manifest hash reflects true content. When the manifest hash matches
    ``written_manifest_sha`` (the archive already on tape) and the run
    is incremental, the source is skipped entirely (returns None). On a
    full run an unchanged archive is not rebuilt — the cached zip is
    reused — but a PlannedFile is still returned so the data lands on
    the new tape.

    Args:
        source: Archive source definition (archive=True).
        staging_dir: Persistent cache directory for zips + manifests.
        written_manifest_sha: manifest_sha of the newest archive version
            already backed up (None = never backed up).
        write_even_if_unchanged: True for full runs — unchanged content
            still produces an entry (new tape needs the data).

    Returns:
        PlannedFile with path = source root (the catalog/header path),
        staging_path = cached zip, sha256 = zip hash, and kind
        "archive"; or None when the incremental run can skip the source
        or the source has no included files.

    Raises:
        SnapshotError: If the source root does not exist (the engine
            applies continue_on_error around this).
    """
    from tapevaultui.services.snapshot import SnapshotError

    root = Path(source.path)
    if not root.is_dir():
        raise SnapshotError(f"Source directory does not exist: {root}")

    staging_dir.mkdir(parents=True, exist_ok=True)
    slug = _source_slug(str(root))
    manifest_path = staging_dir / f"{slug}.manifest.json"
    zip_path = staging_dir / f"{slug}.zip"

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

    reuse = False
    if manifest_path.exists() and zip_path.exists():
        try:
            cached = ArchiveManifest.from_bytes(manifest_path.read_bytes())
            reuse = cached.manifest_sha == manifest_sha
        except ArchiveError:
            reuse = False
    if reuse:
        zip_size = zip_path.stat().st_size
        logger.info("archive_cache_reused", path=str(root), size=zip_size)
    else:
        zip_size = _build_zip(root, entries, zip_path)
        manifest_path.write_bytes(
            ArchiveManifest(entries, manifest_sha).serialize()
        )
        logger.info(
            "archive_built", path=str(root), size=zip_size, files=len(entries)
        )

    stat = zip_path.stat()
    return PlannedFile(
        path=str(root),
        size=zip_size,
        mtime=stat.st_mtime,
        sha256=sha256_of(zip_path),
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
