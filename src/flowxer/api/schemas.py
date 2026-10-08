from __future__ import annotations

from enum import Enum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator


class InputKind(str, Enum):
    mxl_live = "mxl_live"
    file = "file"
    replay = "replay"
    test = "test"
    black = "black"


class MixerState(str, Enum):
    idle = "idle"
    running = "running"
    error = "error"


class ProgramBus(str, Enum):
    live = "live"
    replay = "replay"


class TransitionType(str, Enum):
    cut = "cut"
    mix = "mix"
    stinger = "stinger"


class StingerPhase(str, Enum):
    idle = "idle"
    playing = "playing"
    cut = "cut"
    complete = "complete"


class EssenceRole(str, Enum):
    video = "video"
    audio = "audio"


class VideoEssence(BaseModel):
    """Uncompressed video essence carried on an MXL discrete flow (v210 / VP210)."""

    role: EssenceRole = EssenceRole.video
    media_type: str = Field(default="video/v210", description="MXL video media type")
    flow_id: UUID | None = Field(
        default=None,
        description="MXL video flow UUID. Required for mxl_live inputs when the mixer is on-air.",
    )
    domain_id: str | None = Field(
        default=None,
        description="MXL domain UUID from domain_def.json `id` (IS-05 mxl_domain_id). "
        "Omitted on REST defaults to FlowXer's output domain.",
    )
    width: int | None = Field(default=None, ge=1)
    height: int | None = Field(default=None, ge=1)
    frame_rate_num: int | None = Field(default=None, ge=1)
    frame_rate_den: int | None = Field(default=None, ge=1)

    @field_validator("media_type")
    @classmethod
    def validate_video_media_type(cls, value: str) -> str:
        allowed = {"video/v210", "video/v210a"}
        if value not in allowed:
            raise ValueError(f"video media_type must be one of {sorted(allowed)}")
        return value


class AudioEssence(BaseModel):
    """Uncompressed audio essence carried on an MXL continuous flow (float32 @ 48 kHz)."""

    role: EssenceRole = EssenceRole.audio
    media_type: str = Field(default="audio/float32", description="MXL audio media type")
    flow_id: UUID | None = Field(
        default=None,
        description="MXL audio flow UUID bundled with the video essence on this logical input.",
    )
    domain_id: str | None = Field(
        default=None,
        description="MXL domain UUID from domain_def.json `id` (IS-05 mxl_domain_id). "
        "Omitted on REST defaults to FlowXer's output domain.",
    )
    channels: int = Field(default=2, ge=1, le=64)
    sample_rate: int = Field(default=48000, ge=8000)

    @field_validator("media_type")
    @classmethod
    def validate_audio_media_type(cls, value: str) -> str:
        if value != "audio/float32":
            raise ValueError("audio media_type must be audio/float32")
        return value


class LogicalInputCreate(BaseModel):
    """Register a logical mixer input that virtually bundles video + audio essences."""

    id: str = Field(..., min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._-]+$")
    label: str = Field(..., min_length=1, max_length=128)
    kind: InputKind
    video: VideoEssence | None = None
    audio: AudioEssence | None = None
    file_path: str | None = Field(
        default=None,
        description="Clip filename under storage/clips for file and replay inputs.",
    )
    library_item_id: str | None = Field(
        default=None,
        description="Media-library clip id. Preferred over file_path when set.",
    )
    group_hint: str | None = Field(
        default=None,
        description="Optional NMOS grouphint used to auto-discover matching video/audio flows.",
    )
    stinger_slot_id: str | None = Field(
        default=None,
        description="Stinger slot played when this source is taken to Program or Cut from Preview.",
    )

    @model_validator(mode="after")
    def validate_kind_payload(self) -> LogicalInputCreate:
        if self.kind == InputKind.mxl_live:
            if self.video is None and self.audio is None and not self.group_hint:
                raise ValueError(
                    "mxl_live inputs need a video essence, an audio essence, or a group_hint"
                )
        if self.kind == InputKind.file and not self.file_path and not self.library_item_id:
            raise ValueError("file inputs require file_path or library_item_id")
        return self


class LogicalInput(LogicalInputCreate):
    slot: int = Field(..., ge=0, description="GStreamer input-selector sink index")


class LogicalInputUpdate(BaseModel):
    label: str | None = None
    kind: InputKind | None = None
    video: VideoEssence | None = None
    audio: AudioEssence | None = None
    file_path: str | None = None
    library_item_id: str | None = None
    group_hint: str | None = None
    stinger_slot_id: str | None = Field(
        default=None,
        description=(
            "Stinger slot played when this source is taken to Program or Cut from Preview. "
            "Null disables auto-stinger."
        ),
    )


class MixerStartRequest(BaseModel):
    domain: str | None = Field(default=None, description="MXL domain path override")
    group_hint: str | None = None
    program_input_id: str | None = Field(
        default=None,
        description="Logical input placed on program at start. Defaults to the first live/test input.",
    )
    preview_input_id: str | None = None
    overlay_enabled: bool = False
    overlay_url: str | None = None


class TakeRequest(BaseModel):
    input_id: str
    transition: TransitionType = TransitionType.cut
    duration_ms: int = Field(default=0, ge=0, le=10000)
    stinger_id: str | None = None
    panel_id: str = Field(default="me-1", description="Mixer panel (ME) to cut to program")


class PreviewRequest(BaseModel):
    input_id: str
    panel_id: str = Field(default="me-1", description="Mixer panel (ME) to arm on preview")


class PanelTransitionRequest(BaseModel):
    panel_id: str = Field(default="me-1", description="Mixer panel (ME) that owns Preview/Program")
    duration_ms: int = Field(
        default=400,
        ge=0,
        le=10000,
        description="Mix duration for Fade and Fade to Black. Ignored by Cut.",
    )
    armed: bool | None = Field(
        default=None,
        description="For Wipe: true arms, false disarms. Omit to toggle.",
    )


class OverlayUpdate(BaseModel):
    enabled: bool | None = None
    url: str | None = Field(
        default=None,
        description="HTML5 graphics URL composited by cefsrc (or the built-in renderer fallback).",
    )
    title: str | None = None
    subtitle: str | None = None


class ReplayLoadRequest(BaseModel):
    file_path: str | None = Field(default=None, description="Clip filename under storage/clips")
    library_item_id: str | None = Field(default=None, description="Library clip id")
    input_id: str = Field(default="replay", description="Logical replay input to load")

    @model_validator(mode="after")
    def require_clip_ref(self) -> ReplayLoadRequest:
        if not self.file_path and not self.library_item_id:
            raise ValueError("file_path or library_item_id required")
        return self


class ReplayTransitionRequest(BaseModel):
    stinger_id: str = Field(default="replay-wipe")
    input_id: str | None = Field(
        default=None,
        description="Replay input to take. Defaults to the registered replay input.",
    )


class StingerPlayRequest(BaseModel):
    stinger_id: str = Field(default="replay-wipe")
    target_input_id: str
    direction: str = Field(
        default="to_replay",
        description="to_replay or to_live — recorded for operator status only.",
    )
    panel_id: str = Field(default="me-1")
    flip_flop: bool = Field(
        default=False,
        description="If true, swap Preview and Program at the cut like a Cut through the stinger.",
    )


class ErrorBody(BaseModel):
    detail: str


class FlowDescriptor(BaseModel):
    id: str
    label: str
    description: str = ""
    media_type: str
    format: str
    group_hint: str = ""
    path: str | None = None
    domain_id: str = ""
    active: bool = True
    extra: dict[str, Any] = Field(default_factory=dict)


class DomainInfo(BaseModel):
    path: str
    exists: bool
    flow_count: int = 0
    domain_def: dict[str, Any] | None = None
    id: str = ""
    mirror: bool = False


class StorageClip(BaseModel):
    name: str
    path: str
    size_bytes: int
    suffix: str
    library_item_id: str | None = None
    ready: bool = True


class StingerInfo(BaseModel):
    id: str
    path: str
    frame_count: int
    cut_frame: int
    pattern: str = "frame_%05d.tga"
    width: int
    height: int
    has_alpha: bool = True
    kind: str = Field(default="sequence", description="sequence (TGA) or video")
    media_path: str = ""
    cut_ms: int = 0
    duration_ms: int = 0
    fps: float = 50.0


class OverlayStatus(BaseModel):
    enabled: bool
    url: str
    title: str
    subtitle: str
    renderer: str


class OutputFlows(BaseModel):
    video_flow_id: str
    audio_flow_id: str
    group_hint: str
    media_type_video: str
    media_type_audio: str
    nmos_video: dict[str, Any]
    nmos_audio: dict[str, Any]


class MixerStatus(BaseModel):
    state: MixerState
    backend: str
    program_input_id: str | None
    preview_input_id: str | None
    last_live_input_id: str | None
    program_bus: ProgramBus
    overlay: OverlayStatus
    stinger: dict[str, Any]
    outputs: OutputFlows | None
    raster: str
    frame_rate: str
    video_format: str
    audio_format: str
    pipeline: str | None = None
    error: str | None = None
    workspace: dict[str, Any] | None = None
    panels: list[dict[str, Any]] = Field(default_factory=list)
    keyers: list[dict[str, Any]] = Field(default_factory=list)
    stinger_slots: list[dict[str, Any]] = Field(default_factory=list)
    webrtc_enabled: bool = False
    wipe_armed: bool = False
    last_transition: str = "cut"
    nmos: dict[str, Any] = Field(default_factory=dict)
    # FLOWXER_GPU: "gpu" or "cpu", and why (the reason of a fallback).
    media_path: str = "cpu"
    media_path_reason: str = ""


class HealthResponse(BaseModel):
    status: str
    service: str
    version: str
    mxl_domain: str
    mxl_root: str = ""
    mxl_output_domain_id: str = ""
    gstreamer: bool
    mxl_plugins: bool
    simulate: bool
    mxl_revision: str = ""


class MixerCommandResponse(BaseModel):
    status: str
    mixer: MixerStatus


class MixerPanel(BaseModel):
    id: str
    label: str
    program_input_id: str | None = None
    preview_input_id: str | None = None
    wipe_armed: bool = False
    last_transition: str = "cut"


class DownstreamKeyer(BaseModel):
    id: str
    label: str
    enabled: bool = False
    url: str = ""
    title: str = "FLOWXER"
    subtitle: str = "DMF Vision Mixer"


class StingerSlot(BaseModel):
    id: str
    role: str = Field(description="shared, in, or out")
    label: str
    stinger_id: str = "replay-wipe"
    library_item_id: str | None = Field(
        default=None,
        description="Media-library stinger id. When set, preferred over legacy stinger_id path.",
    )
    kind: str = Field(default="sequence", description="sequence (TGA) or video")
    media_path: str | None = Field(
        default=None,
        description="TGA sequence directory or video file used by this slot",
    )
    cut_ms: int | None = Field(
        default=None,
        ge=0,
        description="Time in the stinger when program cuts. None uses the asset default.",
    )
    cut_frame: int | None = Field(
        default=None,
        ge=0,
        description="Frame index when Program switches under the sting. Preferred over cut_ms in the GUI.",
    )
    ready: bool = Field(default=True, description="False while library mezzanine is converting")


class WorkspaceConfig(BaseModel):
    format_id: str = "1080p50"
    logical_source_count: int = Field(default=8, ge=1, le=24)
    mixer_panel_count: int = Field(default=1, ge=1, le=4)
    stinger_mode: str = Field(
        default="shared",
        description="shared = one TGA sequence for in and out; separate = dedicated in/out stingers",
    )
    stinger_count: int = Field(default=1, ge=1, le=8)
    downstream_keyer_count: int = Field(default=1, ge=0, le=8)
    source_tile_aspect: str = Field(
        default="16:9",
        description="Operator source-tile picture ratio: 16:9 (landscape) or 9:16 (portrait).",
    )


class WorkspaceUpdate(BaseModel):
    format_id: str | None = None
    logical_source_count: int | None = Field(default=None, ge=1, le=24)
    mixer_panel_count: int | None = Field(default=None, ge=1, le=4)
    stinger_mode: str | None = None
    stinger_count: int | None = Field(default=None, ge=1, le=8)
    downstream_keyer_count: int | None = Field(default=None, ge=0, le=8)
    source_tile_aspect: str | None = Field(
        default=None,
        description="16:9 or 9:16 source tiles on the operator deck",
    )


class KeyerUpdate(BaseModel):
    enabled: bool | None = None
    url: str | None = None
    title: str | None = None
    subtitle: str | None = None
    label: str | None = None


class StingerSlotUpdate(BaseModel):
    stinger_id: str | None = None
    library_item_id: str | None = Field(
        default=None,
        description="Assign a library stinger item (clears legacy-only binding when set)",
    )
    kind: str | None = Field(default=None, description="sequence or video")
    media_path: str | None = Field(
        default=None,
        description="TGA sequence id/directory or video clip filename",
    )
    cut_ms: int | None = Field(default=None, ge=0, description="Program cut time in milliseconds")
    cut_frame: int | None = Field(
        default=None,
        ge=0,
        description="Program cut frame (operator GUI uses this; mixer stores cut_ms as well)",
    )
    duration_ms: int | None = Field(default=None, ge=1, description="Video duration when ffprobe is unavailable")
    label: str | None = None


class TallyKind(str, Enum):
    companion = "companion"
    vsm = "vsm"
    bfe = "bfe"
    hi = "hi"
    custom = "custom"


class TallyPreset(BaseModel):
    kind: TallyKind
    label: str
    port: int = 8900
    transport: str = "udp"
    hint: str = ""


class TallyReceiver(BaseModel):
    id: str = Field(..., min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._-]+$")
    kind: TallyKind = TallyKind.custom
    label: str = Field(..., min_length=1, max_length=128)
    host: str = Field(..., min_length=1, max_length=253, description="IPv4 or hostname of the TSL listener")
    port: int = Field(default=8900, ge=1, le=65535)
    transport: str = Field(default="udp", description="udp (TSL default) or tcp (DLE/STX framed)")
    enabled: bool = True
    screen: int = Field(default=0, ge=0, le=65534, description="TSL SCREEN address")
    index_offset: int = Field(
        default=0,
        ge=0,
        le=65534,
        description="Added to each logical source slot to form the TSL display INDEX",
    )
    dle_stx: bool | None = Field(
        default=None,
        description="Force DLE/STX wrapping. None = wrap TCP only (TSL 5.0 spec).",
    )

    @field_validator("transport")
    @classmethod
    def validate_transport(cls, value: str) -> str:
        transport = value.lower()
        if transport not in {"udp", "tcp"}:
            raise ValueError("transport must be udp or tcp")
        return transport


class TallyReceiverStatus(TallyReceiver):
    last_error: str | None = None
    last_sent_at: float | None = None


class TallyConfig(BaseModel):
    protocol: str = "TSL UMD 5.0"
    receivers: list[TallyReceiverStatus] = Field(default_factory=list)
    presets: list[TallyPreset] = Field(default_factory=list)


class TallyReceiversUpdate(BaseModel):
    receivers: list[TallyReceiver]


class ResourceIssue(BaseModel):
    level: str
    message: str


class ResourceInfo(BaseModel):
    cpu_percent: float
    cpu_count: int
    load: dict[str, float] | None = None
    memory_bytes: int
    memory_limit_bytes: int
    memory_percent: float
    uptime_s: float | None = None
    pid: int | None = None
    status: str
    issues: list[ResourceIssue] = Field(default_factory=list)


class ConsoleState(BaseModel):
    workspace: WorkspaceConfig
    formats: list[dict]
    inputs: list[LogicalInput]
    panels: list[MixerPanel]
    keyers: list[DownstreamKeyer]
    stinger_slots: list[StingerSlot]
    mixer: MixerStatus
    resources: ResourceInfo
    webrtc: dict
    clips: list[StorageClip]
    stingers: list[StingerInfo]
    library: list["LibraryItemOut"] = Field(default_factory=list)
    jobs: list["ConvertJobOut"] = Field(default_factory=list)
    tally: TallyConfig = Field(default_factory=TallyConfig)
    nmos: dict[str, Any] = Field(
        default_factory=dict,
        description="IS-04/IS-05 node status: registry, node id, per-input receivers.",
    )
    pinned: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Workspace fields set by the environment, with the variables that set them "
            "(format_id, logical_source_count, mixer_panel_count). The API refuses to change "
            "them; a pinned logical_source_count also fixes the inputs' ids, kinds and labels."
        ),
    )


class LibraryItemOut(BaseModel):
    id: str
    kind: str
    name: str
    tags: list[str] = Field(default_factory=list)
    status: str = "missing"
    ready: bool = False
    playback: str = "unknown"
    has_alpha: bool = False
    cut_frame: int | None = None
    cut_ms: int | None = None
    frame_count: int = 0
    duration_s: float = 0.0
    thumb_url: str | None = None
    error: str | None = None
    source: str = "upload"
    legacy_path: str | None = None
    in_use: bool = False


class LibraryItemPatch(BaseModel):
    name: str | None = None
    tags: list[str] | None = None
    cut_frame: int | None = Field(default=None, ge=0)
    cut_ms: int | None = Field(default=None, ge=0)


class ConvertOptionsIn(BaseModel):
    fit: str = "fit"
    fps_mode: str = "drop"
    loudness: bool = False
    crossfade_ms: int = Field(default=0, ge=0, le=2000)
    map_channels: int = Field(default=0, ge=0, le=2, description="Clip sound: 1 mono, 2 or 0 stereo")
    sequence_fps: float | None = Field(default=None, gt=0)
    cut_frame: int | None = Field(default=None, ge=0)
    cut_ms: int | None = Field(default=None, ge=0)
    tags: list[str] = Field(default_factory=list)


class UploadInitRequest(BaseModel):
    name: str
    size: int = Field(..., ge=0)
    kind: str = Field(default="clip", description="clip or stinger")
    mode: str = Field(default="video", description="video | image_sequence | zip")
    options: ConvertOptionsIn = Field(default_factory=ConvertOptionsIn)


class UploadInitResponse(BaseModel):
    id: str
    chunk_size: int
    received: list[int] = Field(default_factory=list)


class ConvertJobOut(BaseModel):
    id: str
    item_id: str
    format_id: str
    state: str
    progress: float = 0.0
    error: str | None = None
    created_at: float = 0.0
    started_at: float | None = None
    finished_at: float | None = None


class ReconvertRequest(BaseModel):
    options: ConvertOptionsIn | None = None
