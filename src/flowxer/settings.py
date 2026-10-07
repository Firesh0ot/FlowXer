from __future__ import annotations

import logging
import socket
from functools import lru_cache
from pathlib import Path

from flowxer import __version__
from flowxer.domain.nmos import output_domain_uuid, seed_short
from pydantic import AliasChoices, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

log = logging.getLogger(__name__)


class Settings(BaseSettings):
    """Runtime configuration for the FlowXer vision mixer media function."""

    # Platform names (MXL_*, NMOS_*, SHUTDOWN_TIMEOUT_S) and the FLOWXER_* names both
    # work; the platform name wins when both are set.
    model_config = SettingsConfigDict(
        env_prefix="FLOWXER_",
        env_file=".env",
        extra="ignore",
        populate_by_name=True,
    )

    host: str = "127.0.0.1"
    port: int = Field(default=9610, ge=1, le=65535)
    title: str = "FlowXer Vision Mixer"
    version: str = __version__

    # MXL root is scanned for domain_def.json (including mirror-* siblings).
    mxl_root: Path = Field(default=Path("/Volumes/mxl"), validation_alias=AliasChoices("MXL_DOMAIN_SCAN_PATH", "FLOWXER_MXL_ROOT"))
    mxl_output_domain_dir: Path | None = Field(
        default=None, validation_alias=AliasChoices("MXL_OUTPUT_DOMAIN_DIR", "FLOWXER_MXL_OUTPUT_DOMAIN_DIR")
    )
    mxl_output_domain_id: str = Field(default="", validation_alias=AliasChoices("MXL_OUTPUT_DOMAIN_ID", "FLOWXER_MXL_OUTPUT_DOMAIN_ID"))
    # Deprecated: when set, used as both root and output domain (single-domain layout).
    mxl_domain: Path | None = None
    mxl_history_duration_ns: int = Field(
        default=200_000_000, ge=1, validation_alias=AliasChoices("MXL_HISTORY_DURATION", "FLOWXER_MXL_HISTORY_DURATION_NS")
    )
    # Remove the own output domain directory on SIGTERM (never another function's).
    mxl_cleanup_on_exit: bool = Field(default=False, validation_alias=AliasChoices("MXL_CLEANUP_ON_EXIT", "FLOWXER_MXL_CLEANUP_ON_EXIT"))
    # gst-mxl-rs mxlsrc has no offset property; kept for the platform env table.
    read_offset_grains: int = Field(default=2, ge=0)
    nmos_seed: str = Field(default="", validation_alias=AliasChoices("NMOS_SEED", "FLOWXER_NMOS_SEED"))
    # Node label (and device label); empty: FLOWXER_TITLE.
    nmos_label: str = Field(default="", validation_alias=AliasChoices("NMOS_LABEL", "FLOWXER_NMOS_LABEL"))
    # JSON object of tag name to string array, added to the node and the device.
    nmos_tags: dict[str, list[str]] = Field(default_factory=dict, validation_alias=AliasChoices("NMOS_TAGS", "FLOWXER_NMOS_TAGS"))
    nmos_enable: bool = True
    # Registration API: FLOWXER_NMOS_REGISTRY_URL, or NMOS_REGISTRY_ADDRESS and
    # NMOS_REGISTRY_PORT (the platform's names). No DNS-SD.
    nmos_registry_url: str = ""
    nmos_registry_address: str = Field(default="", validation_alias=AliasChoices("NMOS_REGISTRY_ADDRESS", "FLOWXER_NMOS_REGISTRY_ADDRESS"))
    nmos_registry_port: int = Field(default=0, ge=0, le=65535, validation_alias=AliasChoices("NMOS_REGISTRY_PORT", "FLOWXER_NMOS_REGISTRY_PORT"))
    nmos_dns_sd: bool = Field(default=False, validation_alias=AliasChoices("NMOS_DNS_SD", "FLOWXER_NMOS_DNS_SD"))
    nmos_port: int = Field(default=3252, ge=1, le=65535, validation_alias=AliasChoices("NMOS_PORT", "FLOWXER_NMOS_PORT"))
    nmos_host_ip: str = Field(default="", validation_alias=AliasChoices("NMOS_HOST_ADDRESS", "FLOWXER_NMOS_HOST_IP"))
    # Mixer state (inputs, layout, keyers, stingers, tally, IS-05 routes), kept across restarts.
    state_dir: Path = Path("/config")
    shutdown_timeout_s: int = Field(default=10, ge=1, le=300, validation_alias=AliasChoices("SHUTDOWN_TIMEOUT_S", "FLOWXER_SHUTDOWN_TIMEOUT_S"))
    # Bind the Node/Connection APIs. Tests set this false and use TestClient.
    nmos_bind: bool = True
    storage_root: Path = Path("./storage")
    # Media library (mezzanine + originals). Empty → <storage_root>/library.
    library_dir: Path | None = None
    # Optional watched drop folder for auto-ingest (empty disables).
    import_dir: Path | None = None
    convert_concurrency: int = Field(default=1, ge=1, le=8)
    ram_clip_max_s: float = Field(default=20.0, ge=0.0, le=600.0)
    ram_budget_mb: int = Field(default=4096, ge=64, le=262144)
    preroll_frames: int = Field(default=25, ge=0, le=300)
    upload_limit_gb: float = Field(default=20.0, ge=0.1, le=500.0)
    # Set in the mixer image from ARG MXL_REF (io.dmf.mxl.revision).
    mxl_revision: str = ""

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
    # Rate of the GUI monitor pictures taken from the pipeline (0: generated cards).
    monitor_fps: int = Field(default=10, ge=0, le=50)
    webrtc_public_ip: str = ""
    webrtc_udp_port_min: int = Field(default=32600, ge=1, le=65535)
    webrtc_udp_port_max: int = Field(default=32631, ge=1, le=65535)

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
    def resolved_registry_url(self) -> str:
        """Registration API base URL, or empty when no registry is configured."""
        url = (self.nmos_registry_url or "").strip()
        if url:
            return url.rstrip("/")
        address = (self.nmos_registry_address or "").strip()
        if address and self.nmos_registry_port:
            return f"http://{address}:{self.nmos_registry_port}"
        return ""

    @property
    def node_label(self) -> str:
        return (self.nmos_label or "").strip() or self.title

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
    def resolved_library_dir(self) -> Path:
        if self.library_dir is not None:
            return Path(self.library_dir)
        return self.storage_root / "library"

    # Alias used by LibraryService / docs.
    @property
    def library_dir_path(self) -> Path:
        return self.resolved_library_dir

    @property
    def upload_limit_bytes(self) -> int:
        return int(self.upload_limit_gb * (1024**3))

    @property
    def frame_rate(self) -> str:
        return f"{self.frame_rate_num}/{self.frame_rate_den}"

    @property
    def raster(self) -> str:
        return f"{self.width}x{self.height}"

    @property
    def fps(self) -> float:
        return self.frame_rate_num / self.frame_rate_den

    @property
    def resolved_mxl_revision(self) -> str:
        configured = (self.mxl_revision or "").strip()
        if configured:
            return configured
        for path in (Path("/opt/mxl/REF"), Path("/opt/mxl/SHA")):
            try:
                text = path.read_text(encoding="utf-8").strip()
            except OSError:
                continue
            if text:
                return text
        return ""


@lru_cache
def get_settings() -> Settings:
    return Settings()


def ensure_storage(settings: Settings) -> Settings:
    from flowxer.domain.mxl_domain import DomainError, ensure_output_domain

    for path in (
        settings.clips_dir,
        settings.stingers_dir,
        settings.graphics_dir,
        settings.resolved_library_dir,
    ):
        path.mkdir(parents=True, exist_ok=True)
    if settings.import_dir is not None:
        Path(settings.import_dir).mkdir(parents=True, exist_ok=True)
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
