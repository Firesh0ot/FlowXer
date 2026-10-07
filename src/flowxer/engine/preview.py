from __future__ import annotations

import io
from collections import OrderedDict

from PIL import Image, ImageDraw, ImageFont

from flowxer.api.schemas import InputKind
from flowxer.engine.formats import SOURCE_COLORS
from flowxer.engine.pipeline import MONITOR_PREFIX, MONITOR_PROGRAM

_FRAME_CACHE: OrderedDict[str, Image.Image] = OrderedDict()
_CACHE_LIMIT = 48

SMPTE_BARS = (
    (192, 192, 192),
    (192, 192, 0),
    (0, 192, 192),
    (0, 192, 0),
    (192, 0, 192),
    (192, 0, 0),
    (0, 0, 192),
)


def _font(size: int) -> ImageFont.ImageFont:
    try:
        return ImageFont.truetype("DejaVuSans-Bold.ttf", size)
    except OSError:
        return ImageFont.load_default()


def _color_for(name: str) -> tuple[int, int, int]:
    acc = sum(ord(ch) for ch in name)
    return SOURCE_COLORS[acc % len(SOURCE_COLORS)]


def _cache_get(key: str, factory) -> Image.Image:
    hit = _FRAME_CACHE.get(key)
    if hit is not None:
        _FRAME_CACHE.move_to_end(key)
        return hit
    image = factory()
    _FRAME_CACHE[key] = image
    while len(_FRAME_CACHE) > _CACHE_LIMIT:
        _FRAME_CACHE.popitem(last=False)
    return image


def _is_black_source(source) -> bool:
    if source is None:
        return False
    if source.kind == InputKind.black:
        return True
    if source.kind in {InputKind.file, InputKind.replay}:
        path = source.file_path or ""
        return path in {"", "_unassigned"} or path.endswith("_unassigned")
    return False


def _paint_label(draw: ImageDraw.ImageDraw, width: int, height: int, title: str, meta: str) -> None:
    title_font = _font(max(22, height // 10))
    meta_font = _font(max(14, height // 18))
    draw.text((16, height // 2 - 28), title, fill=(255, 255, 255), font=title_font)
    if meta:
        draw.text((16, height // 2 + 16), meta, fill=(220, 230, 255), font=meta_font)


def _draw_source(source, width: int, height: int) -> Image.Image:
    """Picture for one logical source — identical on SRC, PVW and PGM."""
    if _is_black_source(source):
        return Image.new("RGB", (width, height), (0, 0, 0))

    kind = source.kind.value
    if kind == "test":
        image = Image.new("RGB", (width, height), (0, 0, 0))
        draw = ImageDraw.Draw(image)
        bar_h = height * 3 // 4
        for index, color in enumerate(SMPTE_BARS):
            x0 = int(index * width / 7)
            x1 = int((index + 1) * width / 7)
            draw.rectangle([x0, 0, x1, bar_h], fill=color)
        chip = _color_for(source.id)
        draw.rectangle([0, bar_h, width, height], fill=chip)
        _paint_label(draw, width, height, source.label, "TEST")
        return image

    color = _color_for(source.id)
    image = Image.new("RGB", (width, height), color)
    draw = ImageDraw.Draw(image)
    meta = "MXL" if kind == "mxl_live" else kind.upper()
    if kind in {"file", "replay"} and source.file_path:
        meta = source.file_path.split("/")[-1]
    _paint_label(draw, width, height, source.label, meta)
    return image


def _draw_missing(width: int, height: int, title: str) -> Image.Image:
    image = Image.new("RGB", (width, height), (8, 10, 14))
    draw = ImageDraw.Draw(image)
    _paint_label(draw, width, height, title, "NO SIGNAL")
    return image


def render_monitor(
    mixer,
    stream_id: str,
    *,
    width: int = 640,
    height: int = 360,
) -> Image.Image:
    """Picture of the logical source on this monitor (same essence on SRC/PVW/PGM).

    While the pipeline runs this is the picture it produced: the source, or for
    the main panel's Program the mixed output. Otherwise a generated card."""
    live = _live_picture(mixer, stream_id, width, height)
    if live is not None:
        return live
    try:
        input_id, _tally, _badge = resolve_stream(mixer, stream_id)
        source = mixer.get_input(input_id) if input_id else None
    except Exception:
        # A WebRTC monitor whose input or panel was removed keeps running.
        input_id, source = stream_id, None
    if source is None:
        return _draw_missing(width, height, input_id or "NO SIGNAL")
    key = f"{source.id}:{source.kind.value}:{source.label}:{source.file_path}:{width}x{height}"
    return _cache_get(key, lambda: _draw_source(source, width, height))


def _live_picture(mixer, stream_id: str, width: int, height: int) -> Image.Image | None:
    gst = getattr(mixer, "gst", None)
    if gst is None:
        return None
    main_program = bool(mixer.panels) and stream_id == f"panel:{mixer.panels[0].id}:pgm"
    if main_program:
        name = MONITOR_PROGRAM
    else:
        try:
            input_id, _tally, _badge = resolve_stream(mixer, stream_id)
        except Exception:
            return None
        name = f"{MONITOR_PREFIX}{input_id}"
    picture = gst.monitors.get(name)
    if picture is None:
        return None
    picture_width, picture_height, data = picture
    image = Image.frombytes("RGB", (picture_width, picture_height), data)
    if (picture_width, picture_height) != (width, height):
        image = image.resize((width, height))
    return image


def render_jpeg(mixer, stream_id: str, *, width: int = 640, height: int = 360, quality: int = 70) -> bytes:
    image = render_monitor(mixer, stream_id, width=width, height=height)
    buf = io.BytesIO()
    image.save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


class UnknownStream(LookupError):
    """A monitor name that is not `source:<input id>` or `panel:<panel id>:pgm|pvw`."""


def resolve_stream(mixer, stream_id: str) -> tuple[str | None, str, str]:
    """Return (input_id, tally, badge). Tally is GUI chrome only — not burned into the picture.

    Raises UnknownStream for a malformed name or an input or panel that does not exist."""
    kind, _, rest = stream_id.partition(":")
    if kind == "source" and rest in mixer.inputs:
        tally = "off"
        if any(p.program_input_id == rest for p in mixer.panels):
            tally = "pgm"
        elif any(p.preview_input_id == rest for p in mixer.panels):
            tally = "pvw"
        return rest, tally, "SRC"
    if kind == "panel":
        panel_id, _, bus = rest.partition(":")
        for panel in mixer.panels:
            if panel.id != panel_id:
                continue
            if bus == "pgm":
                return panel.program_input_id, "pgm", f"{panel.label} PGM"
            if bus == "pvw":
                return panel.preview_input_id, "pvw", f"{panel.label} PVW"
    if stream_id in mixer.inputs:
        # A bare input id, as before.
        return stream_id, "off", stream_id
    raise UnknownStream(
        f"unknown monitor {stream_id!r}: use source:<input id> or panel:<panel id>:pgm|pvw"
    )
