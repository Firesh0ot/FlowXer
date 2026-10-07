"""Loop-conforming audio to whole-frame lengths (incl. 59.94 cadence)."""

from __future__ import annotations

import array
import struct
from pathlib import Path


def samples_until_grain(index: int, rate_num: int, rate_den: int, sample_rate: int = 48000) -> int:
    """Truncating integer cadence used by mxl-test-player.

    For 60000/1001 at 48 kHz this yields the 800/801 sample pattern per grain.
    """
    if rate_num <= 0 or rate_den <= 0 or index <= 0:
        return 0
    return (index * sample_rate * rate_den) // rate_num


def samples_in_grain(index: int, rate_num: int, rate_den: int, sample_rate: int = 48000) -> int:
    a = samples_until_grain(index, rate_num, rate_den, sample_rate)
    b = samples_until_grain(index + 1, rate_num, rate_den, sample_rate)
    return b - a


def conform_interleaved(
    src: list[float] | array.array,
    src_channels: int,
    src_samples: int,
    dst_channels: int,
    dst_samples: int,
    crossfade_samples: int = 0,
) -> array.array:
    """Pad/truncate interleaved float32 audio and optional loop-point crossfade."""
    if dst_channels < 1 or dst_samples <= 0:
        return array.array("f")
    dst = array.array("f", [0.0] * (dst_samples * dst_channels))
    if src and src_channels > 0 and src_samples > 0:
        n = min(src_samples, dst_samples)
        ch = min(src_channels, dst_channels)
        for i in range(n):
            for c in range(ch):
                dst[i * dst_channels + c] = float(src[i * src_channels + c])
    if crossfade_samples > 1 and dst_samples > crossfade_samples * 2:
        n = crossfade_samples
        for i in range(n):
            a = i / float(n)
            head = i
            tail = dst_samples - n + i
            for c in range(dst_channels):
                h = dst[head * dst_channels + c]
                t = dst[tail * dst_channels + c]
                m = h * (1.0 - a) + t * a
                dst[head * dst_channels + c] = m
                dst[tail * dst_channels + c] = m
    return dst


def read_f32le(path: Path) -> array.array:
    data = path.read_bytes()
    count = len(data) // 4
    out = array.array("f")
    out.frombytes(data[: count * 4])
    return out


def write_f32le(path: Path, samples: array.array) -> None:
    path.write_bytes(samples.tobytes())


def pack_silence(channels: int, samples: int) -> array.array:
    return array.array("f", [0.0] * (max(channels, 1) * max(samples, 0)))


def cut_frame_after_fps_change(
    cut_ms: int | None,
    cut_frame: int | None,
    old_fps: float,
    new_fps: float,
    new_frame_count: int,
) -> tuple[int, int]:
    """Keep the visual cut point when the asset framerate changes.

    Prefer ``cut_ms`` (wall-time of the cut in the original) then recompute
    the frame index at the new rate.
    """
    if new_frame_count <= 0:
        return 0, 0
    if cut_ms is None and cut_frame is not None and old_fps > 0:
        cut_ms = int(round((max(cut_frame, 0) / old_fps) * 1000))
    if cut_ms is None:
        cut_ms = 0
    if new_fps <= 0:
        frame = 0
    else:
        frame = int(round((max(cut_ms, 0) / 1000.0) * new_fps))
    frame = max(0, min(frame, new_frame_count - 1))
    ms = int(round((frame / new_fps) * 1000)) if new_fps > 0 else 0
    return frame, ms


# Keep struct available for potential binary helpers / tests.
_ = struct
