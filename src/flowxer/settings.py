from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration for the FlowXer vision mixer media function."""

    model_config = SettingsConfigDict(
        env_prefix="FLOWXER_",
        env_file=".env",
        extra="ignore",
    )

    host: str = "0.0.0.0"
    port: int = 9610
    title: str = "FlowXer Vision Mixer"
    version: str = "0.1.0"

    mxl_domain: Path = Path("./data/mxl-domain")
    storage_root: Path = Path("./storage")

    group_hint: str = "FlowXer"
    width: int = 1920
    height: int = 1080
    frame_rate_num: int = 50
    frame_rate_den: int = 1
    audio_rate: int = 48000
    audio_channels: int = 2
    video_media_type: str = "video/v210"
    audio_media_type: str = "audio/float32"

    # "auto" uses GStreamer when mxlsrc/mxlsink (or a local fallback) is present.
    gst_mode: str = "auto"
    simulate: bool = False

    overlay_url: str = "http://127.0.0.1:9610/graphics/lower-third.html"
    default_stinger: str = "replay-wipe"
    stinger_frame_count: int = 24
    stinger_auto_tick: bool = True

    @property
    def clips_dir(self) -> Path:
        return self.storage_root / "clips"

    @property
    def stingers_dir(self) -> Path:
        return self.storage_root / "stingers"

    @property
    def graphics_dir(self) -> Path:
        return self.storage_root / "graphics"

    @property
    def frame_rate(self) -> str:
        return f"{self.frame_rate_num}/{self.frame_rate_den}"

    @property
    def raster(self) -> str:
        return f"{self.width}x{self.height}"

    @property
    def fps(self) -> float:
        return self.frame_rate_num / self.frame_rate_den


@lru_cache
def get_settings() -> Settings:
    return Settings()


def ensure_storage(settings: Settings) -> Settings:
    for path in (
        settings.clips_dir,
        settings.stingers_dir,
        settings.graphics_dir,
        settings.mxl_domain,
    ):
        path.mkdir(parents=True, exist_ok=True)
    domain_def = settings.mxl_domain / "domain_def.json"
    if not domain_def.exists():
        domain_def.write_text(
            '{"id":"flowxer-domain","label":"FlowXer MXL domain",'
            '"description":"Uncompressed v210 video and float32 audio essences"}\n',
            encoding="utf-8",
        )
    return settings
