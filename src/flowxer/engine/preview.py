"""The operator previews: one mosaic stream from the engine (NVENC once, MediaMTX, WHEP/HLS),
and (FLOWXER_PREVIEW_PGM, the default) each ME's Program as a sharper stream of its own.

The sources and each ME's Preview (and without FLOWXER_PREVIEW_PGM each Program) are tiles of the
mosaic; the GUI crops each tile out of the same <video> stream. GET /api/v1/preview says where the
streams are and where each picture is in the mosaic, with FlowXer's ids: `tiles` in the mosaic,
`streams` the pictures that are streams of their own (`panel:<id>:pgm`).
"""

from __future__ import annotations

from typing import Any


def _join(base: str, path: str, tail: str) -> str:
    return f"{base.rstrip('/')}/{path.strip('/')}/{tail}"


def preview_info(mixer) -> dict[str, Any]:
    """The mosaic stream and its tile map, or why there is none (off-air, simulate)."""
    engine = getattr(mixer, "engine", None)
    if engine is None or not engine.ready or not engine.mosaic_map:
        reason = "simulated media: no pictures" if mixer.backend == "simulate" else "the mixer is off-air"
        return {"available": False, "reason": reason, "tiles": [], "streams": []}
    mosaic = engine.mosaic_map
    if not mosaic.get("enabled", True):
        return {"available": False, "reason": "the preview mosaic is off", "tiles": [], "streams": []}
    live = [item for item in mixer.list_inputs() if item.kind.value == "mxl_live"]
    players = [item for item in mixer.list_inputs() if item.kind.value == "clip_player"]
    panels = list(mixer.panels)
    tiles = []
    for tile in mosaic.get("tiles") or []:
        kind = tile.get("kind")
        source = str(tile.get("source") or "")
        number = int(source[2:]) if source[2:].isdigit() else 0
        entry = {key: tile.get(key) for key in ("x", "y", "w", "h")}
        if kind in {"input", "player"}:
            items = live if kind == "input" else players
            if not 1 <= number <= len(items):
                continue
            item = items[number - 1]
            entry.update(id=f"source:{item.id}", input_id=item.id, kind=kind, label=item.label)
        elif kind in {"program", "preview"}:
            if not 1 <= number <= len(panels):
                continue
            panel = panels[number - 1]
            bus = "pgm" if kind == "program" else "pvw"
            entry.update(id=f"panel:{panel.id}:{bus}", panel_id=panel.id, kind=kind, label=f"{panel.label} {bus.upper()}")
        else:
            continue
        tiles.append(entry)
    path = str(mosaic.get("path") or "")
    whep_base = str(mosaic.get("whep_base") or "")
    hls_base = str(mosaic.get("hls_base") or "")
    streams = []
    for program in mosaic.get("programs") or []:
        number = int(program.get("me") or 0)
        if not 1 <= number <= len(panels):
            continue
        panel = panels[number - 1]
        stream_path = str(program.get("path") or "")
        streams.append(
            {
                "id": f"panel:{panel.id}:pgm",
                "panel_id": panel.id,
                "kind": "program",
                "label": f"{panel.label} PGM",
                "path": stream_path,
                "width": program.get("width"),
                "height": program.get("height"),
                "fps": program.get("fps"),
                "whep_url": _join(whep_base, stream_path, "whep") if whep_base else None,
                "hls_url": _join(hls_base, stream_path, "index.m3u8") if hls_base else None,
            }
        )
    return {
        "available": True,
        "width": mosaic.get("width"),
        "height": mosaic.get("height"),
        "fps": mosaic.get("fps"),
        "mode": mosaic.get("mode"),
        "path": path,
        # Shared MediaMTX (the preview contract): absolute URLs. Own MediaMTX: the GUI plays from
        # the page's host on these ports.
        "whep_url": _join(whep_base, path, "whep") if whep_base else None,
        "hls_url": _join(hls_base, path, "index.m3u8") if hls_base else None,
        "whep_port": None if whep_base else mosaic.get("whep_port"),
        "hls_port": None if hls_base else mosaic.get("hls_port"),
        "tiles": tiles,
        "streams": streams,
    }
