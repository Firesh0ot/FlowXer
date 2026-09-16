from __future__ import annotations

import logging
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

log = logging.getLogger(__name__)


class Html5Overlay:
    """
    HTML5 graphics keyer.

    Production path: GStreamer `cefsrc` renders a live URL with alpha
    and the compositor keys it over program.

    Fallback: a Pillow BGRA lower-third written next to the mixer so gdkpixbufoverlay
    (or a static PNG) can still show ident graphics without CEF.
    """

    def __init__(
        self,
        *,
        url: str,
        cache_dir: Path,
        width: int,
        height: int,
        title: str = "FLOWXER",
        subtitle: str = "DMF Vision Mixer",
    ) -> None:
        self.url = url
        self.cache_dir = cache_dir
        self.width = width
        self.height = height
        self.title = title
        self.subtitle = subtitle
        self.enabled = False
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    @property
    def png_path(self) -> Path:
        return self.cache_dir / "lower-third.png"

    def update(
        self,
        *,
        enabled: bool | None = None,
        url: str | None = None,
        title: str | None = None,
        subtitle: str | None = None,
    ) -> None:
        if enabled is not None:
            self.enabled = enabled
        if url is not None:
            self.url = url
        if title is not None:
            self.title = title
        if subtitle is not None:
            self.subtitle = subtitle
        self.render_fallback_png()

    def render_fallback_png(self) -> Path:
        image = Image.new("RGBA", (self.width, self.height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        bar_h = 140
        top = self.height - bar_h - 80
        draw.rectangle([80, top, 80 + 12, top + bar_h], fill=(0, 196, 255, 230))
        draw.rectangle([92, top, 920, top + bar_h], fill=(8, 12, 28, 210))
        try:
            title_font = ImageFont.truetype("DejaVuSans-Bold.ttf", 48)
            sub_font = ImageFont.truetype("DejaVuSans.ttf", 28)
        except OSError:
            title_font = ImageFont.load_default()
            sub_font = title_font
        draw.text((120, top + 24), self.title, fill=(255, 255, 255, 255), font=title_font)
        draw.text((120, top + 84), self.subtitle, fill=(180, 220, 255, 255), font=sub_font)
        image.save(self.png_path, format="PNG")
        return self.png_path

    def snapshot(self, renderer: str) -> dict:
        return {
            "enabled": self.enabled,
            "url": self.url,
            "title": self.title,
            "subtitle": self.subtitle,
            "renderer": renderer,
            "fallback_png": str(self.png_path),
        }
