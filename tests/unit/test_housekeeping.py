"""
File:   test_housekeeping.py
Brief:  Housekeeping module tests: transient-file sweep and staging-cache removal.
Author: Mistress-Lukutar
Date:   2026-11-10
Version: v0.1.0
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from tapevaultui.services.housekeeping import (
    STALE_AGE_S,
    remove_staging_cache,
    sweep_tmp,
)


def test_sweep_tmp_removes_all_transient_patterns(tmp_path: Path) -> None:
    """Every TRANSIENT_PATTERNS entry is cleaned up and counts are correct."""
    # Create one file per pattern with a known size.
    transient_files = {
        "verify_data.bin": 1024,
        "t0_abc12345.bin": 2048,
        "hdr_deadbeef.bin": 512,
        "header_f1e2d3c4.json": 4096,
        "restore_payload.bin": 768,
        "archive_backup.zip": 3072,
        "work.zip.tmp": 256,
    }
    for name, size in transient_files.items():
        (tmp_path / name).write_bytes(b"X" * size)

    # Create a file that does NOT match any pattern — it should survive.
    (tmp_path / "keepme.txt").write_bytes(b"keep me")

    removed, freed = sweep_tmp(tmp_path)
    assert removed == len(transient_files)
    assert freed == sum(transient_files.values())

    # All transient files gone.
    for name in transient_files:
        assert not (tmp_path / name).exists()

    # Non-matching file survives.
    assert (tmp_path / "keepme.txt").exists()


def test_sweep_tmp_with_min_age_s(tmp_path: Path) -> None:
    """Files younger than min_age_s survive; older files are removed."""
    old_file = tmp_path / "verify_old.bin"
    new_file = tmp_path / "verify_new.bin"

    old_data = b"old data"
    new_data = b"new data"
    old_file.write_bytes(old_data)
    new_file.write_bytes(new_data)

    # Backdate the old file to 1 hour ago.
    old_time = time.time() - 3600
    os.utime(old_file, (old_time, old_time))

    removed, freed = sweep_tmp(tmp_path, min_age_s=1800)

    assert removed == 1
    assert freed == len(old_data)
    assert not old_file.exists()
    assert new_file.exists()


def test_sweep_tmp_nonexistent_dir_returns_zero(tmp_path: Path) -> None:
    """A nonexistent work directory returns (0, 0) without error."""
    missing = tmp_path / "no_such_dir"
    removed, freed = sweep_tmp(missing)
    assert removed == 0
    assert freed == 0


def test_remove_staging_cache_removes_nested_tree(tmp_path: Path) -> None:
    """remove_staging_cache deletes a directory with nested subdirs/files."""
    cache = tmp_path / "staging"
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "sub1").mkdir(parents=True, exist_ok=True)
    (cache / "sub1" / "file1.zip").write_bytes(b"data1")
    (cache / "sub2").mkdir(parents=True, exist_ok=True)
    (cache / "sub2" / "file2.zip").write_bytes(b"data2")

    remove_staging_cache(cache)
    assert not cache.exists()


def test_remove_staging_cache_nonexistent_is_noop(tmp_path: Path) -> None:
    """Removing a nonexistent staging directory raises no exception."""
    missing = tmp_path / "staging"
    remove_staging_cache(missing)  # should not raise


def test_stale_age_s_constant() -> None:
    """STALE_AGE_S equals 1800 (one-line sanity assert)."""
    assert STALE_AGE_S == 1800
