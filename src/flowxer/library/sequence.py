"""TGA sequence validation and ZIP ingest (Zip-Slip / bomb hardened)."""

from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image

TGA_RE = re.compile(r"^(?P<stem>.*?)(?P<num>\d+)\.tga$", re.IGNORECASE)

DEFAULT_MAX_ENTRIES = 10_000
DEFAULT_MAX_UNPACKED_BYTES = 2 * 1024 * 1024 * 1024  # 2 GiB
DEFAULT_MAX_SINGLE_FILE = 256 * 1024 * 1024


@dataclass
class SequenceIssue:
    level: str  # error | warning
    message: str


@dataclass
class SequenceInfo:
    files: list[Path]
    pattern: str
    frame_count: int
    width: int
    height: int
    has_alpha: bool
    issues: list[SequenceIssue] = field(default_factory=list)
    # First frame number: ffmpeg's image2 input needs it as -start_number.
    start_number: int = 0

    @property
    def ok(self) -> bool:
        return self.frame_count > 0 and not any(i.level == "error" for i in self.issues)


def natural_tga_sort_key(path: Path) -> tuple:
    match = TGA_RE.match(path.name)
    if not match:
        return (path.name.lower(), 0)
    return (match.group("stem").lower(), int(match.group("num")))


def discover_tga_files(directory: Path) -> list[Path]:
    files = [p for p in directory.iterdir() if p.is_file() and p.suffix.lower() == ".tga"]
    return sorted(files, key=natural_tga_sort_key)


def infer_pattern(files: list[Path]) -> str:
    if not files:
        return "frame_%05d.tga"
    first = files[0].name
    match = TGA_RE.match(first)
    if not match:
        return first
    stem = match.group("stem")
    suffix = first[match.end("num"):]
    widths = [len(m.group("num")) for m in (TGA_RE.match(f.name) for f in files) if m]
    # The smallest padding: %02d also matches 100. The extension keeps its case (.TGA).
    return f"{stem}%0{min(widths)}d{suffix}"


def validate_sequence(directory: Path) -> SequenceInfo:
    issues: list[SequenceIssue] = []
    files = discover_tga_files(directory)
    if not files:
        issues.append(SequenceIssue("error", "no .tga frames found"))
        return SequenceInfo([], "frame_%05d.tga", 0, 0, 0, False, issues)

    numbers: list[int] = []
    stems: set[str] = set()
    for path in files:
        match = TGA_RE.match(path.name)
        if not match:
            issues.append(SequenceIssue("error", f"non-numeric TGA name: {path.name}"))
            continue
        stems.add(match.group("stem"))
        numbers.append(int(match.group("num")))

    if len(stems) > 1:
        issues.append(SequenceIssue("error", f"mixed filename stems: {sorted(stems)}"))
    suffixes = {path.name[-4:] for path in files}
    if len(suffixes) > 1:
        issues.append(SequenceIssue("error", f"mixed file name extensions: {sorted(suffixes)}"))
    pattern = infer_pattern(files)
    pad_match = re.search(r"%0(\d+)d", pattern)
    pad = int(pad_match.group(1)) if pad_match else 1
    unpadded = sorted(
        path.name
        for path in files
        if (m := TGA_RE.match(path.name)) and m.group("num") != f"{int(m.group('num')):0{pad}d}"
    )
    if unpadded:
        issues.append(SequenceIssue("error", f"inconsistent zero padding: {', '.join(unpadded[:4])}"))

    if numbers:
        expected = list(range(min(numbers), max(numbers) + 1))
        missing = sorted(set(expected) - set(numbers))
        if missing:
            preview = ", ".join(str(n) for n in missing[:8])
            more = "" if len(missing) <= 8 else f" (+{len(missing) - 8} more)"
            issues.append(SequenceIssue("error", f"gaps in numbering: {preview}{more}"))

    width = height = 0
    has_alpha = True
    for path in files:
        try:
            with Image.open(path) as img:
                w, h = img.size
                mode = img.mode
        except OSError as exc:
            issues.append(SequenceIssue("error", f"cannot read {path.name}: {exc}"))
            continue
        if width == 0:
            width, height = w, h
        elif (w, h) != (width, height):
            issues.append(
                SequenceIssue("error", f"resolution mismatch {path.name}: {w}x{h} != {width}x{height}")
            )
        if mode not in {"RGBA", "LA", "RGBa"}:
            # TGA may load as RGB without alpha.
            if "A" not in mode:
                has_alpha = False

    if not has_alpha:
        issues.append(SequenceIssue("warning", "sequence has no alpha channel"))

    return SequenceInfo(
        files=files,
        pattern=pattern,
        frame_count=len(files),
        width=width,
        height=height,
        has_alpha=has_alpha,
        issues=issues,
        start_number=min(numbers) if numbers else 0,
    )


def _is_ignored_zip_member(name: str) -> bool:
    parts = Path(name).parts
    if "__MACOSX" in parts:
        return True
    base = Path(name).name
    if base.startswith(".") or base == "Thumbs.db":
        return True
    return False


def _tga_members(
    zf: zipfile.ZipFile,
    *,
    max_entries: int,
    max_unpacked_bytes: int,
    max_single_file: int,
) -> tuple[list[zipfile.ZipInfo], str]:
    """Checks on the central directory only: the .tga members and their one root folder."""
    infos = [i for i in zf.infolist() if not i.is_dir()]
    tga_members = []
    for info in infos:
        name = info.filename.replace("\\", "/")
        if _is_ignored_zip_member(name):
            continue
        if Path(name).suffix.lower() != ".tga":
            continue
        tga_members.append(info)

    if not tga_members:
        raise ValueError("zip contains no .tga files")
    if len(tga_members) > max_entries:
        raise ValueError(f"too many entries ({len(tga_members)} > {max_entries})")

    # Exactly one root folder (or all files under one top-level directory).
    tops = set()
    for info in tga_members:
        parts = Path(info.filename.replace("\\", "/")).parts
        if not parts:
            raise ValueError("empty zip member name")
        tops.add(parts[0])
    if len(tops) != 1:
        raise ValueError("zip must contain exactly one root folder")

    total = 0
    for info in tga_members:
        if info.external_attr >> 16 & 0o170000 == 0o120000:
            raise ValueError(f"symlink rejected: {info.filename}")
        # ZipInfo flag_bits bit 11 is UTF-8; create_system etc. — also reject abs paths.
        name = info.filename.replace("\\", "/")
        if name.startswith("/") or ".." in Path(name).parts:
            raise ValueError(f"unsafe path rejected: {info.filename}")
        size = info.file_size
        if size > max_single_file:
            raise ValueError(f"entry too large: {info.filename}")
        total += size
        if total > max_unpacked_bytes:
            raise ValueError("unpacked size exceeds limit")
    return tga_members, next(iter(tops))


def inspect_tga_zip(
    zip_path: Path,
    *,
    max_entries: int = DEFAULT_MAX_ENTRIES,
    max_unpacked_bytes: int = DEFAULT_MAX_UNPACKED_BYTES,
    max_single_file: int = DEFAULT_MAX_SINGLE_FILE,
) -> int:
    """Validate a TGA ZIP without unpacking it (fast enough for an upload request).
    Returns the number of .tga members."""
    if not zipfile.is_zipfile(zip_path):
        raise ValueError("not a zip archive")
    with zipfile.ZipFile(zip_path, "r") as zf:
        members, _ = _tga_members(
            zf,
            max_entries=max_entries,
            max_unpacked_bytes=max_unpacked_bytes,
            max_single_file=max_single_file,
        )
    return len(members)


def safe_extract_tga_zip(
    zip_path: Path,
    dest: Path,
    *,
    max_entries: int = DEFAULT_MAX_ENTRIES,
    max_unpacked_bytes: int = DEFAULT_MAX_UNPACKED_BYTES,
    max_single_file: int = DEFAULT_MAX_SINGLE_FILE,
) -> Path:
    """Extract only ``.tga`` files from a ZIP into ``dest``.

    Rejects Zip-Slip paths, symlinks, and oversized archives. Requires exactly
    one root folder inside the ZIP (or files at a single common root).
    Returns the directory that contains the TGA frames.
    """
    dest.mkdir(parents=True, exist_ok=True)
    if not zipfile.is_zipfile(zip_path):
        raise ValueError("not a zip archive")

    with zipfile.ZipFile(zip_path, "r") as zf:
        tga_members, root_name = _tga_members(
            zf,
            max_entries=max_entries,
            max_unpacked_bytes=max_unpacked_bytes,
            max_single_file=max_single_file,
        )
        dest_resolved = dest.resolve()
        for info in tga_members:
            name = info.filename.replace("\\", "/")
            size = info.file_size
            target = (dest / name).resolve()
            if not target.is_relative_to(dest_resolved):
                raise ValueError(f"zip-slip rejected: {info.filename}")
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info, "r") as src, open(target, "wb") as out:
                remaining = size
                while remaining > 0:
                    chunk = src.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    out.write(chunk)
                    remaining -= len(chunk)

        root = dest / root_name
        if root.is_dir():
            return root
        # Flat single-folder name that was actually a file prefix — use dest.
        return dest
