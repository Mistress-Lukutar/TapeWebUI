"""
File:   test_archive_builder.py
Brief:  Archive source tests: manifest, staged zip cache, skip/rebuild,
        extraction roundtrip.
Author: Mistress-Lukutar
Date:   2026-11-09
Version: v0.11.3
"""

from __future__ import annotations

import json
import os
import time
import zipfile
from collections.abc import Callable
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


def _counting_sha256_of(
    real_fn: Callable[[Path], str], monkeypatch: pytest.MonkeyPatch
) -> list[Path]:
    """Record every ``sha256_of`` call path in both patched modules.

    ``_walk_source`` imports ``sha256_of`` at module level from
    ``tapevaultui.services.snapshot``, so patching only
    ``archive_builder.sha256_of`` misses calls inside the walk.

    Args:
        real_fn: The original ``sha256_of`` function.
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        List of paths passed to ``sha256_of`` during the patch.
    """
    calls: list[Path] = []

    def counting(path: Path) -> str:
        calls.append(Path(path))
        return real_fn(path)

    monkeypatch.setattr("tapevaultui.services.snapshot.sha256_of", counting)
    monkeypatch.setattr("tapevaultui.services.archive_builder.sha256_of", counting)
    return calls


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
    entry = plan_archive_source(_source(tmp_path), staging_dir=tmp_path / "staging")

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
    first = plan_archive_source(_source(tmp_path), staging_dir=tmp_path / "staging")
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
    first = plan_archive_source(_source(tmp_path), staging_dir=tmp_path / "staging")
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
    first = plan_archive_source(_source(tmp_path), staging_dir=tmp_path / "staging")
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
    first = plan_archive_source(_source(tmp_path), staging_dir=tmp_path / "staging")
    assert first is not None
    zip_bytes = Path(first.staging_path).read_bytes()

    # Force a rebuild by wiping the manifest (cache miss).
    (tmp_path / "staging").glob("*.manifest.json")
    for manifest in (tmp_path / "staging").glob("*.manifest.json"):
        manifest.unlink()
    second = plan_archive_source(_source(tmp_path), staging_dir=tmp_path / "staging")
    assert second is not None
    assert Path(second.staging_path).read_bytes() == zip_bytes


def test_plan_empty_source_returns_none(tmp_path: Path) -> None:
    """A source with no included files plans nothing."""
    (tmp_path / "src").mkdir()
    assert (
        plan_archive_source(_source(tmp_path), staging_dir=tmp_path / "staging") is None
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
    entry = plan_archive_source(_source(tmp_path), staging_dir=tmp_path / "staging")
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

    entry = plan_archive_source(_source(tmp_path), staging_dir=tmp_path / "staging")
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
    entry = plan_archive_source(_source(tmp_path), staging_dir=tmp_path / "staging")

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


def test_plan_build_stores_zip_sha_in_manifest(tmp_path: Path) -> None:
    """A fresh build writes the zip SHA-256 into the sidecar manifest."""
    _make_tree(tmp_path)
    entry = plan_archive_source(_source(tmp_path), staging_dir=tmp_path / "staging")
    assert entry is not None

    # Parse the staging manifest JSON and verify zip_sha256 matches.
    manifest_files = list((tmp_path / "staging").glob("*.manifest.json"))
    assert len(manifest_files) == 1
    parsed = ArchiveManifest.from_bytes(manifest_files[0].read_bytes())
    assert parsed.zip_sha256 is not None
    assert parsed.zip_sha256 == entry.sha256
    assert parsed.zip_sha256 == sha256_of(Path(entry.staging_path))


def test_plan_reuse_uses_stored_hash_without_reading_zip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cached-reuse path returns the stored hash without re-reading the zip."""
    _make_tree(tmp_path)
    first = plan_archive_source(_source(tmp_path), staging_dir=tmp_path / "staging")
    assert first is not None

    # Tamper with the zip bytes while leaving the manifest intact.
    zip_path = Path(first.staging_path)
    zip_path.write_bytes(b"tampered")

    # Record every sha256_of call — reuse must NOT read the zip.
    calls = _counting_sha256_of(sha256_of, monkeypatch)

    second = plan_archive_source(
        _source(tmp_path),
        staging_dir=tmp_path / "staging",
        written_manifest_sha=first.manifest_sha,
        write_even_if_unchanged=True,
    )
    assert second is not None
    # Returned sha must be the stored hash (first.sha256), NOT the tampered file's.
    assert second.sha256 == first.sha256
    # Sanity: the tampered zip hashes differently, proving we got the original.
    assert second.sha256 != sha256_of(Path(second.staging_path))
    # The zip path was never passed to sha256_of during reuse.
    assert Path(first.staging_path) not in calls


def test_plan_legacy_manifest_backfills_zip_sha(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A legacy manifest (no zip_sha256 key) triggers one-time backfill."""
    _make_tree(tmp_path)
    first = plan_archive_source(_source(tmp_path), staging_dir=tmp_path / "staging")
    assert first is not None

    # Rewrite the manifest as LEGACY bytes — manually omit zip_sha256.
    manifest_files = list((tmp_path / "staging").glob("*.manifest.json"))
    assert len(manifest_files) == 1
    parsed = ArchiveManifest.from_bytes(manifest_files[0].read_bytes())
    legacy_json = json.dumps(
        {"entries": parsed.entries, "manifest_sha": parsed.manifest_sha}
    ).encode("utf-8")
    manifest_files[0].write_bytes(legacy_json)

    # Plan again — reuse path should detect legacy and backfill.
    calls = _counting_sha256_of(sha256_of, monkeypatch)
    second = plan_archive_source(
        _source(tmp_path),
        staging_dir=tmp_path / "staging",
        written_manifest_sha=first.manifest_sha,
        write_even_if_unchanged=True,
    )
    assert second is not None
    # The fallback computed the real zip hash.
    expected_hash = sha256_of(Path(first.staging_path))
    assert second.sha256 == expected_hash

    # Manifest file now contains the backfilled zip_sha256.
    re_parsed = ArchiveManifest.from_bytes(manifest_files[0].read_bytes())
    assert re_parsed.zip_sha256 == expected_hash

    # The legacy path triggered exactly one sha256_of of the zip (the backfill).
    assert calls.count(Path(first.staging_path)) == 1

    # Overwrite zip bytes again and verify subsequent reuse does NOT call sha256_of.
    Path(first.staging_path).write_bytes(b"tampered")

    calls2 = _counting_sha256_of(sha256_of, monkeypatch)

    third = plan_archive_source(
        _source(tmp_path),
        staging_dir=tmp_path / "staging",
        written_manifest_sha=re_parsed.manifest_sha,
        write_even_if_unchanged=True,
    )
    assert third is not None
    assert third.sha256 == expected_hash  # backfilled hash reused
    # After backfill the stored hash is used — zip path never read again.
    assert Path(first.staging_path) not in calls2


def test_archive_manifest_zip_sha_roundtrip_and_legacy_parse() -> None:
    """ArchiveManifest serialises with zip_sha256; legacy parses as None."""
    entries = {
        "a.txt": (4, 1700000000.0, "f" * 64),
        "nested/b.txt": (2, 1700000001.0, "0" * 64),
    }
    digest = manifest_digest(entries)
    zip_sha = "a" * 64

    manifest = ArchiveManifest(entries, digest, zip_sha)
    roundtrip = ArchiveManifest.from_bytes(manifest.serialize())
    assert roundtrip.zip_sha256 == zip_sha
    assert roundtrip.entries == entries
    assert roundtrip.manifest_sha == digest

    # The serialized JSON contains the key.
    payload = json.loads(manifest.serialize().decode("utf-8"))
    assert "zip_sha256" in payload
    assert payload["zip_sha256"] == zip_sha

    # Legacy JSON without the key parses as None.
    legacy_bytes = json.dumps(
        {"entries": entries, "manifest_sha": digest}
    ).encode("utf-8")
    legacy_parsed = ArchiveManifest.from_bytes(legacy_bytes)
    assert legacy_parsed.zip_sha256 is None

    # Non-string zip_sha256 values are coerced to None.
    bad_bytes = json.dumps(
        {"entries": entries, "manifest_sha": digest, "zip_sha256": 123}
    ).encode("utf-8")
    bad_parsed = ArchiveManifest.from_bytes(bad_bytes)
    assert bad_parsed.zip_sha256 is None
