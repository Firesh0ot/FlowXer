from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


class LibraryKind(str, Enum):
    clip = "clip"
    stinger = "stinger"


class ConversionStatus(str, Enum):
    queued = "queued"
    converting = "converting"
    ready = "ready"
    failed = "failed"
    missing = "missing"


class FitMode(str, Enum):
    fit = "fit"
    fill = "fill"
    center = "center"


class FpsMode(str, Enum):
    drop = "drop"
    motion = "motion"


class UploadMode(str, Enum):
    video = "video"
    image_sequence = "image_sequence"
    zip = "zip"


class ConversionInfo(BaseModel):
    status: ConversionStatus = ConversionStatus.missing
    frames: int = 0
    audio_samples: int = 0
    audio_channels: int = 0
    mezz: str | None = None
    has_alpha: bool = False
    duration_s: float = 0.0
    error: str | None = None
    playback: Literal["ram", "decode_ahead", "unknown"] = "unknown"
    cut_frame: int | None = None
    cut_ms: int | None = None


class ConvertOptions(BaseModel):
    fit: FitMode = FitMode.fit
    fps_mode: FpsMode = FpsMode.drop
    loudness: bool = False
    crossfade_ms: int = Field(default=0, ge=0, le=2000)
    # Sound channels of a clip's mezzanine: 1 (mono) or 2 (stereo, also for 0).
    map_channels: int = Field(default=0, ge=0, le=2)
    sequence_fps: float | None = Field(default=None, gt=0)
    cut_frame: int | None = Field(default=None, ge=0)
    cut_ms: int | None = Field(default=None, ge=0)
    tags: list[str] = Field(default_factory=list)


class LibraryItem(BaseModel):
    id: str
    kind: LibraryKind
    name: str
    tags: list[str] = Field(default_factory=list)
    created_at: float = 0.0
    updated_at: float = 0.0
    original: dict[str, Any] = Field(default_factory=dict)
    options: ConvertOptions = Field(default_factory=ConvertOptions)
    conversions: dict[str, ConversionInfo] = Field(default_factory=dict)
    # Stinger-specific metadata mirrored for API convenience.
    cut_frame: int | None = None
    cut_ms: int | None = None
    has_alpha: bool = False
    source: Literal["upload", "legacy_clip", "legacy_stinger", "import"] = "upload"
    legacy_path: str | None = None

    def conversion_for(self, format_id: str) -> ConversionInfo:
        return self.conversions.get(format_id) or ConversionInfo()

    def is_ready(self, format_id: str) -> bool:
        return self.conversion_for(format_id).status == ConversionStatus.ready


class JobState(str, Enum):
    queued = "queued"
    running = "running"
    done = "done"
    failed = "failed"
    cancelled = "cancelled"


class ConvertJob(BaseModel):
    id: str
    item_id: str
    format_id: str
    state: JobState = JobState.queued
    progress: float = Field(default=0.0, ge=0.0, le=1.0)
    error: str | None = None
    log_tail: str = ""
    created_at: float = 0.0
    started_at: float | None = None
    finished_at: float | None = None


class UploadSession(BaseModel):
    id: str
    name: str
    size: int
    kind: LibraryKind = LibraryKind.clip
    mode: UploadMode = UploadMode.video
    options: ConvertOptions = Field(default_factory=ConvertOptions)
    chunk_size: int = 8 * 1024 * 1024
    received: list[int] = Field(default_factory=list)
    created_at: float = 0.0
    updated_at: float = 0.0
    # receiving → completing → done (item_id set; a repeated complete returns that item)
    state: Literal["receiving", "completing", "done"] = "receiving"
    item_id: str | None = None
