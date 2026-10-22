"""What is visible on an ME's Program, through every path: its background (Program, and both
sources of a running mix), the sources of its visible luma keys and pips, and for each higher ME
it shows (re-entry as background or in a key) what is visible on that ME's Program, recursively.

A key's source is hidden while the keyer is off, its gain is 0, its box shows nothing of the
picture on the raster (size 0, off screen, cropped away), or an opaque pip above it covers the
whole raster at full gain without a soft edge; such a pip also hides the background. Partly
covered layers stay visible. The engine applies the same rules per grain for audio follows video
(engine/src/core/visibility.hpp); tally uses this.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from flowxer.api.schemas import Dme

# Less than this (a fraction of the raster) shows nothing.
MIN_SHOWN = 1e-4


@dataclass(frozen=True)
class KeyLayer:
    """One keyer of an ME as tally sees it."""

    source_id: str | None
    # luma or pip (a linear key's fill is no source of the mixer).
    keys_source: bool
    pip: bool
    upstream: bool
    # On air: on, fading or flying off, or tied to a held transition.
    on_air: bool
    # On air at its full level (not fading).
    full: bool
    # Fading or flying off: visible until it ends, wherever its box is going.
    leaving: bool
    gain: float
    # The box (upstream keyers with a box, always for a pip), else None.
    dme: Dme | None


@dataclass(frozen=True)
class MeLayers:
    """An ME's background sources and its keyers (configuration order)."""

    background: tuple[str, ...]
    keys: tuple[KeyLayer, ...]


def picture_rect(dme: Dme) -> tuple[float, float, float, float]:
    """The box's picture after the crop on the raster (fractions): left, right, top, bottom."""
    half = dme.size / 2
    return (
        dme.x - half + dme.crop.left * dme.size,
        dme.x + half - dme.crop.right * dme.size,
        dme.y - half + dme.crop.top * dme.size,
        dme.y + half - dme.crop.bottom * dme.size,
    )


def box_shows(dme: Dme) -> bool:
    if dme.size <= 0:
        return False
    left, right, top, bottom = picture_rect(dme)
    return min(right, 1.0) - max(left, 0.0) > MIN_SHOWN and min(bottom, 1.0) - max(top, 0.0) > MIN_SHOWN


def box_covers(dme: Dme) -> bool:
    left, right, top, bottom = picture_rect(dme)
    return dme.soft <= 0 and left <= 0 and right >= 1 and top <= 0 and bottom >= 1


def key_visibility(keys: Sequence[KeyLayer]) -> tuple[list[str], bool]:
    """The sources of the visible keys (key order) and whether the background is covered."""
    # DSKs are over the upstream keys; from the top down a covering pip hides what is under it.
    layers = [k for k in keys if k.upstream] + [k for k in keys if not k.upstream]
    found: list[str] = []
    covered = False
    for key in reversed(layers):
        if not key.on_air or key.gain <= 0:
            continue
        if key.dme is not None and not key.leaving and not box_shows(key.dme):
            continue
        if key.keys_source and key.source_id and key.source_id not in found:
            found.append(key.source_id)
        if key.pip and key.dme is not None and key.full and key.gain >= 1 and box_covers(key.dme):
            covered = True
            break
    found.reverse()
    return found, covered


def direct_visibility(me: MeLayers) -> list[str]:
    """What ME shows itself (higher MEs as their ids, not opened up)."""
    keyed, covered = key_visibility(me.keys)
    shown = [] if covered else [s for s in me.background if s]
    return shown + [s for s in keyed if s not in shown]


def visible_on_program(panel_id: str, mes: Mapping[str, MeLayers]) -> list[str]:
    """Everything visible on ME `panel_id`'s Program through any path: its own sources, and for
    each ME it shows, that ME's id and what is visible on its Program (recursively)."""
    out: list[str] = []
    seen: set[str] = set()

    def walk(me_id: str) -> None:
        if me_id in seen or me_id not in mes:
            return
        seen.add(me_id)
        for source in direct_visibility(mes[me_id]):
            if source not in out:
                out.append(source)
            if source in mes:
                walk(source)

    walk(panel_id)
    return out
