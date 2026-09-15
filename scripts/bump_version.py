#!/usr/bin/env python3
"""Bump FlowXer version: (main promotions).(stage promotions).(code pushes)."""

from __future__ import annotations

import argparse
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def parse_version(text: str) -> tuple[int, int, int]:
    parts = text.strip().split(".")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        raise ValueError(f"VERSION must be MAJOR.MINOR.PATCH, got {text!r}")
    return int(parts[0]), int(parts[1]), int(parts[2])


def format_version(major: int, minor: int, patch: int) -> str:
    return f"{major}.{minor}.{patch}"


def read_version(root: Path = ROOT) -> tuple[int, int, int]:
    return parse_version((root / "VERSION").read_text(encoding="utf-8"))


def write_version(major: int, minor: int, patch: int, root: Path = ROOT) -> str:
    version = format_version(major, minor, patch)
    (root / "VERSION").write_text(version + "\n", encoding="utf-8")
    pyproject = root / "pyproject.toml"
    pyproject.write_text(
        re.sub(
            r'^version = "[^"]+"',
            f'version = "{version}"',
            pyproject.read_text(encoding="utf-8"),
            count=1,
            flags=re.M,
        ),
        encoding="utf-8",
    )
    init = root / "src/flowxer/__init__.py"
    init.write_text(
        re.sub(
            r'__version__ = "[^"]+"',
            f'__version__ = "{version}"',
            init.read_text(encoding="utf-8"),
            count=1,
        ),
        encoding="utf-8",
    )
    return version


def bump(part: str, root: Path = ROOT, count: int = 1) -> str:
    if count < 1:
        raise ValueError("count must be >= 1")
    major, minor, patch = read_version(root)
    if part == "patch":
        patch += count
    elif part == "minor":
        minor += count
    elif part == "major":
        major += count
    else:
        raise ValueError(part)
    return write_version(major, minor, patch, root)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("part", choices=["major", "minor", "patch", "show"])
    parser.add_argument("--count", type=int, default=1)
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args()
    if args.part == "show":
        print(format_version(*read_version(args.root)))
        return
    print(bump(args.part, args.root, args.count))


if __name__ == "__main__":
    main()
