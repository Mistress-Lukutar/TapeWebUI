"""
File:   catalog_browser.py
Brief:  Catalog explorer: path-prefix grouping of FileEntry rows into
        directory listings and subtree selections (no tape reads).
Author: Mistress-Lukutar
Date:   2026-11-06
Version: v0.6.3
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from tapevaultui.db.models import BackupSession, FileEntry
from tapevaultui.services.catalog import CatalogRepository

ListingMode = Literal["session", "tape"]


class DirSummary(BaseModel):
    """One child directory with recursive aggregates."""

    path: str
    name: str
    files_count: int
    bytes_total: int


class SessionFileRow(BaseModel):
    """One file of a session (single-session listing mode)."""

    id: int
    path: str
    name: str
    size: int
    mtime: datetime
    sha256: str
    status: str
    tape_file_index: int


class FileVersionRow(BaseModel):
    """One recorded version of a path (merged tape listing mode)."""

    file_id: int
    session_id: int
    job_name: str
    session_started_at: datetime
    size: int
    mtime: datetime
    sha256: str
    status: str
    tape_file_index: int


class MergedFileRow(BaseModel):
    """All recorded versions of one path on a tape."""

    path: str
    name: str
    versions: list[FileVersionRow]


class CatalogListing(BaseModel):
    """A directory listing for the explorer tree."""

    mode: ListingMode
    session_id: int | None = None
    tape_id: int | None = None
    dir: str
    dirs: list[DirSummary]
    files: list[SessionFileRow] = Field(default_factory=list)
    merged_files: list[MergedFileRow] = Field(default_factory=list)


class SelectionSummary(BaseModel):
    """Subtree selection expanded into catalog file ids."""

    file_ids: list[int]
    files_count: int
    bytes_total: int


def session_listing(
    catalog: CatalogRepository, session_id: int, dir_path: str
) -> CatalogListing:
    """Build a directory listing for one session.

    Args:
        catalog: Catalog repository.
        session_id: BackupSession primary key.
        dir_path: Directory prefix ("" lists the roots).

    Returns:
        The child dirs (with recursive aggregates) and files.
    """
    rows = catalog.files_under_prefix(session_id, dir_path)
    dirs, files = _group_session_rows(rows, dir_path)
    return CatalogListing(
        mode="session",
        session_id=session_id,
        dir=dir_path,
        dirs=dirs,
        files=files,
    )


def tape_listing(
    catalog: CatalogRepository, tape_id: int, dir_path: str
) -> CatalogListing:
    """Build a merged directory listing across a tape's live sessions.

    Files are grouped by path; every recorded version is listed newest
    first (erased sessions are excluded).

    Args:
        catalog: Catalog repository.
        tape_id: Tape primary key.
        dir_path: Directory prefix ("" lists the roots).

    Returns:
        The child dirs (with recursive aggregates) and versioned files.
    """
    pairs = catalog.tape_files_under_prefix(tape_id, dir_path)
    dirs = _group_pairs(pairs, dir_path)
    grouped: dict[str, list[tuple[FileEntry, BackupSession]]] = {}
    for entry, session in pairs:
        grouped.setdefault(entry.path, []).append((entry, session))
    merged: list[MergedFileRow] = []
    for path, versions in grouped.items():
        ordered = sorted(
            versions,
            key=lambda pair: (pair[1].started_at, pair[1].id, pair[0].id),
            reverse=True,
        )
        merged.append(
            MergedFileRow(
                path=path,
                name=_basename(path),
                versions=[
                    FileVersionRow(
                        file_id=entry.id,
                        session_id=session.id,
                        job_name=session.job_name,
                        session_started_at=session.started_at,
                        size=entry.size,
                        mtime=entry.mtime,
                        sha256=entry.sha256,
                        status=entry.status,
                        tape_file_index=entry.tape_file_index,
                    )
                    for entry, session in ordered
                ],
            )
        )
    merged.sort(key=lambda row: row.name.lower())
    return CatalogListing(
        mode="tape",
        tape_id=tape_id,
        dir=dir_path,
        dirs=dirs,
        merged_files=merged,
    )


def session_selection(
    catalog: CatalogRepository, session_id: int, prefix: str
) -> SelectionSummary:
    """Expand a session subtree into selectable file ids.

    Args:
        catalog: Catalog repository.
        session_id: BackupSession primary key.
        prefix: Directory prefix ("" selects the whole session).

    Returns:
        The selection summary with catalog file ids.
    """
    rows = catalog.files_under_prefix(session_id, prefix)
    return _selection(rows)


def tape_selection(
    catalog: CatalogRepository, tape_id: int, prefix: str, *, latest_only: bool
) -> SelectionSummary:
    """Expand a tape subtree into selectable file ids.

    Args:
        catalog: Catalog repository.
        tape_id: Tape primary key.
        prefix: Directory prefix ("" selects the whole tape).
        latest_only: Take only the newest version of each path.

    Returns:
        The selection summary with catalog file ids.
    """
    if latest_only:
        ids = catalog.latest_file_ids_under_prefix(tape_id, prefix)
        rows = [entry for entry in (catalog.get_file(fid) for fid in ids) if entry]
        return _selection(rows)
    pairs = catalog.tape_files_under_prefix(tape_id, prefix)
    return _selection([entry for entry, _ in pairs])


class _DirAccumulator:
    """Recursive aggregate counters for one directory."""

    def __init__(self, path: str, name: str) -> None:
        self.path = path
        self.name = name
        self.files_count = 0
        self.bytes_total = 0


def _group_session_rows(
    rows: list[FileEntry], dir_path: str
) -> tuple[list[DirSummary], list[SessionFileRow]]:
    """Group session rows into child dirs (recursive) and child files."""
    separators = _separators_of(rows)
    dirs: dict[str, _DirAccumulator] = {}
    files: list[SessionFileRow] = []
    for entry in rows:
        target = _classify(entry.path, dir_path, separators, dirs)
        if target is None:
            files.append(_file_row(entry))
        else:
            target.files_count += 1
            target.bytes_total += entry.size
    return _sorted_dirs(dirs), files


def _group_pairs(
    pairs: list[tuple[FileEntry, BackupSession]], dir_path: str
) -> list[DirSummary]:
    """Group (file, session) pairs into child dirs.

    Aggregates count distinct paths once each (using the newest
    version's size), not every recorded version.
    """
    rows = [entry for entry, _ in pairs]
    separators = _separators_of(rows)
    by_path: dict[str, tuple[FileEntry, BackupSession]] = {}
    for entry, session in pairs:
        best = by_path.get(entry.path)
        if best is None or (session.started_at, session.id, entry.id) > (
            best[1].started_at,
            best[1].id,
            best[0].id,
        ):
            by_path[entry.path] = (entry, session)
    dirs: dict[str, _DirAccumulator] = {}
    for path, (entry, _session) in by_path.items():
        target = _classify(path, dir_path, separators, dirs)
        if target is not None:
            target.files_count += 1
            target.bytes_total += entry.size
    return _sorted_dirs(dirs)


def _classify(
    path: str,
    dir_path: str,
    separators: list[str],
    dirs: dict[str, _DirAccumulator],
) -> _DirAccumulator | None:
    """Attribute one file path to a child dir accumulator.

    Args:
        path: Absolute source path of the file.
        dir_path: The listed directory prefix.
        separators: Separators present in the data (checked in order).
        dirs: Accumulator map to create/update.

    Returns:
        The accumulator to increment, or None when the file is a direct
        child file of ``dir_path``.
    """
    for sep in separators:
        remainder = _remainder(path, dir_path, sep)
        if remainder is None:
            continue
        if remainder.startswith(sep):
            # Posix absolute path below the listing root: the leading
            # "/" is itself the first child directory.
            child = dirs.get(sep)
            if child is None:
                child = dirs[sep] = _DirAccumulator(sep, sep)
            return child
        cut = remainder.find(sep)
        if cut == -1:
            if "\\" not in remainder and "/" not in remainder:
                return None  # plain name: direct child file
            continue  # may resolve under the other separator
        name = remainder[:cut]
        child_path = _child_dir_path(dir_path, name, sep)
        child = dirs.get(child_path)
        if child is None:
            child = dirs[child_path] = _DirAccumulator(child_path, name)
        return child
    return None  # fallback: treat as a direct child file


def _remainder(path: str, dir_path: str, sep: str) -> str | None:
    """Return the part of ``path`` below ``dir_path`` for a separator.

    Args:
        path: Absolute source path.
        dir_path: Directory prefix ("" for the listing root).
        sep: Separator assumed for this attempt.

    Returns:
        The remainder, or None when the path is not below ``dir_path``
        under this separator.
    """
    if not dir_path:
        return path
    prefix = dir_path if dir_path.endswith(sep) else dir_path + sep
    if not path.startswith(prefix) or path == prefix.rstrip(sep):
        return None
    return path[len(prefix) :]


def _child_dir_path(dir_path: str, name: str, sep: str) -> str:
    """Build the full path of a child directory.

    Args:
        dir_path: Parent directory prefix ("" for the root).
        name: Child directory name ("" name means the posix root "/").
        sep: Path separator.

    Returns:
        The child directory path without a trailing separator (except
        the posix root "/").
    """
    if not dir_path:
        return name if name else sep
    if dir_path == sep:
        return dir_path + name
    return dir_path + sep + name


def _separators_of(rows: list[FileEntry]) -> list[str]:
    """Detect which separators appear in the given paths.

    Args:
        rows: FileEntry rows.

    Returns:
        Separators to try, backslash first when present.
    """
    seps: list[str] = []
    if any("\\" in entry.path for entry in rows):
        seps.append("\\")
    if any("/" in entry.path for entry in rows):
        seps.append("/")
    return seps or ["/"]


def _file_row(entry: FileEntry) -> SessionFileRow:
    """Convert a FileEntry row into an API row.

    Args:
        entry: FileEntry row.

    Returns:
        The SessionFileRow payload.
    """
    return SessionFileRow(
        id=entry.id,
        path=entry.path,
        name=_basename(entry.path),
        size=entry.size,
        mtime=entry.mtime,
        sha256=entry.sha256,
        status=entry.status,
        tape_file_index=entry.tape_file_index,
    )


def _selection(rows: list[FileEntry]) -> SelectionSummary:
    """Summarize rows into a selection payload.

    Args:
        rows: Selected FileEntry rows.

    Returns:
        The selection summary.
    """
    return SelectionSummary(
        file_ids=[entry.id for entry in rows],
        files_count=len(rows),
        bytes_total=sum(entry.size for entry in rows),
    )


def _sorted_dirs(dirs: dict[str, _DirAccumulator]) -> list[DirSummary]:
    """Convert and sort accumulators into API summaries.

    Args:
        dirs: Accumulator map.

    Returns:
        DirSummary list sorted by name (case-insensitive).
    """
    return [
        DirSummary(
            path=acc.path,
            name=acc.name,
            files_count=acc.files_count,
            bytes_total=acc.bytes_total,
        )
        for acc in sorted(dirs.values(), key=lambda acc: acc.name.lower())
    ]


def _basename(path: str) -> str:
    """Return the display name (last segment) of a path.

    Args:
        path: Absolute source path.

    Returns:
        The final path segment (the path itself for roots).
    """
    for sep in ("\\", "/"):
        if sep in path:
            return path.rstrip(sep).rsplit(sep, 1)[-1] or path
    return path
