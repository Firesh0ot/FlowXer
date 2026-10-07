"""RAM-clip vs decode-ahead selection."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RamDecision:
    use_ram: bool
    seconds: float
    estimated_bytes: int
    reason: str


def estimate_frame_bytes(width: int, height: int, *, has_alpha: bool) -> int:
    """BGRA (or BGRX) frame size used for RAM budget estimates."""
    bpp = 4 if has_alpha else 4
    return max(width, 1) * max(height, 1) * bpp


def decide_ram_playback(
    *,
    frames: int,
    fps: float,
    width: int,
    height: int,
    has_alpha: bool,
    ram_clip_max_s: float,
    ram_budget_bytes: int,
    budget_used_bytes: int = 0,
) -> RamDecision:
    fps = fps if fps > 0 else 50.0
    seconds = frames / fps if frames > 0 else 0.0
    frame_bytes = estimate_frame_bytes(width, height, has_alpha=has_alpha)
    estimated = frames * frame_bytes
    if seconds > ram_clip_max_s:
        return RamDecision(
            False,
            seconds,
            estimated,
            f"duration {seconds:.2f}s exceeds RAM_CLIP_MAX_S={ram_clip_max_s}",
        )
    if budget_used_bytes + estimated > ram_budget_bytes:
        return RamDecision(
            False,
            seconds,
            estimated,
            f"estimated {estimated} B would exceed RAM_BUDGET "
            f"({budget_used_bytes}+{estimated} > {ram_budget_bytes})",
        )
    return RamDecision(True, seconds, estimated, "within duration and budget limits")
