"""
File:   test_snapshot.py
Brief:  Source snapshotter tests: walking, patterns, hashing, errors.
Author: Mistress-Lukutar
Date:   2026-11-07
Version: v0.7.0
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tapevaultui.services.job_defs import SourceDef
from tapevaultui.services.snapshot import SnapshotError, snapshot_sources


@pytest.fixture
def source_tree(tmp_path: Path) -> Path:
    """Create a small source tree with mixed file types.

    Args:
        tmp_path: Pytest temp directory.

    Returns:
        The source root path.
    """
    root = tmp_path / "src"
    (root / "sub").mkdir(parents=True)
    (root / "a.jpg").write_bytes(b"AAA")
    (root / "b.txt").write_bytes(b"BB")
    (root / "sub" / "c.jpg").write_bytes(b"CCC")
    return root


def test_snapshot_all_files(source_tree: Path) -> None:
    """Without patterns all files are planned with correct hashes."""
    import hashlib

    plan = snapshot_sources([SourceDef(path=str(source_tree))])
    assert [Path(f.path).name for f in plan] == ["a.jpg", "b.txt", "sub/c.jpg"][
        ::1
    ] or [Path(f.path).name for f in plan] == ["a.jpg", "b.txt", "c.jpg"]
    sizes = sorted(f.size for f in plan)
    assert sizes == [2, 3, 3]
    expected = hashlib.sha256(b"AAA").hexdigest()
    assert any(f.sha256 == expected for f in plan)


def test_snapshot_with_patterns(source_tree: Path) -> None:
    """Patterns restrict the plan to matching files."""
    plan = snapshot_sources([SourceDef(path=str(source_tree), patterns=["**/*.jpg"])])
    names = sorted(Path(f.path).name for f in plan)
    assert names == ["a.jpg", "c.jpg"]


def test_snapshot_excludes_dir_prunes_subtree(tmp_path: Path) -> None:
    """An ignored directory is pruned whole, including deep files."""
    root = tmp_path / "src"
    (root / "temp" / "deep").mkdir(parents=True)
    (root / "keep.txt").write_bytes(b"K")
    (root / "temp" / "junk.tmp").write_bytes(b"J")
    (root / "temp" / "deep" / "buried.tmp").write_bytes(b"B")
    plan = snapshot_sources([SourceDef(path=str(root), excludes=["temp/"])])
    assert [Path(f.path).name for f in plan] == ["keep.txt"]


def test_snapshot_excludes_by_extension(source_tree: Path) -> None:
    """``**/*.jpg`` drops jpg files at any depth, keeps the rest."""
    plan = snapshot_sources([SourceDef(path=str(source_tree), excludes=["**/*.jpg"])])
    names = sorted(Path(f.path).name for f in plan)
    assert names == ["b.txt"]


def test_snapshot_excludes_negation(tmp_path: Path) -> None:
    """A negated rule re-includes a file that other rules ignore."""
    root = tmp_path / "src"
    root.mkdir()
    (root / "a.tmp").write_bytes(b"A")
    (root / "keep.tmp").write_bytes(b"K")
    plan = snapshot_sources(
        [SourceDef(path=str(root), excludes=["*.tmp", "!keep.tmp"])]
    )
    assert [Path(f.path).name for f in plan] == ["keep.tmp"]


def test_snapshot_excludes_combined_with_patterns(tmp_path: Path) -> None:
    """Include patterns and excludes apply together."""
    root = tmp_path / "src"
    (root / "sub").mkdir(parents=True)
    (root / "a.jpg").write_bytes(b"A")
    (root / "b.jpg").write_bytes(b"B")
    (root / "c.txt").write_bytes(b"C")
    (root / "sub" / "d.jpg").write_bytes(b"D")
    plan = snapshot_sources(
        [SourceDef(path=str(root), patterns=["**/*.jpg"], excludes=["b.jpg"])]
    )
    names = sorted(Path(f.path).name for f in plan)
    assert names == ["a.jpg", "d.jpg"]


def test_snapshot_missing_root_continue(source_tree: Path) -> None:
    """A missing source root is skipped with continue_on_error."""
    plan = snapshot_sources(
        [
            SourceDef(path=str(source_tree)),
            SourceDef(path="Z:/does/not/exist"),
        ]
    )
    assert len(plan) == 3


def test_snapshot_missing_root_strict(source_tree: Path) -> None:
    """A missing source root aborts when continue_on_error is False."""
    with pytest.raises(SnapshotError, match="does not exist"):
        snapshot_sources(
            [SourceDef(path="Z:/does/not/exist")],
            continue_on_error=False,
        )


def test_snapshot_deduplicates(tmp_path: Path) -> None:
    """The same directory listed twice yields one copy per file."""
    root = tmp_path / "src"
    root.mkdir()
    (root / "a.txt").write_bytes(b"x")
    plan = snapshot_sources([SourceDef(path=str(root)), SourceDef(path=str(root))])
    assert len(plan) == 1
