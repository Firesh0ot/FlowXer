"""Loop-conforming audio to whole-frame lengths (incl. 59.94 cadence).

ffmpeg pads or cuts the sound to the sample count (see build_clip_audio_command); this
module computes that count and blends the loop point in place.
"""

from __future__ import annotations

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


def crossfade_loop_file(path: Path, channels: int, samples: int, crossfade_samples: int) -> bool:
    """Blend the head and the tail of raw interleaved float32 audio so a loop has no click:
    sample i of both becomes head*(1-a) + tail*a with a = i/n. Only the two n-sample windows
    are read and written. Returns False when the file is too short for a crossfade."""
    # Imported here, not at module level: numpy loaded before GStreamer makes libmxl unwind
    # with libunwind instead of libgcc, and the exception libmxl throws (and catches) for a
    # missing flow then crashes the mixer (SIGSEGV at start with an unrouted or missing flow).
    import numpy as np

    n = crossfade_samples
    if channels < 1 or n <= 1 or samples <= n * 2:
        return False
    frame = channels * 4
    window = n * frame
    tail_offset = (samples - n) * frame
    with open(path, "r+b") as handle:
        head_bytes = handle.read(window)
        handle.seek(tail_offset)
        tail_bytes = handle.read(window)
        if len(head_bytes) != window or len(tail_bytes) != window:
            return False
        head = np.frombuffer(head_bytes, dtype="<f4").reshape(n, channels)
        tail = np.frombuffer(tail_bytes, dtype="<f4").reshape(n, channels)
        a = (np.arange(n, dtype=np.float64) / n)[:, None]
        mixed = (head * (1.0 - a) + tail * a).astype("<f4").tobytes()
        handle.seek(0)
        handle.write(mixed)
        handle.seek(tail_offset)
        handle.write(mixed)
    return True


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
