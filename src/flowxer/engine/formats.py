from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class VideoFormat:
    id: str
    label: str
    width: int
    height: int
    frame_rate_num: int
    frame_rate_den: int

    @property
    def raster(self) -> str:
        return f"{self.width}x{self.height}"

    @property
    def frame_rate(self) -> str:
        return f"{self.frame_rate_num}/{self.frame_rate_den}"


VIDEO_FORMATS: dict[str, VideoFormat] = {
    "1080p50": VideoFormat("1080p50", "1080p50 · v210", 1920, 1080, 50, 1),
    "1080p25": VideoFormat("1080p25", "1080p25 · v210", 1920, 1080, 25, 1),
    "1080p59.94": VideoFormat("1080p59.94", "1080p59.94 · v210", 1920, 1080, 60000, 1001),
    "1080p29.97": VideoFormat("1080p29.97", "1080p29.97 · v210", 1920, 1080, 30000, 1001),
    "720p50": VideoFormat("720p50", "720p50 · v210", 1280, 720, 50, 1),
    "720p59.94": VideoFormat("720p59.94", "720p59.94 · v210", 1280, 720, 60000, 1001),
    "2160p50": VideoFormat("2160p50", "2160p50 · v210", 3840, 2160, 50, 1),
    "2160p25": VideoFormat("2160p25", "2160p25 · v210", 3840, 2160, 25, 1),
}

SOURCE_COLORS = [
    (18, 72, 168),
    (16, 132, 92),
    (168, 92, 18),
    (128, 36, 140),
    (20, 128, 148),
    (148, 36, 48),
    (36, 36, 48),
    (72, 72, 20),
    (24, 88, 120),
    (96, 48, 16),
    (48, 96, 48),
    (88, 24, 88),
]


def format_by_id(format_id: str) -> VideoFormat:
    try:
        return VIDEO_FORMATS[format_id]
    except KeyError as exc:
        raise ValueError(f"unknown format {format_id}") from exc
