#!/usr/bin/env python3
"""Bump FlowXer version: (main promotions).(stage promotions).(code pushes)."""

from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# The files that carry the version, and the one line in each that holds it.
VERSION_LINES = {
    "VERSION": re.compile(r"^\d+\.\d+\.\d+$"),
    "pyproject.toml": re.compile(r'^version = "[^"]+"$'),
    "src/flowxer/__init__.py": re.compile(r'^__version__ = "[^"]+"$'),
}


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


def max_components(*versions: tuple[int, int, int]) -> tuple[int, int, int]:
    if not versions:
        raise ValueError("at least one version is required")
    return (
        max(v[0] for v in versions),
        max(v[1] for v in versions),
        max(v[2] for v in versions),
    )


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


def version_from_git_ref(ref: str, root: Path = ROOT) -> tuple[int, int, int] | None:
    try:
        text = subprocess.check_output(
            ["git", "show", f"{ref}:VERSION"],
            cwd=root,
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    try:
        return parse_version(text)
    except ValueError:
        return None


def resolve_version_conflicts(root: Path = ROOT) -> None:
    """Resolve merge-conflict hunks in the version files that only differ in the version line.

    Merging two branches with different versions conflicts in exactly these lines, and the
    version is written again right after. A hunk with any other line is left to a person.
    """
    for name, pattern in VERSION_LINES.items():
        path = root / name
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        if "<<<<<<< " not in text:
            continue
        out: list[str] = []
        hunk: list[str] | None = None
        for line in text.splitlines(keepends=True):
            bare = line.rstrip("\r\n")
            if hunk is None:
                if bare.startswith("<<<<<<< "):
                    hunk = []
                else:
                    out.append(line)
            elif bare.startswith(">>>>>>> "):
                ours: list[str] = []
                in_ours = True
                for entry in hunk:
                    content = entry.rstrip("\r\n")
                    if content == "=======" or content.startswith("||||||| "):
                        in_ours = False
                    elif content.strip() and not pattern.match(content):
                        raise ValueError(
                            f"{name}: merge conflict outside the version line: {content!r}"
                        )
                    elif in_ours:
                        ours.append(entry)
                out.extend(ours)
                hunk = None
            else:
                hunk.append(line)
        if hunk is not None:
            raise ValueError(f"{name}: unterminated merge conflict")
        path.write_text("".join(out), encoding="utf-8")


def reconcile(root: Path, versions: list[tuple[int, int, int]]) -> str:
    resolve_version_conflicts(root)
    found = list(versions)
    if (root / "VERSION").exists():
        found.append(read_version(root))
    if not found:
        raise ValueError("no versions to reconcile")
    major, minor, patch = max_components(*found)
    return write_version(major, minor, patch, root)


def reconcile_refs(root: Path, refs: list[str]) -> str:
    resolve_version_conflicts(root)
    found: list[tuple[int, int, int]] = []
    if (root / "VERSION").exists():
        found.append(read_version(root))
    for ref in refs:
        parsed = version_from_git_ref(ref, root)
        if parsed is not None:
            found.append(parsed)
    if not found:
        raise ValueError("no versions to reconcile")
    major, minor, patch = max_components(*found)
    return write_version(major, minor, patch, root)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "part",
        choices=["major", "minor", "patch", "show", "set", "reconcile", "reconcile-refs"],
    )
    parser.add_argument(
        "version",
        nargs="?",
        help="X.Y.Z for 'set', or extra version strings for 'reconcile'",
    )
    parser.add_argument("extra", nargs="*", help="Additional X.Y.Z values for 'reconcile'")
    parser.add_argument("--count", type=int, default=1)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument(
        "--refs",
        nargs="+",
        default=["origin/main", "origin/stage", "origin/dev"],
        help="Git refs whose VERSION files are included in reconcile-refs",
    )
    args = parser.parse_args()
    if args.part == "show":
        print(format_version(*read_version(args.root)))
        return
    if args.part == "set":
        if not args.version:
            raise SystemExit("set requires a version, e.g. bump_version.py set 1.3.2")
        print(write_version(*parse_version(args.version), root=args.root))
        return
    if args.part == "reconcile":
        strings = [value for value in [args.version, *args.extra] if value]
        print(reconcile(args.root, [parse_version(value) for value in strings]))
        return
    if args.part == "reconcile-refs":
        print(reconcile_refs(args.root, args.refs))
        return
    print(bump(args.part, args.root, args.count))


if __name__ == "__main__":
    main()
