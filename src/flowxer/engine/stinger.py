from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw

from flowxer.api.schemas import StingerInfo

VIDEO_SUFFIXES = {".mp4", ".mov", ".webm", ".mkv", ".m4v"}


def stinger_dir(root: Path, stinger_id: str) -> Path:
    return root / stinger_id


def cut_frame_from_ms(cut_ms: int, fps: float, frame_count: int) -> int:
    if frame_count <= 0:
        return 0
    frame = int(round((max(cut_ms, 0) / 1000.0) * fps))
    return max(0, min(frame, frame_count))


def cut_ms_from_frame(cut_frame: int, fps: float) -> int:
    if fps <= 0:
        return 0
    return int(round((max(cut_frame, 0) / fps) * 1000))


def duration_ms_from_frames(frame_count: int, fps: float) -> int:
    if fps <= 0:
        return 0
    return int(round((max(frame_count, 0) / fps) * 1000))


def _parse_frame_rate(value: str | None, default: float = 50.0) -> float:
    if not value:
        return default
    if "/" in value:
        num, den = value.split("/", 1)
        try:
            return float(num) / max(float(den), 1.0)
        except ValueError:
            return default
    try:
        return float(value)
    except ValueError:
        return default


def probe_video_media(path: Path) -> dict | None:
    """Best-effort duration/fps probe. Returns None when ffprobe is missing."""
    if shutil.which("ffprobe") is None or not path.is_file():
        return None
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=nb_frames,r_frame_rate,width,height:format=duration",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=8,
        )
        data = json.loads(result.stdout)
        stream = (data.get("streams") or [{}])[0]
        fmt = data.get("format") or {}
        fps = _parse_frame_rate(stream.get("r_frame_rate"))
        duration = float(fmt.get("duration") or 0)
        nb_frames = stream.get("nb_frames")
        frame_count = int(nb_frames) if nb_frames and str(nb_frames).isdigit() else int(round(duration * fps))
        return {
            "fps": fps,
            "duration_ms": int(round(duration * 1000)),
            "frame_count": max(frame_count, 1),
            "width": int(stream.get("width") or 1920),
            "height": int(stream.get("height") or 1080),
        }
    except (OSError, subprocess.SubprocessError, ValueError, json.JSONDecodeError):
        return None


def _video_files(directory: Path) -> list[Path]:
    found = []
    for path in sorted(directory.iterdir()):
        if path.is_file() and path.suffix.lower() in VIDEO_SUFFIXES:
            found.append(path)
    return found


def inspect_stinger(root: Path, stinger_id: str, fps: float = 50.0) -> StingerInfo | None:
    directory = stinger_dir(root, stinger_id)
    if not directory.is_dir():
        return None
    frames = sorted(directory.glob("frame_*.tga"))
    videos = _video_files(directory)
    meta_path = directory / "stinger.json"
    meta: dict = {}
    if meta_path.is_file():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    kind = str(meta.get("kind") or ("video" if videos and not frames else "sequence"))
    width = int(meta.get("width", 1920))
    height = int(meta.get("height", 1080))
    asset_fps = float(meta.get("fps") or fps)
    if kind == "video":
        media = Path(str(meta.get("media_path") or meta.get("video") or (videos[0] if videos else "")))
        if not media.is_absolute() and str(media):
            candidate = directory / media.name
            media = candidate if candidate.exists() else media
        probed = probe_video_media(media) if media and media.exists() else None
        if probed:
            asset_fps = probed["fps"] or asset_fps
            frame_count = int(meta.get("frame_count") or probed["frame_count"])
            duration_ms = int(meta.get("duration_ms") or probed["duration_ms"])
            width = int(probed["width"] or width)
            height = int(probed["height"] or height)
        else:
            frame_count = int(meta.get("frame_count") or 0)
            duration_ms = int(meta.get("duration_ms") or duration_ms_from_frames(frame_count, asset_fps))
        media_path = str(media) if media else str(directory)
        pattern = ""
        has_alpha = bool(meta.get("has_alpha", True))
        if frame_count <= 0:
            return None
    else:
        frame_count = int(meta.get("frame_count", len(frames)))
        if frames:
            with Image.open(frames[0]) as image:
                width, height = image.size
        duration_ms = int(meta.get("duration_ms") or duration_ms_from_frames(frame_count, asset_fps))
        media_path = str(directory)
        pattern = str(meta.get("pattern", "frame_%05d.tga"))
        has_alpha = True
        if frame_count <= 0:
            return None
    if "cut_ms" in meta:
        cut_ms = int(meta["cut_ms"])
        cut_frame = cut_frame_from_ms(cut_ms, asset_fps, frame_count)
        if "cut_frame" in meta:
            cut_frame = int(meta["cut_frame"])
    else:
        cut_frame = int(meta.get("cut_frame", max(frame_count // 2, 0)))
        cut_ms = cut_ms_from_frame(cut_frame, asset_fps)
    return StingerInfo(
        id=stinger_id,
        path=str(directory),
        kind=kind,
        media_path=media_path,
        frame_count=frame_count,
        cut_frame=cut_frame,
        cut_ms=cut_ms,
        duration_ms=duration_ms,
        fps=asset_fps,
        pattern=pattern,
        width=width,
        height=height,
        has_alpha=has_alpha,
    )


def write_stinger_meta(info: StingerInfo) -> StingerInfo:
    directory = Path(info.path)
    directory.mkdir(parents=True, exist_ok=True)
    meta = {
        "id": info.id,
        "kind": info.kind,
        "frame_count": info.frame_count,
        "cut_frame": info.cut_frame,
        "cut_ms": info.cut_ms,
        "duration_ms": info.duration_ms,
        "fps": info.fps,
        "pattern": info.pattern,
        "media_path": info.media_path,
        "width": info.width,
        "height": info.height,
        "has_alpha": info.has_alpha,
    }
    (directory / "stinger.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return info


def update_stinger_cut(
    root: Path,
    stinger_id: str,
    *,
    fps: float,
    cut_ms: int | None = None,
    cut_frame: int | None = None,
) -> StingerInfo:
    info = inspect_stinger(root, stinger_id, fps=fps)
    if info is None:
        raise FileNotFoundError(stinger_id)
    if cut_ms is not None:
        info.cut_ms = max(0, int(cut_ms))
        info.cut_frame = cut_frame_from_ms(info.cut_ms, info.fps or fps, info.frame_count)
    elif cut_frame is not None:
        info.cut_frame = max(0, min(int(cut_frame), info.frame_count))
        info.cut_ms = cut_ms_from_frame(info.cut_frame, info.fps or fps)
    return write_stinger_meta(info)


def register_video_stinger(
    root: Path,
    stinger_id: str,
    video_path: Path,
    *,
    fps: float,
    cut_ms: int | None = None,
    duration_ms: int | None = None,
    width: int = 1920,
    height: int = 1080,
) -> StingerInfo:
    if not video_path.is_file():
        raise FileNotFoundError(str(video_path))
    dest = stinger_dir(root, stinger_id)
    dest.mkdir(parents=True, exist_ok=True)
    dest_file = dest / video_path.name
    if dest_file.resolve() != video_path.resolve():
        shutil.copy2(video_path, dest_file)
    probed = probe_video_media(dest_file)
    asset_fps = (probed["fps"] if probed else fps) or fps
    frames = int(probed["frame_count"] if probed else 0)
    duration = int(probed["duration_ms"] if probed else 0)
    if duration_ms:
        duration = duration_ms
        frames = max(frames, int(round((duration / 1000.0) * asset_fps)))
    if frames <= 0:
        duration = duration or 1000
        frames = max(1, int(round((duration / 1000.0) * asset_fps)))
    if duration <= 0:
        duration = duration_ms_from_frames(frames, asset_fps)
    cut = duration // 2 if cut_ms is None else max(0, min(int(cut_ms), duration))
    info = StingerInfo(
        id=stinger_id,
        path=str(dest),
        kind="video",
        media_path=str(dest_file),
        frame_count=frames,
        cut_frame=cut_frame_from_ms(cut, asset_fps, frames),
        cut_ms=cut,
        duration_ms=duration,
        fps=asset_fps,
        pattern="",
        width=int(probed["width"] if probed else width),
        height=int(probed["height"] if probed else height),
        has_alpha=True,
    )
    return write_stinger_meta(info)


def list_stingers(root: Path) -> list[StingerInfo]:
    if not root.is_dir():
        return []
    found: list[StingerInfo] = []
    for directory in sorted(p for p in root.iterdir() if p.is_dir()):
        info = inspect_stinger(root, directory.name)
        if info and info.frame_count:
            found.append(info)
    return found


def generate_replay_wipe(
    dest: Path,
    *,
    width: int = 1920,
    height: int = 1080,
    frame_count: int = 50,
    bar_width: int = 240,
) -> StingerInfo:
    """
    Write a TGA sequence with an opaque wipe bar that covers the full frame at
    the midpoint — the mixer cut-point for live ↔ replay.
    """
    dest.mkdir(parents=True, exist_ok=True)
    cut_frame = frame_count // 2
    for index in range(frame_count):
        image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        # Ease the leading edge from off-screen left to off-screen right.
        travel = width + bar_width
        denom = max(frame_count - 1, 1)
        leading = int((index / denom) * travel) - bar_width
        trailing = leading + bar_width
        # Fill everything behind the leading edge so the midpoint is fully opaque.
        if index <= cut_frame:
            draw.rectangle([0, 0, max(trailing, 0), height], fill=(8, 12, 28, 255))
            glow = [
                max(leading, 0),
                0,
                min(trailing, width),
                height,
            ]
            draw.rectangle(glow, fill=(0, 196, 255, 255))
        else:
            draw.rectangle([max(leading, 0), 0, width, height], fill=(8, 12, 28, 255))
            draw.rectangle(
                [max(leading, 0), 0, min(trailing, width), height],
                fill=(255, 64, 32, 255),
            )
        image.save(dest / f"frame_{index:05d}.tga", format="TGA")

    meta = {
        "id": dest.name,
        "kind": "sequence",
        "frame_count": frame_count,
        "cut_frame": cut_frame,
        "cut_ms": cut_ms_from_frame(cut_frame, 50.0),
        "duration_ms": duration_ms_from_frames(frame_count, 50.0),
        "fps": 50.0,
        "pattern": "frame_%05d.tga",
        "media_path": str(dest),
        "width": width,
        "height": height,
        "has_alpha": True,
        "description": "Left-to-right wipe used for live ↔ replay stinger transitions",
    }
    (dest / "stinger.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return inspect_stinger(dest.parent, dest.name)  # type: ignore[return-value]


class StingerPlayer:
    """Frame-accurate stinger state machine independent of GStreamer."""

    def __init__(
        self,
        info: StingerInfo,
        target_input_id: str,
        direction: str,
        outgoing_input_id: str | None = None,
        flip_flop: bool = False,
        panel_id: str | None = None,
    ) -> None:
        self.info = info
        self.target_input_id = target_input_id
        self.direction = direction
        self.outgoing_input_id = outgoing_input_id
        self.flip_flop = flip_flop
        self.panel_id = panel_id
        self.frame = 0
        self.cut_fired = False
        self.done = False

    def advance(self, n: int = 1) -> dict:
        events: list[str] = []
        for _ in range(n):
            if self.done:
                break
            self.frame += 1
            if not self.cut_fired and self.frame >= self.info.cut_frame:
                self.cut_fired = True
                events.append("cut")
            if self.frame >= self.info.frame_count:
                self.done = True
                events.append("complete")
        return self.snapshot(events=events)

    def snapshot(self, events: list[str] | None = None) -> dict:
        if self.done:
            phase = "complete"
        elif self.cut_fired:
            phase = "cut"
        elif self.frame > 0:
            phase = "playing"
        else:
            phase = "idle"
        return {
            "id": self.info.id,
            "phase": phase,
            "frame": self.frame,
            "frame_count": self.info.frame_count,
            "cut_frame": self.info.cut_frame,
            "cut_fired": self.cut_fired,
            "done": self.done,
            "target_input_id": self.target_input_id,
            "direction": self.direction,
            "events": events or [],
        }
