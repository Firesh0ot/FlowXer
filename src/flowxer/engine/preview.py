from __future__ import annotations

import io
import time
from datetime import datetime, timezone

from PIL import Image, ImageDraw, ImageFont

from flowxer.engine.formats import SOURCE_COLORS


def _font(size: int) -> ImageFont.ImageFont:
    try:
        return ImageFont.truetype("DejaVuSans-Bold.ttf", size)
    except OSError:
        return ImageFont.load_default()


def _color_for(name: str) -> tuple[int, int, int]:
    acc = sum(ord(ch) for ch in name)
    return SOURCE_COLORS[acc % len(SOURCE_COLORS)]


def render_monitor(
    mixer,
    stream_id: str,
    *,
    width: int = 640,
    height: int = 360,
) -> Image.Image:
    """Generate a live ident frame for a source, PVW or PGM bus."""
    input_id, tally, badge = _resolve_stream(mixer, stream_id)
    try:
        source = mixer.get_input(input_id) if input_id else None
    except Exception:
        source = None
    label = source.label if source else (input_id or "NO SIGNAL")
    kind = source.kind.value if source else "none"
    color = _color_for(input_id or stream_id)
    image = Image.new("RGB", (width, height), color)
    draw = ImageDraw.Draw(image)

    bar_h = height // 8
    for index in range(8):
        x0 = int(index * width / 8)
        x1 = int((index + 1) * width / 8)
        shade = 40 + (index * 24)
        draw.rectangle([x0, 0, x1, bar_h], fill=(shade, shade, min(255, shade + 40)))

    t = time.time()
    sweep = int((t * 80) % width)
    draw.rectangle([sweep, bar_h, min(sweep + 8, width), height], fill=(255, 255, 255))

    if tally == "pgm":
        draw.rectangle([0, 0, width, 8], fill=(220, 32, 32))
        draw.rectangle([0, height - 8, width, height], fill=(220, 32, 32))
    elif tally == "pvw":
        draw.rectangle([0, 0, width, 8], fill=(32, 200, 80))
        draw.rectangle([0, height - 8, width, height], fill=(32, 200, 80))

    title_font = _font(max(22, height // 10))
    meta_font = _font(max(14, height // 18))
    clock = datetime.now(timezone.utc).strftime("%H:%M:%S")
    draw.text((16, height // 2 - 28), label, fill=(255, 255, 255), font=title_font)
    draw.text((16, height // 2 + 16), f"{kind}  {clock}  {badge}", fill=(220, 230, 255), font=meta_font)

    if source and source.kind.value in {"file", "replay"} and source.file_path:
        draw.text((16, height - 36), source.file_path.split("/")[-1], fill=(180, 200, 220), font=meta_font)

    for keyer in getattr(mixer, "keyers", []):
        if keyer.enabled and tally == "pgm":
            draw.rectangle([20, height - 70, width // 2, height - 18], fill=(8, 12, 28))
            draw.text((28, height - 62), keyer.title or keyer.label, fill=(0, 196, 255), font=meta_font)
            break
    return image


def render_jpeg(mixer, stream_id: str, *, width: int = 640, height: int = 360, quality: int = 70) -> bytes:
    image = render_monitor(mixer, stream_id, width=width, height=height)
    buf = io.BytesIO()
    image.save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


def _resolve_stream(mixer, stream_id: str) -> tuple[str | None, str, str]:
    """Return (input_id, tally, badge)."""
    if stream_id.startswith("source:"):
        input_id = stream_id.split(":", 1)[1]
        tally = "off"
        if any(p.program_input_id == input_id for p in mixer.panels):
            tally = "pgm"
        elif any(p.preview_input_id == input_id for p in mixer.panels):
            tally = "pvw"
        return input_id, tally, "SRC"
    if stream_id.startswith("panel:"):
        _, panel_id, bus = stream_id.split(":", 2)
        panel = mixer.get_panel(panel_id)
        if bus == "pgm":
            return panel.program_input_id, "pgm", f"{panel.label} PGM"
        return panel.preview_input_id, "pvw", f"{panel.label} PVW"
    return stream_id, "off", stream_id
