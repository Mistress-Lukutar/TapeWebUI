"""
File:   test_backup_stats.py
Brief:  BackupStats.snapshot() verify-phase override and writing-path regression tests.
Author: Mistress-Lukutar
Date:   2026-11-09
Version: v0.11.0
"""

from __future__ import annotations

from typing import Any

import pytest

from tapevaultui.services.backup_engine import BackupStats


def _make_stats(**overrides: Any) -> BackupStats:
    """Build a BackupStats with deterministic clock anchors and overrides.

    ``started_monotonic`` is pinned to 100.0 so a patched clock at 110.0
    yields an elapsed of exactly 10 s; individual tests may override it.

    Args:
        **overrides: Attribute values to set after construction.

    Returns:
        A configured BackupStats instance.
    """
    stats = BackupStats("testjob")
    stats.started_monotonic = 100.0
    for name, value in overrides.items():
        setattr(stats, name, value)
    return stats


@pytest.fixture()
def clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Patch backup_engine's time.monotonic to a controllable fake clock.

    Returns:
        A one-element list holding the current fake time; tests mutate
        ``clock[0]`` before calling snapshot().
    """
    holder = [1000.0]
    monkeypatch.setattr(
        "tapevaultui.services.backup_engine.time.monotonic", lambda: holder[0]
    )
    return holder


class TestSnapshotVerifyingPhase:
    """snapshot() during verifying is driven by the verify counters."""

    def test_verify_counters_replace_written_counters(self, clock: list[float]) -> None:
        """files_done/bytes_done/speed/eta come from the verify counters."""
        stats = _make_stats(
            state="verifying",
            files_total=3,
            files_done=3,
            bytes_total=1_000_000,
            bytes_done=800_000,
            verified_files=2,
            verified_bytes=500_000,
            verify_file_bytes=100_000,
            verify_started_monotonic=1000.0,
        )
        clock[0] = 1002.0  # 2 s elapsed since verify started

        snap = stats.snapshot()

        assert snap["files_done"] == 2
        assert snap["bytes_done"] == 600_000  # 500k confirmed + 100k in-file
        assert snap["bytes_confirmed"] == 800_000  # written bytes, untouched
        assert snap["speed_bps"] == 300_000.0  # 600k / 2 s
        assert snap["eta_s"] == round(400_000 / 300_000)  # remaining / speed

    def test_verify_capped_at_bytes_total(self, clock: list[float]) -> None:
        """verified progress beyond bytes_total caps and zeroes the ETA."""
        stats = _make_stats(
            state="verifying",
            files_total=3,
            bytes_total=500_000,
            bytes_done=400_000,
            verified_files=3,
            verified_bytes=450_000,
            verify_file_bytes=100_000,
            verify_started_monotonic=1000.0,
        )
        clock[0] = 1005.0

        snap = stats.snapshot()

        assert snap["bytes_done"] == 500_000
        assert snap["eta_s"] == 0
        assert snap["bytes_confirmed"] == 400_000

    def test_verify_without_start_time_keeps_written_counters(
        self, clock: list[float]
    ) -> None:
        """verifying state without verify_started_monotonic falls back to write math."""
        stats = _make_stats(
            state="verifying",
            files_done=3,
            bytes_total=1_000_000,
            bytes_done=800_000,
            verified_files=2,
            verified_bytes=500_000,
            verify_started_monotonic=None,
        )
        clock[0] = 1002.0

        snap = stats.snapshot()

        assert snap["files_done"] == 3
        assert snap["bytes_done"] == 800_000
        assert snap["bytes_confirmed"] == 800_000


class TestSnapshotWritingRegression:
    """snapshot() outside verifying behaves exactly as before the change."""

    def test_writing_state_ignores_verify_counters(self, clock: list[float]) -> None:
        """Writing math: written bytes + intra-file estimate; verify fields unused."""
        stats = _make_stats(
            state="writing",
            files_total=10,
            files_done=3,
            bytes_total=1_000_000,
            bytes_done=300_000,
            verified_files=10,
            verified_bytes=900_000,
            verify_file_bytes=50_000,
            verify_started_monotonic=1000.0,
            current_file_started=108.0,
        )
        clock[0] = 110.0  # elapsed 10 s, 2 s inside the current file

        snap = stats.snapshot()

        assert snap["files_done"] == 3  # not verified_files
        speed = 300_000 / 10
        expected = min(300_000 + speed * 2.0, 1_000_000)
        assert snap["bytes_done"] == round(expected)
        assert snap["bytes_confirmed"] == 300_000
        assert snap["speed_bps"] == round(speed, 1)
