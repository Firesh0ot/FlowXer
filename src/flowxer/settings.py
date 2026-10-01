from __future__ import annotations

import logging
import socket
from functools import lru_cache
from pathlib import Path

from flowxer import __version__
from flowxer.domain.nmos import output_domain_uuid, seed_short
from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

log = logging.getLogger(__name__)


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
    version: str = __version__

    # MXL root is scanned for domain_def.json (including mirror-* siblings).
    mxl_root: Path = Path("/Volumes/mxl")
    mxl_output_domain_dir: Path | None = None
    mxl_output_domain_id: str = ""
    # Deprecated: when set, used as both root and output domain (single-domain layout).
    mxl_domain: Path | None = None
    mxl_history_duration_ns: int = Field(default=200_000_000, ge=1)
    # gst-mxl-rs mxlsrc has no offset property; kept for the platform env table.
    read_offset_grains: int = Field(default=2, ge=0)
    nmos_seed: str = ""
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

    # Empty = unauthenticated (local/dev). Staging must set FLOWXER_API_TOKEN.
    api_token: str = ""
    cors_origins: str = "*"
    max_webrtc_peers: int = Field(default=16, ge=1, le=256)

    mxl_domain_deprecated: bool = False

    @field_validator("api_token")
    @classmethod
    def validate_api_token(cls, value: str) -> str:
        token = (value or "").strip()
        if not token:
            return ""
        from flowxer.engine.security import SAFE_TOKEN_RE

        if not SAFE_TOKEN_RE.fullmatch(token):
            raise ValueError(
                "FLOWXER_API_TOKEN must be 8-128 characters in [A-Za-z0-9._~-]"
            )
        return token

    @model_validator(mode="after")
    def resolve_mxl_layout(self) -> Settings:
        if self.mxl_domain is not None:
            log.warning(
                "FLOWXER_MXL_DOMAIN is deprecated; using %s as both MXL root and "
                "output domain (single-domain layout)",
                self.mxl_domain,
            )
            self.mxl_root = self.mxl_domain
            self.mxl_output_domain_dir = self.mxl_domain
            self.mxl_domain_deprecated = True
        elif self.mxl_output_domain_dir is None:
            self.mxl_output_domain_dir = self.mxl_root / f"flowxer-{self.seed_short}"
        return self

    @property
    def resolved_nmos_seed(self) -> str:
        return (self.nmos_seed or "").strip() or f"{socket.gethostname()}-flowxer"

    @property
    def seed_short(self) -> str:
        return seed_short(self.resolved_nmos_seed)

    @property
    def resolved_output_domain_id(self) -> str:
        configured = (self.mxl_output_domain_id or "").strip()
        if configured:
            return configured
        return output_domain_uuid(self.resolved_nmos_seed)

    @property
    def output_domain(self) -> Path:
        return (self.mxl_output_domain_dir or self.mxl_root).expanduser()

    @property
    def cors_origin_list(self) -> list[str]:
        origins = [part.strip() for part in self.cors_origins.split(",") if part.strip()]
        return origins or ["*"]

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
    from flowxer.domain.mxl_domain import DomainError, ensure_output_domain

    for path in (
        settings.clips_dir,
        settings.stingers_dir,
        settings.graphics_dir,
    ):
        path.mkdir(parents=True, exist_ok=True)
    try:
        ensure_output_domain(
            settings.output_domain,
            domain_id=settings.resolved_output_domain_id,
            label="FlowXer MXL domain",
            description="Uncompressed v210 video and float32 audio essences for the FlowXer vision mixer",
            history_duration_ns=settings.mxl_history_duration_ns,
        )
    except DomainError:
        raise
    return settings
