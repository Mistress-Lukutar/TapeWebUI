"""
File:   test_archive_builder.py
Brief:  Archive source tests: manifest, staged zip cache, skip/rebuild,
        extraction roundtrip.
Author: Mistress-Lukutar
Date:   2026-11-08
Version: v0.8.0
"""

from __future__ import annotations

import os
import time
import zipfile
from pathlib import Path

import pytest

from tapevaultui.services.archive_builder import (
    ArchiveError,
    ArchiveManifest,
    extract_archive,
    manifest_digest,
    plan_archive_source,
)
from tapevaultui.services.job_defs import SourceDef
from tapevaultui.services.snapshot import SnapshotError, sha256_of


def _source(tmp_path: Path, archive: bool = True) -> SourceDef:
    """Build a SourceDef over <tmp_path>/src (ignoring *.log).

    Args:
        tmp_path: Pytest temp dir.
        archive: The archive flag value.

    Returns:
        A SourceDef.
    """
    return SourceDef.model_validate(
        {"path": str(tmp_path / "src"), "archive": archive, "excludes": ["*.log"]}
    )


def _make_tree(tmp_path: Path) -> Path:
    """Create a small source tree: a.txt, nested/b.txt, ignored.log.

    Args:
        tmp_path: Pytest temp dir holding src/.

    Returns:
        The source root.
    """
    src = tmp_path / "src"
    (src / "nested").mkdir(parents=True)
    (src / "a.txt").write_bytes(b"AAAA")
    (src / "nested" / "b.txt").write_bytes(b"BB")
    (src / "ignored.log").write_bytes(b"log")
    return src


def test_plan_builds_zip_with_content_and_mtime(tmp_path: Path) -> None:
    """The staged zip holds the tree uncompressed with restored mtimes."""
    src = _make_tree(tmp_path)
    src.joinpath("a.txt").touch()
    entry = plan_archive_source(
        _source(tmp_path), staging_dir=tmp_path / "staging"
    )

    assert entry is not None
    assert entry.kind == "archive"
    assert entry.path == str(src)
    assert entry.inner_count == 2  # ignored.log excluded by default rules
    assert entry.staging_path is not None
    assert Path(entry.staging_path).exists()
    assert entry.sha256 == sha256_of(Path(entry.staging_path))
    assert entry.manifest_sha

    with zipfile.ZipFile(entry.staging_path) as zf:
        names = sorted(zf.namelist())
        content = zf.read("a.txt")
        mtime = zf.getinfo("a.txt").date_time
    assert names == ["a.txt", "nested/b.txt"]
    assert content == b"AAAA"
    expected = time.localtime(src.joinpath("a.txt").stat().st_mtime)
    assert mtime[:5] == (
        expected.tm_year,
        expected.tm_mon,
        expected.tm_mday,
        expected.tm_hour,
        expected.tm_min,
    )


def test_plan_honors_excludes(tmp_path: Path) -> None:
    """Excludes prune directories/files like the plain snapshot walk."""
    src = _make_tree(tmp_path)
    source = SourceDef.model_validate(
        {
            "path": str(src),
            "archive": True,
            "excludes": ["nested/", "*.log"],
        }
    )
    entry = plan_archive_source(source, staging_dir=tmp_path / "staging")
    assert entry is not None
    with zipfile.ZipFile(entry.staging_path) as zf:
        assert zf.namelist() == ["a.txt"]


def test_plan_incremental_skips_unchanged_source(tmp_path: Path) -> None:
    """A matching written manifest hash skips the source entirely."""
    _make_tree(tmp_path)
    first = plan_archive_source(
        _source(tmp_path), staging_dir=tmp_path / "staging"
    )
    assert first is not None

    second = plan_archive_source(
        _source(tmp_path),
        staging_dir=tmp_path / "staging",
        written_manifest_sha=first.manifest_sha,
    )
    assert second is None


def test_plan_full_reuses_cached_zip_but_returns_entry(tmp_path: Path) -> None:
    """Full runs skip the rebuild but still plan the archive for tape."""
    _make_tree(tmp_path)
    first = plan_archive_source(
        _source(tmp_path), staging_dir=tmp_path / "staging"
    )
    assert first is not None
    zip_mtime = Path(first.staging_path).stat().st_mtime

    second = plan_archive_source(
        _source(tmp_path),
        staging_dir=tmp_path / "staging",
        written_manifest_sha=first.manifest_sha,
        write_even_if_unchanged=True,
    )
    assert second is not None
    assert second.sha256 == first.sha256  # cache reused, not rebuilt
    assert Path(second.staging_path).stat().st_mtime == zip_mtime


def test_plan_rebuilds_when_content_changes(tmp_path: Path) -> None:
    """Changed content produces a new manifest hash and a rebuilt zip."""
    _make_tree(tmp_path)
    first = plan_archive_source(
        _source(tmp_path), staging_dir=tmp_path / "staging"
    )
    assert first is not None

    (tmp_path / "src" / "nested" / "c.txt").write_bytes(b"C")
    second = plan_archive_source(
        _source(tmp_path),
        staging_dir=tmp_path / "staging",
        written_manifest_sha=first.manifest_sha,
    )
    assert second is not None
    assert second.manifest_sha != first.manifest_sha
    assert second.inner_count == 3
    assert second.sha256 != first.sha256


def test_rebuild_is_deterministic(tmp_path: Path) -> None:
    """Identical content builds byte-identical zips."""
    _make_tree(tmp_path)
    first = plan_archive_source(
        _source(tmp_path), staging_dir=tmp_path / "staging"
    )
    assert first is not None
    zip_bytes = Path(first.staging_path).read_bytes()

    # Force a rebuild by wiping the manifest (cache miss).
    (tmp_path / "staging").glob("*.manifest.json")
    for manifest in (tmp_path / "staging").glob("*.manifest.json"):
        manifest.unlink()
    second = plan_archive_source(
        _source(tmp_path), staging_dir=tmp_path / "staging"
    )
    assert second is not None
    assert Path(second.staging_path).read_bytes() == zip_bytes


def test_plan_empty_source_returns_none(tmp_path: Path) -> None:
    """A source with no included files plans nothing."""
    (tmp_path / "src").mkdir()
    assert (
        plan_archive_source(_source(tmp_path), staging_dir=tmp_path / "staging")
        is None
    )


def test_plan_missing_source_raises(tmp_path: Path) -> None:
    """A missing source root raises SnapshotError (engine decides policy)."""
    source = SourceDef.model_validate(
        {"path": str(tmp_path / "missing"), "archive": True}
    )
    with pytest.raises(SnapshotError):
        plan_archive_source(source, staging_dir=tmp_path / "staging")


def test_manifest_roundtrip(tmp_path: Path) -> None:
    """Manifest serialization is lossless and content-hash stable."""
    entries = {
        "a.txt": (4, 1700000000.0, "f" * 64),
        "nested/b.txt": (2, 1700000001.0, "0" * 64),
    }
    manifest = ArchiveManifest(entries, manifest_digest(entries))
    parsed = ArchiveManifest.from_bytes(manifest.serialize())
    assert parsed.entries == entries
    assert parsed.manifest_sha == manifest.manifest_sha
    with pytest.raises(ArchiveError):
        ArchiveManifest.from_bytes(b"not json")


def test_manifest_digest_reflects_content(tmp_path: Path) -> None:
    """The digest changes with any entry change."""
    base = {"a.txt": (4, 1700000000.0, "f" * 64)}
    same = dict(base)
    different = dict(base)
    different["a.txt"] = (5, 1700000000.0, "f" * 64)
    assert manifest_digest(base) == manifest_digest(same)
    assert manifest_digest(base) != manifest_digest(different)


def test_extract_roundtrip_restores_content(tmp_path: Path) -> None:
    """Extraction recreates the tree with content and mtimes."""
    src = _make_tree(tmp_path)
    entry = plan_archive_source(
        _source(tmp_path), staging_dir=tmp_path / "staging"
    )
    assert entry is not None

    target = tmp_path / "restored"
    count = extract_archive(Path(entry.staging_path), target)
    assert count == 2
    assert (target / "a.txt").read_bytes() == b"AAAA"
    assert (target / "nested" / "b.txt").read_bytes() == b"BB"
    original_ts = src.joinpath("a.txt").stat().st_mtime
    restored_ts = (target / "a.txt").stat().st_mtime
    assert abs(original_ts - restored_ts) <= 2


def test_extract_rejects_unsafe_entry_names(tmp_path: Path) -> None:
    """Absolute or parent-relative entry names are refused."""
    evil = tmp_path / "evil.zip"
    with zipfile.ZipFile(evil, "w") as zf:
        zf.writestr("../escape.txt", b"x")
        zf.writestr("/abs.txt", b"y")
    target = tmp_path / "out"
    with pytest.raises(ArchiveError):
        extract_archive(evil, target)
    assert not (tmp_path / "escape.txt").exists()


def test_extract_rejects_corrupt_zip(tmp_path: Path) -> None:
    """Bad zip bytes raise ArchiveError, not zipfile internals."""
    bad = tmp_path / "bad.zip"
    bad.write_bytes(b"definitely not a zip")
    with pytest.raises(ArchiveError):
        extract_archive(bad, tmp_path / "out")


def test_cached_manifest_corruption_triggers_rebuild(tmp_path: Path) -> None:
    """A corrupt sidecar manifest falls back to a fresh build."""
    _make_tree(tmp_path)
    staging = tmp_path / "staging"
    first = plan_archive_source(_source(tmp_path), staging_dir=staging)
    assert first is not None
    manifest_files = list(staging.glob("*.manifest.json"))
    assert len(manifest_files) == 1
    manifest_files[0].write_bytes(b"corrupt")

    second = plan_archive_source(_source(tmp_path), staging_dir=staging)
    assert second is not None
    assert second.sha256 == first.sha256  # rebuilt to identical bytes


def test_mtimes_beyond_zip_range_are_clamped(tmp_path: Path) -> None:
    """Out-of-range mtimes cannot break the zip build."""
    src = tmp_path / "src"
    src.mkdir()
    target = src / "old.txt"
    target.write_bytes(b"x")
    past = time.time() - 60 * 60 * 24 * 365 * 60  # ~60 years ago
    os.utime(target, (past, past))

    entry = plan_archive_source(
        _source(tmp_path), staging_dir=tmp_path / "staging"
    )
    assert entry is not None
    with zipfile.ZipFile(entry.staging_path) as zf:
        assert zf.testzip() is None
