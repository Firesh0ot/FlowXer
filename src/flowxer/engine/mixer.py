from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any
from uuid import UUID

from pydantic import ValidationError

from flowxer import __version__
from flowxer.api.schemas import (
    AudioEssence,
    InputKind,
    Keyer,
    KeyerStage,
    KeyMode,
    LogicalInput,
    LogicalInputCreate,
    LogicalInputUpdate,
    MeOutputs,
    MixerPanel,
    MixerStartRequest,
    MixerState,
    MixerStatus,
    OutputFlows,
    PanelTransition,
    PanelTransitionUpdate,
    ProgramBus,
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


def _autostart_pause(attempt: int) -> float:
    return AUTOSTART_BACKOFF_S[min(attempt, len(AUTOSTART_BACKOFF_S)) - 1]


def _frames(duration_ms: int, fps: float) -> int:
    """A transition duration in grains (at least 1)."""
    return max(1, int(round(duration_ms * fps / 1000.0)))


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
        self._lock = threading.RLock()
        # (input id, essence) -> MXL domain id of a route whose domain is missing; routed once it
        # appears.
        self._missing_domains: dict[tuple[str, str], str] = {}
        # Keyer id -> (MXL domain id, flow id) of its key input (IS-05), flow None when not routed.
        self._key_routes: dict[str, tuple[str | None, str | None]] = {}
        # Set by a Program start and by a stop: FLOWXER_PROGRAM_AUTOSTART stops trying.
        self._autostart_done = threading.Event()
        self.tally = TallyService()
        # FLOWXER_TALLY_TSL, started at the end of __init__.
        self.tally_export: TallyExport | None = None
        # Panel id -> (outgoing, incoming, monotonic end or None while a T-bar holds it) of its last
        # mix or wipe, for the raw tally.
        self._mixes: dict[str, tuple[str | None, str, float | None]] = {}
        # Panel id -> (position, outgoing, incoming) of a transition the T-bar holds.
        self._positions: dict[str, tuple[float, str | None, str]] = {}
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
        self.nmos.reconcile_inputs()
        self._publish_tally()
        self.refresh_library_bindings()
        return self.workspace

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

    def update_keyer(self, keyer_id: str, **changes: Any) -> Keyer:
        """On or off (at the next frame), rate, label, and the key: mode, luma source, clip, soft,
        gain, invert."""
        keyer = self.get_keyer(keyer_id)
        label = changes.pop("label", None)
        enabled = changes.pop("enabled", None)
        if label is not None and label != keyer.label:
            if self._labels_pinned():
                raise MixerError("keyer labels are fixed while FLOWXER_KEYERS_PER_ME or FLOWXER_DSKS_PER_ME sets the keyers")
            keyer.label = label
            # Its key receiver carries the label.
            self.nmos.mark_changed()
        key = {name: value for name, value in changes.items() if name != "rate_ms" and value is not None}
        if key:
            try:
                updated = Keyer.model_validate({**keyer.model_dump(), **key})
            except ValidationError as exc:
                raise MixerError(f"invalid key: {exc.errors()[0]['msg']}") from exc
            if updated.mode == KeyMode.luma:
                if not updated.source_id:
                    raise MixerError("a luma key needs a source")
                self._check_source(self.get_panel(keyer.panel_id), updated.source_id)
            for name in key:
                setattr(keyer, name, getattr(updated, name))
            self._push_key_settings(keyer)
        if changes.get("rate_ms") is not None:
            keyer.rate_ms = changes["rate_ms"]
        if enabled is not None:
            keyer.enabled = enabled
            self._engine_call("POST", f"/keyer/{self._keyer_number(keyer)}", {"on": enabled})
        self._publish_tally()
        return keyer

    def keyer_auto(self, keyer_id: str, duration_ms: int | None = None) -> Keyer:
        """Fade the keyer on (or off when it is on) over duration_ms (default: its rate)."""
        keyer = self.get_keyer(keyer_id)
        keyer.enabled = not keyer.enabled
        frames = _frames(duration_ms or keyer.rate_ms, self.settings.fps)
        self._engine_call("POST", f"/keyer/{self._keyer_number(keyer)}/auto", {"frames": frames})
        self._publish_tally()
        return keyer

    def _push_key_settings(self, keyer: Keyer, client: EngineClient | None = None) -> None:
        body: dict[str, Any] = {"mode": keyer.mode.value, "clip": keyer.clip, "soft": keyer.soft, "gain": keyer.gain, "invert": keyer.invert}
        if keyer.mode == KeyMode.luma:
            body["source"] = self._engine_source(keyer.source_id)
        self._engine_call("POST", f"/keyer/{self._keyer_number(keyer)}/settings", body, client=client)

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

    # ── stingers (configuration; stinger playback comes back in P3) ─────────

    def assign_stinger_slot(self, slot_id: str, stinger_id: str) -> StingerSlot:
        return self.configure_stinger_slot(slot_id, StingerSlotUpdate(stinger_id=stinger_id))

    def get_stinger_slot(self, slot_id: str) -> StingerSlot:
        for item in self.stinger_slots:
            if item.id == slot_id:
                return item
        raise MixerError(f"unknown stinger slot {slot_id}")

    def configure_stinger_slot(self, slot_id: str, payload: StingerSlotUpdate) -> StingerSlot:
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

    def _engine_call(self, method: str, path: str, body: dict[str, Any], client: EngineClient | None = None) -> None:
        """A command to the engine. While the engine is not up (off-air, restarting) nothing is
        sent: its state is pushed when it is ready. A refused command is logged."""
        try:
            if client is not None:
                client.request(method, path, body)
            elif self.engine is not None and self.engine.ready:
                self.engine.call(method, path, body)
        except EngineError as exc:
            log.warning("%s", exc)
            self.error = str(exc)

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
            "FLOWXER_ENGINE_PVW_OUTPUTS": self.settings.pvw_outputs,
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
            "FLOWXER_ENGINE_LABEL": self.settings.node_label,
            "FLOWXER_ENGINE_GPU": self.settings.engine_gpu,
            "FLOWXER_ENGINE_HTTP_PORT": self.settings.engine_port,
        }

    def _on_engine_ready(self, client: EngineClient) -> None:
        """Every (re)start of the engine: push routes, clip players, keyers and buses."""
        with self.nmos.lock:
            # A new engine holds no T-bar transition: Program stays where it was.
            for panel_id in self._positions:
                self._mixes.pop(panel_id, None)
            self._positions.clear()
            for item in self.list_inputs():
                if item.kind == InputKind.mxl_live:
                    self._push_route(item, client)
                elif item.kind == InputKind.clip_player:
                    self._push_player(item, client)
            for keyer in self.keyers:
                self._push_key(keyer, client)
                self._push_key_settings(keyer, client)
                self._engine_call("POST", f"/keyer/{self._keyer_number(keyer)}", {"on": keyer.enabled}, client=client)
            for panel in self.rendered_panels():
                self._push_panel(panel, client)

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
        program = request.program_input_id or self._default_program_id()
        preview = request.preview_input_id or program
        for source in (program, preview):
            if source is not None and self.panels:
                self._check_source(self.panels[0], source)
        self.program_input_id = program
        self.preview_input_id = preview
        if self.panels:
            self.panels[0].program_input_id = program
            self.panels[0].preview_input_id = preview
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
        self.state = MixerState.running
        self._autostart_done.set()
        self._frame_mark = (self.frames_rendered, time.monotonic())
        self._stall_logged = False
        self.last_live_input_id = program
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
        self.state = MixerState.idle
        self.backend = "idle"
        self._mixes.clear()
        self._publish_tally()
        return self.status()

    def shutdown(self) -> None:
        """SIGTERM: stop the engine (it releases the MXL readers and writers), deregister from
        the registry, then remove the own output domain when MXL_CLEANUP_ON_EXIT is set."""
        try:
            self.stop()
        except Exception:
            log.exception("stopping the media engine failed")
        try:
            self.library.stop()
        except Exception:
            log.exception("stopping the media library failed")
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
        (and its rate without a duration). A stinger is a cut in this version."""
        if self.state != MixerState.running:
            self.start(MixerStartRequest(program_input_id=input_id, preview_input_id=input_id))
        panel = self.get_panel(panel_id)
        self._check_source(panel, input_id)
        if transition == TransitionType.stinger:
            self._note_stinger_cut()
            transition = TransitionType.cut
        if transition == TransitionType.wipe and not frames and not duration_ms:
            frames = panel.transition.rate_frames
        self._put_on_program(panel, input_id, transition, flip_flop=False, duration_ms=duration_ms, frames=frames or 0)
        return self.status()

    def cut(self, panel_id: str = "me-1") -> MixerStatus:
        """Preview onto Program, Program onto Preview."""
        panel = self._ensure_running_panel(panel_id)
        incoming = self._preview_id(panel)
        self._put_on_program(panel, incoming, TransitionType.cut, flip_flop=True)
        if panel.wipe_armed:
            panel.wipe_armed = False
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
        """Change the ME's next transition (type, rate, wipe settings, stinger slot)."""
        panel = self.get_panel(panel_id)
        data = panel.transition.model_dump()
        update = payload.model_dump(exclude_unset=True)
        data["wipe"].update(update.pop("wipe", None) or {})
        data.update(update)
        if data.get("stinger_slot_id") is not None:
            self.get_stinger_slot(data["stinger_slot_id"])
        try:
            panel.transition = PanelTransition(**data)
        except ValidationError as exc:
            raise MixerError(f"invalid transition: {exc.errors()[0]['msg']}") from exc
        self._push_transition(panel)
        return panel.transition

    def auto(self, panel_id: str = "me-1", frames: int | None = None) -> MixerStatus:
        """The ME's next transition from Preview to Program over its rate (or `frames`), then the
        buses swap. A stinger is a cut in this version."""
        panel = self._ensure_running_panel(panel_id)
        held = self._positions.pop(panel.id, None)
        if held is not None:
            # From the held T-bar position: the engine runs the rest at the rate.
            position, _outgoing, incoming = held
            rate = frames or panel.transition.rate_frames
            if self._rendered(panel):
                self._engine_call("POST", f"/me/{self._panel_number(panel)}/auto", {"frames": rate})
            rest = max(1, round(rate * (1 - position)))
            swap = panel.preview_input_id == incoming
            self._put_on_program(panel, incoming, panel.transition.type, flip_flop=swap, frames=rest, send=False)
            return self.status()
        incoming = self._preview_id(panel)
        transition = panel.transition.type
        if transition == TransitionType.stinger:
            self._note_stinger_cut()
            transition = TransitionType.cut
        self._put_on_program(panel, incoming, transition, flip_flop=True, frames=frames or panel.transition.rate_frames)
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
            if panel.transition.type == TransitionType.stinger:
                return self.auto(panel.id)
            held = (position, panel.program_input_id, self._preview_id(panel))
            # Tally: both sources are on air while the T-bar holds the transition.
            self._mixes[panel.id] = (held[1], held[2], None)
        if self._rendered(panel):
            self._engine_call("POST", f"/me/{self._panel_number(panel)}/position", {"t": position})
        _position, outgoing, incoming = held
        if position >= 1:
            del self._positions[panel.id]
            swap = panel.preview_input_id == incoming
            self._put_on_program(panel, incoming, panel.transition.type, flip_flop=swap, frames=1, send=False)
        elif position <= 0:
            self._positions.pop(panel.id, None)
            self._mixes.pop(panel.id, None)
            self._publish_tally()
        else:
            self._positions[panel.id] = (position, outgoing, incoming)
            self._publish_tally()
        return self.status()

    def set_wipe(self, panel_id: str = "me-1", armed: bool | None = None) -> MixerStatus:
        """Arm (or toggle) Wipe. Wipes and stingers come in P3: an armed Wipe does not change the
        next Cut yet."""
        panel = self._ensure_running_panel(panel_id)
        panel.wipe_armed = (not panel.wipe_armed) if armed is None else armed
        return self.status()

    def _note_stinger_cut(self) -> None:
        log.info("stingers come back in P3: this stinger take is a cut")

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
    ) -> None:
        """Program to `source_id` by `transition` over `frames` (else `duration_ms`, else a mix's
        default); `flip_flop` puts the old Program on Preview. `send=False`: the engine does it
        already (the T-bar)."""
        held = self._positions.pop(panel.id, None)
        if held is not None:
            # A held T-bar transition ends first, as if it had run to its end (as in the engine).
            _position, _outgoing, incoming = held
            swap = panel.preview_input_id == incoming
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
        if transition in (TransitionType.mix, TransitionType.wipe):
            frames = frames or _frames(duration_ms or DEFAULT_MIX_MS, self.settings.fps)
            # Tally: both sources are on air (LH) for the whole mix or wipe.
            self._mixes[panel.id] = (outgoing, source_id, time.monotonic() + frames / self.settings.fps)
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
        if send and self._rendered(panel):
            body: dict[str, Any] = {"source": self._engine_source(source_id), "frames": frames}
            if transition == TransitionType.wipe:
                # Without a type the engine mixes (frames > 0) or cuts.
                body["type"] = transition.value
            if swapped:
                body["preview"] = self._engine_source(outgoing)
            self._engine_call("POST", f"/me/{self._panel_number(panel)}/take", body)
        wipe = panel.transition.wipe
        if transition == TransitionType.wipe and wipe.alternate:
            # Flip-flop: the next wipe runs back (this one keeps the direction it started with).
            wipe.reverse = not wipe.reverse
            self._push_transition(panel)
        self._publish_tally()

    def set_preview(self, input_id: str, panel_id: str = "me-1") -> MixerStatus:
        if self.state != MixerState.running:
            self.start(MixerStartRequest(preview_input_id=input_id, program_input_id=self._default_program_id()))
        panel = self.get_panel(panel_id)
        self._check_source(panel, input_id)
        panel.preview_input_id = input_id
        if panel.id == self.panels[0].id:
            self.preview_input_id = input_id
        if self._rendered(panel):
            self._engine_call("POST", f"/me/{self._panel_number(panel)}/preview", {"source": self._engine_source(input_id)})
        self._publish_tally()
        return self.status()

    def play_stinger(
        self,
        stinger_id: str,
        target_input_id: str,
        direction: str | None = None,
        outgoing_input_id: str | None = None,
        flip_flop: bool = False,
        panel_id: str | None = None,
    ) -> MixerStatus:
        """Stingers come back in P3 (on the GPU): until then a stinger is a cut to the target."""
        if self.state != MixerState.running:
            self.start(MixerStartRequest())
        panel = self.get_panel(panel_id or (self.panels[0].id if self.panels else "me-1"))
        self._check_source(panel, target_input_id)
        self._note_stinger_cut()
        self._put_on_program(panel, target_input_id, TransitionType.cut, flip_flop=flip_flop)
        return self.status()

    def advance_stinger(self, frames: int = 1) -> MixerStatus:
        """No stinger runs in this version (kept for the API)."""
        return self.status()

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

    def _export_tally(self) -> None:
        """FLOWXER_TALLY_TSL: wake the export thread, which takes the state and sends it."""
        if self.tally_export is not None:
            self.tally_export.changed()

    def _tally_snapshot(self) -> TallySnapshot:
        """Program, Preview and the running mix of each ME, and the re-entry sources each ME can
        take (lamps off while Program is stopped). Called by the export thread."""
        running = self.state == MixerState.running
        mes = []
        for panel in list(self.panels):
            reentry = tuple(
                (other.id, other.label, REENTRY_INDEX + self._panel_number(other))
                for other in self.reentry_sources(panel)
            )
            if not running:
                mes.append(MeTally(reentry=reentry))
                continue
            transition = self._mixes.get(panel.id)
            keyers = [keyer for keyer in self.keyers if keyer.panel_id == panel.id]
            keyed = tuple(k.source_id for k in keyers if k.enabled and k.mode == KeyMode.luma and k.source_id)
            keys = tuple(self._key_tally(keyer) for keyer in keyers if keyer.mode == KeyMode.linear)
            mes.append(MeTally(panel.program_input_id, panel.preview_input_id, transition, reentry, keyed, keys))
        return TallySnapshot(
            inputs=tuple((item.id, item.label) for item in self.list_inputs()),
            mes=tuple(mes),
        )

    def _key_tally(self, keyer: Keyer) -> tuple[int, str, bool]:
        """A linear keyer's key input on the raw tally: INDEX 2000 + 100·n + k (upstream keyer k of
        ME n) or 3000 + 100·n + k (DSK k), its receiver label, on air."""
        same = [k for k in self.keyers if k.panel_id == keyer.panel_id and k.stage == keyer.stage]
        base = 2000 if keyer.stage == KeyerStage.upstream else 3000
        index = base + 100 * self._panel_number(self.get_panel(keyer.panel_id)) + same.index(keyer) + 1
        return index, keyer.receiver_label, keyer.enabled

    # ── saved state and config export/import ─────────────────────────────────

    def export_state(self) -> dict:
        """Inputs, layout, keyers, stingers, tally and IS-05 routes. Holds no secrets."""
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
                "keyers": [keyer.model_dump(mode="json") for keyer in self.keyers],
                "stinger_slots": slots,
                "tally_receivers": [item.model_dump(mode="json") for item in self.tally.receivers],
                "nmos_receivers": self.nmos.export_routes(),
            }

    def import_state(self, data: dict) -> dict:
        """Restore an export_state() document (POST /config/import). The mixer must be stopped."""
        if self.state == MixerState.running:
            raise MixerError("stop the mixer before importing a configuration")
        self._apply_state(data, checked=True)
        self._publish_tally()
        self.persist()
        return self.export_state()

    def persist(self) -> None:
        with self.nmos.lock:
            self._store.save(self.export_state())

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
            inputs = [LogicalInput(**item) for item in data.get("inputs") or []]
            panels = [MixerPanel(**item) for item in data.get("panels") or []]
            keyers = [Keyer(**item) for item in data.get("keyers") or []]
            slots = [StingerSlot(**item) for item in data.get("stinger_slots") or []]
            tally = [TallyReceiver(**item) for item in data.get("tally_receivers") or []]
            routes = list(data.get("nmos_receivers") or [])
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
            self.inputs = {item.id: item for item in inputs}
            self.panels = panels
            self.keyers = keyers
            self.stinger_slots = slots
            main = panels[0] if panels else None
            self.program_input_id = main.program_input_id if main else None
            self.preview_input_id = main.preview_input_id if main else None
            self._sync_sources()
            self._sync_panels()
            self._sync_keyers()
            self._sync_stinger_slots()
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
            stinger={
                "id": None,
                "phase": "idle",
                "frame": 0,
                "frame_count": 0,
                "cut_frame": 0,
                "cut_fired": False,
                "done": True,
                "target_input_id": None,
                "direction": None,
                "events": [],
            },
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
