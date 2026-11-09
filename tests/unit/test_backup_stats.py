"""
File:   test_backup_stats.py
Brief:  BackupStats.snapshot() verify-phase override, write-speed window
        and indeterminate-progress regression tests.
Author: Mistress-Lukutar
Date:   2026-11-09
Version: v0.11.2
"""

from __future__ import annotations

from typing import Any

import pytest

from tapevaultui.services.backup_engine import BackupStats


@pytest.fixture()
def clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Patch backup_engine's time.monotonic to a controllable fake clock.

    Returns:
        A one-element list holding the current fake time; tests mutate
        ``clock[0]`` before calling snapshot().  BackupStats instances
        must be constructed AFTER patching so their clock anchors pick
        up the fake clock.
    """
    holder = [1000.0]
    monkeypatch.setattr(
        "tapevaultui.services.backup_engine.time.monotonic", lambda: holder[0]
    )
    return holder


def _make_stats(**overrides: Any) -> BackupStats:
    """Build a BackupStats with raw attribute overrides.

    The instance is constructed under the (already patched) fake clock,
    so ``started_monotonic`` / ``state_since_monotonic`` anchor at
    ``clock[0]``; individual tests may override them afterwards.

    Args:
        **overrides: Attribute values to set after construction.

    Returns:
        A configured BackupStats instance.
    """
    stats = BackupStats("testjob")
    for name, value in overrides.items():
        setattr(stats, name, value)
    return stats


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
        assert snap["eta_s"] == round(400_000 / 300_000)
        assert snap["indeterminate"] is False

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
        assert snap["eta_s"] is None  # no write time accumulated -> speed 0


class TestSnapshotWritingPhase:
    """Write speed derives from the writing-state wall time only."""

    def test_speed_uses_completed_write_time(self, clock: list[float]) -> None:
        """Speed ignores preparation and the still-unwritten current file."""
        stats = BackupStats("testjob")  # anchors at clock 1000
        clock[0] = 1008.0
        stats.set_state("writing")  # 8 s of queued/preparing do NOT count
        stats.files_total = 10
        stats.files_done = 3
        stats.bytes_total = 1_000_000
        stats.bytes_done = 300_000
        stats.current_file_started = 1012.0
        clock[0] = 1014.0  # 6 s in writing, 2 s inside the current file

        snap = stats.snapshot()

        speed = 300_000 / 4  # 6 s writing - 2 s inside the current file
        estimated = min(300_000 + speed * 2.0, 1_000_000)
        assert snap["bytes_done"] == round(estimated)
        assert snap["bytes_confirmed"] == 300_000
        assert snap["speed_bps"] == round(speed, 1)
        assert snap["eta_s"] == round((1_000_000 - estimated) / speed)
        assert snap["indeterminate"] is False
        # Whole-job elapsed still reported for information.
        assert snap["elapsed_s"] == 14.0
        assert snap["write_elapsed_s"] == 6.0

    def test_media_wait_excluded_from_write_speed(self, clock: list[float]) -> None:
        """waiting_media between tape passes does not dilute the speed."""
        stats = BackupStats("testjob")
        stats.bytes_done = 100_000
        clock[0] = 1000.0
        stats.set_state("writing")
        clock[0] = 1006.0  # 6 s writing
        stats.current_file_started = 1004.0
        stats.set_state("waiting_media")
        clock[0] = 1060.0  # 54 s waiting for the next cartridge

        waiting = stats.snapshot()
        assert waiting["bytes_done"] == 100_000  # stale in-file timer cleared
        assert waiting["speed_bps"] == round(100_000 / 6, 1)
        assert waiting["write_elapsed_s"] == 6.0

        stats.set_state("writing")
        clock[0] = 1062.0  # +2 s writing on the next tape

        snap = stats.snapshot()

        assert snap["speed_bps"] == round(100_000 / 8, 1)
        assert snap["write_elapsed_s"] == 8.0

    def test_first_file_is_indeterminate(self, clock: list[float]) -> None:
        """No completed file yet: zero speed, no ETA, indeterminate flag."""
        stats = BackupStats("testjob")
        clock[0] = 1010.0
        stats.set_state("writing")
        stats.files_total = 4
        stats.bytes_total = 800_000
        stats.current_file_started = 1010.0
        clock[0] = 1030.0  # 20 s into the first file, none completed

        snap = stats.snapshot()

        assert snap["bytes_done"] == 0
        assert snap["bytes_confirmed"] == 0
        assert snap["speed_bps"] == 0.0
        assert snap["eta_s"] is None
        assert snap["indeterminate"] is True

    def test_preparing_is_indeterminate(self, clock: list[float]) -> None:
        """Preparation (bytes_total unknown) reports an indeterminate bar."""
        stats = BackupStats("testjob")
        clock[0] = 1005.0
        stats.set_state("preparing")

        snap = stats.snapshot()

        assert snap["bytes_total"] == 0
        assert snap["indeterminate"] is True


class TestPhaseAccumulation:
    """set_state accumulates write/verify wall time across transitions."""

    def test_phases_accumulate_across_passes(self, clock: list[float]) -> None:
        """A tape-full split (write -> verify -> write) keeps both buckets."""
        stats = BackupStats("testjob")
        clock[0] = 1000.0
        stats.set_state("preparing")
        clock[0] = 1010.0
        stats.set_state("writing")
        clock[0] = 1030.0
        stats.set_state("verifying")
        clock[0] = 1060.0
        stats.set_state("writing")  # second tape pass
        clock[0] = 1075.0
        stats.set_state("done")

        assert stats.write_elapsed_s == pytest.approx(35.0)  # 20 + 15
        assert stats.verify_elapsed_s == pytest.approx(30.0)
        snap = stats.snapshot()
        assert snap["write_elapsed_s"] == 35.0
        assert snap["verify_elapsed_s"] == 30.0

    def test_done_zero_byte_run_not_indeterminate(self, clock: list[float]) -> None:
        """A finished run never reports indeterminate, even at 0 bytes."""
        stats = BackupStats("testjob")
        stats.set_state("done")

        snap = stats.snapshot()

        assert snap["indeterminate"] is False
