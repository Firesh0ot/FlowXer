from __future__ import annotations

from enum import Enum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator


class InputKind(str, Enum):
    mxl_live = "mxl_live"
    clip_player = "clip_player"
    black = "black"


# Input kinds of FlowXer before the engine: test sources and file/replay inputs are clip players
# now (saved states, exports and API clients that still send them).
LEGACY_INPUT_KINDS = {"test": "clip_player", "file": "clip_player", "replay": "clip_player"}


def _migrate_kind(data):
    if isinstance(data, dict) and data.get("kind") in LEGACY_INPUT_KINDS:
        data = dict(data, kind=LEGACY_INPUT_KINDS[data["kind"]])
    return data


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
    wipe = "wipe"
    stinger = "stinger"


# SMPTE ST 258 wipe codes (docs/engine-p3.md §4): bars 1, 2; corner boxes 3-6; barn doors 21, 22;
# edge boxes 23-26; diagonals 41, 42; diagonal barn doors 45, 46; box 101; diamond 102; circle 119;
# clock 201-204.
WIPE_CODES = (1, 2, 3, 4, 5, 6, 21, 22, 23, 24, 25, 26, 41, 42, 45, 46, 101, 102, 119, 201, 202, 203, 204)


class WipeSettings(BaseModel):
    pattern: int = Field(default=1, description="SMPTE wipe code: " + ", ".join(map(str, WIPE_CODES)))
    reverse: bool = Field(default=False, description="Run the pattern backwards")
    alternate: bool = Field(
        default=False, description="Flip-flop: reverse toggles after each wipe, so the next one goes back"
    )
    soft: float = Field(default=0.0, ge=0.0, le=1.0, description="Soft edge, a fraction of the pattern's travel")
    border: float = Field(default=0.0, ge=0.0, le=1.0, description="Border width, a fraction of the pattern's travel")
    border_color: str = Field(default="#ffffff", pattern=r"^#[0-9a-fA-F]{6}$")

    @field_validator("pattern")
    @classmethod
    def _known_code(cls, value: int) -> int:
        if value not in WIPE_CODES:
            raise ValueError(f"pattern must be one of {', '.join(map(str, WIPE_CODES))}")
        return value


class WipeSettingsUpdate(BaseModel):
    pattern: int | None = None
    reverse: bool | None = None
    alternate: bool | None = None
    soft: float | None = None
    border: float | None = None
    border_color: str | None = None


class PanelTransition(BaseModel):
    """An ME's next transition: what Auto runs."""

    type: TransitionType = Field(default=TransitionType.mix, description="mix, wipe or stinger")
    rate_frames: int = Field(default=25, ge=1, le=1000, description="Duration of Auto in frames")
    wipe: WipeSettings = Field(default_factory=WipeSettings)
    stinger_slot_id: str | None = Field(default=None, description="The stinger slot of type stinger")
    ties: list[str] = Field(
        default_factory=list,
        description=(
            "Keyer ties: the ME's upstream keyers (ids) that Cut, Auto and the T-bar turn on or off with the "
            "background (a fading keyer fades with the transition, a flying keyer flies with it, with a stinger "
            "they cut at its cut point)"
        ),
    )
    background: bool = Field(
        default=True,
        description="BKGD: the background transitions; false: only the tied keyers (key only: Cut cuts them, Auto runs each one's Auto)",
    )

    @field_validator("type")
    @classmethod
    def _not_cut(cls, value: TransitionType) -> TransitionType:
        if value == TransitionType.cut:
            raise ValueError("type must be mix, wipe or stinger")
        return value


class PanelTransitionUpdate(BaseModel):
    type: TransitionType | None = None
    rate_frames: int | None = None
    wipe: WipeSettingsUpdate | None = None
    stinger_slot_id: str | None = None
    ties: list[str] | None = None
    background: bool | None = None


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
    """Register a logical mixer input: a live input (video + audio essences on MXL), a clip
    player (a clip or a still from the library) or Black."""

    id: str = Field(..., min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._-]+$")
    label: str = Field(..., min_length=1, max_length=128)
    kind: InputKind
    video: VideoEssence | None = None
    audio: AudioEssence | None = None
    library_item_id: str | None = Field(
        default=None,
        description="Clip player: the library clip or still it plays (none: black).",
    )
    loop: bool = Field(default=False, description="Clip player: start the clip again at its end.")
    play_on_cut: bool = Field(
        default=False, description="Clip player: play when the player is taken to an ME's Program."
    )
    group_hint: str | None = Field(
        default=None,
        description="Optional NMOS grouphint used to auto-discover matching video/audio flows.",
    )
    stinger_slot_id: str | None = Field(
        default=None,
        description="Stinger slot played when this source is taken to Program or Cut from Preview.",
    )

    @model_validator(mode="before")
    @classmethod
    def migrate_kind(cls, data):
        return _migrate_kind(data)

    @model_validator(mode="after")
    def validate_kind_payload(self) -> LogicalInputCreate:
        if self.kind == InputKind.mxl_live:
            if self.video is None and self.audio is None and not self.group_hint:
                raise ValueError(
                    "mxl_live inputs need a video essence, an audio essence, or a group_hint"
                )
        return self


class LogicalInput(LogicalInputCreate):
    slot: int = Field(
        ...,
        ge=0,
        description="Source number from 0 (the TSL INDEX): live inputs, clip players, then Black.",
    )


class LogicalInputUpdate(BaseModel):
    label: str | None = None
    kind: InputKind | None = None
    video: VideoEssence | None = None
    audio: AudioEssence | None = None
    library_item_id: str | None = None
    loop: bool | None = None
    play_on_cut: bool | None = None
    group_hint: str | None = None
    stinger_slot_id: str | None = Field(
        default=None,
        description=(
            "Stinger slot played when this source is taken to Program or Cut from Preview. "
            "Null disables auto-stinger."
        ),
    )

    @model_validator(mode="before")
    @classmethod
    def migrate_kind(cls, data):
        return _migrate_kind(data)


class MixerStartRequest(BaseModel):
    domain: str | None = Field(default=None, description="MXL domain path override")
    group_hint: str | None = None
    program_input_id: str | None = Field(
        default=None,
        description="Logical input placed on ME 1 Program at start. Defaults to the first live input.",
    )
    preview_input_id: str | None = None


class TakeRequest(BaseModel):
    input_id: str = Field(
        ..., description="A logical input, or me-<n> for the Program of a higher ME (re-entry)"
    )
    transition: TransitionType = TransitionType.cut
    duration_ms: int = Field(default=0, ge=0, le=10000)
    frames: int | None = Field(
        default=None,
        ge=0,
        le=1000,
        description="Duration in frames (instead of duration_ms); a wipe without either takes the ME's rate",
    )
    stinger_id: str | None = None
    panel_id: str = Field(default="me-1", description="Mixer panel (ME) to cut to program")


class PreviewRequest(BaseModel):
    input_id: str = Field(
        ..., description="A logical input, or me-<n> for the Program of a higher ME (re-entry)"
    )
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


class AutoRequest(BaseModel):
    panel_id: str = Field(default="me-1", description="Mixer panel (ME)")
    frames: int | None = Field(default=None, ge=1, le=1000, description="Duration in frames (default: the ME's rate)")


class PositionRequest(BaseModel):
    panel_id: str = Field(default="me-1", description="Mixer panel (ME)")
    position: float = Field(
        ..., ge=0.0, le=1.0, description="T-bar: off 0 starts the ME's transition, 1 completes it, 0 ends it without the swap"
    )


class PlayerCueRequest(BaseModel):
    frame: int = Field(default=0, ge=0, description="Frame of the clip to show (0 = the first)")


class KeyerAutoRequest(BaseModel):
    duration_ms: int | None = Field(
        default=None, ge=1, le=10000, description="Fade on or off over this time (default: the keyer's rate)"
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


class OutputFlows(BaseModel):
    video_flow_id: str
    audio_flow_id: str
    group_hint: str
    media_type_video: str
    media_type_audio: str
    nmos_video: dict[str, Any]
    nmos_audio: dict[str, Any]


class MeOutputs(BaseModel):
    """The MXL flows of one rendering ME (output domain: FlowXer's)."""

    panel_id: str
    pgm_video_flow_id: str
    pgm_audio_flow_id: str
    pvw_video_flow_id: str | None = None


class MixerStatus(BaseModel):
    state: MixerState
    backend: str
    program_input_id: str | None
    preview_input_id: str | None
    last_live_input_id: str | None
    program_bus: ProgramBus
    stinger: dict[str, Any]
    outputs: OutputFlows | None = Field(default=None, description="ME 1 Program flows")
    me_outputs: list[MeOutputs] = Field(
        default_factory=list, description="PGM video and audio and PVW video of every rendering ME"
    )
    render_mes: int = Field(default=1, description="The first n MEs render (FLOWXER_RENDER_MES)")
    latency_grains: int = Field(default=4, description="FLOWXER_LATENCY")
    raster: str
    frame_rate: str
    video_format: str
    audio_format: str
    error: str | None = None
    workspace: dict[str, Any] | None = None
    panels: list[dict[str, Any]] = Field(default_factory=list)
    keyers: list[dict[str, Any]] = Field(default_factory=list)
    stinger_slots: list[dict[str, Any]] = Field(default_factory=list)
    wipe_armed: bool = False
    last_transition: str = "cut"
    nmos: dict[str, Any] = Field(default_factory=dict)
    engine: dict[str, Any] = Field(
        default_factory=dict,
        description="The media engine: process state, restarts, fps, latency and its recommendation",
    )


class HealthResponse(BaseModel):
    status: str
    service: str
    version: str
    mxl_domain: str
    mxl_root: str = ""
    mxl_output_domain_id: str = ""
    simulate: bool
    engine: str = Field(default="stopped", description="State of the media engine process")
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
    transition: PanelTransition = Field(default_factory=PanelTransition)
    ftb: bool = Field(default=False, description="Fade to Black: the ME's whole output (DSKs and sound too) is black")


class KeyerStage(str, Enum):
    upstream = "upstream"
    downstream = "downstream"


class KeyMode(str, Enum):
    linear = "linear"
    luma = "luma"
    pip = "pip"


class DmeCrop(BaseModel):
    left: float = Field(default=0.0, ge=0.0, le=1.0)
    right: float = Field(default=0.0, ge=0.0, le=1.0)
    top: float = Field(default=0.0, ge=0.0, le=1.0)
    bottom: float = Field(default=0.0, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _leaves_a_picture(self) -> "DmeCrop":
        if self.left + self.right >= 1.0 or self.top + self.bottom >= 1.0:
            raise ValueError("the crop must leave a picture (left + right < 1, top + bottom < 1)")
        return self


class DmeBorder(BaseModel):
    width: float = Field(default=0.0, ge=0.0, le=64.0, description="Outer border width in raster pixels (0: none)")
    soft: float = Field(default=0.0, ge=0.0, le=64.0, description="Soft outer edge of the border in pixels")
    color: str = Field(default="#ffffff", pattern=r"^#[0-9a-fA-F]{6}$")
    opacity: float = Field(default=1.0, ge=0.0, le=1.0)


class DmeShadow(BaseModel):
    dx: float = Field(default=0.0, ge=-128.0, le=128.0, description="Offset in raster pixels")
    dy: float = Field(default=0.0, ge=-128.0, le=128.0)
    blur: float = Field(default=0.0, ge=0.0, le=128.0, description="Soft edge of the shadow in pixels")
    opacity: float = Field(default=0.0, ge=0.0, le=1.0, description="0: no shadow")


class Dme(BaseModel):
    """A keyer's box (docs/engine-p4.md §3): its fill (and key) cropped, scaled and placed. x and
    y are the centre of the uncropped picture as fractions of the raster, size its scale (1 = the
    full raster); the crop cuts the picture without moving it. Upstream keyers only; always on
    for a pip."""

    enabled: bool = False
    x: float = Field(default=0.5, ge=-1.0, le=2.0)
    y: float = Field(default=0.5, ge=-1.0, le=2.0)
    size: float = Field(default=0.5, ge=0.0, le=2.0)
    soft: float = Field(default=0.0, ge=0.0, le=64.0, description="Soft edge of the picture without a border, in pixels")
    crop: DmeCrop = Field(default_factory=DmeCrop)
    border: DmeBorder = Field(default_factory=DmeBorder)
    shadow: DmeShadow = Field(default_factory=DmeShadow)


class Ease(str, Enum):
    linear = "linear"
    ease = "ease"
    ease_in = "ease-in"
    ease_out = "ease-out"
    hold = "hold"


class DmeLoop(str, Enum):
    off = "off"
    wrap = "wrap"
    pingpong = "pingpong"


class DmeKeyframeBorder(BaseModel):
    width: float = Field(default=0.0, ge=0.0, le=64.0)
    soft: float = Field(default=0.0, ge=0.0, le=64.0)
    opacity: float = Field(default=1.0, ge=0.0, le=1.0)


class DmeKeyframe(BaseModel):
    """A keyframe (docs/engine-p4.md §5): a box state (the Dme fields without `enabled` and the
    border colour, which are not animated) and the time from the keyframe before: `frames` with
    its `ease` (linear, ease = smoothstep in and out, ease-in, ease-out, hold = stay, then jump at
    the end). Size is interpolated in log space, the rest linearly. Keyframe 0's frames and ease
    are used when a run reaches it from a box off the keyframes' path."""

    frames: int = Field(default=25, ge=1, le=1000)
    ease: Ease = Field(default=Ease.linear)
    x: float = Field(default=0.5, ge=-1.0, le=2.0)
    y: float = Field(default=0.5, ge=-1.0, le=2.0)
    size: float = Field(default=0.5, ge=0.0, le=2.0)
    soft: float = Field(default=0.0, ge=0.0, le=64.0)
    crop: DmeCrop = Field(default_factory=DmeCrop)
    border: DmeKeyframeBorder = Field(default_factory=DmeKeyframeBorder)
    shadow: DmeShadow = Field(default_factory=DmeShadow)


class KeyerRun(BaseModel):
    """A keyer's run along its keyframes as the engine reports it (not saved): running (waiting
    for its first frame or moving), whether it is the keyer's Auto (`fly`), its loop, the keyframe
    the box moves to (or, not running, the one it is at), and the frames since it started."""

    running: bool = False
    fly: bool = False
    loop: DmeLoop = DmeLoop.off
    keyframe: int | None = None
    step: int = 0


class Keyer(BaseModel):
    """An upstream keyer (before the transition) or a DSK (after it) of one ME. Keys show on the
    ME's Program only (keys have no preview). Linear: its key input, a v210a fill + key MXL flow
    (NMOS receiver `receiver_label`). Luma: a self key of a source the ME may take. Pip (upstream
    only): a source the ME may take, opaque, in a box. An upstream keyer's `dme` puts its key in a
    box (always on for a pip); its `keyframes` animate the box (runs, loops), and with `fly` its
    Auto flies the box from keyframe 0 (off) to the last (on) instead of fading."""

    id: str
    label: str
    panel_id: str = Field(default="me-1", description="The ME it keys")
    stage: KeyerStage = Field(default=KeyerStage.downstream)
    enabled: bool = Field(default=False, description="On air")
    rate_ms: int = Field(default=500, ge=1, le=10000, description="Duration of its Auto (fade on or off)")
    mode: KeyMode = Field(default=KeyMode.linear)
    source_id: str | None = Field(default=None, description="Luma and pip: the source (an input or me-<n>)")
    clip: float = Field(default=0.5, ge=0.0, le=1.0, description="Luma: the level that keys (0 black .. 1 white)")
    soft: float = Field(default=0.1, ge=0.0, le=1.0, description="Luma: the softness above the clip")
    gain: float = Field(default=1.0, ge=0.0, le=1.0, description="Scales the key")
    invert: bool = Field(default=False, description="Luma: key the dark parts")
    dme: Dme = Field(default_factory=Dme, description="The box (upstream keyers): where it is now")
    fly: bool = Field(default=False, description="Auto flies the box along its keyframes (at least 2) instead of fading")
    keyframes: list[DmeKeyframe] = Field(default_factory=list, max_length=8, description="Up to 8 (upstream keyers)")
    run: KeyerRun | None = Field(default=None, description="The engine's run of the box (not saved; none off-air)")

    @model_validator(mode="after")
    def _pip_is_a_box(self) -> "Keyer":
        # A pip keyer is always a box (states saved before P4 M2 could say otherwise).
        if self.mode == KeyMode.pip:
            self.dme.enabled = True
        return self

    @property
    def receiver_label(self) -> str:
        """Its key input's NMOS receiver: `ME n Key k` (upstream) or `<label> Key` (DSK)."""
        return self.label if self.stage == KeyerStage.upstream else f"{self.label} Key"


# P2 name of the keyer model.
DownstreamKeyer = Keyer


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
    keyers_per_me: int = Field(default=2, ge=0, le=8, description="Upstream keyers of every rendering ME")
    dsks_per_me: int = Field(default=1, ge=0, le=4, description="DSKs of every rendering ME")
    stinger_players: int = Field(
        default=1, ge=0, le=4, description="Stinger players (at most the rendering MEs; each plays one stinger at a time)"
    )
    source_tile_aspect: str = Field(
        default="16:9",
        description="Operator source-tile picture ratio: 16:9 (landscape) or 9:16 (portrait).",
    )

    @model_validator(mode="before")
    @classmethod
    def _p2_keyers(cls, data):
        # P2: downstream_keyer_count (0 or 1, DSK 1 on ME 1) is the DSKs per ME now.
        if isinstance(data, dict) and "downstream_keyer_count" in data:
            data = dict(data)
            count = data.pop("downstream_keyer_count")
            data.setdefault("dsks_per_me", min(int(count or 0), 4))
        return data


class WorkspaceUpdate(BaseModel):
    format_id: str | None = None
    logical_source_count: int | None = Field(default=None, ge=1, le=24)
    mixer_panel_count: int | None = Field(default=None, ge=1, le=4)
    stinger_mode: str | None = None
    stinger_count: int | None = Field(default=None, ge=1, le=8)
    keyers_per_me: int | None = Field(default=None, ge=0, le=8)
    dsks_per_me: int | None = Field(default=None, ge=0, le=4)
    stinger_players: int | None = Field(default=None, ge=0, le=4)
    source_tile_aspect: str | None = Field(
        default=None,
        description="16:9 or 9:16 source tiles on the operator deck",
    )

    @model_validator(mode="before")
    @classmethod
    def _p2_keyers(cls, data):
        if isinstance(data, dict) and "downstream_keyer_count" in data:
            data = dict(data)
            count = data.pop("downstream_keyer_count")
            data.setdefault("dsks_per_me", min(int(count or 0), 4) if count is not None else None)
        return data


class KeyerUpdate(BaseModel):
    enabled: bool | None = Field(default=None, description="On or off at the next frame")
    label: str | None = None
    rate_ms: int | None = Field(default=None, ge=1, le=10000, description="Duration of its Auto")
    mode: KeyMode | None = None
    source_id: str | None = None
    clip: float | None = Field(default=None, ge=0.0, le=1.0)
    soft: float | None = Field(default=None, ge=0.0, le=1.0)
    gain: float | None = Field(default=None, ge=0.0, le=1.0)
    invert: bool | None = None
    dme: dict[str, Any] | None = Field(
        default=None,
        description="Box fields to change (see Dme: enabled, x, y, size, soft, crop, border, shadow); missing fields keep their values. "
        "PUT /keyers/{id}/dme replaces the whole box.",
    )
    fly: bool | None = Field(default=None, description="Auto flies the box along its keyframes instead of fading")


class KeyerRunRequest(BaseModel):
    """A run of the box from where it is: to keyframe `to` (default the last; keyframe 0 with
    `reverse`) through the keyframes in between, at their own frames and ease; or a loop (wrap:
    to the last keyframe, then jump to 0; pingpong: back and forth; down first with `reverse`)
    until stopped. `frames` 0 jumps to keyframe `to` at the next frame."""

    to: int | None = Field(default=None, ge=0, le=7)
    reverse: bool = False
    loop: DmeLoop = Field(default=DmeLoop.off)
    frames: int | None = Field(default=None, ge=0, le=0, description="0: jump to the keyframe")

    @model_validator(mode="after")
    def _one_target(self) -> "KeyerRunRequest":
        if self.to is not None and (self.reverse or self.loop != DmeLoop.off):
            raise ValueError("to excludes reverse and loop")
        if self.frames is not None and self.loop != DmeLoop.off:
            raise ValueError("frames 0 (a jump) excludes a loop")
        return self


class DmePreset(str, Enum):
    corner_tl = "corner-tl"
    corner_tr = "corner-tr"
    corner_bl = "corner-bl"
    corner_br = "corner-br"
    split = "split"
    quad = "quad"
    full = "full"


class KeyerPresetRequest(BaseModel):
    name: DmePreset


class DmePresetInfo(BaseModel):
    name: DmePreset
    title: str
    keyers: int = Field(description="1: the keyer it is applied to; 2 and 4: the ME's upstream keyers 1..n")
    description: str


# ── audio (P5, docs/engine-p5.md) ──────────────────────────────────────────

AUDIO_MIN_DB = -90.0
AUDIO_MAX_DB = 10.0


class AudioStrip(BaseModel):
    """One strip of an ME's audio mix: a live input, a clip player, the re-entered Program of a
    higher ME (its panel id) or the stinger."""

    fader_db: float = Field(default=0.0, ge=AUDIO_MIN_DB, le=AUDIO_MAX_DB, description="-90 (off) .. +10 dB")
    mute: bool = Field(default=False, description="Silent; the fader stays where it is")
    afv: bool = Field(
        default=True,
        description=(
            "Audio follows video: heard only while its source is on the ME's Program (and along a "
            "transition's curve). Off: always in the mix at its fader level. The stinger strip has none."
        ),
    )


class AudioStripUpdate(BaseModel):
    fader_db: float | None = Field(default=None, ge=AUDIO_MIN_DB, le=AUDIO_MAX_DB)
    mute: bool | None = None
    afv: bool | None = None


class PanelAudio(BaseModel):
    """An ME's audio: its master fader and the strips that are not at their defaults."""

    master_db: float = Field(default=0.0, ge=AUDIO_MIN_DB, le=AUDIO_MAX_DB, description="-90 (off) .. +10 dB")
    strips: dict[str, AudioStrip] = Field(default_factory=dict, description="By strip id (input id, panel id or stinger)")


class PanelAudioUpdate(BaseModel):
    master_db: float | None = Field(default=None, ge=AUDIO_MIN_DB, le=AUDIO_MAX_DB)


class InputAudio(BaseModel):
    """A live input's audio, for every ME: its delay and its channel map."""

    delay_ms: float = Field(
        default=0.0, ge=0.0, le=5000.0, description="Audio later than its video by this (ms, whole 48 kHz samples; at most FLOWXER_AUDIO_MAX_DELAY_MS)"
    )
    channel_map: list[int] = Field(
        default_factory=list,
        description="Per PGM channel the input channel it takes (1-based, 0 silent); empty: 1:1 (a 1-channel flow on every channel)",
    )


class InputAudioUpdate(BaseModel):
    delay_ms: float | None = Field(default=None, ge=0.0, le=5000.0)
    channel_map: list[int] | None = None


class AudioConfig(BaseModel):
    """The saved audio state (the state file's and the export's `audio`): per live input its delay and
    channel map, per panel its master and strips. Missing values are the defaults."""

    inputs: dict[str, InputAudio] = Field(default_factory=dict)
    panels: dict[str, PanelAudio] = Field(default_factory=dict)


# ── snapshots (docs/snapshots.md) ──────────────────────────────────────────

SNAPSHOT_ALL = "all"
SNAPSHOT_MAX = 99


class SnapshotOptions(BaseModel):
    """What a recall takes besides each ME's Preview, its transition and the settings of its keyers
    that are off air."""

    program: bool = Field(
        default=False,
        description=(
            "Program/on-air: also Program (a cut), the keyers' and DSKs' on or off (cuts), the settings of "
            "keyers on air and FTB (a cut). Off: none of these changes"
        ),
    )
    audio: bool = Field(default=False, description="Also the ME's audio: its strips and master (each ramps over one grain)")


class SnapshotOptionsUpdate(BaseModel):
    program: bool | None = None
    audio: bool | None = None


class SnapshotKeyer(BaseModel):
    """A keyer in a snapshot: on air or not, its rate, its key and its box (a Keyer without its id,
    label, ME, stage and run)."""

    enabled: bool = False
    rate_ms: int = Field(default=500, ge=1, le=10000)
    mode: KeyMode = Field(default=KeyMode.linear)
    source_id: str | None = None
    clip: float = Field(default=0.5, ge=0.0, le=1.0)
    soft: float = Field(default=0.1, ge=0.0, le=1.0)
    gain: float = Field(default=1.0, ge=0.0, le=1.0)
    invert: bool = False
    dme: Dme = Field(default_factory=Dme)
    fly: bool = False
    keyframes: list[DmeKeyframe] = Field(default_factory=list, max_length=8)


class MeSnapshot(BaseModel):
    """One ME in a snapshot."""

    program_input_id: str | None = None
    preview_input_id: str | None = None
    ftb: bool = False
    transition: PanelTransition = Field(default_factory=PanelTransition)
    keyers: dict[str, SnapshotKeyer] = Field(default_factory=dict, description="Its upstream keyers and DSKs, by keyer id")
    audio: PanelAudio | None = Field(
        default=None, description="Its master and the strips not at their defaults; none for an ME that did not render"
    )


class SnapshotInfo(BaseModel):
    number: int = Field(ge=1, le=SNAPSHOT_MAX, description="1..99, the snapshot's id (one number space for all)")
    name: str = Field(min_length=1, max_length=40)
    panel_id: str = Field(description="me-<n>: that ME; all: every ME")
    options: SnapshotOptions = Field(default_factory=SnapshotOptions, description="The defaults of its recall")
    stored_at: str = Field(default="", description="When it was stored (UTC, ISO 8601)")


class Snapshot(SnapshotInfo):
    """A named memory of one ME or of all MEs (docs/snapshots.md)."""

    mes: dict[str, MeSnapshot] = Field(default_factory=dict, description="By panel id")


class SnapshotConfig(BaseModel):
    """The saved snapshots (the state file's and the export's `snapshots`). Missing: none."""

    startup: int | None = Field(default=None, ge=1, le=SNAPSHOT_MAX, description="Recalled with its options when the mixer starts")
    items: list[Snapshot] = Field(default_factory=list)

    @model_validator(mode="after")
    def _numbers(self) -> "SnapshotConfig":
        numbers = [item.number for item in self.items]
        if len(set(numbers)) != len(numbers):
            raise ValueError("duplicate snapshot numbers")
        if self.startup is not None and self.startup not in numbers:
            raise ValueError(f"the start-up snapshot {self.startup} does not exist")
        self.items.sort(key=lambda item: item.number)
        return self


class SnapshotStore(BaseModel):
    panel_id: str = Field(description="me-<n>: that ME; all: every ME")
    number: int | None = Field(
        default=None, ge=1, le=SNAPSHOT_MAX, description="Default: the lowest free number. A used number is stored over"
    )
    name: str | None = Field(default=None, max_length=40, description="Default: the old name, else '<ME> #<number>' or 'All MEs #<number>'")
    options: SnapshotOptionsUpdate | None = Field(default=None, description="The recall's defaults (default: the old ones, else off)")


class SnapshotUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=40)
    options: SnapshotOptionsUpdate | None = None


class SnapshotStartup(BaseModel):
    number: int | None = Field(default=None, ge=1, le=SNAPSHOT_MAX, description="None: no start-up snapshot")


class SnapshotRecallResult(BaseModel):
    snapshot: SnapshotInfo
    options: SnapshotOptions = Field(description="The options this recall used")
    skipped: list[str] = Field(
        default_factory=list,
        description="What it did not change, and why: a source, ME, keyer or stinger slot that is gone, a keyer on air, a held T-bar",
    )


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
    keyers: list[Keyer]
    stinger_slots: list[StingerSlot]
    mixer: MixerStatus
    resources: ResourceInfo
    preview: dict[str, Any] = Field(
        default_factory=dict,
        description="The preview mosaic (one WebRTC/HLS stream) and its tile map, as GET /preview",
    )
    sources: dict[str, dict[str, Any]] = Field(
        default_factory=dict,
        description=(
            "Per input id what the engine reports: state, arrival lag (video and audio, ms), "
            "clip player state"
        ),
    )
    clips: list[StorageClip]
    stingers: list[StingerInfo]
    library: list["LibraryItemOut"] = Field(default_factory=list)
    jobs: list["ConvertJobOut"] = Field(default_factory=list)
    tally: TallyConfig = Field(default_factory=TallyConfig)
    nmos: dict[str, Any] = Field(
        default_factory=dict,
        description="IS-04/IS-05 node status: registry, node id, per-input receivers.",
    )
    audio: dict[str, Any] = Field(
        default_factory=dict,
        description="The audio settings, as GET /audio (not the meters: GET /audio/meters)",
    )
    snapshots: dict[str, Any] = Field(
        default_factory=dict, description="The snapshots without their contents and the start-up snapshot, as GET /snapshots"
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
    # A clip or stinger with sound (a stinger's sound goes into PGM).
    has_sound: bool = False
    cut_frame: int | None = None
    cut_ms: int | None = None
    frame_count: int = 0
    duration_s: float = 0.0
    thumb_url: str | None = None
    error: str | None = None
    source: str = "upload"
    legacy_path: str | None = None
    in_use: bool = False


class GrabRequest(BaseModel):
    """A frame grab (docs/engine-p4.md §6): the picture of the next output grain as a library still."""

    source: str = Field(
        ...,
        description="A live input or clip player id, a panel id (its Program; also <panel id>:pgm) or <panel id>:pvw (its Preview)",
    )
    player: str | None = Field(default=None, description="A clip player to load the still into once it is ready (the frame memory)")
    name: str | None = Field(
        default=None, min_length=1, max_length=128, description="Default: Grab <source label> <YYYY-MM-DD HH:MM:SS> (local time)"
    )


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
    kind: str = Field(default="clip", description="clip, still or stinger")
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
