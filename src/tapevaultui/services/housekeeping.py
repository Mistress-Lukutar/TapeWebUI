"""
File:   housekeeping.py
Brief:  Startup / job-start cleanup of transient work files and legacy caches.
Author: Mistress-Lukutar
Date:   2026-11-10
Version: v0.1.0
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path

import structlog

logger = structlog.get_logger(__name__)

# Glob patterns covering every transient file the app writes into the
# work dir (data/tmp): verify read-back buffers, tape header reads
# (t0/hdr), per-session header JSONs, restore downloads and freshly
# built archive zips. Each is created, used and deleted within a single
# operation, so any survivor is crash/cancellation leftovers.
TRANSIENT_PATTERNS = (
    "verify_*.bin",
    "t0_*.bin",
    "hdr_*.bin",
    "header_*.json",
    "restore_*.bin",
    "archive_*.zip",
    "*.zip.tmp",
)

# Job-start sweep age floor. The API-side busy guards are check-then-act,
# so a device verify or restore started just before a job enqueues can
# still be running when the job's sweep fires; files younger than this
# may belong to it. Nothing legitimate lives longer: verify/restore
# buffers are per-item and released within minutes even for multi-GB
# files (direct reads run at ~75 MiB/s).
STALE_AGE_S = 1800


def sweep_tmp(work_dir: Path, *, min_age_s: float | None = None) -> tuple[int, int]:
    """Remove transient files from the work directory.

    Args:
        work_dir: The app work directory (``<data>/tmp``).
        min_age_s: Skip files younger than this many seconds (protects
            files possibly still in use by an overlapping drive
            operation). None removes every match regardless of age —
            only safe at app startup, when nothing else is running.

    Returns:
        (removed_files, freed_bytes).
    """
    if not work_dir.is_dir():
        return (0, 0)

    removed = 0
    freed = 0
    now = time.time()
    for pattern in TRANSIENT_PATTERNS:
        for path in work_dir.glob(pattern):
            if not path.is_file():
                continue
            if min_age_s is not None and now - path.stat().st_mtime < min_age_s:
                continue
            try:
                size = path.stat().st_size
                path.unlink()
            except OSError as exc:
                # Locked by an in-flight operation, or vanished between
                # glob and unlink: leave it for the next sweep instead
                # of failing the whole pass.
                logger.debug("tmp_sweep_skip", path=str(path), error=str(exc))
                continue
            freed += size
            removed += 1
    if removed:
        logger.info("tmp_swept", files=removed, bytes=freed)
    return (removed, freed)


def remove_staging_cache(staging_dir: Path) -> None:
    """Delete the legacy persistent archive-zip cache directory.

    The persistent staging cache was removed in favour of building each
    run's archive zips fresh in the work dir; a leftover directory is
    dead weight (multi-GB, and superseded entries were never pruned).
    Called once at app startup; idempotent.

    Args:
        staging_dir: The cache directory (``<data>/staging``).
    """
    if not staging_dir.is_dir():
        return
    freed = 0
    for path in staging_dir.rglob("*"):
        try:
            if path.is_file():
                freed += path.stat().st_size
        except OSError:
            continue
    shutil.rmtree(staging_dir, ignore_errors=True)
    logger.info("staging_cache_removed", path=str(staging_dir), bytes=freed)
