"""
File:   test_archive_builder.py
Brief:  Archive source tests: manifest, fresh zip build, skip/rebuild,
        extraction roundtrip.
Author: Mistress-Lukutar
Date:   2026-11-09
Version: v0.11.3
"""

from __future__ import annotations

import os
import time
import zipfile
from pathlib import Path

import pytest

from tapevaultui.services.archive_builder import (
    ArchiveError,
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
    entry = plan_archive_source(_source(tmp_path), work_dir=tmp_path / "work")

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
    entry = plan_archive_source(source, work_dir=tmp_path / "work")
    assert entry is not None
    with zipfile.ZipFile(entry.staging_path) as zf:
        assert zf.namelist() == ["a.txt"]


def test_plan_incremental_skips_unchanged_source(tmp_path: Path) -> None:
    """A matching written manifest hash skips the source entirely."""
    _make_tree(tmp_path)
    first = plan_archive_source(_source(tmp_path), work_dir=tmp_path / "work")
    assert first is not None

    second = plan_archive_source(
        _source(tmp_path),
        work_dir=tmp_path / "work",
        written_manifest_sha=first.manifest_sha,
    )
    assert second is None


def test_plan_rebuilds_when_content_changes(tmp_path: Path) -> None:
    """Changed content produces a new manifest hash and a rebuilt zip."""
    _make_tree(tmp_path)
    first = plan_archive_source(_source(tmp_path), work_dir=tmp_path / "work")
    assert first is not None

    (tmp_path / "src" / "nested" / "c.txt").write_bytes(b"C")
    second = plan_archive_source(
        _source(tmp_path),
        work_dir=tmp_path / "work",
        written_manifest_sha=first.manifest_sha,
    )
    assert second is not None
    assert second.manifest_sha != first.manifest_sha
    assert second.inner_count == 3
    assert second.sha256 != first.sha256


def test_rebuild_is_deterministic(tmp_path: Path) -> None:
    """Identical content builds byte-identical zips."""
    _make_tree(tmp_path)
    work = tmp_path / "work"
    first = plan_archive_source(_source(tmp_path), work_dir=work)
    assert first is not None
    zip_bytes = Path(first.staging_path).read_bytes()

    # Force a rebuild by deleting the existing archive zip.
    for z in work.glob("archive_*.zip"):
        z.unlink()
    second = plan_archive_source(_source(tmp_path), work_dir=work)
    assert second is not None
    assert Path(second.staging_path).read_bytes() == zip_bytes


def test_plan_writes_fresh_zip_into_work_dir(tmp_path: Path) -> None:
    """After a successful plan, exactly one archive_*.zip exists in work_dir,
    staging_path points at it, and no *.manifest.json is present.
    """
    _make_tree(tmp_path)
    work = tmp_path / "work"
    entry = plan_archive_source(_source(tmp_path), work_dir=work)

    assert entry is not None
    zip_files = list(work.glob("archive_*.zip"))
    assert len(zip_files) == 1
    assert Path(entry.staging_path).resolve() == zip_files[0].resolve()
    manifest_files = list(work.glob("*.manifest.json"))
    assert len(manifest_files) == 0


def test_plan_overwrites_leftover_zip_on_replan(tmp_path: Path) -> None:
    """A stale junk zip at the expected path is replaced on a subsequent plan
    with changed content.
    """
    from tapevaultui.services.archive_builder import _source_slug

    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_bytes(b"OLD")
    source = SourceDef.model_validate(
        {"path": str(src), "archive": True, "excludes": []}
    )
    work = tmp_path / "work"
    slug = _source_slug(str(src))

    # First plan creates the zip.
    first = plan_archive_source(source, work_dir=work)
    assert first is not None

    # Overwrite with junk bytes.
    zip_path = work / f"archive_{slug}.zip"
    zip_path.write_bytes(b"junk content")

    # Change the source and re-plan.
    (src / "a.txt").write_bytes(b"NEW")
    second = plan_archive_source(source, work_dir=work)
    assert second is not None
    with zipfile.ZipFile(second.staging_path) as zf:
        assert zf.read("a.txt") == b"NEW"


def test_plan_empty_source_returns_none(tmp_path: Path) -> None:
    """A source with no included files plans nothing."""
    (tmp_path / "src").mkdir()
    assert (
        plan_archive_source(_source(tmp_path), work_dir=tmp_path / "work") is None
    )


def test_plan_missing_source_raises(tmp_path: Path) -> None:
    """A missing source root raises SnapshotError (engine decides policy)."""
    source = SourceDef.model_validate(
        {"path": str(tmp_path / "missing"), "archive": True}
    )
    with pytest.raises(SnapshotError):
        plan_archive_source(source, work_dir=tmp_path / "work")


def test_manifest_digest_stable_for_same_entries() -> None:
    """manifest_digest produces identical output for identical entry sets."""
    entries = {
        "a.txt": (4, 1700000000.0, "f" * 64),
        "nested/b.txt": (2, 1700000001.0, "0" * 64),
    }
    d1 = manifest_digest(entries)
    d2 = manifest_digest(entries)
    assert d1 == d2
    assert len(d1) == 64


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
    entry = plan_archive_source(_source(tmp_path), work_dir=tmp_path / "work")
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


def test_mtimes_beyond_zip_range_are_clamped(tmp_path: Path) -> None:
    """Out-of-range mtimes cannot break the zip build."""
    src = tmp_path / "src"
    src.mkdir()
    target = src / "old.txt"
    target.write_bytes(b"x")
    past = time.time() - 60 * 60 * 24 * 365 * 60  # ~60 years ago
    os.utime(target, (past, past))

    entry = plan_archive_source(_source(tmp_path), work_dir=tmp_path / "work")
    assert entry is not None
    with zipfile.ZipFile(entry.staging_path) as zf:
        assert zf.testzip() is None


def test_build_zip_streams_instead_of_whole_file_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression (run 39, MemoryError on a 10.9 GiB HF blob): zip entry
    bytes must stream in bounded chunks — an unbounded ``read()`` would
    load a whole file into RAM and crash on sources with very large
    single files. A guard object fails the build if any code requests an
    unbounded read.
    """
    src = tmp_path / "src"
    src.mkdir()
    payload = os.urandom(1 << 20)  # 1 MiB block, written 32x below
    big = src / "big.bin"
    big.write_bytes(payload * 32)  # 32 MiB — content integrity matters,
    # the guard (not the size) is what proves streaming.
    expected = big.read_bytes()  # captured BEFORE the guard is installed
    assert expected == payload * 32

    real_open = Path.open

    def guarded_open(self, mode="r", *args, **kwargs):
        handle = real_open(self, mode, *args, **kwargs)
        if mode != "rb":
            return handle

        class _BoundedOnly:
            def __init__(self, fh):
                self._fh = fh

            def read(self, size=-1):
                assert size is not None and size > 0, (
                    "unbounded whole-file read() in archive build"
                )
                return self._fh.read(size)

            def __enter__(self):
                self._fh.__enter__()
                return self

            def __exit__(self, *args):
                return self._fh.__exit__(*args)

        return _BoundedOnly(handle)

    monkeypatch.setattr(Path, "open", guarded_open)
    entry = plan_archive_source(_source(tmp_path), work_dir=tmp_path / "work")

    assert entry is not None
    assert entry.inner_count == 1
    assert entry.sha256 == sha256_of(Path(entry.staging_path))
    with zipfile.ZipFile(entry.staging_path) as zf:
        assert zf.read("big.bin") == expected


def test_failed_build_removes_partial_tmp(tmp_path: Path) -> None:
    """A crashed build leaves no half-written .zip.tmp in the cache."""
    from tapevaultui.services.archive_builder import _build_zip

    src = tmp_path / "src"
    (src / "nested").mkdir(parents=True)
    (src / "a.txt").write_bytes(b"A" * 1024)
    (src / "nested" / "b.txt").write_bytes(b"BB")
    entries = {
        "a.txt": (1024, 0.0, "sha:a"),
        "nested/b.txt": (2, 0.0, "sha:b"),
    }
    zip_path = tmp_path / "staging" / "cache.zip"
    zip_path.parent.mkdir()
    # Listed in the manifest but gone on disk: the build raises midway,
    # after a.txt was already streamed into the partial temp file.
    (src / "nested" / "b.txt").unlink()
    with pytest.raises(OSError):
        _build_zip(src, entries, zip_path)
    assert not zip_path.with_suffix(".zip.tmp").exists()


def test_build_zip_enables_zip64_for_large_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression (second run-39 failure): zf.open("w") sizes the local
    header from ZipInfo.file_size — without publishing the manifest size
    it never enables Zip64, and closing any entry whose data exceeds
    ZIP64_LIMIT raises "File size too large". The real limit is 4 GiB,
    so the test patches it down to 1 MiB to exercise the same branch.
    """
    from tapevaultui.services.archive_builder import _build_zip

    src = tmp_path / "src"
    src.mkdir()
    expected = os.urandom(64 * 1024) * 32  # 2 MiB — over the fake limit
    (src / "big.bin").write_bytes(expected)
    monkeypatch.setattr(zipfile, "ZIP64_LIMIT", 1024 * 1024)

    entries = {"big.bin": (len(expected), (src / "big.bin").stat().st_mtime, "sha:x")}
    zip_path = tmp_path / "staging" / "cache.zip"
    zip_path.parent.mkdir()  # plan_archive_source ensures this in prod
    size, zip_sha = _build_zip(src, entries, zip_path)

    assert size > 0
    assert zip_sha == sha256_of(zip_path)
    with zipfile.ZipFile(zip_path) as zf:
        assert zf.getinfo("big.bin").file_size == len(expected)
        assert zf.read("big.bin") == expected


def test_walk_skips_file_vanished_before_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression (run 42): a file deleted between the walk listing and
    its read is skipped with a warning instead of failing the source.
    """
    src = _make_tree(tmp_path)
    victim = src / "a.txt"

    def vanishing_sha256_of(path: Path) -> str:
        if path == victim:
            path.unlink()  # vanish after listing, before the read
            raise FileNotFoundError(2, "No such file or directory")
        return sha256_of(path)

    monkeypatch.setattr(
        "tapevaultui.services.archive_builder.sha256_of", vanishing_sha256_of
    )
    entry = plan_archive_source(_source(tmp_path), work_dir=tmp_path / "work")

    assert entry is not None
    assert entry.inner_count == 1  # a.txt skipped, nested/b.txt remains
    with zipfile.ZipFile(entry.staging_path) as zf:
        assert zf.namelist() == ["nested/b.txt"]


def test_plan_rebuilds_after_file_vanishes_during_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file deleted after the walk (mid zip-stream) triggers exactly one
    re-walk + rebuild so the archive is consistent.
    """
    from tapevaultui.services.archive_builder import _build_zip

    src = _make_tree(tmp_path)
    victim = src / "nested" / "b.txt"
    real_build = _build_zip
    builds = {"count": 0}

    def flaky_build(root: Path, entries, zip_path: Path) -> tuple[int, str]:
        builds["count"] += 1
        if builds["count"] == 1:
            victim.unlink()  # vanish between walk and build open()
            real_build(root, entries, zip_path)  # raises OSError midway
        return real_build(root, entries, zip_path)

    monkeypatch.setattr(
        "tapevaultui.services.archive_builder._build_zip", flaky_build
    )
    entry = plan_archive_source(_source(tmp_path), work_dir=tmp_path / "work")

    assert entry is not None
    assert builds["count"] == 2  # one retry, not a loop
    assert entry.inner_count == 1
    with zipfile.ZipFile(entry.staging_path) as zf:
        assert zf.namelist() == ["a.txt"]
    assert not Path(entry.staging_path).with_suffix(".zip.tmp").exists()
