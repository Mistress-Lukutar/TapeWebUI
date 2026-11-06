"""
File:   bump-version.py
Brief:  Bump version and date across the project.
Author: Mistress-Lukutar
Date:   2026-11-06
Version: v1.0.0
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import UTC, datetime
from pathlib import Path


def today_str() -> str:
    """Return current UTC date in ISO format."""
    return datetime.now(UTC).strftime("%Y-%m-%d")


def bump_pyproject(path: Path, bump: str) -> str:
    """Bump version in pyproject.toml and return the new version string."""
    content = path.read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*"(\d+)\.(\d+)\.(\d+)"', content, re.MULTILINE)
    if not match:
        raise ValueError("Version not found in pyproject.toml")

    major, minor, patch = map(int, match.groups())
    if bump == "major":
        major, minor, patch = major + 1, 0, 0
    elif bump == "minor":
        major, minor, patch = major, minor + 1, 0
    else:
        major, minor, patch = major, minor, patch + 1

    new_version = f"{major}.{minor}.{patch}"
    new_content = re.sub(
        r'^version\s*=\s*"\d+\.\d+\.\d+"',
        f'version = "{new_version}"',
        content,
        count=1,
        flags=re.MULTILINE,
    )
    path.write_text(new_content, encoding="utf-8")
    return new_version


def bump_python_file(path: Path, new_version: str, new_date: str) -> None:
    """Update Date and Version in a Python file header."""
    content = path.read_text(encoding="utf-8")

    content = re.sub(
        r"^Date:\s*\d{4}-\d{2}-\d{2}",
        f"Date:   {new_date}",
        content,
        count=1,
        flags=re.MULTILINE,
    )

    content = re.sub(
        r"^Version:\s*v\d+\.\d+\.\d+",
        f"Version: v{new_version}",
        content,
        count=1,
        flags=re.MULTILINE,
    )

    path.write_text(content, encoding="utf-8")


def bump_init(init_file: Path, new_version: str) -> None:
    """Update __version__ in package __init__.py."""
    content = init_file.read_text(encoding="utf-8")
    new_content = re.sub(
        r'^__version__\s*=\s*".*?"',
        f'__version__ = "{new_version}"',
        content,
        count=1,
        flags=re.MULTILINE,
    )
    init_file.write_text(new_content, encoding="utf-8")


def main() -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description="Bump project version and dates")
    parser.add_argument(
        "--bump",
        choices=("patch", "minor", "major"),
        default="patch",
        help="Semver component to increment",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path.cwd(),
        help="Project root directory",
    )
    parser.add_argument(
        "paths",
        nargs="*",
        type=Path,
        help="Files or directories to process (default: src/ and tests/)",
    )
    args = parser.parse_args()

    pyproject = args.root / "pyproject.toml"
    if not pyproject.exists():
        print("pyproject.toml not found", file=sys.stderr)
        return 1

    new_version = bump_pyproject(pyproject, args.bump)
    new_date = today_str()
    print(f"pyproject.toml -> {new_version}")

    for init_file in args.root.rglob("__init__.py"):
        if "src" in init_file.parts and "test" not in init_file.parts:
            bump_init(init_file, new_version)
            print(f"{init_file} -> {new_version}")
            break

    targets: list[Path] = []
    if args.paths:
        for p in args.paths:
            if p.is_dir():
                targets.extend(p.rglob("*.py"))
            elif p.suffix == ".py":
                targets.append(p)
    else:
        for default in (args.root / "src", args.root / "tests"):
            if default.exists():
                targets.extend(default.rglob("*.py"))

    for py_file in targets:
        bump_python_file(py_file, new_version, new_date)
        print(f"{py_file} -> {new_version}, {new_date}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
