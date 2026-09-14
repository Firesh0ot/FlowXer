from __future__ import annotations

import json
from pathlib import Path

from PIL import Image, ImageDraw

from flowxer.api.schemas import StingerInfo


def stinger_dir(root: Path, stinger_id: str) -> Path:
    return root / stinger_id


def inspect_stinger(root: Path, stinger_id: str) -> StingerInfo | None:
    directory = stinger_dir(root, stinger_id)
    if not directory.is_dir():
        return None
    frames = sorted(directory.glob("frame_*.tga"))
    meta_path = directory / "stinger.json"
    meta: dict = {}
    if meta_path.is_file():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    frame_count = int(meta.get("frame_count", len(frames)))
    cut_frame = int(meta.get("cut_frame", max(frame_count // 2, 0)))
    width = int(meta.get("width", 1920))
    height = int(meta.get("height", 1080))
    if frames:
        with Image.open(frames[0]) as image:
            width, height = image.size
    return StingerInfo(
        id=stinger_id,
        path=str(directory),
        frame_count=frame_count,
        cut_frame=cut_frame,
        pattern=str(meta.get("pattern", "frame_%05d.tga")),
        width=width,
        height=height,
        has_alpha=True,
    )


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
        "frame_count": frame_count,
        "cut_frame": cut_frame,
        "pattern": "frame_%05d.tga",
        "width": width,
        "height": height,
        "has_alpha": True,
        "description": "Left-to-right wipe used for live ↔ replay stinger transitions",
    }
    (dest / "stinger.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return inspect_stinger(dest.parent, dest.name)  # type: ignore[return-value]


class StingerPlayer:
    """Frame-accurate stinger state machine independent of GStreamer."""

    def __init__(self, info: StingerInfo, target_input_id: str, direction: str) -> None:
        self.info = info
        self.target_input_id = target_input_id
        self.direction = direction
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
