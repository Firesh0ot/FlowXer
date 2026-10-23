from __future__ import annotations

import copy
import logging
import math
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

from pydantic import ValidationError

from flowxer import __version__
from flowxer.api.schemas import (
    DME_CROP_MAX_PX,
    AudioConfig,
    AudioEssence,
    AudioStrip,
    AudioStripUpdate,
    ClipEnd,
    Dme,
    DmeCrop,
    DmeKeyframe,
    DmeLoop,
    DmePreset,
    DmePresetInfo,
    InputAudio,
    InputAudioUpdate,
    InputKind,
    Keyer,
    KeyerRun,
    KeyerStage,
    KeyMode,
    LogicalInput,
    LogicalInputCreate,
    LogicalInputUpdate,
    MeOutputs,
    MeSnapshot,
    MixerPanel,
    MixerStartRequest,
    MixerState,
    MixerStatus,
    OutputFlows,
    PanelAudio,
    PanelAudioUpdate,
    PanelTransition,
    PanelTransitionUpdate,
    ProgramBus,
    SNAPSHOT_ALL,
    SNAPSHOT_MAX,
    Snapshot,
    SnapshotConfig,
    SnapshotKeyer,
    SnapshotOptions,
    SnapshotOptionsUpdate,
    SnapshotStore,
    SnapshotUpdate,
    StingerInfo,
    StingerSlot,
    StingerSlotUpdate,
    TallyReceiver,
    TallyReceiverStatus,
    TransitionType,
    VideoEssence,
    WorkspaceConfig,
    WorkspaceUpdate,
)
from flowxer.domain import nmos
from flowxer.domain.mxl_domain import (
    DomainError,
    ensure_output_domain,
    find_flow_domain,
    flows_by_group_hint_in_root,
    list_flows_in_root,
    remove_output_domain,
    resolve_domain_path,
)
from flowxer.engine.formats import format_by_id
from flowxer.engine.runtime import EngineClient, EngineError, EngineRuntime
from flowxer.engine.security import SecurityError, contained_path, require_safe_id
from flowxer.engine.state import STATE_FORMAT, StateStore
from flowxer.engine.stinger import (
    cut_frame_from_ms,
    cut_ms_from_frame,
    duration_ms_from_frames,
    generate_replay_wipe,
    inspect_stinger,
    list_stingers,
    register_video_stinger,
    update_stinger_cut,
)
from flowxer.engine.tally import TallyService
from flowxer.engine.tally_export import MeTally, TallyExport, TallySnapshot
from flowxer.engine.visibility import KeyLayer, MeLayers, direct_visibility, visible_keys_on_program, visible_on_program
from flowxer.engine.watchdog import ControlPlaneWatchdog
from flowxer.library.models import LibraryItem, LibraryKind
from flowxer.library.service import LibraryService
from flowxer.settings import Settings, ensure_storage

log = logging.getLogger(__name__)
# A running Program without a new frame for this long is reported in the status (error).
PROGRAM_STALL_S = 3.0

CLIP_SUFFIXES = {".mp4", ".mov", ".mkv", ".ts", ".mxf", ".wav", ".m4a"}
# A mix (take with transition mix) without a duration.
DEFAULT_MIX_MS = 400
# An input whose MXL domain is missing (a fabrics mirror after a node reboot) is checked this often.
DOMAIN_WATCH_S = 2.0
# FLOWXER_PROGRAM_AUTOSTART tries again after these pauses (s) while Program does not start; the
# last one repeats.
AUTOSTART_BACKOFF_S = (2.0, 5.0, 10.0, 30.0)
# Sources in slot order (the TSL INDEX): live inputs, clip players, then Black.
KIND_ORDER = {InputKind.mxl_live: 0, InputKind.clip_player: 1, InputKind.black: 2}
# The re-entered Program of ME n is the source "me-<n>" (TSL INDEX 1000 + n).
REENTRY_INDEX = 1000
# A stinger take that finds every stinger player busy waits this long for one to be free.
STINGER_WAIT_S = 0.5
# A frame grab is in the library within a few grains; the control plane waits at most this long.
GRAB_TIMEOUT_S = 3.0
# Audio delays are whole 48 kHz samples.
AUDIO_SAMPLES_PER_MS = 48
# A fader that moves sends up to 20 values a second: its changes are saved at most this often.
PERSIST_INTERVAL_S = 1.0
# GET /audio/meters: one engine call per 100 ms block, however many viewers poll.
METERS_CACHE_S = 0.08
# Meter values below this are this (silence).
METER_FLOOR_DB = -120.0
# A clip player's end action (docs/clip-end.md): the engine's players are read this often while
# a clip player with one is on a rendering ME's Program; within CLIP_END_HORIZON grains of its
# start a timer sends the take CLIP_END_LEAD grains early, held by the engine for its grain. An end
# found up to CLIP_END_LATE grains late still acts (at once).
CLIP_END_POLL_S = 0.1
CLIP_END_HORIZON = 12
CLIP_END_LEAD = 3
CLIP_END_LATE = 50


def _autostart_pause(attempt: int) -> float:
    return AUTOSTART_BACKOFF_S[min(attempt, len(AUTOSTART_BACKOFF_S)) - 1]


def _frames(duration_ms: int, fps: float) -> int:
    """A transition duration in grains (at least 1)."""
    return max(1, int(round(duration_ms * fps / 1000.0)))


def _merged(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """`base` with `patch` applied, nested objects merged (a keyer's box from a partial PATCH)."""
    out = dict(base)
    for name, value in patch.items():
        out[name] = _merged(out[name], value) if isinstance(value, dict) and isinstance(out.get(name), dict) else value
    return out


def _box_state(box: dict[str, Any]) -> dict[str, Any]:
    """The animated fields of a box (Dme, a keyframe or the engine's box): not `enabled`, not the
    border colour."""
    border = box.get("border") or {}
    state = {name: box[name] for name in ("x", "y", "size", "soft", "crop", "shadow") if name in box}
    state["border"] = {name: border[name] for name in ("width", "soft", "opacity") if name in border}
    return state


_CROP_SIDES = ("left", "right", "top", "bottom")


def _even(px: float) -> int:
    """The engine crops left and right on whole 4:2:2 sample pairs: the nearest even column."""
    return 2 * math.floor(px / 2 + 0.5)


def _engine_box(box: dict[str, Any], width: int, height: int) -> dict[str, Any]:
    """A box (or a part of one, or a keyframe) for the engine, whose crop is fractions of the
    source: the API's pixels over the raster (left and right on even columns, so the engine's
    float32 rounding lands on the same column as the GUI's geometry)."""
    crop = box.get("crop")
    if not isinstance(crop, dict):
        return box
    out = dict(crop)
    for side in _CROP_SIDES:
        if side in out:
            out[side] = _even(out[side]) / width if side in ("left", "right") else out[side] / height
    return {**box, "crop": out}


def _box_from_engine_units(box: dict[str, Any], width: int, height: int) -> dict[str, Any]:
    """The engine's box (crop in fractions, between keyframes not whole) with the crop in pixels."""
    crop = box.get("crop")
    if not isinstance(crop, dict):
        return box
    out = dict(crop)
    for side in _CROP_SIDES:
        if isinstance(out.get(side), (int, float)):
            out[side] = round(out[side] * (width if side in ("left", "right") else height))
    return {**box, "crop": out}


def _crop_error(crop: DmeCrop, width: int, height: int) -> str | None:
    """Why the engine would refuse the crop (left and right as it gets them: on even columns)."""
    if _even(crop.left) + _even(crop.right) >= width or crop.top + crop.bottom >= height:
        return (
            f"the crop must leave a picture: left + right < {width} (each on an even column) and top + bottom < {height} "
            "source pixels"
        )
    return None


def repair_crops(data: Any, width: int, height: int) -> list[str]:
    """A saved state's crops that the raster cannot take (no picture left, out of range) become no
    crop on that axis; returns what it changed (for the log). Only on restore: an import refuses."""
    fixed: list[str] = []

    def walk(node: Any, where: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "crop" and isinstance(value, dict):
                    sides = {side: value.get(side, 0) for side in _CROP_SIDES}
                    bad = [side for side, v in sides.items() if not isinstance(v, (int, float)) or v < 0 or v > DME_CROP_MAX_PX]
                    pairs = {("left", "right"): width, ("top", "bottom"): height}
                    for (a, b), size in pairs.items():
                        values_ok = a not in bad and b not in bad
                        whole = values_ok and (_even(sides[a]) + _even(sides[b]) if size == width else sides[a] + sides[b]) < size
                        if not whole:
                            fixed.append(f"{where}.crop {a}/{b} {sides[a]}/{sides[b]}")
                            value[a] = 0
                            value[b] = 0
                else:
                    walk(value, f"{where}.{key}" if where else str(key))
        elif isinstance(node, list):
            for index, item in enumerate(node):
                walk(item, f"{where}[{index}]")

    walk(data, "")
    return fixed


def _legacy_crop(crop: Any) -> bool:
    """A crop saved before 2026-10-22: fractions of the source (a value between 0 and 1 that is
    not whole; all zeros is the same in both)."""
    return isinstance(crop, dict) and any(
        isinstance(crop.get(side), float) and 0 < crop[side] < 1 for side in _CROP_SIDES
    )


def crop_paths(node: Any, path: tuple = ()) -> list[tuple[tuple, dict]]:
    """Every crop (a dict under the key "crop") in a document, with its path."""
    found: list[tuple[tuple, dict]] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "crop" and isinstance(value, dict):
                found.append((path + (key,), value))
            else:
                found += crop_paths(value, path + (key,))
    elif isinstance(node, list):
        for index, item in enumerate(node):
            found += crop_paths(item, path + (index,))
    return found


def _scaled_crop(crop: dict, raster: tuple[int, int], to: tuple[int, int]) -> dict:
    """A crop in pixels of `raster` in pixels of `to` (whole pixels)."""
    return {
        side: round((crop.get(side) or 0) * (to[0] / raster[0] if side in ("left", "right") else to[1] / raster[1]))
        for side in _CROP_SIDES
    }


def scale_crops(data: Any, sx: float, sy: float, *, legacy_only: bool | None) -> int:
    """Every crop in a document (keyers, keyframes, snapshots: any dict under the key "crop") times
    sx (left, right) and sy (top, bottom), rounded to whole pixels. `legacy_only` True: only crops
    in fractions (_legacy_crop), to pixels of the raster sx × sy; False: only crops in pixels (from
    one raster to another); None: all. Returns how many it changed."""
    changed = 0
    if isinstance(data, dict):
        for key, value in data.items():
            if key == "crop" and isinstance(value, dict) and (legacy_only is None or _legacy_crop(value) == legacy_only):
                for side in _CROP_SIDES:
                    if isinstance(value.get(side), (int, float)):
                        value[side] = round(value[side] * (sx if side in ("left", "right") else sy))
                changed += 1
            else:
                changed += scale_crops(value, sx, sy, legacy_only=legacy_only)
    elif isinstance(data, list):
        for item in data:
            changed += scale_crops(item, sx, sy, legacy_only=legacy_only)
    return changed


# DME presets (docs/engine-p4.md §5): keyframe 0 off screen on the near side, keyframe 1 on.
DME_PRESETS = [
    DmePresetInfo(name=DmePreset.corner_tl, title="Top left", keyers=1, description="1/3 size in the top left corner inside the 5 % action-safe margin, 4 px white border, soft shadow"),
    DmePresetInfo(name=DmePreset.corner_tr, title="Top right", keyers=1, description="1/3 size in the top right corner, as corner-tl"),
    DmePresetInfo(name=DmePreset.corner_bl, title="Bottom left", keyers=1, description="1/3 size in the bottom left corner, as corner-tl"),
    DmePresetInfo(name=DmePreset.corner_br, title="Bottom right", keyers=1, description="1/3 size in the bottom right corner, as corner-tl"),
    DmePresetInfo(name=DmePreset.split, title="Split", keyers=2, description="the ME's keyers 1 and 2: each source cropped to its centre half, full size, side by side"),
    DmePresetInfo(name=DmePreset.quad, title="Quad", keyers=4, description="the ME's keyers 1 to 4 at 1/2 size, 2 x 2"),
    DmePresetInfo(name=DmePreset.full, title="Full", keyers=1, description="size 1, centred: keyframe 0 is the box as it is, so the box flies to and from full screen"),
]
# Keyframe 0 of a preset: this far beyond the raster edge (fractions), border and shadow included.
_OFF = 0.05
_NO_CROP = {"left": 0, "right": 0, "top": 0, "bottom": 0}
_PLAIN = {"soft": 0.0, "border": {"width": 0.0, "soft": 0.0, "opacity": 1.0}, "shadow": {"dx": 0.0, "dy": 0.0, "blur": 0.0, "opacity": 0.0}}


def _preset_boxes(name: DmePreset, width: int, height: int, current: dict[str, Any]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """(on, off) box fields of each keyer a preset writes (1, 2 or 4). `current`: the box of the
    keyer it is applied to (keyframe 0 of `full`)."""
    if name in (DmePreset.corner_tl, DmePreset.corner_tr, DmePreset.corner_bl, DmePreset.corner_br):
        size, border = 1.0 / 3.0, 4.0
        left = name in (DmePreset.corner_tl, DmePreset.corner_bl)
        top = name in (DmePreset.corner_tl, DmePreset.corner_tr)
        # The picture and its border inside the 5 % action-safe margin.
        mx = (0.05 * width + border) / width + size / 2
        my = (0.05 * height + border) / height + size / 2
        on = {
            "x": mx if left else 1.0 - mx, "y": my if top else 1.0 - my, "size": size, "soft": 0.0, "crop": _NO_CROP,
            "border": {"width": border, "soft": 0.0, "opacity": 1.0, "color": "#ffffff"},
            "shadow": {"dx": 8.0, "dy": 8.0, "blur": 16.0, "opacity": 0.5},
        }
        return [(on, {**on, "x": -size / 2 - _OFF if left else 1.0 + size / 2 + _OFF})]
    if name == DmePreset.split:
        # A quarter of the source's width off each side (pixels).
        crop = {"left": round(width / 4), "right": round(width / 4), "top": 0, "bottom": 0}
        return [
            ({"x": x, "y": 0.5, "size": 1.0, "crop": crop, **_PLAIN}, {"x": off, "y": 0.5, "size": 1.0, "crop": crop, **_PLAIN})
            for x, off in ((0.25, -0.25 - _OFF), (0.75, 1.25 + _OFF))
        ]
    if name == DmePreset.quad:
        return [
            ({"x": x, "y": y, "size": 0.5, "crop": _NO_CROP, **_PLAIN}, {"x": off, "y": y, "size": 0.5, "crop": _NO_CROP, **_PLAIN})
            for y in (0.25, 0.75)
            for x, off in ((0.25, -0.25 - _OFF), (0.75, 1.25 + _OFF))
        ]
    return [({"x": 0.5, "y": 0.5, "size": 1.0, "crop": _NO_CROP}, _box_state(current))]


# The T-bar lever starts nothing within this much of the end it rests at (end-of-travel hysteresis).
TBAR_DEADBAND = 0.02
# The deadband's edges count (1 − 0.98 is 0.020000000000000018 in floats).
_EPS = 1e-9
# Hardware levers (`source`) kept per ME for the pickup.
TBAR_SOURCES = 64

# A mix's or a key fade's end is published this long after it (s), so it has ended for sure.
TALLY_END_MARGIN_S = 0.05


class MixerError(RuntimeError):
    pass


class StingerNotReady(MixerError):
    """Raised when a library stinger has no mezzanine yet; callers hard-cut instead."""


class VisionMixer:
    """Control plane of the vision mixer: state, decisions, NMOS, tally, library. The media
    engine (a child process) renders what this state says."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.library = LibraryService(settings)
        self.library.consumers_fn = self._library_consumers
        self.library.add_listener(self._on_library_job)
        self.inputs: dict[str, LogicalInput] = {}
        self.workspace = self._pinned(WorkspaceConfig())
        self.panels: list[MixerPanel] = []
        self.keyers: list[Keyer] = []
        self.stinger_slots: list[StingerSlot] = []
        self.started_at = time.time()
        self.state = MixerState.idle
        self.backend = "idle"
        self.program_input_id: str | None = None
        self.preview_input_id: str | None = None
        self.last_live_input_id: str | None = None
        self.program_bus = ProgramBus.live
        self.error: str | None = None
        # ME 1 Program flows (kept for clients of the single-ME API) and every rendering ME's.
        self.outputs: OutputFlows | None = None
        self.me_outputs: dict[str, MeOutputs] = {}
        # The media engine while Program is on air (None in simulate mode and off-air).
        self.engine: EngineRuntime | None = None
        self.last_transition: str = "cut"
        # Program frames of earlier runs; frames_rendered adds the running engine's.
        self._frames_before = 0
        self._frames_dropped_before = 0
        # (frames_rendered, monotonic time) when Program last advanced; a stall is reported in status().
        self._frame_mark: tuple[int, float] = (0, 0.0)
        self._stall_logged = False
        # Operations that need the control plane; /livez fails when one is stuck.
        self.watchdog = ControlPlaneWatchdog()
        self.transition_counts: dict[str, int] = {"cut": 0, "mix": 0, "wipe": 0, "stinger": 0}
        # Stinger takes that became cuts here: no mezzanine yet, or every stinger player busy.
        self.stinger_fallbacks: dict[str, int] = {"not_ready": 0, "busy": 0}
        self._lock = threading.RLock()
        # (input id, essence) -> MXL domain id of a route whose domain is missing; routed once it
        # appears.
        self._missing_domains: dict[tuple[str, str], str] = {}
        # Keyer id -> (MXL domain id, flow id) of its key input (IS-05), flow None when not routed.
        self._key_routes: dict[str, tuple[str | None, str | None]] = {}
        # Keyer id -> monotonic time until which a keyer that fades or flies off is still on air
        # (raw tally).
        self._key_until: dict[str, float] = {}
        # A keyer fading in (Auto on, or tied to a mix): the monotonic time its fade ends. Until
        # then it is not at full level (a full-raster pip covers nothing yet).
        self._key_in: dict[str, float] = {}
        # The receivers' republish timers (a mix's or a key's fade end), cancelled on stop.
        self._tally_timers: set[threading.Timer] = set()
        self._tally_timers_lock = threading.Lock()
        # Set by a Program start and by a stop: FLOWXER_PROGRAM_AUTOSTART stops trying.
        self._autostart_done = threading.Event()
        self.tally = TallyService()
        # FLOWXER_TALLY_TSL, started at the end of __init__.
        self.tally_export: TallyExport | None = None
        # Panel id -> (outgoing, incoming, monotonic end or None while a T-bar holds it) of its last
        # mix or wipe, for the raw tally.
        self._mixes: dict[str, tuple[str | None, str, float | None]] = {}
        # Panel id -> (position, outgoing, incoming) of a transition the T-bar holds, and the keyers
        # tied to it (on air while it is held; they change state when it completes).
        self._positions: dict[str, tuple[float, str | None, str]] = {}
        # (ME, source) of hardware levers (without from_end) that came to the resting end since it
        # last changed, the engine started or a state was loaded: the others are ignored until they
        # do (pickup). Empty at start: every hardware lever picks up first.
        self._lever_picked: dict[tuple[str, str], None] = {}  # least recently used first
        # Per ME the hardware lever (source) that holds its transition.
        self._lever_driver: dict[str, str] = {}
        # Each crop's value before the format changes of this session (path -> (raster, crop)),
        # so back-and-forth changes scale from it and do not drift (_scale_crops).
        self._crop_base: dict[tuple, tuple[tuple[int, int], dict]] = {}
        self._held_ties: dict[str, list[str]] = {}
        # Stinger player -> the stinger it holds (its mezzanine), and the monotonic time until which
        # it plays one; the last stinger taken (for the status).
        self._stinger_cued: dict[int, str] = {}
        self._stinger_until: dict[int, float] = {}
        self._stinger_run: dict[str, Any] | None = None
        # P5 audio: per live input its delay and channel map, per panel its master and strips.
        self.audio = AudioConfig()
        # Saves of audio changes, at most once a second (persist_soon).
        self._persist_lock = threading.Lock()
        self._persist_at = 0.0
        self._persist_timer: threading.Timer | None = None
        # Clip players' end actions: per panel the timer of the next one (player id, start grain,
        # timer), the clip it saw playing on its Program and the last one done (player id, end
        # grain), the engine's last /players (render index, monotonic time) and the stinger cues
        # asked for.
        self._clip_end_timers: dict[str, tuple[str, int, threading.Timer]] = {}
        self._clip_end_seen: dict[str, tuple[str, int]] = {}
        self._clip_end_done: dict[str, tuple[str, int]] = {}
        self._players_seen: tuple[int, float] | None = None
        self._clip_end_cued: set[tuple[str, str]] = set()
        # The engine's last /audio/meters and when it was fetched.
        self._meters_lock = threading.Lock()
        self._meters_raw: dict[str, Any] | None = None
        self._meters_at = 0.0
        # Snapshots (docs/snapshots.md) and the one recalled at start.
        self.snapshots = SnapshotConfig()
        from flowxer.nmos.service import NmosNode

        self.nmos = NmosNode(self)
        ensure_storage(settings)
        self._ensure_default_stinger()
        self._sync_sources()
        self._sync_panels()
        self._sync_keyers()
        self._sync_stinger_slots()
        self._store = StateStore(settings.state_dir)
        self._restore_state()
        self.library.start(self.workspace.format_id, fps=self.settings.fps)
        self.refresh_library_bindings()
        if settings.tally_tsl_target is not None:
            self.tally_export = TallyExport(*settings.tally_tsl_target, self._tally_snapshot)

    # ── structure ────────────────────────────────────────────────────────────

    def _ensure_default_stinger(self) -> None:
        dest = self.settings.stingers_dir / self.settings.default_stinger
        existing = inspect_stinger(self.settings.stingers_dir, self.settings.default_stinger)
        if existing and existing.frame_count:
            return
        generate_replay_wipe(
            dest,
            width=self.settings.width,
            height=self.settings.height,
            frame_count=self.settings.stinger_frame_count,
        )

    def _pinned(self, workspace: WorkspaceConfig) -> WorkspaceConfig:
        """The structure the environment sets (FLOWXER_FORMAT, ...) wins over saved values."""
        pinned = self.settings.pinned_workspace
        return workspace.model_copy(update={field: value for field, (value, _) in pinned.items()})

    def _inputs_pinned_by(self) -> str | None:
        """The variables that set the input list, or None when the API may change it."""
        pinned = self.settings.pinned_workspace.get("logical_source_count")
        return pinned[1] if pinned else None

    @staticmethod
    def _new_input(input_id: str, label: str, kind: InputKind, slot: int, previous: LogicalInput | None = None) -> LogicalInput:
        essences = {"video": VideoEssence(), "audio": AudioEssence()} if kind == InputKind.mxl_live else {}
        return LogicalInput(
            id=input_id,
            label=label,
            kind=kind,
            slot=slot,
            stinger_slot_id=previous.stinger_slot_id if previous else None,
            **essences,
        )

    def _pin_sources(self) -> None:
        """The inputs the environment sets: live cam-1..N, clip-1..C, Black. Ids, kinds and labels
        come from the environment; routes, media and auto-stingers of an input with the same id
        and kind stay."""
        plan = [
            (f"cam-{n}", label, InputKind.mxl_live)
            for n, label in enumerate(self.settings.live_input_labels, 1)
        ]
        plan += [
            (f"clip-{n}", f"Clip {n}", InputKind.clip_player)
            for n in range(1, (self.settings.clip_players or 0) + 1)
        ]
        plan.append(("black", "Black", InputKind.black))
        inputs: dict[str, LogicalInput] = {}
        for slot, (input_id, label, kind) in enumerate(plan):
            item = self.inputs.get(input_id)
            if item is None or item.kind != kind:
                item = self._new_input(input_id, label, kind, slot, item)
                self._apply_default_domain(item)
            item.label = label
            item.slot = slot
            inputs[input_id] = item
        self.inputs = inputs

    def _renumber(self) -> None:
        """Slots in source order: live inputs, clip players, Black (each kind in its order)."""
        ordered = sorted(self.inputs.values(), key=lambda item: (KIND_ORDER[item.kind], item.slot))
        for slot, item in enumerate(ordered):
            item.slot = slot

    def _sync_sources(self) -> None:
        if self._inputs_pinned_by():
            self._pin_sources()
        else:
            desired = self.workspace.logical_source_count
            while len(self.inputs) > desired:
                # The last live input goes first, then the last clip player; Black stays.
                removable = sorted(
                    (
                        item
                        for item in self.inputs.values()
                        if item.id not in {self.program_input_id, self.preview_input_id} and item.kind != InputKind.black
                    ),
                    key=lambda i: (i.kind == InputKind.mxl_live, i.slot),
                )
                if not removable:
                    break
                del self.inputs[removable[-1].id]
            # A layout that grows gets Black and one clip player first (the colour bars, a clip),
            # then live inputs.
            if len(self.inputs) < desired and not any(item.kind == InputKind.black for item in self.inputs.values()):
                self.inputs["black"] = self._new_input("black", "Black", InputKind.black, len(self.inputs))
            if len(self.inputs) < desired and not any(item.kind == InputKind.clip_player for item in self.inputs.values()):
                self.inputs["clip-1"] = self._new_input("clip-1", "Clip 1", InputKind.clip_player, len(self.inputs))
            index = 1
            while len(self.inputs) < desired:
                source_id = f"cam-{index}"
                index += 1
                if source_id in self.inputs:
                    continue
                item = self._new_input(source_id, f"Camera {index - 1}", InputKind.mxl_live, len(self.inputs))
                self._apply_default_domain(item)
                # New live inputs come before the clip players and Black.
                item.slot = -1
                self.inputs[source_id] = item
        self._renumber()
        for name in ("program_input_id", "preview_input_id", "last_live_input_id"):
            if getattr(self, name) not in self.inputs:
                setattr(self, name, None)
        for panel in self.panels:
            if not self._source_known(panel, panel.program_input_id):
                panel.program_input_id = None
            if not self._source_known(panel, panel.preview_input_id):
                panel.preview_input_id = None
        valid_slots = {item.id for item in self.stinger_slots}
        for item in self.inputs.values():
            if item.stinger_slot_id and item.stinger_slot_id not in valid_slots:
                item.stinger_slot_id = None

    def _sync_panels(self) -> None:
        desired = self.workspace.mixer_panel_count
        while len(self.panels) < desired:
            n = len(self.panels) + 1
            self.panels.append(
                MixerPanel(
                    id=f"me-{n}",
                    label=f"ME {n}",
                    program_input_id=self.program_input_id,
                    preview_input_id=self.preview_input_id,
                )
            )
        self.panels = self.panels[:desired]
        if self.panels:
            if self.program_input_id:
                self.panels[0].program_input_id = self.program_input_id
            if self.preview_input_id:
                self.panels[0].preview_input_id = self.preview_input_id

    def _labels_pinned(self) -> bool:
        pinned = self.settings.pinned_workspace
        return "keyers_per_me" in pinned or "dsks_per_me" in pinned

    def _sync_keyers(self) -> None:
        """keyers_per_me upstream keyers (`me-n-key-k`, "ME n Key k") and dsks_per_me DSKs on every
        rendering ME, in the engine's order. ME 1's DSKs keep P2's ids and labels (`dsk-k`,
        "DSK k"), the other MEs' are `me-n-dsk-k` ("ME n DSK k"). A keyer keeps its state by id."""
        previous = {keyer.id: keyer for keyer in self.keyers}
        keyers: list[Keyer] = []
        for n, panel in enumerate(self.rendered_panels(), 1):
            wanted = [(f"me-{n}-key-{k}", f"ME {n} Key {k}", KeyerStage.upstream) for k in range(1, self.workspace.keyers_per_me + 1)]
            wanted += [
                (f"dsk-{k}", f"DSK {k}", KeyerStage.downstream) if n == 1 else (f"me-{n}-dsk-{k}", f"ME {n} DSK {k}", KeyerStage.downstream)
                for k in range(1, self.workspace.dsks_per_me + 1)
            ]
            for keyer_id, label, stage in wanted:
                keyer = previous.get(keyer_id) or Keyer(id=keyer_id, label=label)
                keyer.panel_id, keyer.stage = panel.id, stage
                if self._labels_pinned():
                    keyer.label = label
                keyers.append(keyer)
        self.keyers = keyers
        for keyer_id in list(self._key_routes):
            if not any(keyer.id == keyer_id for keyer in self.keyers):
                del self._key_routes[keyer_id]
        # Keyer ties name the ME's upstream keyers (in their order); BKGD is on without a tie.
        for panel in self.panels:
            transition = panel.transition
            transition.ties = [k.id for k in keyers if k.panel_id == panel.id and k.stage == KeyerStage.upstream and k.id in transition.ties]
            transition.background = transition.background or not transition.ties

    def _sync_stinger_slots(self) -> None:
        previous = {slot.id: slot for slot in self.stinger_slots}
        default = self.settings.default_stinger
        default_info = inspect_stinger(self.settings.stingers_dir, default, fps=self.settings.fps)
        slots: list[StingerSlot] = []
        count = self.workspace.stinger_count

        def _make(slot_id: str, role: str, label: str) -> StingerSlot:
            if slot_id in previous:
                return previous[slot_id]
            slot = StingerSlot(id=slot_id, role=role, label=label, stinger_id=default)
            if default_info:
                slot.kind = default_info.kind
                slot.media_path = default_info.media_path
                slot.cut_ms = default_info.cut_ms
                slot.cut_frame = default_info.cut_frame
            return slot

        if self.workspace.stinger_mode == "separate":
            for n in range(1, count + 1):
                slots.append(_make(f"in-{n}", "in", f"Stinger IN {n}"))
                slots.append(_make(f"out-{n}", "out", f"Stinger OUT {n}"))
        else:
            for n in range(1, count + 1):
                slots.append(_make(f"shared-{n}", "shared", f"Stinger {n}"))
        self.stinger_slots = slots
        valid = {item.id for item in slots}
        for source in self.inputs.values():
            if source.stinger_slot_id and source.stinger_slot_id not in valid:
                source.stinger_slot_id = None

    def apply_workspace(self, payload: WorkspaceUpdate) -> WorkspaceConfig:
        patch = payload.model_dump(exclude_unset=True)
        for field, (value, variables) in self.settings.pinned_workspace.items():
            if field in patch and patch[field] != value:
                raise MixerError(f"{field} is set by {variables} (environment)")
        display_only = set(patch) <= {"source_tile_aspect"}
        if self.state == MixerState.running and not display_only:
            raise MixerError("stop the mixer before changing console layout")
        data = self.workspace.model_dump()
        if patch.get("stinger_mode") not in {None, "shared", "separate"}:
            raise MixerError("stinger_mode must be shared or separate")
        aspect = patch.get("source_tile_aspect")
        if aspect not in {None, "16:9", "9:16"}:
            raise MixerError("source_tile_aspect must be 16:9 or 9:16")
        data.update(patch)
        format_changed = data["format_id"] != self.workspace.format_id
        if format_changed:
            fmt = format_by_id(data["format_id"])
            # DME crops are source pixels: they keep their share of the picture in the new raster.
            self._scale_crops(fmt.width / self.settings.width, fmt.height / self.settings.height)
            self.settings.width = fmt.width
            self.settings.height = fmt.height
            self.settings.frame_rate_num = fmt.frame_rate_num
            self.settings.frame_rate_den = fmt.frame_rate_den
        self.workspace = WorkspaceConfig(**data)
        self._sync_sources()
        self._sync_panels()
        self._sync_keyers()
        self._sync_stinger_slots()
        if format_changed:
            # Off-air here; re-convert library items for the new raster/rate.
            self.library.set_format(self.workspace.format_id, fps=self.settings.fps)
        self._prune_audio()
        self.nmos.reconcile_inputs()
        self._publish_tally()
        self.refresh_library_bindings()
        return self.workspace

    def _scale_crops(self, sx: float, sy: float) -> None:
        """Every keyer's, keyframe's and snapshot's DME crop times sx, sy (a format change). A crop
        that has not been changed since an earlier format change is scaled from the value it had
        then (its base), so going back and forth between formats does not drift by rounding."""
        old = (self.settings.width, self.settings.height)
        new = (round(old[0] * sx), round(old[1] * sy))
        doc = {
            "keyers": {keyer.id: {"dme": keyer.dme.model_dump(), "keyframes": [frame.model_dump() for frame in keyer.keyframes]} for keyer in self.keyers},
            "snapshots": self.snapshots.model_dump(),
        }
        bases: dict[tuple, tuple[tuple[int, int], dict]] = {}
        for path, crop in crop_paths(doc):
            base = self._crop_base.get(path)
            if base is not None and _scaled_crop(base[1], base[0], old) == {side: crop.get(side) for side in _CROP_SIDES}:
                raster, value = base
            else:
                raster, value = old, {side: crop.get(side, 0) for side in _CROP_SIDES}
            crop.update(_scaled_crop(value, raster, new))
            bases[path] = (raster, value)
        self._crop_base = bases
        for keyer in self.keyers:
            entry = doc["keyers"][keyer.id]
            keyer.dme = Dme.model_validate(entry["dme"])
            keyer.keyframes = [DmeKeyframe.model_validate(frame) for frame in entry["keyframes"]]
        self.snapshots = type(self.snapshots).model_validate(doc["snapshots"])

    @property
    def render_count(self) -> int:
        """The first n panels render (FLOWXER_RENDER_MES, default every panel)."""
        if not self.panels:
            return 0
        wanted = self.settings.render_mes or len(self.panels)
        return max(1, min(wanted, len(self.panels)))

    def rendered_panels(self) -> list[MixerPanel]:
        return list(self.panels[: self.render_count])

    def _panel_number(self, panel: MixerPanel) -> int:
        return self.panels.index(panel) + 1

    def _rendered(self, panel: MixerPanel) -> bool:
        return panel in self.rendered_panels()

    def get_panel(self, panel_id: str) -> MixerPanel:
        for panel in self.panels:
            if panel.id == panel_id:
                return panel
        raise MixerError(f"unknown mixer panel {panel_id}")

    def _reentry_panel(self, source_id: str | None) -> MixerPanel | None:
        """The panel whose Program the source me-<n> re-enters, or None."""
        for panel in self.panels:
            if panel.id == source_id:
                return panel
        return None

    def _source_known(self, panel: MixerPanel, source_id: str | None) -> bool:
        if source_id is None:
            return True
        if source_id in self.inputs:
            return True
        reentered = self._reentry_panel(source_id)
        return reentered is not None and self._reentry_allowed(panel, reentered)

    def _reentry_allowed(self, panel: MixerPanel, reentered: MixerPanel) -> bool:
        """ME m takes the Program of ME n when n > m and ME n renders (0-frame re-entry)."""
        return (
            reentered in self.rendered_panels()
            and self._panel_number(reentered) > self._panel_number(panel)
        )

    def _check_source(self, panel: MixerPanel, source_id: str) -> None:
        if source_id in self.inputs:
            return
        reentered = self._reentry_panel(source_id)
        if reentered is None:
            raise MixerError(f"unknown input {source_id}")
        if not self._reentry_allowed(panel, reentered):
            raise MixerError(
                f"{panel.label} can take the Program of a higher rendering ME only (re-entry): not {reentered.label}"
            )

    def source_label(self, source_id: str | None) -> str:
        if source_id is None:
            return ""
        if source_id in self.inputs:
            return self.inputs[source_id].label
        panel = self._reentry_panel(source_id)
        return panel.label if panel else source_id

    def reentry_sources(self, panel: MixerPanel) -> list[MixerPanel]:
        """The rendering MEs whose Program this panel can take."""
        return [other for other in self.rendered_panels() if self._reentry_allowed(panel, other)]

    # ── keyers ───────────────────────────────────────────────────────────────

    def get_keyer(self, keyer_id: str) -> Keyer:
        for keyer in self.keyers:
            if keyer.id == keyer_id:
                return keyer
        raise MixerError(f"unknown keyer {keyer_id}")

    def _keyer_number(self, keyer: Keyer) -> int:
        return self.keyers.index(keyer) + 1

    def keyer_views(self) -> list[Keyer]:
        """The keyers as the engine renders them: while a run moves a box, its `dme` is where the
        engine's last status (once a second) had it; `run` is the engine's run (none off-air)."""
        entries = (self.engine_status() or {}).get("keyers") or []
        return [self._live_keyer(keyer, entries[n] if n < len(entries) else None) for n, keyer in enumerate(self.keyers)]

    def _live_keyer(self, keyer: Keyer, entry: Any) -> Keyer:
        run = entry.get("run") if isinstance(entry, dict) else None
        if not isinstance(run, dict):
            return keyer
        update: dict[str, Any] = {
            "run": KeyerRun(
                running=bool(run.get("running")),
                fly=bool(run.get("fly")),
                loop=run.get("loop") if run.get("loop") in {loop.value for loop in DmeLoop} else DmeLoop.off,
                keyframe=run.get("keyframe") if isinstance(run.get("keyframe"), int) else None,
                step=int(run.get("step") or 0),
            )
        }
        if run.get("running") and keyer.stage == KeyerStage.upstream and isinstance(entry.get("dme"), dict):
            update["dme"] = Dme.model_validate(_merged(keyer.dme.model_dump(), _box_state(self._from_engine_box(entry["dme"]))))
        return keyer.model_copy(update=update)

    def _flies(self, keyer: Keyer) -> bool:
        """Its Auto flies the box: fly, a box, at least 2 keyframes (else it fades)."""
        return (
            keyer.fly and keyer.stage == KeyerStage.upstream and (keyer.mode == KeyMode.pip or keyer.dme.enabled) and len(keyer.keyframes) >= 2
        )

    def _box_at(self, keyer: Keyer, keyframe: DmeKeyframe) -> Dme:
        """The keyer's box in the state of `keyframe` (enabled and the border colour stay)."""
        return Dme.model_validate(_merged(keyer.dme.model_dump(), _box_state(keyframe.model_dump())))

    def _box_from_engine(self, keyer: Keyer, reply: Any) -> None:
        """The box where the engine has it after a command (its reply), unless a run moves it."""
        if not isinstance(reply, dict) or not isinstance(reply.get("dme"), dict) or (reply.get("run") or {}).get("running"):
            return
        keyer.dme = Dme.model_validate(_merged(keyer.dme.model_dump(), _box_state(self._from_engine_box(reply["dme"]))))

    def _from_engine_box(self, box: dict[str, Any]) -> dict[str, Any]:
        return _box_from_engine_units(box, self.settings.width, self.settings.height)

    def _check_crop(self, crop: DmeCrop) -> None:
        error = _crop_error(crop, self.settings.width, self.settings.height)
        if error:
            raise MixerError(error)

    def _with_run(self, keyer: Keyer, reply: Any) -> Keyer:
        return self._live_keyer(keyer, reply)

    def update_keyer(self, keyer_id: str, **changes: Any) -> Keyer:
        """On or off (at the next frame), rate, label, and the key: mode, source (luma, pip),
        clip, soft, gain, invert, fly, and the box (`dme`, merged into the current one; a change
        of its position, size, crop, border or shadow stops a run there). A keyer that flies cuts
        on at its last keyframe and off at keyframe 0."""
        keyer = self.get_keyer(keyer_id)
        label = changes.pop("label", None)
        enabled = changes.pop("enabled", None)
        box_patch = changes.get("dme")
        if label is not None and label != keyer.label:
            if self._labels_pinned():
                raise MixerError("keyer labels are fixed while FLOWXER_KEYERS_PER_ME or FLOWXER_DSKS_PER_ME sets the keyers")
            keyer.label = label
            # Its key receiver carries the label.
            self.nmos.mark_changed()
        key = {name: value for name, value in changes.items() if name != "rate_ms" and value is not None}
        if "dme" in key:
            key["dme"] = _merged(keyer.dme.model_dump(), key["dme"])
        if key:
            try:
                updated = Keyer.model_validate({**keyer.model_dump(), **key})
            except ValidationError as exc:
                raise MixerError(f"invalid key: {exc.errors()[0]['msg']}") from exc
            if updated.mode in (KeyMode.luma, KeyMode.pip):
                if not updated.source_id:
                    raise MixerError(f"a {updated.mode.value} key needs a source")
                self._check_source(self.get_panel(keyer.panel_id), updated.source_id)
            if (updated.mode == KeyMode.pip or updated.dme.enabled) and updated.stage != KeyerStage.upstream:
                raise MixerError("pip and dme only on upstream keyers")
            if updated.mode == KeyMode.pip and isinstance(box_patch, dict) and box_patch.get("enabled") is False:
                raise MixerError("a pip keyer is always a box (dme.enabled stays true)")
            self._check_crop(updated.dme.crop)
            for name in key:
                setattr(keyer, name, getattr(updated, name))
            # A pip is always a box (the engine does the same).
            keyer.dme.enabled = updated.dme.enabled
            reply = self._push_key_settings(keyer, box=box_patch if "dme" in key else None)
            self._box_from_engine(keyer, reply)
        if changes.get("rate_ms") is not None:
            keyer.rate_ms = changes["rate_ms"]
        if enabled is not None:
            keyer.enabled = enabled
            self._key_until.pop(keyer.id, None)
            self._key_in.pop(keyer.id, None)
            if self._flies(keyer):
                keyer.dme = self._box_at(keyer, keyer.keyframes[-1 if enabled else 0])
            self._engine_call("POST", f"/keyer/{self._keyer_number(keyer)}", {"on": enabled})
        self._publish_tally()
        return keyer

    def replace_keyer_box(self, keyer_id: str, box: Dme) -> Keyer:
        """The whole box (PUT /keyers/{id}/dme; a pip's box stays enabled). Stops a run."""
        keyer = self.get_keyer(keyer_id)
        if keyer.stage != KeyerStage.upstream:
            raise MixerError("pip and dme only on upstream keyers")
        self._check_crop(box.crop)
        keyer.dme = box.model_copy(deep=True)
        if keyer.mode == KeyMode.pip:
            keyer.dme.enabled = True
        self._box_from_engine(keyer, self._push_key_settings(keyer, box=keyer.dme.model_dump()))
        return keyer

    def set_keyframes(self, keyer_id: str, items: list[dict[str, Any]]) -> Keyer:
        """Replaces the keyer's keyframes (up to 8, upstream keyers): each one's missing box fields
        take the keyer's box. A run stops where the box is."""
        keyer = self.get_keyer(keyer_id)
        if keyer.stage != KeyerStage.upstream:
            raise MixerError("keyframes only on upstream keyers")
        if len(items) > 8:
            raise MixerError("at most 8 keyframes")
        state = _box_state(keyer.dme.model_dump())
        try:
            keyframes = [DmeKeyframe.model_validate(_merged(state, item)) for item in items]
        except ValidationError as exc:
            raise MixerError(f"invalid keyframe: {exc.errors()[0]['msg']}") from exc
        for keyframe in keyframes:
            self._check_crop(keyframe.crop)
        keyer.keyframes = keyframes
        self._box_from_engine(keyer, self._push_keyframes(keyer))
        return keyer

    def run_keyer(
        self, keyer_id: str, *, to: int | None = None, reverse: bool = False, loop: DmeLoop = DmeLoop.off, frames: int | None = None
    ) -> Keyer:
        """A run of the box from where it is to keyframe `to` (the last; 0 with reverse), a loop,
        or (frames 0) a jump. A run that ends leaves the box at its keyframe."""
        keyer = self.get_keyer(keyer_id)
        if keyer.stage != KeyerStage.upstream or not (keyer.mode == KeyMode.pip or keyer.dme.enabled):
            raise MixerError("the keyer has no box (dme)")
        count = len(keyer.keyframes)
        target = to if to is not None else (0 if reverse else count - 1)
        if count == 0:
            raise MixerError("the keyer has no keyframes")
        if target >= count:
            raise MixerError(f"no keyframe {target} (the keyer has {count})")
        if loop != DmeLoop.off and count < 2:
            raise MixerError("a loop needs 2 keyframes")
        body: dict[str, Any] = {"reverse": reverse, "loop": loop.value}
        if to is not None:
            body["to"] = to
        if frames is not None:
            body["frames"] = frames
        reply = self._engine_call("POST", f"/keyer/{self._keyer_number(keyer)}/run", body)
        if loop == DmeLoop.off:
            keyer.dme = self._box_at(keyer, keyer.keyframes[target])
        return self._with_run(keyer, reply)

    def stop_keyer(self, keyer_id: str) -> Keyer:
        """Stops a run: the box holds where it is (a keyer flying off stays on air)."""
        keyer = self.get_keyer(keyer_id)
        reply = self._engine_call("POST", f"/keyer/{self._keyer_number(keyer)}/stop", {})
        self._box_from_engine(keyer, reply)
        if isinstance(reply, dict) and reply.get("on") is True and not keyer.enabled:
            keyer.enabled = True
            self._key_until.pop(keyer.id, None)
            self._key_in.pop(keyer.id, None)
            self._publish_tally()
        return self._with_run(keyer, reply)

    def dme_presets(self) -> list[DmePresetInfo]:
        return list(DME_PRESETS)

    def apply_preset(self, keyer_id: str, name: DmePreset) -> list[Keyer]:
        """Writes a preset's box and keyframes (0 off screen, 1 on) into the keyer (split: the ME's
        keyers 1 and 2, quad: 1 to 4), each with its own rate and ease. A keyer on air flies to
        keyframe 1 from where its box is; one off air is there at once."""
        keyer = self.get_keyer(keyer_id)
        if keyer.stage != KeyerStage.upstream:
            raise MixerError("presets only on upstream keyers")
        boxes = _preset_boxes(name, self.settings.width, self.settings.height, keyer.dme.model_dump())
        upstream = [k for k in self.keyers if k.panel_id == keyer.panel_id and k.stage == KeyerStage.upstream]
        if len(boxes) > 1 and len(upstream) < len(boxes):
            panel = self.get_panel(keyer.panel_id)
            raise MixerError(f"{name.value} needs {len(boxes)} upstream keyers ({panel.label} has {len(upstream)})")
        targets = upstream[: len(boxes)] if len(boxes) > 1 else [keyer]
        for target, (on, off) in zip(targets, boxes):
            frames = _frames(target.rate_ms, self.settings.fps)
            on_box = Dme.model_validate(_merged(target.dme.model_dump(), {**on, "enabled": True}))
            target.keyframes = [
                DmeKeyframe.model_validate({**_box_state(_merged(on_box.model_dump(), off)), "frames": frames, "ease": "ease"}),
                DmeKeyframe.model_validate({**_box_state(on_box.model_dump()), "frames": frames, "ease": "ease"}),
            ]
            self._push_keyframes(target)
            if target.enabled:
                # On air: the box flies there (enabled and the colour change at once).
                patch = {"enabled": True, "border": {"color": on_box.border.color}}
                target.dme = Dme.model_validate(_merged(target.dme.model_dump(), patch))
                self._push_key_settings(target, box=patch)
                self._engine_call("POST", f"/keyer/{self._keyer_number(target)}/run", {"to": 1, "reverse": False, "loop": "off"})
                target.dme = on_box
            else:
                target.dme = on_box
                self._box_from_engine(target, self._push_key_settings(target, box=on_box.model_dump()))
        return targets

    def keyer_auto(self, keyer_id: str, duration_ms: int | None = None) -> Keyer:
        """Fade the keyer on (or off when it is on) over duration_ms (default: its rate); a keyer
        that flies runs its box from keyframe 0 to the last (on) or back to 0 (off) instead."""
        keyer = self.get_keyer(keyer_id)
        keyer.enabled = not keyer.enabled
        frames = _frames(duration_ms or keyer.rate_ms, self.settings.fps)
        span = frames
        if self._flies(keyer):
            keyer.dme = self._box_at(keyer, keyer.keyframes[-1 if keyer.enabled else 0])
            span = sum(item.frames for item in keyer.keyframes[1:])
        if keyer.enabled:
            self._key_until.pop(keyer.id, None)
            # Fading (or flying) in: at full level when it ends.
            self._key_in[keyer.id] = time.monotonic() + span / self.settings.fps
            self._tally_again_at(self._key_in[keyer.id])
        else:
            self._key_in.pop(keyer.id, None)
            # The raw tally keeps it on air until the fade or the fly has ended.
            self._key_until[keyer.id] = time.monotonic() + span / self.settings.fps
            self._tally_again_at(self._key_until[keyer.id])
        self._engine_call("POST", f"/keyer/{self._keyer_number(keyer)}/auto", {"frames": frames})
        self._publish_tally()
        return keyer

    def _push_key_settings(self, keyer: Keyer, client: EngineClient | None = None, box: dict[str, Any] | None = None) -> Any:
        """Mode, source, clip, soft, gain, invert, fly; `box`: the box fields to change (the whole
        box when the engine starts). Returns the engine's reply (the keyer), or None."""
        body: dict[str, Any] = {
            "mode": keyer.mode.value, "clip": keyer.clip, "soft": keyer.soft, "gain": keyer.gain, "invert": keyer.invert, "fly": keyer.fly,
        }
        if keyer.mode in (KeyMode.luma, KeyMode.pip):
            body["source"] = self._engine_source(keyer.source_id)
        if box is not None and keyer.stage == KeyerStage.upstream:
            body["dme"] = _engine_box(box, self.settings.width, self.settings.height)
        return self._engine_call("POST", f"/keyer/{self._keyer_number(keyer)}/settings", body, client=client)

    def _push_keyframes(self, keyer: Keyer, client: EngineClient | None = None) -> Any:
        if keyer.stage != KeyerStage.upstream:
            return None
        body = {
            "keyframes": [_engine_box(item.model_dump(mode="json"), self.settings.width, self.settings.height) for item in keyer.keyframes]
        }
        return self._engine_call("PUT", f"/keyer/{self._keyer_number(keyer)}/keyframes", body, client=client)

    def apply_key_route(self, keyer_id: str, *, domain_id: str | None, flow_id: str | None, enabled: bool) -> None:
        """IS-05 activation of a keyer's key receiver."""
        keyer = self.get_keyer(keyer_id)
        if flow_id:
            try:
                UUID(str(flow_id))
            except ValueError as exc:
                raise MixerError(f"invalid flow id {flow_id}") from exc
        self._key_routes[keyer.id] = (domain_id or self.settings.resolved_output_domain_id, flow_id if enabled else None)
        self._push_key(keyer)

    def _key_route(self, keyer: Keyer) -> dict[str, str]:
        domain_id, flow_id = self._key_routes.get(keyer.id, (None, None))
        if not flow_id:
            return {"domain": "", "flow": ""}
        path = self._mxl_source_path(keyer.id, "key", domain_id, flow_id)
        return {"domain": path or "", "flow": flow_id if path else ""}

    def _push_key(self, keyer: Keyer, client: EngineClient | None = None) -> None:
        self._engine_call("PUT", f"/key/{self._keyer_number(keyer)}", self._key_route(keyer), client=client)

    # ── frame grab ───────────────────────────────────────────────────────────

    def _grab_source(self, source: str) -> tuple[str, str, str]:
        """(engine source, bus, label) of a grab source: a live input or clip player id, a panel id
        (its Program, also <panel>:pgm) or <panel>:pvw (its Preview)."""
        item = self.inputs.get(source)
        if item is not None:
            if item.kind == InputKind.black:
                raise MixerError("Black has no picture to grab")
            return self._engine_source(source), "pgm", item.label
        panel_id, _, bus = source.partition(":")
        panel = self._reentry_panel(panel_id)
        if panel is None or bus not in {"", "pgm", "pvw"}:
            raise MixerError(f"unknown grab source {source}: an input, a clip player, a panel (its Program) or <panel>:pvw")
        if not self._rendered(panel):
            raise MixerError(f"{panel.label} does not render (FLOWXER_RENDER_MES)")
        bus = bus or "pgm"
        return f"me{self._panel_number(panel)}", bus, f"{panel.label} {bus.upper()}"

    def grab(self, source: str, *, player: str | None = None, name: str | None = None) -> LibraryItem:
        """A frame grab (docs/engine-p4.md §6): the picture `source` has in the next output grain
        (a live input's grain i - L as the MEs use it, a clip player's picture, an ME's Program or
        Preview), copied by the engine without a stall, as a library still (kind still, source
        grab, tag grab) in the production raster with no conversion. Named `name`, else
        "Grab <source> <YYYY-MM-DD HH:MM:SS>" (local time). `player`: a clip player that loads the
        still once it is there (the frame memory)."""
        engine_source, bus, label = self._grab_source(source)
        target = self.get_input(player) if player is not None else None
        if target is not None and target.kind != InputKind.clip_player:
            raise MixerError(f"input {player} is not a clip player")
        if self.engine is None or not self.engine.ready:
            raise MixerError("the mixer is off-air: there is nothing to grab")
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        fmt = self.workspace.format_id
        item_id, path = self.library.grab_target(fmt)
        try:
            grab = self.engine.call("POST", "/grab", {"source": engine_source, "bus": bus, "path": str(path)}) or {}
            deadline = time.monotonic() + GRAB_TIMEOUT_S
            while grab.get("state") not in {"done", "failed"} and time.monotonic() < deadline:
                time.sleep(0.01)
                grab = self.engine.call("GET", f"/grab/{grab.get('grab')}") or {}
        except EngineError as exc:
            self.library.drop_grab(item_id)
            raise MixerError(str(exc)) from exc
        if grab.get("state") != "done" or not path.is_file():
            self.library.drop_grab(item_id)
            raise MixerError(f"the grab of {label} failed: {grab.get('error') or 'no picture in time'}")
        info = {"source": source, "label": label, "index": grab.get("index"), "tai": grab.get("tai"), "source_index": grab.get("source_index")}
        item = self.library.add_grab(item_id, fmt, name=name or f"Grab {label} {stamp}", grab=info)
        log.info("grab of %s (grain %s) as library still %s", label, grab.get("index"), item.id)
        if target is not None:
            self.update_input(target.id, LogicalInputUpdate(library_item_id=item.id))
        return item

    # ── stingers (slots and playback) ────────────────────────────────────────

    def assign_stinger_slot(self, slot_id: str, stinger_id: str) -> StingerSlot:
        return self.configure_stinger_slot(slot_id, StingerSlotUpdate(stinger_id=stinger_id))

    def get_stinger_slot(self, slot_id: str) -> StingerSlot:
        for item in self.stinger_slots:
            if item.id == slot_id:
                return item
        raise MixerError(f"unknown stinger slot {slot_id}")

    def configure_stinger_slot(self, slot_id: str, payload: StingerSlotUpdate) -> StingerSlot:
        slot = self._configure_stinger_slot(slot_id, payload)
        self._cue_stingers()
        return slot

    def _configure_stinger_slot(self, slot_id: str, payload: StingerSlotUpdate) -> StingerSlot:
        slot = self.get_stinger_slot(slot_id)
        kind = payload.kind or slot.kind or "sequence"
        if kind not in {"sequence", "video"}:
            raise MixerError("stinger kind must be sequence or video")
        if payload.label is not None:
            slot.label = payload.label
        slot.kind = kind

        library_item_id = (
            payload.library_item_id if "library_item_id" in payload.model_fields_set else slot.library_item_id
        )
        if payload.stinger_id and "library_item_id" not in payload.model_fields_set:
            # A stinger id names a legacy stinger; one that only the library has binds the item.
            legacy = inspect_stinger(self.settings.stingers_dir, payload.stinger_id, fps=self.settings.fps)
            found = None if legacy and legacy.frame_count else self._find_library_stinger(payload.stinger_id)
            library_item_id = found.id if found else None
        elif payload.media_path:
            library_item_id = None
        if library_item_id:
            item = self.library.get(library_item_id)
            if item is None or item.kind != LibraryKind.stinger:
                raise MixerError(f"unknown library stinger {library_item_id}")
            if payload.cut_frame is not None or payload.cut_ms is not None:
                # The cut belongs to the item (like stinger.json for legacy stingers).
                self.library.patch(item.id, cut_frame=payload.cut_frame, cut_ms=payload.cut_ms)
            slot.library_item_id = item.id
            self._bind_library_slot(slot)
            return slot
        if slot.library_item_id:
            slot.library_item_id = None
            slot.ready = True
            if not payload.stinger_id and not payload.media_path:
                # Unbound from the library without new media: back to the default stinger.
                slot.stinger_id = self.settings.default_stinger
                slot.media_path = None
                slot.cut_ms = slot.cut_frame = None
                kind = slot.kind = "sequence"

        if kind == "video" and payload.media_path:
            video = Path(payload.media_path)
            try:
                if video.is_absolute():
                    video = contained_path(video, self.settings.clips_dir, self.settings.stingers_dir)
                else:
                    clip = self.settings.clips_dir / video.name
                    sting = self.settings.stingers_dir / video.name
                    if clip.exists():
                        video = clip.resolve()
                    elif sting.exists():
                        video = sting.resolve()
                    else:
                        raise MixerError(f"video not found: {payload.media_path}")
                    contained_path(video, self.settings.clips_dir, self.settings.stingers_dir)
            except SecurityError as exc:
                raise MixerError(str(exc)) from exc
            stinger_id = payload.stinger_id or slot.stinger_id or video.stem
            if stinger_id == self.settings.default_stinger:
                stinger_id = video.stem
            info = register_video_stinger(
                self.settings.stingers_dir,
                stinger_id,
                video,
                fps=self.settings.fps,
                cut_ms=payload.cut_ms,
                duration_ms=payload.duration_ms,
                width=self.settings.width,
                height=self.settings.height,
            )
            slot.stinger_id = info.id
            slot.media_path = info.media_path
            slot.cut_ms = info.cut_ms
            slot.cut_frame = info.cut_frame
            slot.kind = "video"
            if payload.cut_frame is not None or payload.cut_ms is not None:
                info = update_stinger_cut(
                    self.settings.stingers_dir,
                    slot.stinger_id,
                    fps=self.settings.fps,
                    cut_ms=payload.cut_ms,
                    cut_frame=payload.cut_frame,
                )
                slot.cut_ms = info.cut_ms
                slot.cut_frame = info.cut_frame
            return slot

        if payload.stinger_id:
            info = self.get_stinger(payload.stinger_id)
            slot.stinger_id = info.id
            slot.media_path = info.media_path
            if payload.cut_ms is None and payload.cut_frame is None:
                slot.cut_ms = info.cut_ms
                slot.cut_frame = info.cut_frame
                slot.kind = info.kind
        else:
            info = self.get_stinger(slot.stinger_id)

        if payload.cut_ms is not None or payload.cut_frame is not None:
            info = update_stinger_cut(
                self.settings.stingers_dir,
                slot.stinger_id,
                fps=self.settings.fps,
                cut_ms=payload.cut_ms,
                cut_frame=payload.cut_frame,
            )
            slot.cut_ms = info.cut_ms
            slot.cut_frame = info.cut_frame
        elif slot.cut_ms is None:
            slot.cut_ms = info.cut_ms
            slot.cut_frame = info.cut_frame
        if payload.media_path and kind == "sequence":
            slot.media_path = info.media_path
        return slot

    # ── inputs ───────────────────────────────────────────────────────────────

    def register_input(self, payload: LogicalInputCreate) -> LogicalInput:
        if pinned := self._inputs_pinned_by():
            raise MixerError(f"the input list is set by {pinned} (environment)")
        if self.state == MixerState.running:
            raise MixerError("stop the mixer before adding inputs")
        if payload.id in self.inputs or self._reentry_panel(payload.id) is not None:
            raise MixerError(f"input {payload.id} already exists")
        if payload.library_item_id:
            self._check_player_media(payload.library_item_id)
        logical = LogicalInput(slot=len(self.inputs), **payload.model_dump())
        self._apply_default_domain(logical)
        self.inputs[logical.id] = logical
        self._renumber()
        self._prune_audio()
        self.nmos.sync_from_rest(logical.id)
        self._publish_tally()
        return logical

    def update_input(self, input_id: str, payload: LogicalInputUpdate) -> LogicalInput:
        current = self.get_input(input_id)
        patch = payload.model_dump(exclude_unset=True)
        pinned = self._inputs_pinned_by()
        if pinned and any(
            field in patch and patch[field] != getattr(current, field)
            for field in ("label", "kind")
        ):
            raise MixerError(f"label and kind of {input_id} are set by {pinned} (environment)")
        if self.state == MixerState.running and "kind" in patch and patch["kind"] != current.kind:
            raise MixerError("stop the mixer before changing the kind of an input")
        if "stinger_slot_id" in patch:
            slot_id = patch["stinger_slot_id"] or None
            if slot_id:
                self.get_stinger_slot(slot_id)
            patch["stinger_slot_id"] = slot_id
        if patch.get("library_item_id"):
            self._check_player_media(patch["library_item_id"])
        data = current.model_dump()
        data.update(patch)
        updated = LogicalInput(**data)
        self._apply_default_domain(updated)
        self.inputs[input_id] = updated
        self._renumber()
        self._prune_audio()
        self.nmos.sync_from_rest(input_id)
        if updated.kind == InputKind.mxl_live and (
            "video" in patch or "audio" in patch or "kind" in patch
        ):
            self._push_route(updated)
        if updated.kind == InputKind.clip_player and (
            {"library_item_id", "loop", "play_on_cut", "kind"} & set(patch)
        ):
            self._push_player(updated)
        self._publish_tally()
        if "stinger_slot_id" in patch:
            self._cue_stingers()
        return updated

    def delete_input(self, input_id: str) -> None:
        if pinned := self._inputs_pinned_by():
            raise MixerError(f"the input list is set by {pinned} (environment)")
        if self.state == MixerState.running:
            raise MixerError("stop the mixer before removing inputs")
        if input_id not in self.inputs:
            raise MixerError(f"unknown input {input_id}")
        del self.inputs[input_id]
        self._renumber()
        self._prune_audio()
        self.nmos.reconcile_inputs()
        self._publish_tally()

    def get_input(self, input_id: str) -> LogicalInput:
        try:
            return self.inputs[input_id]
        except KeyError as exc:
            raise MixerError(f"unknown input {input_id}") from exc

    def list_inputs(self) -> list[LogicalInput]:
        return sorted(self.inputs.values(), key=lambda item: item.slot)

    def _apply_default_domain(self, item: LogicalInput) -> None:
        if item.kind != InputKind.mxl_live:
            return
        default = self.settings.resolved_output_domain_id
        if item.video is not None and not item.video.domain_id:
            item.video = item.video.model_copy(update={"domain_id": default})
        if item.audio is not None and not item.audio.domain_id:
            item.audio = item.audio.model_copy(update={"domain_id": default})

    def apply_nmos_receiver(
        self,
        input_id: str,
        role: str,
        domain_id: str | None,
        flow_id: str | None,
        enabled: bool,
    ) -> None:
        """IS-05 activation → logical input essence → the engine opens (or closes) the reader."""
        item = self.get_input(input_id)
        default_domain = self.settings.resolved_output_domain_id
        resolved_domain = domain_id or default_domain
        parsed_flow: UUID | None = None
        if flow_id:
            try:
                parsed_flow = UUID(str(flow_id))
            except ValueError as exc:
                raise MixerError(f"invalid flow id {flow_id}") from exc
        current = item.video if role == "video" else item.audio
        default_type = self.settings.video_media_type if role == "video" else self.settings.audio_media_type
        data = current.model_dump() if current else {"media_type": default_type}
        data["flow_id"] = parsed_flow if enabled else None
        if not enabled:
            # master_enable false: nothing is read (the IS-05 active parameters keep the flow).
            data["flow_id"] = None
        data["domain_id"] = resolved_domain
        if role == "video":
            item.video = VideoEssence(**data)
        else:
            item.audio = AudioEssence(**data)
        self._push_route(item)

    # ── routes to the engine ─────────────────────────────────────────────────

    def _engine_source(self, source_id: str | None) -> str:
        """FlowXer source id → engine source: in<k> (live), cp<k> (clip player), black, me<n>."""
        if source_id is None:
            return "black"
        item = self.inputs.get(source_id)
        if item is not None:
            if item.kind == InputKind.black:
                return "black"
            same = [other for other in self.list_inputs() if other.kind == item.kind]
            k = same.index(item) + 1
            return f"in{k}" if item.kind == InputKind.mxl_live else f"cp{k}"
        panel = self._reentry_panel(source_id)
        if panel is not None:
            return f"me{self._panel_number(panel)}"
        return "black"

    def _engine_index(self, item: LogicalInput) -> int:
        return [other.id for other in self.list_inputs() if other.kind == item.kind].index(item.id) + 1

    def _essence_route(self, item: LogicalInput, role: str) -> dict[str, str]:
        essence = item.video if role == "video" else item.audio
        if essence is None or not essence.flow_id:
            self._missing_domains.pop((item.id, role), None)
            return {"domain": "", "flow": ""}
        flow_id = str(essence.flow_id)
        path = self._mxl_source_path(item.id, role, essence.domain_id, flow_id)
        return {"domain": path or "", "flow": flow_id if path else ""}

    def _push_route(self, item: LogicalInput, client: EngineClient | None = None) -> None:
        if item.kind != InputKind.mxl_live:
            return
        body = {"video": self._essence_route(item, "video"), "audio": self._essence_route(item, "audio")}
        log.info(
            "input %s: video %s, audio %s",
            item.id,
            body["video"]["flow"] or "not routed",
            body["audio"]["flow"] or "not routed",
        )
        self._engine_call("PUT", f"/input/{self._engine_index(item)}", body, client=client)

    def _mxl_source_path(self, input_id: str, role: str, domain_id: str | None, flow_id: str) -> str | None:
        """Domain directory for a routed essence. A route that names no domain or the wrong one
        (REST and IS-05 default it to the own output domain) still finds its flow: the named
        domain when it holds the flow, else any domain below the root that does (local before
        mirror), else the named domain, where the flow may still appear. None while the named
        domain does not exist (a fabrics mirror after a node reboot): _watch_domains routes it
        once the domain appears."""
        self._missing_domains.pop((input_id, role), None)
        root = self.settings.mxl_root
        resolved = resolve_domain_path(root, str(domain_id)) if domain_id else None
        if flow_id and (resolved is None or not (resolved / f"{flow_id}.mxl-flow").is_dir()):
            holder = find_flow_domain(root, flow_id)
            if holder is not None:
                if domain_id and resolved != holder:
                    log.info("MXL flow %s for %s %s is in %s, not in domain %s", flow_id, input_id, role, holder, domain_id)
                return str(holder)
        if not domain_id:
            return None
        if resolved is None:
            log.warning(
                "MXL domain id %s for %s %s not found under %s; it waits for it (black and silence)",
                domain_id,
                input_id,
                role,
                root,
            )
            self._missing_domains[(input_id, role)] = str(domain_id)
            return None
        return str(resolved)

    def _watch_domains(self, runtime: EngineRuntime) -> None:
        """While `runtime` runs Program: routes that wait for a missing MXL domain are applied once
        the domain (or the flow in another domain) appears."""
        while self.engine is runtime:
            time.sleep(DOMAIN_WATCH_S)
            if self.engine is runtime and self._missing_domains:
                self._attach_waiting_sources()

    def _waiting_flow(self, owner: str, role: str) -> str | None:
        if role == "key":
            return self._key_routes.get(owner, (None, None))[1]
        item = self.inputs.get(owner)
        essence = getattr(item, role, None) if item is not None and item.kind == InputKind.mxl_live else None
        return str(essence.flow_id) if essence is not None and essence.flow_id else None

    def _attach_waiting_sources(self) -> None:
        root = self.settings.mxl_root
        for (owner, role), domain_id in list(self._missing_domains.items()):
            # IS-05 activations change the route under this lock: check and route under it too.
            with self.nmos.lock:
                flow = self._waiting_flow(owner, role)
                if flow is None:
                    # Routed elsewhere or gone.
                    self._missing_domains.pop((owner, role), None)
                    continue
                if resolve_domain_path(root, domain_id) is None and find_flow_domain(root, flow) is None:
                    continue
                log.info("MXL domain %s appeared: %s (%s) reads %s", domain_id, owner, role, flow)
                keyer = next((k for k in self.keyers if k.id == owner), None)
                if role == "key" and keyer is not None:
                    self._push_key(keyer)
                elif owner in self.inputs:
                    self._push_route(self.inputs[owner])

    # ── clip players ─────────────────────────────────────────────────────────

    def _check_player_media(self, library_item_id: str) -> LibraryItem:
        item = self.library.get(library_item_id)
        if item is None or item.kind not in {LibraryKind.clip, LibraryKind.still}:
            raise MixerError(f"unknown library clip or still {library_item_id}")
        return item

    def _player_media(self, item: LogicalInput) -> dict[str, Any]:
        """What the engine loads for a clip player: its library item's files for the current
        format (empty while it converts or without an item: the player shows black). A clip:
        its HEVC 4:4:4 mezzanine, its sound file and frame count; a still: its v210 picture."""
        lib = self.library.get(item.library_item_id or "")
        if lib is None:
            return {"path": "", "kind": "", "loop": item.loop, "play_on_cut": item.play_on_cut}
        fmt = self.workspace.format_id
        path = self.library.mezzanine_path(lib.id, fmt)
        if lib.kind == LibraryKind.still:
            return {"path": str(path) if path else "", "kind": "still", "loop": item.loop, "play_on_cut": item.play_on_cut}
        conv = lib.conversion_for(fmt)
        audio = self.library.audio_path(lib.id, fmt)
        return {
            "path": str(path) if path else "",
            "kind": "clip",
            "audio": str(audio) if audio else "",
            "channels": conv.audio_channels,
            "frames": conv.frames,
            "loop": item.loop,
            "play_on_cut": item.play_on_cut,
        }

    def _push_player(self, item: LogicalInput, client: EngineClient | None = None) -> None:
        if item.kind != InputKind.clip_player:
            return
        self._engine_call("POST", f"/player/{self._engine_index(item)}/load", self._player_media(item), client=client)

    def player_command(self, input_id: str, action: str, frame: int = 0) -> LogicalInput:
        """Clip player transport: cue (a frame, 0 = the first), play, pause, stop (back to the
        first frame and hold)."""
        item = self.get_input(input_id)
        if item.kind != InputKind.clip_player:
            raise MixerError(f"input {input_id} is not a clip player")
        if action not in {"cue", "play", "pause", "stop"}:
            raise MixerError(f"unknown player action {action}")
        if self.engine is None:
            if self.settings.simulate and self.state == MixerState.running:
                return item
            raise MixerError("the mixer is off-air")
        body = {"frame": frame} if action == "cue" else {}
        # A stop, pause or cue before the end cancels its end action (a play times it again).
        self._cancel_clip_end(player_id=input_id)
        try:
            self.engine.call("POST", f"/player/{self._engine_index(item)}/{action}", body)
        except EngineError as exc:
            raise MixerError(str(exc)) from exc
        return item

    # ── library ──────────────────────────────────────────────────────────────

    def _library_consumers(self, item_id: str) -> list[str]:
        """Inputs and stinger slots that use a library item: derived from the mixer state,
        so it holds after a restart or a config import."""
        users = [f"input:{i.id}" for i in list(self.inputs.values()) if i.library_item_id == item_id]
        users += [f"slot:{s.id}" for s in list(self.stinger_slots) if s.library_item_id == item_id]
        return users

    def _bind_library_slot(self, slot: StingerSlot) -> StingerInfo | None:
        """Point a slot at its library item as it is now. None when the item is gone."""
        item = self.library.get(slot.library_item_id or "")
        if item is None or item.kind != LibraryKind.stinger:
            slot.ready = False
            return None
        info = self._stinger_info_from_library(item)
        slot.stinger_id = item.id
        slot.kind = info.kind
        slot.media_path = info.media_path or None
        slot.cut_frame = info.cut_frame
        slot.cut_ms = info.cut_ms
        slot.ready = bool(info.media_path)
        return info

    def refresh_library_bindings(self) -> None:
        """Clip players and stinger slots follow their library items: a conversion that ends (or
        a format change) gives the player its media."""
        with self.nmos.lock:
            players = [item for item in self.inputs.values() if item.kind == InputKind.clip_player and item.library_item_id]
            for slot in list(self.stinger_slots):
                if slot.library_item_id:
                    self._bind_library_slot(slot)
        if self.engine is not None:
            for item in players:
                self._push_player(item)
            self._cue_stingers()

    def _on_library_job(self, job) -> None:
        if job.state in {"done", "failed", "cancelled"} and job.format_id == self.workspace.format_id:
            self.refresh_library_bindings()

    def library_item_out(self, item: LibraryItem):
        from flowxer.api.schemas import LibraryItemOut

        fmt = self.workspace.format_id
        conv = item.conversion_for(fmt)
        thumb = self.library.store.thumb_path(item.id)
        return LibraryItemOut(
            id=item.id,
            kind=item.kind.value,
            name=item.name,
            tags=list(item.tags),
            status=conv.status.value,
            ready=item.is_ready(fmt),
            playback=conv.playback,
            has_alpha=item.has_alpha or conv.has_alpha,
            has_sound=bool(conv.audio),
            cut_frame=item.cut_frame if item.cut_frame is not None else conv.cut_frame,
            cut_ms=item.cut_ms if item.cut_ms is not None else conv.cut_ms,
            frame_count=conv.frames or int(item.original.get("frame_count") or 0),
            duration_s=conv.duration_s,
            thumb_url=f"/api/v1/library/{item.id}/thumb.jpg" if thumb.is_file() else None,
            error=conv.error,
            source=item.source,
            legacy_path=item.legacy_path,
            in_use=self.library.is_in_use(item.id),
        )

    def _stinger_info_from_library(self, item: LibraryItem) -> StingerInfo:
        """A library stinger plays its mezzanine; media_path is empty until that is ready."""
        fmt = self.workspace.format_id
        conv = item.conversion_for(fmt)
        mezz = self.library.mezzanine_path(item.id, fmt)
        frames = conv.frames or int(item.original.get("frame_count") or 0)
        fps = self.settings.fps
        if item.cut_frame is not None:
            cut_frame = item.cut_frame
        else:
            cut_frame = conv.cut_frame if conv.cut_frame is not None else frames // 2
        cut_ms = item.cut_ms if item.cut_ms is not None else (
            conv.cut_ms if conv.cut_ms is not None else cut_ms_from_frame(cut_frame, fps)
        )
        return StingerInfo(
            id=item.id,
            path=str(mezz.parent if mezz else self.library.store.item_dir(item.id)),
            frame_count=max(frames, 0),
            cut_frame=cut_frame or 0,
            pattern=str(item.original.get("pattern") or "frame_%05d.tga"),
            width=int(item.original.get("width") or self.settings.width),
            height=int(item.original.get("height") or self.settings.height),
            has_alpha=bool(item.has_alpha or conv.has_alpha),
            kind="video",
            media_path=str(mezz) if mezz else "",
            cut_ms=cut_ms or 0,
            duration_ms=duration_ms_from_frames(frames, fps) if frames else 0,
            fps=fps,
        )

    def _find_library_stinger(self, stinger_id: str) -> LibraryItem | None:
        direct = self.library.get(stinger_id)
        if direct and direct.kind == LibraryKind.stinger:
            return direct
        for item in self.library.list_items(kind=LibraryKind.stinger):
            if item.name == stinger_id or item.legacy_path == f"stingers/{stinger_id}":
                return item
        return None

    # ── storage ──────────────────────────────────────────────────────────────

    def list_clips(self) -> list[dict]:
        """The library clips and stills a clip player can play (ready for the current format)."""
        clips = []
        for item in self.library.list_items():
            if item.kind not in {LibraryKind.clip, LibraryKind.still}:
                continue
            mezz = self.library.mezzanine_path(item.id, self.workspace.format_id)
            clips.append(
                {
                    "name": item.name,
                    "path": str(mezz) if mezz else "",
                    "size_bytes": mezz.stat().st_size if mezz else 0,
                    "suffix": mezz.suffix if mezz else "",
                    "library_item_id": item.id,
                    "ready": bool(mezz),
                    "kind": item.kind.value,
                }
            )
        return clips

    def list_stingers(self) -> list[StingerInfo]:
        items = list(list_stingers(self.settings.stingers_dir))
        legacy = {f"stingers/{item.id}" for item in items}
        for lib in self.library.list_items(kind=LibraryKind.stinger):
            # A legacy stinger and its imported library item are listed once (the legacy one).
            if lib.legacy_path in legacy:
                continue
            items.append(self._stinger_info_from_library(lib))
        return items

    def get_stinger(self, stinger_id: str) -> StingerInfo:
        try:
            require_safe_id(stinger_id, what="stinger id")
        except SecurityError as exc:
            raise MixerError(str(exc)) from exc
        # Prefer the legacy storage/stingers tree when present so cut-frame edits
        # and scripts/generate_stinger.py keep working unchanged.
        info = inspect_stinger(self.settings.stingers_dir, stinger_id, fps=self.settings.fps)
        if info is not None and info.frame_count:
            return info
        lib = self._find_library_stinger(stinger_id)
        if lib is not None:
            lib_info = self._stinger_info_from_library(lib)
            if not lib_info.media_path:
                raise StingerNotReady(f"stinger {stinger_id} not ready")
            return lib_info
        raise MixerError(f"unknown stinger {stinger_id}")

    # ── engine ───────────────────────────────────────────────────────────────

    def _engine_call(self, method: str, path: str, body: dict[str, Any], client: EngineClient | None = None) -> Any:
        """A command to the engine, and its reply. While the engine is not up (off-air,
        restarting) nothing is sent (None): its state is pushed when it is ready. A refused
        command is logged."""
        try:
            if client is not None:
                return client.request(method, path, body)
            if self.engine is not None and self.engine.ready:
                return self.engine.call(method, path, body)
        except EngineError as exc:
            log.warning("%s", exc)
            self.error = str(exc)
        return None

    def _format_token(self) -> str:
        return self.workspace.format_id.replace(".", "")

    def _compute_outputs(self, group_hint: str) -> dict[str, MeOutputs]:
        """The MXL flow ids of every rendering ME. ME 1 Program keeps FlowXer's ids from before the
        engine, so downstream activations survive an upgrade."""

        def flow(role: str) -> str:
            return nmos.flow_uuid(
                group_hint,
                role,
                seed=self.settings.resolved_nmos_seed,
                width=self.settings.width,
                height=self.settings.height,
                frame_rate_num=self.settings.frame_rate_num,
                frame_rate_den=self.settings.frame_rate_den,
            )

        outputs: dict[str, MeOutputs] = {}
        for number, panel in enumerate(self.rendered_panels(), 1):
            if number == 1:
                video, audio = flow("video"), flow("audio")
            else:
                video, audio = flow(f"{panel.id}:pgm-video"), flow(f"{panel.id}:pgm-audio")
            outputs[panel.id] = MeOutputs(
                panel_id=panel.id,
                pgm_video_flow_id=video,
                pgm_audio_flow_id=audio,
                pvw_video_flow_id=flow(f"{panel.id}:pvw-video") if self.settings.pvw_outputs else None,
            )
        return outputs

    def _engine_config(self, group_hint: str) -> dict[str, Any]:
        """The structure the engine starts with (its routes and buses follow on ready)."""
        live = [item for item in self.list_inputs() if item.kind == InputKind.mxl_live]
        players = [item for item in self.list_inputs() if item.kind == InputKind.clip_player]
        output_domain = str(self.settings.output_domain.resolve())
        return {
            "FLOWXER_ENGINE_FORMAT": self._format_token(),
            "FLOWXER_ENGINE_LATENCY": self.settings.latency,
            "FLOWXER_ENGINE_MES": self.render_count,
            "FLOWXER_ENGINE_INPUTS": [{"label": item.label} for item in live],
            "FLOWXER_ENGINE_PLAYERS": [{"label": item.label} for item in players],
            "FLOWXER_ENGINE_KEYS": [
                {"label": keyer.receiver_label, "me": self._panel_number(self.get_panel(keyer.panel_id)), "stage": keyer.stage.value}
                for keyer in self.keyers
            ],
            "FLOWXER_ENGINE_STINGER_PLAYERS": self._stinger_players(),
            "FLOWXER_ENGINE_PVW_OUTPUTS": self.settings.pvw_outputs,
            "FLOWXER_ENGINE_PGM_PREVIEW": self.settings.preview_pgm,
            "FLOWXER_ENGINE_PGM_PREVIEW_WIDTH": self.settings.preview_pgm_width,
            "FLOWXER_ENGINE_PGM_PREVIEW_FPS": self.settings.preview_pgm_fps,
            "FLOWXER_ENGINE_PGM_PREVIEW_KBPS": self.settings.preview_pgm_kbps,
            "FLOWXER_ENGINE_OUTPUT_DOMAIN": output_domain,
            "FLOWXER_ENGINE_OUTPUT_DOMAIN_ID": self.settings.resolved_output_domain_id,
            "FLOWXER_ENGINE_OUTPUTS": [
                {
                    "me": number,
                    "pgm_video": outputs.pgm_video_flow_id,
                    "pgm_audio": outputs.pgm_audio_flow_id,
                    "pvw_video": outputs.pvw_video_flow_id or "",
                    "label": panel.label,
                    "group": f"{group_hint} {panel.label}",
                }
                for number, panel in enumerate(self.rendered_panels(), 1)
                if (outputs := self.me_outputs.get(panel.id)) is not None
            ],
            "FLOWXER_ENGINE_AUDIO_CHANNELS": self.settings.audio_channels,
            "FLOWXER_ENGINE_AUDIO_MAX_DELAY_MS": self.settings.audio_max_delay_ms,
            "FLOWXER_ENGINE_LABEL": self.settings.node_label,
            "FLOWXER_ENGINE_GPU": self.settings.engine_gpu,
            "FLOWXER_ENGINE_HTTP_PORT": self.settings.engine_port,
        }

    def _on_engine_ready(self, client: EngineClient) -> None:
        """Every (re)start of the engine: push routes, clip players, keyers, buses and the audio
        (delays and channel maps, masters and strips)."""
        with self.nmos.lock:
            # A new engine holds no T-bar transition: Program and the tied keyers stay where they were.
            for panel_id in self._positions:
                self._mixes.pop(panel_id, None)
            self._positions.clear()
            self._held_ties.clear()
            self._arm_lever_pickup()
            for item in self.list_inputs():
                if item.kind == InputKind.mxl_live:
                    self._push_route(item, client)
                    self._push_input_audio(item, client)
                elif item.kind == InputKind.clip_player:
                    self._push_player(item, client)
            for keyer in self.keyers:
                # On before the settings: a new engine's keyer does not fly yet, so the box stays
                # where it was (it would jump to a keyframe).
                self._push_key(keyer, client)
                self._push_keyframes(keyer, client)
                self._engine_call("POST", f"/keyer/{self._keyer_number(keyer)}", {"on": keyer.enabled}, client=client)
                self._push_key_settings(keyer, client, box=keyer.dme.model_dump() if keyer.stage == KeyerStage.upstream else None)
            for panel in self.rendered_panels():
                self._push_panel(panel, client)
                self._push_panel_audio(panel, client=client)
            # A new engine holds no stinger.
            self._stinger_cued.clear()
            self._stinger_until.clear()
            self._cue_stingers(client)

    def _push_panel(self, panel: MixerPanel, client: EngineClient | None = None) -> None:
        m = self._panel_number(panel)
        self._engine_call("POST", f"/me/{m}/program", {"source": self._engine_source(panel.program_input_id)}, client=client)
        self._engine_call("POST", f"/me/{m}/preview", {"source": self._engine_source(panel.preview_input_id)}, client=client)
        self._engine_call("POST", f"/me/{m}/ftb", {"on": panel.ftb}, client=client)
        self._push_transition(panel, client)

    def _push_transition(self, panel: MixerPanel, client: EngineClient | None = None) -> None:
        """The ME's next transition (its wipe is what a take with type wipe uses)."""
        if not self._rendered(panel):
            return
        transition = panel.transition
        wipe = transition.wipe
        body = {
            "type": transition.type.value,
            "frames": transition.rate_frames,
            "pattern": wipe.pattern,
            "reverse": wipe.reverse,
            "soft": wipe.soft,
            "border": wipe.border,
            "border_color": wipe.border_color,
            "ties": [self._keyer_number(keyer) for keyer in self._tied(panel)],
        }
        self._engine_call("POST", f"/me/{self._panel_number(panel)}/transition", body, client=client)

    # ── mixer lifecycle ──────────────────────────────────────────────────────

    def start(self, request: MixerStartRequest | None = None) -> MixerStatus:
        # The lock: an autostart attempt and an operator's start must not both start an engine.
        with self._lock, self.watchdog.busy("Program start"):
            return self._start(request)

    def _start(self, request: MixerStartRequest | None = None) -> MixerStatus:
        request = request or MixerStartRequest()
        if self.state == MixerState.running:
            raise MixerError("mixer is already running")
        if not self.inputs:
            raise MixerError("register at least one logical input")
        output = self.settings.output_domain.resolve()
        if request.domain:
            requested = Path(request.domain).expanduser().resolve()
            if requested != output:
                raise MixerError("MXL domain path override is not allowed")
        try:
            ensure_output_domain(
                output,
                domain_id=self.settings.resolved_output_domain_id,
                label="FlowXer MXL domain",
                description="Uncompressed v210 video and float32 audio essences for the FlowXer vision mixer",
                history_duration_ns=self.settings.mxl_history_duration_ns,
            )
        except DomainError as exc:
            raise MixerError(str(exc)) from exc
        group_hint = request.group_hint or self.settings.group_hint
        self._bind_group_hints()
        # ME 1's buses: the request's, else the saved ones (while ME 1 can still take them), else
        # the first input (Preview: Program). A start-up snapshot then wins over them.
        program = request.program_input_id or self._saved_bus("program_input_id") or self._default_program_id()
        preview = request.preview_input_id or self._saved_bus("preview_input_id") or program
        for source in (program, preview):
            if source is not None and self.panels:
                self._check_source(self.panels[0], source)
        self.program_input_id = program
        self.preview_input_id = preview
        if self.panels:
            self.panels[0].program_input_id = program
            self.panels[0].preview_input_id = preview
        if self.snapshots.startup is not None:
            self._recall_startup()
        self.me_outputs = self._compute_outputs(group_hint)
        first = next(iter(self.me_outputs.values()), None)
        self.outputs = None
        if first is not None:
            self.outputs = OutputFlows(
                video_flow_id=first.pgm_video_flow_id,
                audio_flow_id=first.pgm_audio_flow_id,
                group_hint=group_hint,
                media_type_video=self.settings.video_media_type,
                media_type_audio=self.settings.audio_media_type,
                nmos_video=nmos.video_flow_def(
                    flow_id=first.pgm_video_flow_id,
                    group_hint=group_hint,
                    label=f"{self.panels[0].label} PGM Video",
                    description="FlowXer program video (uncompressed v210)",
                    width=self.settings.width,
                    height=self.settings.height,
                    frame_rate_num=self.settings.frame_rate_num,
                    frame_rate_den=self.settings.frame_rate_den,
                    media_type=self.settings.video_media_type,
                ),
                nmos_audio=nmos.audio_flow_def(
                    flow_id=first.pgm_audio_flow_id,
                    group_hint=group_hint,
                    label=f"{self.panels[0].label} PGM Audio",
                    description="FlowXer program audio (uncompressed float32)",
                    channels=self.settings.audio_channels,
                    sample_rate=self.settings.audio_rate,
                ),
            )
        self.error = None
        if self.settings.simulate:
            self.backend = "simulate"
        else:
            runtime = EngineRuntime(
                binary=self.settings.engine_bin,
                port=self.settings.engine_port,
                state_dir=self.settings.state_dir,
                on_ready=self._on_engine_ready,
            )
            try:
                runtime.start(self._engine_config(group_hint))
            except EngineError as exc:
                self.state = MixerState.error
                self.backend = "idle"
                self.error = str(exc)
                raise MixerError(self.error) from exc
            self.engine = runtime
            self.backend = "engine"
            threading.Thread(target=self._watch_domains, args=(runtime,), name="mxl-domain-watch", daemon=True).start()
            threading.Thread(target=self._watch_clip_ends, args=(runtime,), name="clip-end-watch", daemon=True).start()
        self.state = MixerState.running
        self._autostart_done.set()
        self._frame_mark = (self.frames_rendered, time.monotonic())
        self._stall_logged = False
        self.last_live_input_id = self.program_input_id
        self.program_bus = ProgramBus.live
        self.nmos.sync_senders_from_outputs()
        self._publish_tally()
        return self.status()

    def autostart(self) -> None:
        """FLOWXER_PROGRAM_AUTOSTART: ME 1 Program on the first live input (or the first
        input), Preview on the next one."""
        inputs = self.list_inputs()
        program = inputs[0].id if inputs else None
        preview = inputs[1].id if len(inputs) > 1 else program
        if not self._autostart_attempt(program, preview, 1):
            threading.Thread(
                target=self._autostart_retry, args=(program, preview), name="program-autostart", daemon=True
            ).start()

    def _autostart_retry(self, program: str | None, preview: str | None) -> None:
        """Program did not start: try again after growing pauses until it runs, an operator
        starts or stops it, or the process ends."""
        attempt = 1
        while not self._autostart_done.wait(_autostart_pause(attempt)):
            attempt += 1
            if self._autostart_attempt(program, preview, attempt):
                return

    def _autostart_attempt(self, program: str | None, preview: str | None, attempt: int) -> bool:
        """One FLOWXER_PROGRAM_AUTOSTART start. True when Program runs (also when it already ran)."""
        try:
            self.start(MixerStartRequest(program_input_id=program, preview_input_id=preview))
        except MixerError as exc:
            if self.state == MixerState.running:
                return True
            log.error(
                "FLOWXER_PROGRAM_AUTOSTART: Program did not start (attempt %d, next in %.0f s): %s",
                attempt,
                _autostart_pause(attempt),
                exc,
            )
            return False
        log.info("FLOWXER_PROGRAM_AUTOSTART: Program on %s, Preview on %s (attempt %d)", program, preview, attempt)
        return True

    def engine_status(self) -> dict[str, Any] | None:
        """The engine's last /status (fetched once a second), or None."""
        return self.engine.status if self.engine is not None else None

    def program_stalled_s(self) -> float | None:
        """Seconds without a new Program frame while on air with the engine; None while frames flow."""
        if self.state != MixerState.running or self.engine is None or not self.engine.ready:
            return None
        frames = self.frames_rendered
        now = time.monotonic()
        if frames != self._frame_mark[0]:
            self._frame_mark = (frames, now)
            self._stall_logged = False
            return None
        idle = now - self._frame_mark[1]
        if idle < PROGRAM_STALL_S:
            return None
        if not self._stall_logged:
            self._stall_logged = True
            log.warning("Program renders no frames (%s so far, none for %.0f s)", frames, idle)
        return idle

    def _engine_output(self) -> dict[str, Any]:
        status = self.engine_status() or {}
        return status.get("output") or {}

    @property
    def frames_rendered(self) -> int:
        """Program grains the engine wrote (none in simulate)."""
        return self._frames_before + int(self._engine_output().get("committed") or 0)

    @property
    def frames_dropped(self) -> int:
        """Output grains skipped because the engine was late (it stays on the TAI grid)."""
        return self._frames_dropped_before + int(self._engine_output().get("skipped") or 0)

    def stop(self) -> MixerStatus:
        with self.watchdog.busy("Program stop"):
            return self._stop()

    def _stop(self) -> MixerStatus:
        self._autostart_done.set()
        runtime = self.engine
        if runtime is not None:
            output = self._engine_output()
            self._frames_before += int(output.get("committed") or 0)
            self._frames_dropped_before += int(output.get("skipped") or 0)
            self.engine = None
            runtime.stop()
        self._missing_domains.clear()
        self._cancel_clip_end()
        self._players_seen = None
        self.state = MixerState.idle
        self.backend = "idle"
        self._mixes.clear()
        self._key_in.clear()
        self._cancel_tally_timers()
        self._publish_tally()
        return self.status()

    def shutdown(self) -> None:
        """SIGTERM: stop the engine (it releases the MXL readers and writers), deregister from
        the registry, then remove the own output domain when MXL_CLEANUP_ON_EXIT is set. An audio
        change still waiting to be saved is saved."""
        with self._persist_lock:
            pending, self._persist_timer = self._persist_timer, None
        if pending is not None:
            pending.cancel()
            self.persist()
        try:
            self.stop()
        except Exception:
            log.exception("stopping the media engine failed")
        try:
            self.library.stop()
        except Exception:
            log.exception("stopping the media library failed")
        self._cancel_tally_timers()
        self.tally.close()
        if self.tally_export is not None:
            self.tally_export.close()
        self.nmos.shutdown()
        if self.settings.mxl_cleanup_on_exit:
            remove_output_domain(
                self.settings.output_domain,
                domain_id=self.settings.resolved_output_domain_id,
                root=self.settings.mxl_root,
            )

    def _default_program_id(self) -> str | None:
        inputs = self.list_inputs()
        return inputs[0].id if inputs else None

    def _saved_bus(self, name: str) -> str | None:
        """ME 1's saved Program or Preview (`name`), while ME 1 can still take it."""
        source = getattr(self.panels[0], name) if self.panels else None
        return source if source is not None and self._source_known(self.panels[0], source) else None

    def _bind_group_hints(self) -> None:
        for item in self.inputs.values():
            if item.kind != InputKind.mxl_live or not item.group_hint:
                continue
            matched = flows_by_group_hint_in_root(self.settings.mxl_root, item.group_hint)
            if "video" in matched and (item.video is None or item.video.flow_id is None):
                video = item.video.model_dump() if item.video else {}
                video["flow_id"] = matched["video"].id
                if matched["video"].domain_id:
                    video["domain_id"] = matched["video"].domain_id
                item.video = VideoEssence(**video)
            if "audio" in matched and (item.audio is None or item.audio.flow_id is None):
                audio = item.audio.model_dump() if item.audio else {}
                audio["flow_id"] = matched["audio"].id
                if matched["audio"].domain_id:
                    audio["domain_id"] = matched["audio"].domain_id
                item.audio = AudioEssence(**audio)

    # ── takes ────────────────────────────────────────────────────────────────

    def take(
        self,
        input_id: str,
        transition: TransitionType = TransitionType.cut,
        stinger_id: str | None = None,
        panel_id: str = "me-1",
        duration_ms: int = 0,
        frames: int | None = None,
    ) -> MixerStatus:
        """Direct source → Program (source-tile right-click). A wipe uses the ME's wipe settings
        (and its rate without a duration). A stinger plays `stinger_id` (or the slot by
        direction); a cut plays the source's auto-stinger. A stinger that cannot play is a cut."""
        if self.state != MixerState.running:
            self.start(MixerStartRequest(program_input_id=input_id, preview_input_id=input_id))
        panel = self.get_panel(panel_id)
        self._check_source(panel, input_id)
        if transition in (TransitionType.stinger, TransitionType.cut):
            if transition == TransitionType.stinger:
                slot = self._slot_for_stinger(stinger_id) or self._slot_for_target(input_id)
            else:
                slot, stinger_id = self._auto_stinger(input_id), None
            if slot is not None and self._play_stinger_on(panel, slot, input_id, flip_flop=False, stinger_id=stinger_id):
                return self.status()
            transition = TransitionType.cut
        if transition == TransitionType.wipe and not frames and not duration_ms:
            frames = panel.transition.rate_frames
        self._put_on_program(panel, input_id, transition, flip_flop=False, duration_ms=duration_ms, frames=frames or 0)
        return self.status()

    def cut(self, panel_id: str = "me-1") -> MixerStatus:
        """Preview onto Program, Program onto Preview. The Preview source's auto-stinger plays,
        else an armed Wipe plays the stinger slot by direction (and is used up), as in FlowXer 14."""
        panel = self._ensure_running_panel(panel_id)
        if not panel.transition.background:
            return self._keys_only(panel, auto=False)
        incoming = self._preview_id(panel)
        slot = self._auto_stinger(incoming)
        if slot is None and panel.wipe_armed:
            panel.wipe_armed = False
            slot = self._slot_for_target(incoming)
        if slot is None or not self._play_stinger_on(panel, slot, incoming, flip_flop=True, ties=True):
            self._put_on_program(panel, incoming, TransitionType.cut, flip_flop=True, ties=True)
        return self.status()

    def fade(self, panel_id: str = "me-1", duration_ms: int = 400) -> MixerStatus:
        """Dissolve Preview onto Program."""
        panel = self._ensure_running_panel(panel_id)
        incoming = self._preview_id(panel)
        self._put_on_program(panel, incoming, TransitionType.mix, flip_flop=True, duration_ms=duration_ms)
        return self.status()

    def fade_to_black(self, panel_id: str = "me-1", duration_ms: int = 600) -> MixerStatus:
        """Fade the ME's whole output (the DSKs and the sound too) to black, or back up. The
        buses do not change; tally does not change."""
        panel = self._ensure_running_panel(panel_id)
        panel.ftb = not panel.ftb
        if self._rendered(panel):
            frames = _frames(duration_ms or 600, self.settings.fps)
            self._engine_call("POST", f"/me/{self._panel_number(panel)}/ftb/auto", {"frames": frames})
        return self.status()

    def update_transition(self, panel_id: str, payload: PanelTransitionUpdate) -> PanelTransition:
        """Change the ME's next transition (type, rate, wipe settings, stinger slot, keyer ties and
        BKGD). Ties are the ME's upstream keyers (kept in their order); BKGD off needs a tie."""
        panel = self.get_panel(panel_id)
        data = panel.transition.model_dump()
        update = payload.model_dump(exclude_unset=True)
        data["wipe"].update(update.pop("wipe", None) or {})
        data.update(update)
        for name in ("ties", "background"):
            if data.get(name) is None:
                data[name] = getattr(panel.transition, name)
        if data.get("stinger_slot_id") is not None:
            self.get_stinger_slot(data["stinger_slot_id"])
        upstream = [keyer.id for keyer in self.keyers if keyer.panel_id == panel.id and keyer.stage == KeyerStage.upstream]
        others = [tie for tie in data["ties"] if tie not in upstream]
        if others:
            raise MixerError(f"ties must be upstream keyers of {panel.label}: not {', '.join(others)}")
        data["ties"] = [keyer_id for keyer_id in upstream if keyer_id in data["ties"]]
        if not data["background"] and not data["ties"]:
            raise MixerError("BKGD off (key only) needs a tied keyer")
        try:
            panel.transition = PanelTransition(**data)
        except ValidationError as exc:
            raise MixerError(f"invalid transition: {exc.errors()[0]['msg']}") from exc
        self._push_transition(panel)
        self._cue_stingers()
        return panel.transition

    def auto(self, panel_id: str = "me-1", frames: int | None = None) -> MixerStatus:
        """The ME's next transition from Preview to Program over its rate (or `frames`), then the
        buses swap. A stinger plays the ME's stinger slot (or the slot by direction) over its own
        length."""
        panel = self._ensure_running_panel(panel_id)
        status = self._auto(panel, frames)
        # The virtual lever goes to its other end with the Auto (it runs there at the rate); only
        # once the Auto has started (a failure raised above).
        self._flip_tbar(panel)
        return status

    def _auto(self, panel: MixerPanel, frames: int | None) -> MixerStatus:
        held = self._positions.pop(panel.id, None)
        if held is not None:
            # From the held T-bar position: the engine runs the rest at the rate (with the keyers
            # tied to it).
            position, _outgoing, incoming = held
            rate = frames or panel.transition.rate_frames
            if self._rendered(panel):
                self._engine_call("POST", f"/me/{self._panel_number(panel)}/auto", {"frames": rate})
            rest = max(1, round(rate * (1 - position)))
            swap = panel.preview_input_id == incoming
            self._end_held_ties(panel, rest)
            self._put_on_program(panel, incoming, panel.transition.type, flip_flop=swap, frames=rest, send=False)
            return self.status()
        if not panel.transition.background:
            return self._keys_only(panel, auto=True)
        incoming = self._preview_id(panel)
        transition = panel.transition.type
        if transition == TransitionType.stinger:
            if self._play_stinger_on(panel, self._transition_slot(panel, incoming), incoming, flip_flop=True, ties=True):
                return self.status()
            transition = TransitionType.cut
        self._put_on_program(panel, incoming, transition, flip_flop=True, frames=frames or panel.transition.rate_frames, ties=True)
        return self.status()

    def _tied(self, panel: MixerPanel) -> list[Keyer]:
        """The keyers tied to the ME's next transition."""
        return [keyer for keyer in self.keyers if keyer.panel_id == panel.id and keyer.id in panel.transition.ties]

    def _take_ties(self, keyers: list[Keyer], frames: int) -> None:
        """Tied keyers go to their other state with the ME's transition, which the engine runs
        (docs/engine-p4.md §5): a keyer that goes off stays on air (raw tally) for the `frames` of the
        transition (0: a cut); a keyer that flies ends at its last keyframe (on) or keyframe 0 (off)."""
        now = time.monotonic()
        for keyer in keyers:
            keyer.enabled = not keyer.enabled
            if self._flies(keyer):
                keyer.dme = self._box_at(keyer, keyer.keyframes[-1 if keyer.enabled else 0])
            self._key_in.pop(keyer.id, None)
            if keyer.enabled or frames <= 0:
                self._key_until.pop(keyer.id, None)
                if keyer.enabled and frames > 0:
                    self._key_in[keyer.id] = now + frames / self.settings.fps
                    self._tally_again_at(self._key_in[keyer.id])
            else:
                self._key_until[keyer.id] = now + frames / self.settings.fps
                self._tally_again_at(self._key_until[keyer.id])

    def _end_held_ties(self, panel: MixerPanel, frames: int = 0) -> None:
        """A T-bar transition that completes (now, or by an Auto over `frames`): its tied keyers
        change state with it."""
        held = self._held_ties.pop(panel.id, [])
        self._take_ties([keyer for keyer in self.keyers if keyer.id in held], frames)

    def _keys_only(self, panel: MixerPanel, *, auto: bool) -> MixerStatus:
        """BKGD off (key only): Cut turns the tied keyers on or off at the next frame, Auto runs each
        one's own Auto (its rate, or its fly)."""
        for keyer in self._tied(panel):
            if auto:
                self.keyer_auto(keyer.id)
            else:
                self.update_keyer(keyer.id, enabled=not keyer.enabled)
        return self.status()

    def set_position(self, panel_id: str = "me-1", position: float = 0.0) -> MixerStatus:
        """The T-bar (0..1): moving off 0 starts the ME's next transition from Preview to Program
        and holds it there; 1 completes it (the buses swap); 0 ends it without the swap. Auto
        runs the rest. A stinger does not scrub: moving off 0 runs it as an Auto."""
        panel = self._ensure_running_panel(panel_id)
        held = self._positions.get(panel.id)
        if held is None:
            if position <= 0:
                return self.status()
            if panel.transition.type == TransitionType.stinger or not panel.transition.background:
                return self.auto(panel.id)
            held = (position, panel.program_input_id, self._preview_id(panel))
            # Tally: both sources (and the tied keyers) are on air while the T-bar holds the
            # transition.
            self._mixes[panel.id] = (held[1], held[2], None)
            self._held_ties[panel.id] = list(panel.transition.ties)
        if self._rendered(panel):
            self._engine_call("POST", f"/me/{self._panel_number(panel)}/position", {"t": position})
        _position, outgoing, incoming = held
        if position >= 1:
            # (Also straight from rest to the end: a whole transition in one move.)
            self._positions.pop(panel.id, None)
            self._flip_tbar(panel)
            swap = panel.preview_input_id == incoming
            self._end_held_ties(panel)
            self._put_on_program(panel, incoming, panel.transition.type, flip_flop=swap, frames=1, send=False)
        elif position <= 0:
            self._positions.pop(panel.id, None)
            self._mixes.pop(panel.id, None)
            self._held_ties.pop(panel.id, None)
            self._publish_tally()
        else:
            self._positions[panel.id] = (position, outgoing, incoming)
            self._publish_tally()
        return self.status()

    def set_lever(
        self, panel_id: str = "me-1", lever: float = 0.0, from_end: str | None = None, source: str = ""
    ) -> tuple[MixerStatus, str | None]:
        """The T-bar lever where it physically is (0 top .. 1 bottom): the transition's progress
        counted from the end it rests at (panel.tbar_end), so the lever works both ways
        (ping-pong). Within TBAR_DEADBAND of its resting end it starts nothing; while it holds a
        transition, within TBAR_DEADBAND of either end it snaps there (back without the swap, or
        complete). Returns the status and, when the value was ignored, why ("stale_from",
        "pickup").

        Another console's Auto or Cut, or the lever's own completion, moves the resting end:
        - `from_end` (the end the client's drag began at; the GUI sends it): a value whose
          from_end is not the resting end is ignored (it would take the shot back, or move a
          transition someone else holds from the other end);
        - without it (a hardware lever, told apart by `source`): the lever counts only once it has
          come within TBAR_DEADBAND of the resting end (pickup), at start, after the engine
          (re)starts, after a state is loaded and after each change of the end; until then it is
          ignored, also while another console holds a transition.
        With a stinger or BKGD off (an Auto from the lever, no scrubbing) the flip makes the lever
        wait for the next drag (from_end) or for its pickup, so one move fires one Auto."""
        panel = self._ensure_running_panel(panel_id)
        held = panel.id in self._positions
        rest = 0.0 if panel.tbar_end == "top" else 1.0
        if from_end is not None:
            if from_end != panel.tbar_end:
                log.debug("T-bar of %s: lever from %s ignored (rests at %s)", panel.id, from_end, panel.tbar_end)
                return self.status(), "stale_from"
        elif (panel.id, source) not in self._lever_picked:
            # Pickup: where the lever is now (the resting end, or a transition another lever holds),
            # so taking over never makes the transition jump.
            held_at = self._positions[panel.id][0] if held else 0.0
            current = held_at if panel.tbar_end == "top" else 1.0 - held_at
            if abs(lever - current) > TBAR_DEADBAND + _EPS:
                log.debug("T-bar of %s: lever %.3f of %r ignored until it is at %.3f", panel.id, lever, source, current)
                return self.status(), "pickup"
            self._pick_lever(panel.id, source)
        else:
            # Recently used: the last to go when the cap is reached.
            self._pick_lever(panel.id, source)
        position = lever if panel.tbar_end == "top" else 1.0 - lever
        if held:
            if position <= TBAR_DEADBAND + _EPS:
                position = 0.0
            elif position >= 1.0 - TBAR_DEADBAND - _EPS:
                position = 1.0
        elif position <= TBAR_DEADBAND + _EPS:
            return self.status(), None
        end = panel.tbar_end
        before = self._positions[panel.id][0] if held else 0.0
        status = self.set_position(panel.id, position)
        if panel.id in self._positions and from_end is None and abs(position - before) > _EPS:
            self._lever_driver[panel.id] = source  # this hardware lever moved the held transition
        elif panel.id not in self._positions:
            self._lever_driver.pop(panel.id, None)
        if from_end is None and panel.tbar_end != end and abs(lever - (0.0 if panel.tbar_end == "top" else 1.0)) <= TBAR_DEADBAND + _EPS:
            # The hardware lever completed it itself: it is at the new resting end already.
            self._pick_lever(panel.id, source)
        return status, None

    def _pick_lever(self, panel_id: str, source: str) -> None:
        """A hardware lever came to the resting end, or sent a value that counted (most recent
        last). At most TBAR_SOURCES per ME are kept: the least recently used one goes (it picks up
        again), never the one that holds the ME's transition."""
        mine = [item for item in self._lever_picked if item[0] == panel_id]
        if (panel_id, source) not in self._lever_picked and len(mine) >= TBAR_SOURCES:
            driver = (panel_id, self._lever_driver.get(panel_id)) if panel_id in self._positions else None
            victim = next((item for item in mine if item != driver), None)
            if victim is not None:
                del self._lever_picked[victim]
        self._lever_picked.pop((panel_id, source), None)
        self._lever_picked[(panel_id, source)] = None

    def _arm_lever_pickup(self, panel_id: str | None = None) -> None:
        """Hardware levers (without from_end) must come to the resting end before they count:
        for one ME (its end changed) or every ME (start, engine restart, a loaded state)."""
        self._lever_picked = {item: None for item in self._lever_picked if panel_id is not None and item[0] != panel_id}

    def tbar_state(self) -> dict[str, dict[str, Any]]:
        """Per ME the T-bar: the end the lever rests at, the progress of a transition it holds
        (0: none) and where the lever is (0 top .. 1 bottom)."""
        out: dict[str, dict[str, Any]] = {}
        for panel in list(self.panels):
            held = self._positions.get(panel.id)
            position = held[0] if held is not None else 0.0
            lever = position if panel.tbar_end == "top" else 1.0 - position
            out[panel.id] = {"end": panel.tbar_end, "position": position, "lever": lever}
        return out

    def _flip_tbar(self, panel: MixerPanel) -> None:
        """A transition of the T-bar (or an Auto) completed: the lever rests at the other end. A
        lever that does not say where its drag began (a hardware panel) must first come to that end
        (pickup) before it counts again."""
        panel.tbar_end = "bottom" if panel.tbar_end == "top" else "top"
        self._arm_lever_pickup(panel.id)

    def set_wipe(self, panel_id: str = "me-1", armed: bool | None = None) -> MixerStatus:
        """Arm (or toggle) Wipe: the next Cut plays the stinger slot by direction (FlowXer 14's
        meaning, kept for its clients)."""
        panel = self._ensure_running_panel(panel_id)
        panel.wipe_armed = (not panel.wipe_armed) if armed is None else armed
        self._cue_stingers()
        return self.status()

    def _ensure_running_panel(self, panel_id: str) -> MixerPanel:
        if self.state != MixerState.running:
            self.start(MixerStartRequest())
        return self.get_panel(panel_id)

    def _preview_id(self, panel: MixerPanel) -> str:
        incoming = panel.preview_input_id
        if not incoming:
            raise MixerError("arm a source on Preview before cutting or fading")
        self._check_source(panel, incoming)
        return incoming

    def _put_on_program(
        self,
        panel: MixerPanel,
        source_id: str,
        transition: TransitionType,
        *,
        flip_flop: bool,
        duration_ms: int = 0,
        frames: int = 0,
        send: bool = True,
        sting: dict[str, int] | None = None,
        ties: bool = False,
        at: int | None = None,
    ) -> None:
        """Program to `source_id` by `transition` over `frames` (else `duration_ms`, else a mix's
        default); `flip_flop` puts the old Program on Preview. `send=False`: the engine does it
        already (the T-bar). A stinger: `sting` is its player and cut frame, `frames` its length.
        `ties`: the ME's Cut or Auto, which take its tied keyers along. `at`: the engine holds the
        take for that output grain (a clip player's end action, a few grains ahead)."""
        self._cancel_clip_end(panel.id)
        # Seconds until the engine makes it (the tally of a mix ends that much later).
        wait = self._grains_until(at) / self.settings.fps
        held = self._positions.pop(panel.id, None)
        if held is not None:
            # A held T-bar transition ends first, as if it had run to its end (as in the engine);
            # the lever counts it as done.
            self._flip_tbar(panel)
            _position, _outgoing, incoming = held
            swap = panel.preview_input_id == incoming
            self._end_held_ties(panel)
            self._put_on_program(panel, incoming, panel.transition.type, flip_flop=swap, frames=1, send=False)
        outgoing = panel.program_input_id
        panel.program_input_id = source_id
        panel.last_transition = transition.value
        self.last_transition = transition.value
        kind = transition.value
        self.transition_counts[kind] = self.transition_counts.get(kind, 0) + 1
        swapped = flip_flop and outgoing and outgoing != source_id
        if swapped:
            panel.preview_input_id = outgoing
        if transition in (TransitionType.mix, TransitionType.wipe, TransitionType.stinger):
            frames = frames or _frames(duration_ms or DEFAULT_MIX_MS, self.settings.fps)
            # Tally: both sources are on air (LH) for the whole mix, wipe or stinger.
            self._mixes[panel.id] = (outgoing, source_id, time.monotonic() + wait + frames / self.settings.fps)
            self._tally_again_at(self._mixes[panel.id][2], export=False)
        else:
            frames = 0
            self._mixes.pop(panel.id, None)
        if self.panels and panel.id == self.panels[0].id:
            self.program_input_id = source_id
            if swapped:
                self.preview_input_id = outgoing
            if source_id in self.inputs and self.inputs[source_id].kind == InputKind.mxl_live:
                self.last_live_input_id = source_id
            self.program_bus = ProgramBus.live
        tied = self._tied(panel) if ties else []
        if send and self._rendered(panel):
            body: dict[str, Any] = {"source": self._engine_source(source_id), "frames": 0 if sting else frames}
            if transition == TransitionType.wipe or sting:
                # Without a type the engine mixes (frames > 0) or cuts.
                body["type"] = transition.value
            if sting:
                body.update(sting)
            if swapped:
                body["preview"] = self._engine_source(outgoing)
            if tied:
                body["ties"] = True
            if at is not None:
                body["at"] = at
            self._engine_call("POST", f"/me/{self._panel_number(panel)}/take", body)
        self._take_ties(tied, frames)
        wipe = panel.transition.wipe
        if transition == TransitionType.wipe and wipe.alternate:
            # Flip-flop: the next wipe runs back (this one keeps the direction it started with).
            wipe.reverse = not wipe.reverse
            self._push_transition(panel)
        self._publish_tally()

    def set_preview(self, input_id: str, panel_id: str = "me-1") -> MixerStatus:
        if self.state != MixerState.running:
            # Program stays where it was saved.
            self.start(MixerStartRequest(preview_input_id=input_id))
        self._set_preview(self.get_panel(panel_id), input_id)
        return self.status()

    def _set_preview(self, panel: MixerPanel, input_id: str) -> None:
        self._check_source(panel, input_id)
        panel.preview_input_id = input_id
        if panel.id == self.panels[0].id:
            self.preview_input_id = input_id
        if self._rendered(panel):
            self._engine_call("POST", f"/me/{self._panel_number(panel)}/preview", {"source": self._engine_source(input_id)})
        self._publish_tally()
        self._cue_stingers()

    def play_stinger(
        self,
        stinger_id: str,
        target_input_id: str,
        direction: str | None = None,
        outgoing_input_id: str | None = None,
        flip_flop: bool = False,
        panel_id: str | None = None,
    ) -> MixerStatus:
        """Plays `stinger_id` (its slot's cut, else the stinger's own) to the target on the GPU; a
        stinger that cannot play (it still converts, every player busy) is a cut."""
        if self.state != MixerState.running:
            self.start(MixerStartRequest())
        panel = self.get_panel(panel_id or (self.panels[0].id if self.panels else "me-1"))
        self._check_source(panel, target_input_id)
        slot = self._slot_for_stinger(stinger_id) or self._slot_for_target(target_input_id, direction)
        if not self._play_stinger_on(panel, slot, target_input_id, flip_flop=flip_flop, stinger_id=stinger_id):
            self._put_on_program(panel, target_input_id, TransitionType.cut, flip_flop=flip_flop)
        return self.status()

    def advance_stinger(self, frames: int = 1) -> MixerStatus:
        """The engine runs stingers on the grain clock (kept for the API)."""
        return self.status()

    def _stinger_players(self) -> int:
        """Stinger players of the engine: the workspace's, at most the rendering MEs."""
        return min(self.workspace.stinger_players, self.render_count)

    def _auto_stinger(self, input_id: str) -> StingerSlot | None:
        """The auto-stinger slot of a source (played when it is cut or taken to Program)."""
        source = self.inputs.get(input_id)
        if source is None or not source.stinger_slot_id:
            return None
        return next((slot for slot in self.stinger_slots if slot.id == source.stinger_slot_id), None)

    def _slot_for_target(self, target_input_id: str, direction: str | None = None) -> StingerSlot | None:
        """The slot by direction: to a clip player (or "to_replay") an `in` slot, else `out`; a
        shared slot serves both."""
        if direction is None:
            target = self.inputs.get(target_input_id)
            direction = "to_replay" if target is not None and target.kind == InputKind.clip_player else "to_live"
        role = "in" if direction == "to_replay" else "out"
        for slot in self.stinger_slots:
            if slot.role in {role, "shared"}:
                return slot
        return self.stinger_slots[0] if self.stinger_slots else None

    def _slot_for_stinger(self, stinger_id: str | None) -> StingerSlot | None:
        if not stinger_id:
            return None
        return next((slot for slot in self.stinger_slots if stinger_id in {slot.stinger_id, slot.library_item_id}), None)

    def _transition_slot(self, panel: MixerPanel, incoming: str) -> StingerSlot | None:
        """The slot of the ME's transition type stinger: its own, else the slot by direction."""
        slot_id = panel.transition.stinger_slot_id
        slot = next((slot for slot in self.stinger_slots if slot.id == slot_id), None) if slot_id else None
        return slot or self._slot_for_target(incoming)

    def _cue_slot(self, panel: MixerPanel) -> StingerSlot | None:
        """The stinger the ME's next Cut or Auto plays: Preview's auto-stinger, an armed Wipe's
        slot, or the slot of the transition type stinger."""
        incoming = panel.preview_input_id
        if not incoming:
            return None
        slot = self._auto_stinger(incoming)
        if slot is None and panel.wipe_armed:
            slot = self._slot_for_target(incoming)
        if slot is None and panel.transition.type == TransitionType.stinger:
            slot = self._transition_slot(panel, incoming)
        if slot is None and self._clip_end_action(panel) == ClipEnd.stinger:
            # The end action of the clip on Program.
            slot = self._transition_slot(panel, incoming)
        return slot

    def _stinger_media(self, slot: StingerSlot | None, stinger_id: str | None = None) -> tuple[dict[str, Any], int] | None:
        """What a stinger player loads for the slot's stinger (or `stinger_id`), and its cut frame;
        None while it has no mezzanine (it converts)."""
        own = stinger_id is None or (slot is not None and stinger_id in {slot.stinger_id, slot.library_item_id})
        if own and slot is not None and slot.library_item_id:
            item = self.library.get(slot.library_item_id)
        else:
            name = stinger_id if not own else (slot.stinger_id if slot is not None else None)
            item = self._find_library_stinger(name) if name else None
        fmt = self.workspace.format_id
        if item is None or item.kind != LibraryKind.stinger or not self.library.queue.ready_on_disk(item, fmt):
            return None
        conv = item.conversion_for(fmt)
        cut = self._stinger_info_from_library(item).cut_frame
        if own and slot is not None and not slot.library_item_id and slot.cut_ms is not None:
            # A legacy stinger: its cut is the slot's (stinger.json), in ms of any rate.
            cut = cut_frame_from_ms(slot.cut_ms, self.settings.fps, conv.frames)
        alpha = self.library.alpha_path(item.id, fmt)
        audio = self.library.audio_path(item.id, fmt)
        body = {
            "path": str(self.library.mezzanine_path(item.id, fmt)),
            "alpha": str(alpha) if alpha else "",
            "audio": str(audio) if audio else "",
            "channels": conv.audio_channels,
            "frames": conv.frames,
        }
        return body, cut

    def _load_stinger(self, player: int, body: dict[str, Any], client: EngineClient | None = None) -> None:
        if self._stinger_cued.get(player) != body["path"]:
            self._engine_call("POST", f"/stinger/{player}/load", body, client=client)
            self._stinger_cued[player] = body["path"]

    def _cue_stingers(self, client: EngineClient | None = None) -> None:
        """Stinger player k holds the stinger ME k's next Cut or Auto plays, so a take starts at
        the next grain (a player that plays keeps its stinger)."""
        if self.engine is None and client is None:
            return
        now = time.monotonic()
        for number, panel in enumerate(self.rendered_panels()[: self._stinger_players()], 1):
            slot = self._cue_slot(panel)
            media = self._stinger_media(slot) if slot is not None and self._stinger_until.get(number, 0.0) <= now else None
            if media is not None:
                self._load_stinger(number, media[0], client)

    def _play_stinger_on(
        self,
        panel: MixerPanel,
        slot: StingerSlot | None,
        incoming: str,
        *,
        flip_flop: bool,
        stinger_id: str | None = None,
        ties: bool = False,
        at: int | None = None,
    ) -> bool:
        """Plays a stinger on the ME to `incoming`: on the player that holds it, else on a free one
        (loaded now; the engine starts it when its first frame is decoded). With every player busy
        it waits up to STINGER_WAIT_S for one. False when it cannot play (no mezzanine yet, no
        player free in time): the caller cuts. `ties`: the ME's tied keyers cut at its cut point."""
        media = self._stinger_media(slot, stinger_id)
        if media is None or not self._rendered(panel):
            log.warning("stinger %s is not ready: a cut", stinger_id or (slot.stinger_id if slot else None))
            self.stinger_fallbacks["not_ready"] += 1
            return False
        body, cut = media
        players = range(1, self._stinger_players() + 1)
        now = time.monotonic()
        free = [p for p in players if self._stinger_until.get(p, 0.0) <= now]
        soonest = min((self._stinger_until.get(p, 0.0) for p in players), default=None)
        if not free and soonest is not None and soonest - now <= STINGER_WAIT_S:
            # A player is free again soon (for example right after its stinger): wait for it.
            time.sleep(soonest - now)
            now = time.monotonic()
            free = [p for p in players if self._stinger_until.get(p, 0.0) <= now]
        if not free:
            log.warning("every stinger player plays a stinger: a cut")
            self.stinger_fallbacks["busy"] += 1
            return False
        player = next((p for p in free if self._stinger_cued.get(p) == body["path"]), free[0])
        cued = self._stinger_cued.get(player) == body["path"]
        self._load_stinger(player, body)
        frames = max(1, int(body["frames"]))
        seconds = frames / self.settings.fps
        # A stinger loaded now may wait for its first frame (the engine waits at most 50 grains).
        self._stinger_until[player] = now + self._grains_until(at) / self.settings.fps + seconds + (0.1 if cued else 1.0)
        self._put_on_program(
            panel, incoming, TransitionType.stinger, flip_flop=flip_flop, frames=frames, sting={"stinger": player, "cut": cut}, ties=ties, at=at
        )
        self._stinger_run = {
            "id": stinger_id or (slot.stinger_id if slot else None),
            "panel_id": panel.id,
            "player": player,
            "frame_count": frames,
            "cut_frame": cut,
            "target_input_id": incoming,
            "started": now,
        }
        # When it has played, the player cues the next stinger again.
        timer = threading.Timer(self._stinger_until[player] - now + 0.1, self._cue_stingers)
        timer.daemon = True
        timer.start()
        return True

    def _stinger_status(self) -> dict[str, Any]:
        """The last stinger taken, by the control plane's clock."""
        run = self._stinger_run
        if run is None:
            return {"id": None, "phase": "idle", "frame": 0, "frame_count": 0, "cut_frame": 0, "cut_fired": False, "done": True,
                    "target_input_id": None, "direction": None, "events": []}
        frame = min(run["frame_count"], int((time.monotonic() - run["started"]) * self.settings.fps))
        done = frame >= run["frame_count"]
        cut_fired = frame >= run["cut_frame"]
        return {
            "id": run["id"],
            "phase": "complete" if done else "cut" if cut_fired else "playing",
            "frame": frame,
            "frame_count": run["frame_count"],
            "cut_frame": run["cut_frame"],
            "cut_fired": cut_fired,
            "done": done,
            "target_input_id": run["target_input_id"],
            "direction": None,
            "panel_id": run["panel_id"],
            "player": run["player"],
            "events": [],
        }

    # ── a clip player's end action (docs/clip-end.md) ───────────────────────

    def _clip_end_action(self, panel: MixerPanel) -> ClipEnd:
        """What the ME does at the end of the clip on its Program: its clip player's on_end while
        it does not loop and Preview is another source; else off."""
        item = self.inputs.get(panel.program_input_id or "")
        if item is None or item.kind != InputKind.clip_player or item.loop or not self._rendered(panel):
            return ClipEnd.off
        if not panel.preview_input_id or panel.preview_input_id == item.id:
            return ClipEnd.off
        return item.on_end

    def _clip_end_start(self, panel: MixerPanel, end: int) -> int:
        """The output grain the ME's end action starts in, for a clip whose last frame is in grain
        `end`: a cut on the grain after it; an auto `rate` grains before, so its last grain (the
        target in full) is the clip's last frame; a stinger so that its cut point is there (a
        stinger that cannot play cuts on the grain after it)."""
        action = self._clip_end_action(panel)
        if action == ClipEnd.auto:
            return end - panel.transition.rate_frames + 1
        if action == ClipEnd.stinger:
            media = self._stinger_media(self._transition_slot(panel, panel.preview_input_id or ""))
            if media is not None:
                return end - media[1]
        return end + 1

    def _grains_until(self, at: int | None) -> float:
        """Grains from now to output grain `at` (0 without one or when it is past), from the
        engine's last /players."""
        if at is None or self._players_seen is None:
            return 0.0
        index, seen = self._players_seen
        return max(0.0, at - index - (time.monotonic() - seen) * self.settings.fps)

    def _watch_clip_ends(self, runtime: EngineRuntime) -> None:
        """While `runtime` runs Program: reads the engine's clip players (GET /players) while a clip
        player with an end action is on a rendering ME's Program."""
        while self.engine is runtime:
            time.sleep(CLIP_END_POLL_S)
            if self.engine is not runtime or not runtime.ready:
                continue
            if not any(self._clip_end_action(panel) != ClipEnd.off for panel in self.rendered_panels()):
                continue
            try:
                reply = runtime.call("GET", "/players")
            except EngineError:
                continue
            if isinstance(reply, dict):
                self._clip_end_tick(reply, time.monotonic())

    def _clip_end_tick(self, reply: dict[str, Any], seen: float) -> None:
        """One read of the engine's players (`index`: the last grain planned, `players`: each one's
        state and end_index) at monotonic time `seen`: a timer for each ME whose end action starts
        within CLIP_END_HORIZON grains, to send it CLIP_END_LEAD grains early."""
        index = int(reply.get("index") or 0)
        players = reply.get("players") or []
        self._players_seen = (index, seen)
        for panel in self.rendered_panels():
            action = self._clip_end_action(panel)
            if action == ClipEnd.off:
                continue
            item = self.inputs[panel.program_input_id or ""]
            if action == ClipEnd.stinger and (panel.id, item.id) not in self._clip_end_cued:
                # Cue the stinger early, so it starts on its grain.
                self._clip_end_cued.add((panel.id, item.id))
                self._cue_stingers()
            k = self._engine_index(item)
            entry = players[k - 1] if k <= len(players) and isinstance(players[k - 1], dict) else {}
            end = entry.get("end_index")
            if not isinstance(end, int) or end < index - CLIP_END_LATE or self._clip_end_done.get(panel.id) == (item.id, end):
                continue
            if entry.get("state") == "playing":
                self._clip_end_seen[panel.id] = (item.id, end)
            elif self._clip_end_seen.get(panel.id) != (item.id, end):
                # Ended before it was on this ME's Program (or before it was seen playing there).
                continue
            start = self._clip_end_start(panel, end)
            if start - index > CLIP_END_HORIZON:
                continue
            pending = self._clip_end_timers.get(panel.id)
            if pending is not None and pending[:2] == (item.id, start):
                continue
            if pending is not None:
                pending[2].cancel()
            delay = max(0.0, (start - CLIP_END_LEAD - index) / self.settings.fps - (time.monotonic() - seen))
            timer = threading.Timer(delay, self._clip_end_fire, args=(panel.id, item.id, end, start))
            timer.daemon = True
            self._clip_end_timers[panel.id] = (item.id, start, timer)
            timer.start()

    def _cancel_clip_end(self, panel_id: str | None = None, player_id: str | None = None) -> None:
        """Cancels the end actions not sent yet: of one panel, of one clip player, or all."""
        for key, (item_id, _start, timer) in list(self._clip_end_timers.items()):
            if (panel_id is None or key == panel_id) and (player_id is None or item_id == player_id):
                timer.cancel()
                self._clip_end_timers.pop(key, None)
        if panel_id is None and player_id is None:
            self._clip_end_cued.clear()

    def _clip_end_fire(self, panel_id: str, item_id: str, end: int, start: int) -> None:
        """The ME's end action, through the paths of a Cut, an Auto and a stinger (tally, tied
        keyers, audio follow): held by the engine for grain `start`, or at once when that is past.
        Nothing when the clip is no longer on Program, Preview is the same, a transition runs on
        the ME or this end was done."""
        with self._lock:
            pending = self._clip_end_timers.get(panel_id)
            if pending is None or pending[:2] != (item_id, start):
                return
            del self._clip_end_timers[panel_id]
            if self.state != MixerState.running or self.engine is None:
                return
            panel = self.get_panel(panel_id)
            action = self._clip_end_action(panel)
            mix = self._mixes.get(panel.id)
            busy = panel.id in self._positions or (mix is not None and (mix[2] is None or mix[2] > time.monotonic()))
            if action == ClipEnd.off or panel.program_input_id != item_id or busy:
                return
            self._clip_end_done[panel.id] = (item_id, end)
            incoming = panel.preview_input_id or ""
            at = start if self._grains_until(start) >= 1 else None
            log.info("%s: %s at the end of %s (grain %s)", panel.label, action.value, item_id, start)
            if action == ClipEnd.auto:
                kind = panel.transition.type if panel.transition.type in (TransitionType.mix, TransitionType.wipe) else TransitionType.mix
                self._put_on_program(panel, incoming, kind, flip_flop=True, frames=panel.transition.rate_frames, ties=True, at=at)
            elif action != ClipEnd.stinger or not self._play_stinger_on(
                panel, self._transition_slot(panel, incoming), incoming, flip_flop=True, ties=True, at=at
            ):
                cut_at = end + 1 if self._grains_until(end + 1) >= 1 else None
                self._put_on_program(panel, incoming, TransitionType.cut, flip_flop=True, ties=True, at=cut_at)
            self._clip_end_cued.discard((panel.id, item_id))
            self.persist()

    # ── tally ────────────────────────────────────────────────────────────────

    def _require_running(self) -> None:
        if self.state != MixerState.running:
            raise MixerError("mixer is not running")

    def replace_tally_receivers(self, receivers: list[TallyReceiver]) -> list[TallyReceiverStatus]:
        try:
            self.tally.replace(receivers)
        except ValueError as exc:
            raise MixerError(str(exc)) from exc
        return self.publish_tally()

    def publish_tally(self) -> list[TallyReceiverStatus]:
        return self._publish_tally()

    def _publish_tally(self) -> list[TallyReceiverStatus]:
        self._export_tally()
        try:
            return self.tally.publish(self)
        except Exception as exc:
            log.warning("tally publish failed: %s", exc)
            return self.tally.status()

    def _tally_again_at(self, at: float | None, *, export: bool = True) -> None:
        """The tally once more when a mix or a key's fade or fly ends (monotonic `at`): what is
        visible changes then without an operator action. `export`: the raw tally export too (it
        sends at a mix's end by itself)."""
        export = export and self.tally_export is not None
        if at is None or not (self.tally.receivers or export):
            return
        timer: threading.Timer

        def fire() -> None:
            with self._tally_timers_lock:
                if timer not in self._tally_timers:
                    return  # cancelled meanwhile
                self._tally_timers.discard(timer)
            self._publish_tally_quietly(export=export)

        timer = threading.Timer(max(0.0, at - time.monotonic()) + TALLY_END_MARGIN_S, fire)
        timer.daemon = True
        with self._tally_timers_lock:
            self._tally_timers.add(timer)
        timer.start()

    def _cancel_tally_timers(self) -> None:
        """Stop, a reload, shutdown: no republish after them."""
        with self._tally_timers_lock:
            timers, self._tally_timers = self._tally_timers, set()
        for timer in timers:
            timer.cancel()

    def _publish_tally_quietly(self, *, export: bool = True) -> None:
        try:
            if export:
                self._publish_tally()
            else:
                self.tally.publish(self)
        except Exception:  # a timer thread must not die loudly
            log.exception("tally publish after a mix or a key fade failed")

    def _export_tally(self) -> None:
        """FLOWXER_TALLY_TSL: wake the export thread, which takes the state and sends it."""
        if self.tally_export is not None:
            self.tally_export.changed()

    def _tally_snapshot(self) -> TallySnapshot:
        """Per ME what is visible on its Program through any path (engine/visibility.py, as for the
        receivers and the console), its Preview, its running mix (when to send again), the
        re-entry sources it can take, and the linear key inputs of it and of the higher MEs it
        can take, red while visible on its Program (lamps off while Program is stopped). Called by
        the export thread."""
        running = self.state == MixerState.running
        keyers = self.keyer_views()
        mes_layers = self._me_layers(keyers) if running else {}
        mes = []
        for panel in list(self.panels):
            reachable = self.reentry_sources(panel)
            reentry = tuple((other.id, other.label, REENTRY_INDEX + self._panel_number(other)) for other in reachable)
            # The key inputs of this ME and of every ME it can show (nested ones on the outer screen too).
            panel_ids = [panel.id, *(other.id for other in reachable)]
            linear = [k for k in self.keyers if k.panel_id in panel_ids and k.mode == KeyMode.linear]
            if not running:
                mes.append(MeTally(reentry=reentry))
                continue
            visible = visible_on_program(panel.id, mes_layers)
            visible_keys = set(visible_keys_on_program(panel.id, mes_layers))
            mes.append(
                MeTally(
                    panel.program_input_id,
                    panel.preview_input_id,
                    self._mixes.get(panel.id),
                    reentry,
                    tuple(self._key_tally(k, k.id in visible_keys) for k in linear),
                    tuple(visible),
                )
            )
        return TallySnapshot(
            inputs=tuple((item.id, item.label) for item in self.list_inputs()),
            mes=tuple(mes),
        )

    def _me_layers(self, keyers: list[Keyer] | None = None) -> dict[str, MeLayers]:
        """Each ME's background (Program, both sources of a running mix) and keyers as tally sees
        them (engine/visibility.py); `keyers` as keyer_views() gives them (boxes where they are)."""
        keyers = self.keyer_views() if keyers is None else keyers
        now = time.monotonic()
        out: dict[str, MeLayers] = {}
        for panel in list(self.panels):
            background = [panel.program_input_id] if panel.program_input_id else []
            mix = self._mixes.get(panel.id)
            if mix is not None and (mix[2] is None or now < mix[2]):
                background += [source for source in mix[:2] if source and source not in background]
            held = self._held_ties.get(panel.id, [])
            layers = []
            for keyer in keyers:
                if keyer.panel_id != panel.id:
                    continue
                leaving = not keyer.enabled and now < self._key_until.get(keyer.id, 0.0)
                boxed = keyer.stage == KeyerStage.upstream and (keyer.mode == KeyMode.pip or keyer.dme.enabled)
                layers.append(
                    KeyLayer(
                        source_id=keyer.source_id,
                        keys_source=keyer.mode in (KeyMode.luma, KeyMode.pip),
                        pip=keyer.mode == KeyMode.pip,
                        upstream=keyer.stage == KeyerStage.upstream,
                        on_air=keyer.enabled or leaving or keyer.id in held,
                        # Not while it fades in (the shared vectors' fade >= 1).
                        full=keyer.enabled and keyer.id not in held and now >= self._key_in.get(keyer.id, 0.0),
                        leaving=leaving,
                        gain=keyer.gain,
                        dme=keyer.dme if boxed else None,
                        key_id=keyer.id,
                    )
                )
            out[panel.id] = MeLayers(tuple(background), tuple(layers), self.settings.width, self.settings.height)
        return out

    def program_visibility(self, keyers: list[Keyer] | None = None) -> dict[str, list[str]]:
        """Per ME what is visible on its Program through any path (engine/visibility.py): its
        background unless a full-raster pip covers it, its visible keys' sources, and what the MEs
        it shows have on their Programs, recursively."""
        mes = self._me_layers(keyers)
        return {panel.id: visible_on_program(panel.id, mes) for panel in list(self.panels)}

    def _key_tally(self, keyer: Keyer, on_air: bool) -> tuple[int, str, bool]:
        """A linear keyer's key input on the raw tally: INDEX 2000 + 100·n + k (upstream keyer k of
        ME n) or 3000 + 100·n + k (DSK k), its receiver label, on air."""
        same = [k for k in self.keyers if k.panel_id == keyer.panel_id and k.stage == keyer.stage]
        base = 2000 if keyer.stage == KeyerStage.upstream else 3000
        index = base + 100 * self._panel_number(self.get_panel(keyer.panel_id)) + same.index(keyer) + 1
        return index, keyer.receiver_label, on_air

    # ── audio (P5, docs/engine-p5.md) ────────────────────────────────────────

    def audio_channels(self) -> int:
        """The PGM's channels: the engine's while it runs (an existing flow keeps its own), else
        FLOWXER_AUDIO_CHANNELS."""
        channels = ((self.engine_status() or {}).get("audio") or {}).get("channels")
        return int(channels) if isinstance(channels, int) and channels > 0 else self.settings.audio_channels

    def audio_strips(self, panel: MixerPanel) -> list[tuple[str, str, str]]:
        """A rendering panel's strips in slot order, as (id, label, kind): its live inputs, clip
        players, the Programs of higher rendering MEs (re-entry) and the stinger."""
        strips = [(item.id, item.label, "input") for item in self.list_inputs() if item.kind == InputKind.mxl_live]
        strips += [(item.id, item.label, "clip_player") for item in self.list_inputs() if item.kind == InputKind.clip_player]
        strips += [(other.id, other.label, "panel") for other in self.reentry_sources(panel)]
        strips.append(("stinger", "Stinger", "stinger"))
        return strips

    def _audio_panel(self, panel_id: str) -> MixerPanel:
        panel = self.get_panel(panel_id)
        if not self._rendered(panel):
            raise MixerError(f"{panel.label} does not render (FLOWXER_RENDER_MES): it has no audio")
        return panel

    def audio_state(self) -> dict[str, Any]:
        """GET /audio: per live input its delay, channel map and its flow's channels; per rendering
        panel its master and strips; the PGM's channels and the longest delay."""
        engine_inputs = (self.engine_status() or {}).get("inputs") or []
        inputs: dict[str, Any] = {}
        for k, item in enumerate(item for item in self.list_inputs() if item.kind == InputKind.mxl_live):
            audio = self.audio.inputs.get(item.id) or InputAudio()
            flow = ((engine_inputs[k].get("audio") or {}).get("channels") if k < len(engine_inputs) else None) or None
            inputs[item.id] = {
                "label": item.label,
                "delay_ms": audio.delay_ms,
                "channel_map": list(audio.channel_map),
                "channels": flow or (item.audio.channels if item.audio is not None else None),
            }
        panels: dict[str, Any] = {}
        for panel in self.rendered_panels():
            audio = self.audio.panels.get(panel.id) or PanelAudio()
            strips = {}
            for strip_id, label, kind in self.audio_strips(panel):
                strip = audio.strips.get(strip_id) or AudioStrip()
                strips[strip_id] = {
                    "label": label,
                    "kind": kind,
                    "fader_db": strip.fader_db,
                    "mute": strip.mute,
                    "afv": None if kind == "stinger" else strip.afv,
                }
            panels[panel.id] = {"label": panel.label, "master_db": audio.master_db, "strips": strips}
        return {"channels": self.audio_channels(), "max_delay_ms": self.settings.audio_max_delay_ms, "inputs": inputs, "panels": panels}

    def update_input_audio(self, input_id: str, payload: InputAudioUpdate) -> dict[str, Any]:
        """A live input's delay (whole 48 kHz samples, 0 .. FLOWXER_AUDIO_MAX_DELAY_MS) and channel
        map (one entry per PGM channel, 0 silent; [] is 1:1), for every ME. The engine crossfades
        from the old reading to the new one over one grain."""
        item = self.get_input(input_id)
        if item.kind != InputKind.mxl_live:
            raise MixerError(f"{item.label} is not a live input: a delay and a channel map are for live inputs")
        current = self.audio.inputs.get(input_id) or InputAudio()
        delay = current.delay_ms if payload.delay_ms is None else float(payload.delay_ms)
        channel_map = list(current.channel_map) if payload.channel_map is None else list(payload.channel_map)
        if delay > self.settings.audio_max_delay_ms:
            raise MixerError(f"delay_ms must be 0..{self.settings.audio_max_delay_ms} (FLOWXER_AUDIO_MAX_DELAY_MS)")
        channels = self.audio_channels()
        if channel_map and len(channel_map) != channels:
            raise MixerError(f"channel_map needs one entry per PGM channel ({channels}), or none for 1:1")
        if any(value < 0 or value > 64 for value in channel_map):
            raise MixerError("channel_map entries are input channels 1..64, or 0 for silence")
        value = InputAudio(delay_ms=round(delay * AUDIO_SAMPLES_PER_MS) / AUDIO_SAMPLES_PER_MS, channel_map=channel_map)
        if value == InputAudio():
            self.audio.inputs.pop(input_id, None)
        else:
            self.audio.inputs[input_id] = value
        self._push_input_audio(item)
        return self.audio_state()["inputs"][input_id]

    def update_panel_audio(self, panel_id: str, payload: PanelAudioUpdate) -> dict[str, Any]:
        """A panel's master fader (ramps over one grain)."""
        panel = self._audio_panel(panel_id)
        audio = self.audio.panels.setdefault(panel.id, PanelAudio())
        if payload.master_db is not None:
            audio.master_db = payload.master_db
        self._tidy_panel_audio(panel.id)
        self._push_panel_audio(panel, {"master_db": audio.master_db})
        return self.audio_state()["panels"][panel.id]

    def update_audio_strip(self, panel_id: str, strip_id: str, payload: AudioStripUpdate) -> dict[str, Any]:
        """A strip's fader, mute and AFV on one panel (each change ramps over one grain)."""
        panel = self._audio_panel(panel_id)
        if strip_id not in {sid for sid, _, _ in self.audio_strips(panel)}:
            raise MixerError(f"unknown strip {strip_id} on {panel.label}")
        patch = payload.model_dump(exclude_none=True)
        if strip_id == "stinger" and "afv" in patch:
            raise MixerError("the stinger strip has no afv")
        audio = self.audio.panels.setdefault(panel.id, PanelAudio())
        strip = (audio.strips.get(strip_id) or AudioStrip()).model_copy(update=patch)
        audio.strips[strip_id] = strip
        self._tidy_panel_audio(panel.id)
        self._push_panel_audio(panel, {"strips": {self._engine_strip(strip_id): self._strip_body(strip_id, strip)}})
        return self.audio_state()["panels"][panel.id]["strips"][strip_id]

    def _tidy_panel_audio(self, panel_id: str) -> None:
        """Only what is not at its default is kept (and saved)."""
        audio = self.audio.panels.get(panel_id)
        if audio is None:
            return
        for strip_id in [sid for sid, strip in audio.strips.items() if strip == AudioStrip()]:
            del audio.strips[strip_id]
        if audio == PanelAudio():
            del self.audio.panels[panel_id]

    def _engine_strip(self, strip_id: str) -> str:
        """FlowXer's strip id → the engine's: in<k>, cp<k>, me<n>, stinger."""
        return "stinger" if strip_id == "stinger" else self._engine_source(strip_id)

    @staticmethod
    def _strip_body(strip_id: str, strip: AudioStrip) -> dict[str, Any]:
        body: dict[str, Any] = {"fader_db": strip.fader_db, "mute": strip.mute}
        if strip_id != "stinger":
            body["afv"] = strip.afv
        return body

    def _push_input_audio(self, item: LogicalInput, client: EngineClient | None = None) -> None:
        if item.kind != InputKind.mxl_live:
            return
        audio = self.audio.inputs.get(item.id) or InputAudio()
        self._engine_call(
            "POST", f"/input/{self._engine_index(item)}/audio", {"delay_ms": audio.delay_ms, "map": list(audio.channel_map)}, client=client
        )

    def _push_panel_audio(self, panel: MixerPanel, body: dict[str, Any] | None = None, client: EngineClient | None = None) -> None:
        """The panel's master and strips (all of them without `body`: after an engine start)."""
        if not self._rendered(panel):
            return
        if body is None:
            audio = self.audio.panels.get(panel.id) or PanelAudio()
            body = {
                "master_db": audio.master_db,
                "strips": {
                    self._engine_strip(sid): self._strip_body(sid, audio.strips.get(sid) or AudioStrip()) for sid, _, _ in self.audio_strips(panel)
                },
            }
        self._engine_call("POST", f"/me/{self._panel_number(panel)}/audio", body, client=client)

    def audio_meters(self) -> dict[str, Any]:
        """GET /audio/meters: the engine's last 100 ms block with FlowXer's ids (live inputs and clip
        players by id, stinger players as stinger-<k>, panels by id with their strips). At most
        100 ms old, one engine call however many viewers poll. Off-air (or in simulate) every
        meter is silence (-120) and each strip shows what its settings and AFV give."""
        with self._meters_lock:
            now = time.monotonic()
            if now - self._meters_at >= METERS_CACHE_S:
                raw = None
                if self.engine is not None and self.engine.ready:
                    try:
                        raw = self.engine.call("GET", "/audio/meters")
                    except EngineError:
                        raw = None
                self._meters_raw, self._meters_at = raw if isinstance(raw, dict) else None, now
            raw = self._meters_raw
        return self._meters_view(raw)

    def _meters_view(self, raw: dict[str, Any] | None) -> dict[str, Any]:
        channels = self.audio_channels()
        floor = [METER_FLOOR_DB] * channels
        live = [item for item in self.list_inputs() if item.kind == InputKind.mxl_live]
        players = [item for item in self.list_inputs() if item.kind == InputKind.clip_player]
        engine_sources = (raw or {}).get("sources") or {}
        sources: dict[str, Any] = {}
        for prefix, items in (("in", live), ("cp", players)):
            for k, item in enumerate(items, 1):
                entry = engine_sources.get(f"{prefix}{k}") or {}
                sources[item.id] = {"peak_dbfs": entry.get("peak_dbfs", floor), "rms_dbfs": entry.get("rms_dbfs", floor)}
        for k in range(1, self._stinger_players() + 1):
            entry = engine_sources.get(f"st{k}") or {}
            sources[f"stinger-{k}"] = {"peak_dbfs": entry.get("peak_dbfs", floor), "rms_dbfs": entry.get("rms_dbfs", floor)}
        engine_mes = (raw or {}).get("mes") or {}
        panels: dict[str, Any] = {}
        layers = self._me_layers() if raw is None else {}
        pairs = (channels + 1) // 2
        for panel in self.rendered_panels():
            entry = engine_mes.get(str(self._panel_number(panel))) or {}
            engine_strips = entry.get("strips") or {}
            audio = self.audio.panels.get(panel.id) or PanelAudio()
            strips = {}
            for strip_id, _label, kind in self.audio_strips(panel):
                measured = engine_strips.get(self._engine_strip(strip_id))
                if measured is None:
                    setting = audio.strips.get(strip_id) or AudioStrip()
                    shown = direct_visibility(layers[panel.id]) if panel.id in layers else [panel.program_input_id]
                    on_air = kind != "stinger" and (not setting.afv or strip_id in shown)
                    gain = setting.fader_db if on_air and not setting.mute and setting.fader_db > -90.0 else -90.0
                    measured = {"gain_db": gain, "heard": gain > -90.0}
                strips[strip_id] = measured
            panels[panel.id] = {
                "peak_dbfs": entry.get("peak_dbfs", floor),
                "true_peak_dbtp": entry.get("true_peak_dbtp", floor),
                "rms_dbfs": entry.get("rms_dbfs", floor),
                "loudness": entry.get("loudness")
                or [{"pair": p, "momentary": METER_FLOOR_DB, "short_term": METER_FLOOR_DB, "integrated": METER_FLOOR_DB} for p in range(1, pairs + 1)],
                "overs": int(entry.get("overs") or 0),
                "strips": strips,
            }
        return {"block": (raw or {}).get("block"), "channels": channels, "sources": sources, "panels": panels}

    def reset_loudness(self, panel_id: str) -> dict[str, Any]:
        """The panel's integrated loudness from now on (the engine's next 100 ms block)."""
        panel = self._audio_panel(panel_id)
        self._engine_call("POST", f"/me/{self._panel_number(panel)}/audio/loudness/reset", {})
        with self._meters_lock:
            self._meters_at = 0.0
        return {"panel_id": panel.id, "loudness_reset": True}

    def _prune_audio(self) -> None:
        """When the structure changes: the audio of inputs that are no live input any more, of panels
        that no longer render and of strips a panel no longer has is dropped (as keyers are)."""
        live = {item.id for item in self.list_inputs() if item.kind == InputKind.mxl_live}
        for input_id in [i for i in self.audio.inputs if i not in live]:
            del self.audio.inputs[input_id]
        rendered = {panel.id: panel for panel in self.rendered_panels()}
        for panel_id in list(self.audio.panels):
            panel = rendered.get(panel_id)
            if panel is None:
                del self.audio.panels[panel_id]
                continue
            valid = {sid for sid, _, _ in self.audio_strips(panel)}
            strips = self.audio.panels[panel_id].strips
            for strip_id in [s for s in strips if s not in valid]:
                del strips[strip_id]
            self._tidy_panel_audio(panel_id)

    # ── snapshots (docs/snapshots.md) ────────────────────────────────────────

    def snapshots_view(self) -> dict[str, Any]:
        """GET /snapshots (and the console): every snapshot without its contents, by number, and
        the start-up snapshot."""
        return {
            "startup": self.snapshots.startup,
            "snapshots": [item.model_dump(mode="json", exclude={"mes"}) for item in self.snapshots.items],
        }

    def get_snapshot(self, number: int) -> Snapshot:
        for item in self.snapshots.items:
            if item.number == number:
                return item
        raise MixerError(f"unknown snapshot {number}")

    @staticmethod
    def _snapshot_name(name: str) -> str:
        name = name.strip()
        if not name:
            raise MixerError("a snapshot needs a name")
        return name

    def store_snapshot(self, payload: SnapshotStore) -> Snapshot:
        """The current state of one ME (or of every ME) into `number`, by default the lowest free
        one. A used number is stored over: it keeps its name and options unless the request gives
        new ones."""
        panels = list(self.panels) if payload.panel_id == SNAPSHOT_ALL else [self.get_panel(payload.panel_id)]
        used = {item.number: item for item in self.snapshots.items}
        number = payload.number or next((n for n in range(1, SNAPSHOT_MAX + 1) if n not in used), None)
        if number is None:
            raise MixerError(f"every snapshot number (1..{SNAPSHOT_MAX}) is used: store over one or delete one")
        old = used.get(number)
        if payload.name is not None:
            name = self._snapshot_name(payload.name)
        elif old is not None:
            name = old.name
        else:
            name = f"All MEs #{number}" if payload.panel_id == SNAPSHOT_ALL else f"{panels[0].label} #{number}"
        options = old.options if old is not None else SnapshotOptions()
        if payload.options is not None:
            options = options.model_copy(update=payload.options.model_dump(exclude_none=True))
        snapshot = Snapshot(
            number=number,
            name=name,
            panel_id=payload.panel_id,
            options=options,
            stored_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            mes={panel.id: self._capture_me(panel) for panel in panels},
        )
        self.snapshots.items = sorted([item for item in self.snapshots.items if item.number != number] + [snapshot], key=lambda item: item.number)
        return snapshot

    def _capture_me(self, panel: MixerPanel) -> MeSnapshot:
        fields = set(SnapshotKeyer.model_fields)
        audio = (self.audio.panels.get(panel.id) or PanelAudio()).model_copy(deep=True) if self._rendered(panel) else None
        return MeSnapshot(
            program_input_id=panel.program_input_id,
            preview_input_id=panel.preview_input_id,
            ftb=panel.ftb,
            transition=panel.transition.model_copy(deep=True),
            keyers={k.id: SnapshotKeyer.model_validate(k.model_dump(include=fields)) for k in self.keyers if k.panel_id == panel.id},
            audio=audio,
        )

    def update_snapshot(self, number: int, payload: SnapshotUpdate) -> Snapshot:
        """Rename it, or change the defaults of its recall."""
        snapshot = self.get_snapshot(number)
        if payload.name is not None:
            snapshot.name = self._snapshot_name(payload.name)
        if payload.options is not None:
            snapshot.options = snapshot.options.model_copy(update=payload.options.model_dump(exclude_none=True))
        return snapshot

    def delete_snapshot(self, number: int) -> None:
        self.snapshots.items.remove(self.get_snapshot(number))
        if self.snapshots.startup == number:
            self.snapshots.startup = None

    def set_startup_snapshot(self, number: int | None) -> dict[str, Any]:
        """The snapshot recalled (with its options) when the mixer starts; None: none."""
        if number is not None:
            self.get_snapshot(number)
        self.snapshots.startup = number
        return self.snapshots_view()

    def recall_snapshot(self, number: int, overrides: SnapshotOptionsUpdate | None = None) -> dict[str, Any]:
        """Recall through the paths of the single changes, so the engine, tally and the saved state
        stay consistent. Always: each ME's Preview, its transition and the settings of its keyers
        that are off air. With `program`: also Program (a cut), the keyers' and DSKs' on or off
        (cuts), the settings of keyers on air and FTB (a cut). With `audio`: also the ME's strips
        and master (each ramps over one grain). What no longer fits the console (a source, ME,
        keyer or stinger slot that is gone) is skipped and reported, as are keyers on air without
        `program`. Off-air it only changes the state, which the engine gets when it starts."""
        snapshot = self.get_snapshot(number)
        options = snapshot.options
        if overrides is not None:
            options = options.model_copy(update=overrides.model_dump(exclude_none=True))
        present = {panel.id for panel in self.panels}
        if snapshot.panel_id != SNAPSHOT_ALL and snapshot.panel_id not in present:
            raise MixerError(f"snapshot {number} is of {snapshot.panel_id}, which the console does not have")
        skipped = [f"{panel_id}: not on the console" for panel_id in snapshot.mes if panel_id not in present]
        # Higher MEs first: a lower ME that re-enters one takes its new Program at once.
        for panel in reversed(self.panels):
            if panel.id in snapshot.mes:
                self._recall_me(panel, snapshot.mes[panel.id], options, skipped)
        if skipped:
            log.info("snapshot %d (%s) skipped: %s", number, snapshot.name, "; ".join(skipped))
        info = snapshot.model_dump(mode="json", exclude={"mes"})
        return {"snapshot": info, "options": options.model_dump(), "skipped": skipped}

    def _recall_startup(self) -> None:
        number = self.snapshots.startup
        try:
            result = self.recall_snapshot(number)
        except MixerError as exc:
            log.error("start-up snapshot %s not recalled: %s", number, exc)
            return
        log.info("start-up snapshot %s (%s) recalled", number, result["snapshot"]["name"])

    def _key_on_air(self, keyer: Keyer) -> bool:
        """On, fading or flying off, or tied to a transition the T-bar holds (as the raw tally)."""
        held = self._held_ties.get(keyer.panel_id, [])
        return keyer.enabled or time.monotonic() < self._key_until.get(keyer.id, 0.0) or keyer.id in held

    def _recall_me(self, panel: MixerPanel, stored: MeSnapshot, options: SnapshotOptions, skipped: list[str]) -> None:
        """One ME of a recall. With Program/on-air, FTB going on comes first and FTB going off
        last, and keyers go off before and on after the rest, so the new look shows at once."""
        on_air = options.program
        current = {keyer.id: keyer for keyer in self.keyers if keyer.panel_id == panel.id}
        skipped += [f"{panel.label}: keyer {keyer_id} is not on the console" for keyer_id in stored.keyers if keyer_id not in current]
        keyers = [(current[keyer_id], item) for keyer_id, item in stored.keyers.items() if keyer_id in current]

        def attempt(what: str, change, *args) -> None:
            try:
                change(*args)
            except MixerError as exc:
                skipped.append(f"{what}: {exc}")

        if on_air and stored.ftb and not panel.ftb:
            self._cut_ftb(panel, True)
        if on_air:
            for keyer, item in keyers:
                if not item.enabled and self._key_on_air(keyer):
                    attempt(keyer.label, self._cut_key, keyer, False)
            if stored.program_input_id and stored.program_input_id != panel.program_input_id:
                attempt(f"{panel.label} Program", self._cut_program, panel, stored.program_input_id)
        if stored.preview_input_id and stored.preview_input_id != panel.preview_input_id:
            attempt(f"{panel.label} Preview", self._set_preview, panel, stored.preview_input_id)
        if panel.id in self._positions:
            skipped.append(f"{panel.label} transition: kept, the T-bar holds a transition")
        else:
            attempt(f"{panel.label} transition", self._recall_transition, panel, stored.transition, skipped)
        for keyer, item in keyers:
            if not on_air and self._key_on_air(keyer):
                skipped.append(f"{keyer.label}: kept, it is on air (recall with Program/on-air to change it)")
                continue
            attempt(keyer.label, self._recall_keyer, keyer, item)
        if on_air:
            for keyer, item in keyers:
                if item.enabled and not keyer.enabled:
                    attempt(keyer.label, self._cut_key, keyer, True, item)
            if not stored.ftb and panel.ftb:
                self._cut_ftb(panel, False)
        if options.audio and stored.audio is not None and self._rendered(panel):
            self._recall_audio(panel, stored.audio)
        elif options.audio and (stored.audio is not None or self._rendered(panel)):
            skipped.append(f"{panel.label} audio: it {'no longer renders' if stored.audio is not None else 'did not render when stored'}")

    def _cut_ftb(self, panel: MixerPanel, on: bool) -> None:
        panel.ftb = on
        if self._rendered(panel):
            self._engine_call("POST", f"/me/{self._panel_number(panel)}/ftb", {"on": on})

    def _cut_program(self, panel: MixerPanel, source_id: str) -> None:
        self._check_source(panel, source_id)
        self._put_on_program(panel, source_id, TransitionType.cut, flip_flop=False)

    def _cut_key(self, keyer: Keyer, on: bool, item: SnapshotKeyer | None = None) -> None:
        """A keyer on or off at the next frame; a keyer that flies cuts on at its last keyframe, so
        its stored box follows when it was elsewhere."""
        self.update_keyer(keyer.id, enabled=on)
        if item is not None and keyer.stage == KeyerStage.upstream and keyer.dme != item.dme:
            self.replace_keyer_box(keyer.id, item.dme)

    def _recall_transition(self, panel: MixerPanel, stored: PanelTransition, skipped: list[str]) -> None:
        data = stored.model_dump()
        upstream = [k.id for k in self.keyers if k.panel_id == panel.id and k.stage == KeyerStage.upstream]
        data["ties"] = [tie for tie in data["ties"] if tie in upstream]
        data["background"] = data["background"] or not data["ties"]
        slot = data["stinger_slot_id"]
        if slot is not None and not any(item.id == slot for item in self.stinger_slots):
            skipped.append(f"{panel.label} transition: stinger slot {slot} is not on the console")
            data["stinger_slot_id"] = None
        if data != panel.transition.model_dump():
            self.update_transition(panel.id, PanelTransitionUpdate(**data))

    def _recall_keyer(self, keyer: Keyer, item: SnapshotKeyer) -> None:
        """Its rate, key and box (keyframes first: a box that flies needs them)."""
        if item.keyframes != keyer.keyframes:
            self.set_keyframes(keyer.id, [frame.model_dump(mode="json") for frame in item.keyframes])
        changes = {
            name: getattr(item, name)
            for name in ("rate_ms", "mode", "source_id", "clip", "soft", "gain", "invert", "fly")
            if getattr(item, name) != getattr(keyer, name)
        }
        if item.dme != keyer.dme:
            changes["dme"] = item.dme.model_dump(mode="json")
        if changes:
            self.update_keyer(keyer.id, **changes)

    def _recall_audio(self, panel: MixerPanel, stored: PanelAudio) -> None:
        """The ME's master and strips (strips the snapshot does not name: their defaults)."""
        strips = {sid for sid, _, _ in self.audio_strips(panel)}
        audio = PanelAudio(master_db=stored.master_db, strips={sid: strip.model_copy() for sid, strip in stored.strips.items() if sid in strips})
        if audio == (self.audio.panels.get(panel.id) or PanelAudio()):
            return
        self.audio.panels[panel.id] = audio
        self._tidy_panel_audio(panel.id)
        self._push_panel_audio(panel)

    # ── saved state and config export/import ─────────────────────────────────

    def export_state(self) -> dict:
        """Inputs, layout, keyers, stingers, tally, IS-05 routes, audio and snapshots. Holds no
        secrets."""
        # The NMOS lock guards the state: IS-05 activations hold it when they persist.
        with self.nmos.lock:
            slots = [
                slot.model_dump(mode="json", exclude={"media_path", "ready"} if slot.library_item_id else None)
                for slot in self.stinger_slots
            ]
            return {
                "format": STATE_FORMAT,
                "flowxer_version": __version__,
                "workspace": self.workspace.model_dump(mode="json"),
                "inputs": [item.model_dump(mode="json") for item in self.list_inputs()],
                "panels": [panel.model_dump(mode="json") for panel in self.panels],
                "keyers": [keyer.model_dump(mode="json", exclude={"run"}) for keyer in self.keyers],
                "stinger_slots": slots,
                "tally_receivers": [item.model_dump(mode="json") for item in self.tally.receivers],
                "nmos_receivers": self.nmos.export_routes(),
                "audio": self.audio.model_dump(mode="json"),
                "snapshots": self.snapshots.model_dump(mode="json"),
            }

    def import_state(self, data: dict) -> dict:
        """Restore an export_state() document (POST /config/import). The mixer must be stopped."""
        if self.state == MixerState.running:
            raise MixerError("stop the mixer before importing a configuration")
        self._cancel_tally_timers()
        self._apply_state(data, checked=True)
        self._publish_tally()
        self.persist()
        return self.export_state()

    def persist(self) -> None:
        with self.nmos.lock:
            self._store.save(self.export_state())
        self._persist_at = time.monotonic()

    def persist_soon(self) -> None:
        """Audio changes (a moving fader sends up to 20 a second): saved at most once a second, the
        last one within a second."""
        with self._persist_lock:
            if self._persist_timer is not None:
                return
            wait = self._persist_at + PERSIST_INTERVAL_S - time.monotonic()
            if wait > 0:
                self._persist_timer = threading.Timer(wait, self._persist_pending)
                self._persist_timer.daemon = True
                self._persist_timer.start()
                return
        self.persist()

    def _persist_pending(self) -> None:
        with self._persist_lock:
            if self._persist_timer is None:
                return
            self._persist_timer = None
        self.persist()

    def _restore_state(self) -> None:
        saved = self._store.load()
        if saved is None:
            return
        try:
            # Written by this function: no DNS lookups, so a resolver hiccup keeps the state.
            self._apply_state(saved, checked=False)
        except MixerError as exc:
            log.error("ignoring %s, starting with defaults: %s", self._store.path, exc)
            return
        log.info("restored the mixer state from %s", self._store.path)

    def _apply_state(self, data: dict, *, checked: bool) -> None:
        """Validate everything first, then replace the state. `checked` applies the
        storage and egress checks of the single-object API calls."""
        if not isinstance(data, dict) or data.get("format") != STATE_FORMAT:
            raise MixerError(f"not a {STATE_FORMAT} document")
        try:
            workspace = self._pinned(WorkspaceConfig(**dict(data.get("workspace") or {})))
            fmt = format_by_id(workspace.format_id)
            data = copy.deepcopy(data)
            # Crops in pixels of another format (a pinned FLOWXER_FORMAT, an import from another
            # raster) keep their share of the picture. A missing or unknown saved format: as is.
            saved_id = str((data.get("workspace") or {}).get("format_id") or "")
            try:
                saved = format_by_id(saved_id) if saved_id else None
            except (KeyError, ValueError):
                saved = None
                log.warning("the saved format %r is unknown: DME crops are taken as pixels of %s", saved_id, fmt.id)
            if saved is not None and (saved.width, saved.height) != (fmt.width, fmt.height):
                scaled = scale_crops(data, fmt.width / saved.width, fmt.height / saved.height, legacy_only=False)
                if scaled:
                    log.info("scaled %d DME crops from %s to %s", scaled, saved.id, fmt.id)
            # DME crops saved as fractions (before 2026-10-22) become pixels of the raster.
            migrated = scale_crops(data, fmt.width, fmt.height, legacy_only=True)
            if migrated:
                log.info("converted %d DME crops from fractions to pixels of %dx%d", migrated, fmt.width, fmt.height)
            if not checked:
                # A restore keeps the rest of the state: a crop the raster cannot take goes.
                for fixed in repair_crops(data, fmt.width, fmt.height):
                    log.warning("the saved state's DME %s leaves no picture on %s: no crop there", fixed, fmt.id)
            else:
                # An import refuses one, the snapshots' too (repair_crops on a copy finds them).
                bad = repair_crops(copy.deepcopy(data), fmt.width, fmt.height)
                if bad:
                    raise ValueError(f"DME {bad[0]}: the crop must leave a picture on {fmt.id}")
            inputs = [LogicalInput(**item) for item in data.get("inputs") or []]
            panels = [MixerPanel(**item) for item in data.get("panels") or []]
            keyers = [Keyer(**item) for item in data.get("keyers") or []]
            for keyer in keyers:
                for crop in [keyer.dme.crop, *(frame.crop for frame in keyer.keyframes)]:
                    error = _crop_error(crop, fmt.width, fmt.height)
                    if error:
                        raise ValueError(f"{keyer.id}: {error}")
            slots = [StingerSlot(**item) for item in data.get("stinger_slots") or []]
            tally = [TallyReceiver(**item) for item in data.get("tally_receivers") or []]
            routes = list(data.get("nmos_receivers") or [])
            # P5: missing (a P4 state) is the defaults.
            audio = AudioConfig(**dict(data.get("audio") or {}))
            # Missing (a state from before snapshots): none.
            snapshots = SnapshotConfig(**dict(data.get("snapshots") or {}))
            for item in inputs:
                if item.library_item_id:
                    require_safe_id(item.library_item_id, what="library item id")
            for slot in slots:
                if slot.library_item_id:
                    require_safe_id(slot.library_item_id, what="library item id")
                    slot.stinger_id = slot.library_item_id
                    slot.media_path = None
                    continue
                require_safe_id(slot.stinger_id, what="stinger id")
                if slot.media_path:
                    contained_path(slot.media_path, self.settings.clips_dir, self.settings.stingers_dir)
        except (ValidationError, SecurityError, TypeError, ValueError) as exc:
            raise MixerError(f"invalid configuration: {exc}") from exc
        if len({item.id for item in inputs}) != len(inputs):
            raise MixerError("invalid configuration: duplicate input ids")
        with self.nmos.lock:
            if checked:
                try:
                    self.tally.replace(tally)
                except ValueError as exc:
                    raise MixerError(f"invalid configuration: {exc}") from exc
            else:
                self.tally.receivers = tally
            self.settings.width = fmt.width
            self.settings.height = fmt.height
            self.settings.frame_rate_num = fmt.frame_rate_num
            self.settings.frame_rate_den = fmt.frame_rate_den
            self.workspace = workspace
            # A loaded state may have the T-bar at another end: hardware levers pick up again.
            self._arm_lever_pickup()
            self.inputs = {item.id: item for item in inputs}
            self.panels = panels
            self.keyers = keyers
            self.stinger_slots = slots
            self.audio = audio
            self.snapshots = snapshots
            main = panels[0] if panels else None
            self.program_input_id = main.program_input_id if main else None
            self.preview_input_id = main.preview_input_id if main else None
            self._sync_sources()
            self._sync_panels()
            self._sync_keyers()
            self._sync_stinger_slots()
            self._prune_audio()
            if self.panels:
                self.program_input_id = self.panels[0].program_input_id
                self.preview_input_id = self.panels[0].preview_input_id
            self.refresh_library_bindings()
            for item in self.inputs.values():
                if item.library_item_id and self.library.get(item.library_item_id) is None:
                    log.warning("input %s: library clip %s is missing", item.id, item.library_item_id)
            for slot in self.stinger_slots:
                if slot.library_item_id and self.library.get(slot.library_item_id) is None:
                    log.warning("stinger slot %s: library stinger %s is missing", slot.id, slot.library_item_id)
            self.nmos.reconcile_inputs()
            self.nmos.import_routes(routes)

    # ── status ───────────────────────────────────────────────────────────────

    def status(self) -> MixerStatus:
        return MixerStatus(
            state=self.state,
            backend=self.backend,
            program_input_id=self.program_input_id,
            preview_input_id=self.preview_input_id,
            last_live_input_id=self.last_live_input_id,
            program_bus=self.program_bus,
            stinger=self._stinger_status(),
            outputs=self.outputs,
            me_outputs=list(self.me_outputs.values()),
            render_mes=self.render_count,
            latency_grains=self.settings.latency,
            raster=self.settings.raster,
            frame_rate=self.settings.frame_rate,
            video_format=self.settings.video_media_type,
            audio_format=self.settings.audio_media_type,
            error=self._status_error(),
            workspace=self.workspace.model_dump(),
            panels=[panel.model_dump() for panel in self.panels],
            keyers=[keyer.model_dump() for keyer in self.keyers],
            stinger_slots=[slot.model_dump() for slot in self.stinger_slots],
            wipe_armed=bool(self.panels[0].wipe_armed) if self.panels else False,
            last_transition=self.last_transition,
            nmos=self.nmos.status(),
            engine=self.engine.summary() if self.engine is not None else {"state": "simulate" if self.backend == "simulate" else "stopped"},
        )

    def _status_error(self) -> str | None:
        # The state stays "running" (on air: the GUI's on-air switch and start() depend on it).
        stalled = self.program_stalled_s()
        if stalled is not None and self.error is None:
            return f"Program renders no frames (none for {stalled:.0f} s)"
        if self.engine is not None and self.engine.state == "restarting":
            return f"the media engine restarts ({self.engine.last_error or 'it exited'})"
        return self.error

    def source_runtime(self) -> dict[str, dict[str, Any]]:
        """Per input id what the engine reports (state, arrival lag, clip player state)."""
        status = self.engine_status() or {}
        live = [item for item in self.list_inputs() if item.kind == InputKind.mxl_live]
        players = [item for item in self.list_inputs() if item.kind == InputKind.clip_player]
        out: dict[str, dict[str, Any]] = {}
        for item, entry in zip(live, status.get("inputs") or []):
            audio = entry.get("audio") or {}
            out[item.id] = {
                "state": entry.get("state"),
                "audio_state": audio.get("state"),
                "arrival_ms": entry.get("arrival_ms"),
                "audio_lag_ms": audio.get("lag_ms"),
                "late": entry.get("late"),
                "missing": entry.get("missing"),
                "audio_missing": audio.get("missing"),
            }
        for item, entry in zip(players, status.get("players") or []):
            out[item.id] = {key: value for key, value in entry.items() if key not in {"player", "label"}}
        for keyer, entry in zip(self.keyers, status.get("keys") or []):
            out[keyer.id] = {"state": entry.get("state"), "arrival_ms": entry.get("arrival_ms")}
        return out

    def domain_flows(self):
        return list_flows_in_root(self.settings.mxl_root)
