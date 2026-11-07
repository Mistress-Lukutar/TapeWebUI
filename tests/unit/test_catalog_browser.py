"""
File:   test_catalog_browser.py
Brief:  Catalog explorer grouping tests (listings and subtree selections).
Author: Mistress-Lukutar
Date:   2026-11-07
Version: v0.7.0
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from tapevaultui.db.models import FileEntry
from tapevaultui.services.catalog import SESSION_HEADER_PATH, CatalogRepository
from tapevaultui.services.catalog_browser import (
    session_listing,
    session_selection,
    tape_listing,
    tape_selection,
)


@pytest.fixture
def repo() -> CatalogRepository:
    """Repository on a fresh in-memory database.

    Returns:
        CatalogRepository with all tables created.
    """
    repository = CatalogRepository("sqlite://")
    repository.create_all()
    return repository


def _add(
    repo: CatalogRepository,
    session_id: int,
    index: int,
    path: str,
    *,
    size: int = 10,
    mtime: datetime | None = None,
) -> FileEntry:
    """Insert one file row (test helper).

    Args:
        repo: Catalog repository.
        session_id: BackupSession primary key.
        index: Absolute tape file index.
        path: Source path.
        size: File size.
        mtime: Modification time (defaults to now).

    Returns:
        The created FileEntry.
    """
    return repo.add_file(
        session_id,
        tape_file_index=index,
        path=path,
        size=size,
        mtime=mtime or datetime.now(UTC),
        sha256="0" * 64,
    )


def test_session_listing_groups_dirs(repo: CatalogRepository) -> None:
    """Root listing shows drive roots with recursive aggregates."""
    tape = repo.register_tape("u1")
    session = repo.create_session(tape.id, job_name="j")
    _add(repo, session.id, 0, SESSION_HEADER_PATH)
    _add(repo, session.id, 1, "C:/data/a.txt", size=5)
    _add(repo, session.id, 2, "C:/data/sub/b.txt", size=7)
    _add(repo, session.id, 3, "C:/data/sub/c.txt", size=1)
    _add(repo, session.id, 4, "D:/top.txt", size=3)

    listing = session_listing(repo, session.id, "")

    assert listing.mode == "session"
    assert [d.path for d in listing.dirs] == ["C:", "D:"]
    root_c = listing.dirs[0]
    assert (root_c.files_count, root_c.bytes_total) == (3, 13)
    assert (listing.dirs[1].files_count, listing.dirs[1].bytes_total) == (1, 3)
    assert listing.files == []  # nothing directly at the root

    nested = session_listing(repo, session.id, "C:/data")
    assert [d.name for d in nested.dirs] == ["sub"]
    assert [f.name for f in nested.files] == ["a.txt"]

    leaf = session_listing(repo, session.id, "C:/data/sub")
    assert leaf.dirs == []
    assert [f.name for f in leaf.files] == ["b.txt", "c.txt"]


def test_session_listing_posix_root(repo: CatalogRepository) -> None:
    """Absolute posix paths group under the "/" root directory."""
    tape = repo.register_tape("u2")
    session = repo.create_session(tape.id, job_name="j")
    _add(repo, session.id, 1, "/srv/photos/a.jpg", size=2)
    _add(repo, session.id, 2, "/etc/app.conf", size=4)

    listing = session_listing(repo, session.id, "")
    assert [d.path for d in listing.dirs] == ["/"]
    assert (listing.dirs[0].files_count, listing.dirs[0].bytes_total) == (2, 6)

    inside = session_listing(repo, session.id, "/")
    assert [d.path for d in inside.dirs] == ["/etc", "/srv"]


def test_tape_listing_merges_versions(repo: CatalogRepository) -> None:
    """Merged listing groups versions per path, newest first."""
    tape = repo.register_tape("u3")
    old_time = datetime.now(UTC) - timedelta(hours=2)
    s1 = repo.create_session(tape.id, job_name="old", started_at=old_time)
    _add(repo, s1.id, 1, "C:/data/a.txt", size=1)
    new_time = datetime.now(UTC)
    s2 = repo.create_session(tape.id, job_name="new", started_at=new_time)
    _add(repo, s2.id, 1, "C:/data/a.txt", size=2)
    _add(repo, s2.id, 2, "C:/data/b.txt", size=3)
    repo.finish_session(s1.id, status="ok")
    repo.finish_session(s2.id, status="ok")

    listing = tape_listing(repo, tape.id, "")
    assert listing.mode == "tape"
    assert listing.dirs[0].files_count == 2  # distinct paths, not versions

    merged = {row.path: row for row in listing.merged_files}
    versions = merged["C:/data/a.txt"].versions
    assert len(versions) == 2
    assert versions[0].session_id == s2.id  # newest first
    assert versions[0].job_name == "new"
    assert versions[1].session_id == s1.id


def test_tape_listing_excludes_erased(repo: CatalogRepository) -> None:
    """Erased sessions are invisible in the merged listing."""
    tape = repo.register_tape("u4")
    s1 = repo.create_session(tape.id, job_name="gone")
    _add(repo, s1.id, 1, "C:/a.txt")
    repo.finish_session(s1.id, status="ok")
    repo.mark_tape_erased(tape.id)
    s2 = repo.create_session(tape.id, job_name="fresh")
    _add(repo, s2.id, 1, "C:/b.txt")

    listing = tape_listing(repo, tape.id, "")
    assert [row.path for row in listing.merged_files] == ["C:/b.txt"]
    assert listing.dirs[0].files_count == 1


def test_tape_selection_latest_only(repo: CatalogRepository) -> None:
    """Subtree selection can take only the newest version per path."""
    tape = repo.register_tape("u5")
    s1 = repo.create_session(tape.id, job_name="old", started_at=datetime.now(UTC))
    _add(repo, s1.id, 1, "C:/a.txt", size=1)
    s2 = repo.create_session(
        tape.id, job_name="new", started_at=datetime.now(UTC) + timedelta(hours=1)
    )
    _add(repo, s2.id, 1, "C:/a.txt", size=2)
    _add(repo, s2.id, 2, "C:/b.txt", size=3)

    latest = tape_selection(repo, tape.id, "", latest_only=True)
    assert latest.files_count == 2
    assert latest.bytes_total == 5
    assert len(latest.file_ids) == 2

    everything = tape_selection(repo, tape.id, "C:/", latest_only=False)
    assert everything.files_count == 3
    assert everything.bytes_total == 6


def test_session_selection_prefix(repo: CatalogRepository) -> None:
    """Session subtree selection returns ids under the prefix."""
    tape = repo.register_tape("u6")
    session = repo.create_session(tape.id, job_name="j")
    header = _add(repo, session.id, 0, SESSION_HEADER_PATH)
    one = _add(repo, session.id, 1, "C:/a.txt")
    two = _add(repo, session.id, 2, "C:/sub/b.txt")

    selection = session_selection(repo, session.id, "C:/sub")
    assert selection.file_ids == [two.id]
    assert selection.files_count == 1

    whole = session_selection(repo, session.id, "")
    assert set(whole.file_ids) == {one.id, two.id}
    assert header.id not in whole.file_ids
