from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

from flowxer.api.schemas import (
    DownstreamKeyer,
    InputKind,
    LogicalInput,
    LogicalInputCreate,
    LogicalInputUpdate,
    MixerPanel,
    MixerStartRequest,
    MixerState,
    MixerStatus,
    OutputFlows,
    OverlayStatus,
    ProgramBus,
    StingerInfo,
    StingerSlot,
    TransitionType,
    WorkspaceConfig,
    WorkspaceUpdate,
    StingerSlotUpdate,
    TallyReceiver,
    TallyReceiverStatus,
)
from flowxer.domain import nmos
from flowxer.domain.mxl_domain import flows_by_group_hint, list_flows
from flowxer.engine.capabilities import probe_backend
from flowxer.engine.formats import format_by_id
from flowxer.engine.gst_runtime import GstRuntime, try_start_gst
from flowxer.engine.overlay import Html5Overlay
from flowxer.engine.pipeline import build_pipeline_description
from flowxer.engine.stinger import (
    StingerPlayer,
    cut_frame_from_ms,
    cut_ms_from_frame,
    generate_replay_wipe,
    inspect_stinger,
    list_stingers,
    register_video_stinger,
    update_stinger_cut,
)
from flowxer.engine.tally import TallyService
from flowxer.engine.webrtc import webrtc_available
from flowxer.settings import Settings, ensure_storage

log = logging.getLogger(__name__)

CLIP_SUFFIXES = {".mp4", ".mov", ".mkv", ".ts", ".mxf", ".wav", ".m4a"}


class MixerError(RuntimeError):
    pass


class VisionMixer:
    """Control-plane + media-plane orchestrator for the DMF vision mixer."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.inputs: dict[str, LogicalInput] = {}
        self.workspace = WorkspaceConfig()
        self.panels: list[MixerPanel] = []
        self.keyers: list[DownstreamKeyer] = []
        self.stinger_slots: list[StingerSlot] = []
        self.started_at = time.time()
        self.state = MixerState.idle
        self.backend = "idle"
        self.program_input_id: str | None = None
        self.preview_input_id: str | None = None
        self.last_live_input_id: str | None = None
        self.program_bus = ProgramBus.live
        self.pipeline: str | None = None
        self.error: str | None = None
        self.outputs: OutputFlows | None = None
        self.gst: GstRuntime | None = None
        self.stinger_player: StingerPlayer | None = None
        self.last_transition: str = "cut"
        self._lock = threading.RLock()
        self._stinger_clock: threading.Thread | None = None
        self.tally = TallyService()
        self.overlay = Html5Overlay(
            url=settings.overlay_url,
            cache_dir=settings.graphics_dir,
            width=settings.width,
            height=settings.height,
        )
        ensure_storage(settings)
        self.overlay.render_fallback_png()
        self._ensure_default_stinger()
        self._sync_sources()
        self._sync_panels()
        self._sync_keyers()
        self._sync_stinger_slots()

    # ── catalog ──────────────────────────────────────────────────────────────

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

    def _sync_sources(self) -> None:
        desired = self.workspace.logical_source_count
        while len(self.inputs) > desired:
            last = self.list_inputs()[-1]
            if last.id in {self.program_input_id, self.preview_input_id}:
                break
            del self.inputs[last.id]
        existing = {item.id for item in self.inputs.values()}
        slot = len(self.inputs)
        index = 1
        while len(self.inputs) < desired:
            source_id = f"cam-{index}"
            index += 1
            if source_id in existing:
                continue
            kind = InputKind.test
            label = f"Camera {index - 1}"
            remaining = desired - len(self.inputs)
            if remaining == 2 and "black" not in existing:
                source_id, label, kind = "black", "Black", InputKind.black
            elif remaining == 1 and "replay" not in existing:
                source_id, label, kind = "replay", "Replay", InputKind.replay
            self.inputs[source_id] = LogicalInput(
                id=source_id, label=label, kind=kind, slot=slot
            )
            existing.add(source_id)
            slot += 1
        for slot, item in enumerate(self.list_inputs()):
            item.slot = slot
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

    def _sync_keyers(self) -> None:
        desired = self.workspace.downstream_keyer_count
        while len(self.keyers) < desired:
            n = len(self.keyers) + 1
            self.keyers.append(
                DownstreamKeyer(
                    id=f"dsk-{n}",
                    label=f"DSK {n}",
                    url=self.settings.overlay_url,
                    title=self.overlay.title,
                    subtitle=self.overlay.subtitle,
                    enabled=self.overlay.enabled if n == 1 else False,
                )
            )
        self.keyers = self.keyers[:desired]
        if self.keyers:
            first = self.keyers[0]
            self.overlay.url = first.url
            self.overlay.title = first.title
            self.overlay.subtitle = first.subtitle
            self.overlay.enabled = first.enabled

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
        if data["format_id"] != self.workspace.format_id:
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
        self._publish_tally()
        return self.workspace

    def get_panel(self, panel_id: str) -> MixerPanel:
        for panel in self.panels:
            if panel.id == panel_id:
                return panel
        raise MixerError(f"unknown mixer panel {panel_id}")

    def get_keyer(self, keyer_id: str) -> DownstreamKeyer:
        for keyer in self.keyers:
            if keyer.id == keyer_id:
                return keyer
        raise MixerError(f"unknown downstream keyer {keyer_id}")

    def update_keyer(self, keyer_id: str, **kwargs) -> DownstreamKeyer:
        keyer = self.get_keyer(keyer_id)
        for field, value in kwargs.items():
            if value is not None:
                setattr(keyer, field, value)
        if keyer.id == (self.keyers[0].id if self.keyers else ""):
            self.overlay.update(
                enabled=keyer.enabled,
                url=keyer.url,
                title=keyer.title,
                subtitle=keyer.subtitle,
            )
            self._apply_overlay_alpha()
        return keyer

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

        if kind == "video" and payload.media_path:
            video = Path(payload.media_path)
            if not video.is_absolute():
                clip = self.settings.clips_dir / video.name
                sting = self.settings.stingers_dir / video.name
                if clip.exists():
                    video = clip
                elif sting.exists():
                    video = sting
                else:
                    raise MixerError(f"video not found: {payload.media_path}")
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

    def _slot_for(self, direction: str) -> StingerSlot | None:
        role = "in" if direction == "to_replay" else "out"
        for slot in self.stinger_slots:
            if slot.role in {role, "shared"}:
                return slot
        return self.stinger_slots[0] if self.stinger_slots else None

    def _stinger_for(self, direction: str) -> str:
        slot = self._slot_for(direction)
        if slot is not None:
            return slot.stinger_id
        return self.settings.default_stinger

    def _info_for_slot(self, slot: StingerSlot | None, stinger_id: str | None = None) -> StingerInfo:
        info = self.get_stinger(stinger_id or (slot.stinger_id if slot else self.settings.default_stinger))
        if slot is None:
            return info
        fps = info.fps or self.settings.fps
        cut_ms = slot.cut_ms if slot.cut_ms is not None else info.cut_ms
        cut_frame = (
            cut_frame_from_ms(cut_ms, fps, info.frame_count)
            if slot.cut_ms is not None
            else (slot.cut_frame if slot.cut_frame is not None else info.cut_frame)
        )
        if slot.cut_frame is not None and slot.cut_ms is None:
            cut_frame = slot.cut_frame
            cut_ms = cut_ms_from_frame(cut_frame, fps)
        return info.model_copy(
            update={
                "kind": slot.kind or info.kind,
                "media_path": slot.media_path or info.media_path,
                "cut_ms": cut_ms,
                "cut_frame": cut_frame,
            }
        )

    def _seed_default_inputs(self) -> None:
        self._sync_sources()

    def register_input(self, payload: LogicalInputCreate) -> LogicalInput:
        if self.state == MixerState.running:
            raise MixerError("stop the mixer before adding inputs")
        if payload.id in self.inputs:
            raise MixerError(f"input {payload.id} already exists")
        resolved = self._resolve_file_path(payload)
        logical = LogicalInput(slot=len(self.inputs), **resolved.model_dump())
        self.inputs[logical.id] = logical
        return logical

    def update_input(self, input_id: str, payload: LogicalInputUpdate) -> LogicalInput:
        current = self.get_input(input_id)
        patch = payload.model_dump(exclude_unset=True)
        if "stinger_slot_id" in patch:
            slot_id = patch["stinger_slot_id"] or None
            if slot_id:
                self.get_stinger_slot(slot_id)
            patch["stinger_slot_id"] = slot_id
        if self.state == MixerState.running and payload.file_path is None:
            # Live metadata updates are allowed; topology changes are not.
            data = current.model_dump()
            for field in ("label", "kind", "video", "audio", "group_hint", "stinger_slot_id"):
                if field in patch:
                    data[field] = patch[field]
            updated = LogicalInput(**data)
            self.inputs[input_id] = updated
            self._publish_tally()
            return updated
        if self.state == MixerState.running and payload.file_path is not None:
            if current.kind not in {InputKind.file, InputKind.replay}:
                raise MixerError("only file/replay inputs can change clip while on-air")
            return self.load_clip(input_id, payload.file_path)
        data = current.model_dump()
        data.update(patch)
        updated = LogicalInput(**data)
        if updated.kind in {InputKind.file, InputKind.replay} and updated.file_path:
            updated.file_path = self._resolve_clip(updated.file_path)
        self.inputs[input_id] = updated
        self._publish_tally()
        return updated

    def delete_input(self, input_id: str) -> None:
        if self.state == MixerState.running:
            raise MixerError("stop the mixer before removing inputs")
        if input_id not in self.inputs:
            raise MixerError(f"unknown input {input_id}")
        del self.inputs[input_id]
        for slot, item in enumerate(self.inputs.values()):
            item.slot = slot

    def get_input(self, input_id: str) -> LogicalInput:
        try:
            return self.inputs[input_id]
        except KeyError as exc:
            raise MixerError(f"unknown input {input_id}") from exc

    def list_inputs(self) -> list[LogicalInput]:
        return sorted(self.inputs.values(), key=lambda item: item.slot)

    def _resolve_file_path(self, payload: LogicalInputCreate) -> LogicalInputCreate:
        if payload.kind in {InputKind.file, InputKind.replay} and payload.file_path:
            if payload.file_path != "_unassigned":
                return payload.model_copy(update={"file_path": self._resolve_clip(payload.file_path)})
        return payload

    def _resolve_clip(self, file_path: str) -> str:
        candidate = Path(file_path)
        if not candidate.is_absolute():
            candidate = self.settings.clips_dir / candidate
        if not candidate.exists():
            raise MixerError(f"clip not found: {file_path}")
        return str(candidate.resolve())

    # ── storage ──────────────────────────────────────────────────────────────

    def list_clips(self) -> list[dict]:
        clips = []
        for path in sorted(self.settings.clips_dir.rglob("*")):
            if path.is_file() and path.suffix.lower() in CLIP_SUFFIXES:
                clips.append(
                    {
                        "name": path.name,
                        "path": str(path),
                        "size_bytes": path.stat().st_size,
                        "suffix": path.suffix.lower(),
                    }
                )
        return clips

    def list_stingers(self) -> list[StingerInfo]:
        return list_stingers(self.settings.stingers_dir)

    def get_stinger(self, stinger_id: str) -> StingerInfo:
        info = inspect_stinger(self.settings.stingers_dir, stinger_id, fps=self.settings.fps)
        if info is None or not info.frame_count:
            raise MixerError(f"unknown stinger {stinger_id}")
        return info

    # ── mixer lifecycle ──────────────────────────────────────────────────────

    def start(self, request: MixerStartRequest | None = None) -> MixerStatus:
        request = request or MixerStartRequest()
        if self.state == MixerState.running:
            raise MixerError("mixer is already running")
        if not self.inputs:
            raise MixerError("register at least one logical input")

        domain = Path(request.domain) if request.domain else self.settings.mxl_domain
        domain.mkdir(parents=True, exist_ok=True)
        group_hint = request.group_hint or self.settings.group_hint
        if request.overlay_url:
            self.overlay.url = request.overlay_url
        if request.overlay_enabled:
            self.overlay.enabled = True

        self._bind_group_hints(domain)

        video_id = nmos.flow_uuid(group_hint, "video")
        audio_id = nmos.flow_uuid(group_hint, "audio")
        self.outputs = OutputFlows(
            video_flow_id=video_id,
            audio_flow_id=audio_id,
            group_hint=group_hint,
            media_type_video=self.settings.video_media_type,
            media_type_audio=self.settings.audio_media_type,
            nmos_video=nmos.video_flow_def(
                flow_id=video_id,
                group_hint=group_hint,
                label=f"{group_hint} PGM video",
                description="FlowXer program video (uncompressed v210 / VP210)",
                width=self.settings.width,
                height=self.settings.height,
                frame_rate_num=self.settings.frame_rate_num,
                frame_rate_den=self.settings.frame_rate_den,
                media_type=self.settings.video_media_type,
            ),
            nmos_audio=nmos.audio_flow_def(
                flow_id=audio_id,
                group_hint=group_hint,
                label=f"{group_hint} PGM audio",
                description="FlowXer program audio (uncompressed float32)",
                channels=self.settings.audio_channels,
                sample_rate=self.settings.audio_rate,
            ),
        )

        capabilities = probe_backend()
        use_mxl = bool(capabilities["mxl_plugins"])
        use_cef = bool(capabilities["cefsrc"])
        slot = self.stinger_slots[0] if self.stinger_slots else None
        stinger = self._info_for_slot(slot).model_dump()
        description = build_pipeline_description(
            settings=self.settings,
            inputs=self.list_inputs(),
            overlay_url=self.overlay.url,
            overlay_enabled=self.overlay.enabled,
            stinger=stinger,
            output_video_flow_id=video_id,
            output_audio_flow_id=audio_id,
            domain=str(domain),
            use_mxl_sink=use_mxl,
            use_cefsrc=use_cef,
        )
        self.pipeline = description

        force_sim = self.settings.simulate or self.settings.gst_mode == "simulate"
        self.gst = None
        if not force_sim and capabilities["gstreamer"]:
            self.gst = try_start_gst(description)

        self.backend = "gstreamer" if self.gst else "simulate"
        self.state = MixerState.running
        self.error = None
        self.program_input_id = request.program_input_id or self._default_program_id()
        self.preview_input_id = request.preview_input_id or self.program_input_id
        self.last_live_input_id = self.program_input_id
        self.program_bus = ProgramBus.replay if self._is_replay(self.program_input_id) else ProgramBus.live
        if self.panels:
            self.panels[0].program_input_id = self.program_input_id
            self.panels[0].preview_input_id = self.preview_input_id
        self._apply_program()
        self._apply_overlay_alpha()
        self._publish_tally()
        return self.status()

    def stop(self) -> MixerStatus:
        if self.gst is not None:
            self.gst.stop()
            self.gst = None
        self.state = MixerState.idle
        self.backend = "idle"
        self.stinger_player = None
        self._publish_tally()
        return self.status()

    def _default_program_id(self) -> str:
        for item in self.list_inputs():
            if item.kind in {InputKind.mxl_live, InputKind.test}:
                return item.id
        return next(iter(self.inputs))

    def _bind_group_hints(self, domain: Path) -> None:
        for item in self.inputs.values():
            if item.kind != InputKind.mxl_live or not item.group_hint:
                continue
            matched = flows_by_group_hint(domain, item.group_hint)
            if "video" in matched and (item.video is None or item.video.flow_id is None):
                video = item.video.model_dump() if item.video else {}
                video["flow_id"] = matched["video"].id
                from flowxer.api.schemas import VideoEssence

                item.video = VideoEssence(**video)
            if "audio" in matched and (item.audio is None or item.audio.flow_id is None):
                audio = item.audio.model_dump() if item.audio else {}
                audio["flow_id"] = matched["audio"].id
                from flowxer.api.schemas import AudioEssence

                item.audio = AudioEssence(**audio)

    def _is_replay(self, input_id: str | None) -> bool:
        if not input_id:
            return False
        return self.get_input(input_id).kind in {InputKind.replay, InputKind.file}

    def _apply_program(self) -> None:
        if self.program_input_id is None:
            return
        slot = self.get_input(self.program_input_id).slot
        if self.gst is not None:
            self.gst.set_active_slot("vsel", slot)
            self.gst.set_active_slot("asel", slot)

    def _apply_overlay_alpha(self) -> None:
        if self.gst is not None:
            self.gst.set_compositor_alpha("sink_1", 1.0 if self.overlay.enabled else 0.0)
            self.gst.set_overlay_png(self.overlay.png_path)

    # ── takes / replay / overlay ─────────────────────────────────────────────

    def take(
        self,
        input_id: str,
        transition: TransitionType = TransitionType.cut,
        stinger_id: str | None = None,
        panel_id: str = "me-1",
    ) -> MixerStatus:
        """Direct source → Program (source-tile right-click). Does not consume Wipe."""
        if self.state != MixerState.running:
            self.start(MixerStartRequest(program_input_id=input_id, preview_input_id=input_id))
        target = self.get_input(input_id)
        panel = self.get_panel(panel_id)
        if transition == TransitionType.stinger:
            return self.play_stinger(stinger_id or self._stinger_for("to_replay"), input_id, panel_id=panel_id)
        if transition == TransitionType.cut:
            auto = self._auto_stinger_for(target.id)
            if auto is not None:
                return self.play_stinger(
                    auto.stinger_id,
                    input_id,
                    direction="to_replay" if self._is_replay(input_id) else "to_live",
                    panel_id=panel.id,
                )
        self._put_on_program(panel, target.id, TransitionType(transition), flip_flop=False)
        return self.status()

    def cut(self, panel_id: str = "me-1") -> MixerStatus:
        """Flip Preview onto Program. If Wipe is armed, play the TGA stinger instead."""
        panel = self._ensure_running_panel(panel_id)
        incoming = self._preview_id(panel)
        auto = self._auto_stinger_for(incoming)
        if auto is not None:
            outgoing = panel.program_input_id
            return self.play_stinger(
                auto.stinger_id,
                incoming,
                direction="to_replay" if self._is_replay(incoming) else "to_live",
                outgoing_input_id=outgoing,
                flip_flop=True,
                panel_id=panel.id,
            )
        if panel.wipe_armed:
            outgoing = panel.program_input_id
            stinger_id = self._stinger_for("to_replay" if self._is_replay(incoming) else "to_live")
            status = self.play_stinger(
                stinger_id,
                incoming,
                direction="to_replay" if self._is_replay(incoming) else "to_live",
                outgoing_input_id=outgoing,
                flip_flop=True,
                panel_id=panel.id,
            )
            panel.wipe_armed = False
            return status
        self._put_on_program(panel, incoming, TransitionType.cut, flip_flop=True)
        return self.status()

    def fade(self, panel_id: str = "me-1", duration_ms: int = 400) -> MixerStatus:
        """Dissolve Preview onto Program. Does not consume an armed Wipe."""
        panel = self._ensure_running_panel(panel_id)
        incoming = self._preview_id(panel)
        self._put_on_program(panel, incoming, TransitionType.mix, flip_flop=True, duration_ms=duration_ms)
        return self.status()

    def fade_to_black(self, panel_id: str = "me-1", duration_ms: int = 600) -> MixerStatus:
        """Fade Program to Black, or fade up from Black onto Preview."""
        panel = self._ensure_running_panel(panel_id)
        if "black" not in self.inputs:
            raise MixerError("no black input is registered")
        if panel.program_input_id == "black":
            target = self._preview_id(panel)
        else:
            target = "black"
        self._put_on_program(panel, target, TransitionType.mix, flip_flop=False, duration_ms=duration_ms)
        return self.status()

    def set_wipe(self, panel_id: str = "me-1", armed: bool | None = None) -> MixerStatus:
        """Arm (or toggle) Wipe so the next Cut plays the TGA stinger."""
        panel = self._ensure_running_panel(panel_id)
        panel.wipe_armed = (not panel.wipe_armed) if armed is None else armed
        return self.status()

    def _auto_stinger_for(self, input_id: str) -> StingerSlot | None:
        source = self.get_input(input_id)
        if not source.stinger_slot_id:
            return None
        return self.get_stinger_slot(source.stinger_slot_id)

    def _ensure_running_panel(self, panel_id: str) -> MixerPanel:
        if self.state != MixerState.running:
            self.start(MixerStartRequest())
        return self.get_panel(panel_id)

    def _preview_id(self, panel: MixerPanel) -> str:
        incoming = panel.preview_input_id
        if not incoming:
            raise MixerError("arm a source on Preview before cutting or fading")
        self.get_input(incoming)
        return incoming

    def _put_on_program(
        self,
        panel: MixerPanel,
        input_id: str,
        transition: TransitionType,
        *,
        flip_flop: bool,
        duration_ms: int = 0,
    ) -> None:
        target = self.get_input(input_id)
        outgoing = panel.program_input_id
        panel.program_input_id = target.id
        panel.last_transition = transition.value
        self.last_transition = transition.value
        if flip_flop and outgoing and outgoing != target.id:
            panel.preview_input_id = outgoing
        if panel.id == (self.panels[0].id if self.panels else panel.id):
            self.program_input_id = target.id
            if flip_flop and outgoing and outgoing != target.id:
                self.preview_input_id = outgoing
            if target.kind not in {InputKind.replay, InputKind.file}:
                self.last_live_input_id = target.id
                self.program_bus = ProgramBus.live
            else:
                self.program_bus = ProgramBus.replay
            self._apply_program()
        _ = duration_ms  # mix duration is recorded; GST input-selector is a hard switch
        self._publish_tally()

    def set_preview(self, input_id: str, panel_id: str = "me-1") -> MixerStatus:
        if self.state != MixerState.running:
            self.start(MixerStartRequest(preview_input_id=input_id, program_input_id=self._default_program_id()))
        self.get_input(input_id)
        panel = self.get_panel(panel_id)
        panel.preview_input_id = input_id
        if panel.id == self.panels[0].id:
            self.preview_input_id = input_id
        self._publish_tally()
        return self.status()

    def load_clip(self, input_id: str, file_path: str) -> LogicalInput:
        target = self.get_input(input_id)
        if target.kind not in {InputKind.file, InputKind.replay}:
            raise MixerError(f"input {input_id} cannot play files")
        resolved = self._resolve_clip(file_path)
        target.file_path = resolved
        return target

    def take_replay(self, stinger_id: str, input_id: str | None = None) -> MixerStatus:
        replay_id = input_id or self._replay_input_id()
        replay = self.get_input(replay_id)
        if replay.file_path in {None, "_unassigned"}:
            raise MixerError("load a clip onto the replay input first")
        if self.last_live_input_id is None:
            self.last_live_input_id = self.program_input_id
        return self.play_stinger(stinger_id or self._stinger_for("to_replay"), replay_id, direction="to_replay")

    def return_live(self, stinger_id: str, input_id: str | None = None) -> MixerStatus:
        live_id = input_id or self.last_live_input_id or self._default_program_id()
        return self.play_stinger(stinger_id or self._stinger_for("to_live"), live_id, direction="to_live")

    def play_stinger(
        self,
        stinger_id: str,
        target_input_id: str,
        direction: str | None = None,
        outgoing_input_id: str | None = None,
        flip_flop: bool = False,
        panel_id: str | None = None,
    ) -> MixerStatus:
        if self.state != MixerState.running:
            self.start(MixerStartRequest())
        if self.stinger_player and not self.stinger_player.done:
            remaining = self.stinger_player.info.frame_count - self.stinger_player.frame + 1
            self.advance_stinger(max(remaining, 1))
        if direction is None:
            direction = "to_replay" if self._is_replay(target_input_id) else "to_live"
        slot = self._slot_for(direction)
        if slot is not None and stinger_id and slot.stinger_id != stinger_id:
            for item in self.stinger_slots:
                if item.stinger_id == stinger_id:
                    slot = item
                    break
        info = self._info_for_slot(slot, stinger_id)
        self.get_input(target_input_id)
        panel = self.get_panel(panel_id or (self.panels[0].id if self.panels else "me-1"))
        outgoing = outgoing_input_id if outgoing_input_id is not None else panel.program_input_id
        self.last_transition = "stinger"
        panel.last_transition = "stinger"
        self.stinger_player = StingerPlayer(
            info,
            target_input_id,
            direction,
            outgoing_input_id=outgoing,
            flip_flop=flip_flop,
            panel_id=panel.id,
        )
        if self.gst is not None:
            self.gst.set_compositor_alpha("sink_2", 1.0)
        # Advance to first frame so status reports "playing".
        self.advance_stinger(1)
        self._arm_stinger_clock()
        return self.status()

    def advance_stinger(self, frames: int = 1) -> MixerStatus:
        """Advance the TGA stinger. Called by the GST pad probe or tests."""
        if self.stinger_player is None:
            return self.status()
        snapshot = self.stinger_player.advance(frames)
        if "cut" in snapshot["events"]:
            target_id = self.stinger_player.target_input_id
            panel = None
            if self.stinger_player.panel_id:
                try:
                    panel = self.get_panel(self.stinger_player.panel_id)
                except MixerError:
                    panel = None
            if panel is None and self.panels:
                panel = self.panels[0]
            if panel is not None:
                outgoing = self.stinger_player.outgoing_input_id
                panel.program_input_id = target_id
                panel.last_transition = "stinger"
                if self.stinger_player.flip_flop and outgoing and outgoing != target_id:
                    panel.preview_input_id = outgoing
            self.program_input_id = target_id
            if self.stinger_player.flip_flop and self.stinger_player.outgoing_input_id:
                self.preview_input_id = self.stinger_player.outgoing_input_id
            if self.stinger_player.direction == "to_live":
                self.last_live_input_id = self.program_input_id
                self.program_bus = ProgramBus.live
            else:
                self.program_bus = ProgramBus.replay
            self._apply_program()
            self._publish_tally()
        if "complete" in snapshot["events"]:
            if self.gst is not None:
                self.gst.set_compositor_alpha("sink_2", 0.0)
            self.stinger_player = None
        return self.status()

    def _arm_stinger_clock(self) -> None:
        """Advance the TGA FSM in simulate (and when no GST pad probe is ticking)."""
        if not self.settings.stinger_auto_tick:
            return
        if self._stinger_clock is not None and self._stinger_clock.is_alive():
            return

        def tick() -> None:
            delay = 1.0 / max(self.settings.fps, 1.0)
            while True:
                time.sleep(delay)
                if self.stinger_player is None:
                    return
                self.advance_stinger(1)
                if self.stinger_player is None:
                    return

        self._stinger_clock = threading.Thread(target=tick, daemon=True, name="stinger-clock")
        self._stinger_clock.start()

    def set_overlay(self, **kwargs) -> MixerStatus:
        self.overlay.update(**kwargs)
        if self.keyers:
            if kwargs.get("enabled") is not None:
                self.keyers[0].enabled = kwargs["enabled"]
            if kwargs.get("url") is not None:
                self.keyers[0].url = kwargs["url"]
            if kwargs.get("title") is not None:
                self.keyers[0].title = kwargs["title"]
            if kwargs.get("subtitle") is not None:
                self.keyers[0].subtitle = kwargs["subtitle"]
        self._apply_overlay_alpha()
        return self.status()

    def _replay_input_id(self) -> str:
        for item in self.list_inputs():
            if item.kind == InputKind.replay:
                return item.id
        raise MixerError("no replay input is registered")

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
        try:
            return self.tally.publish(self)
        except Exception as exc:
            log.warning("tally publish failed: %s", exc)
            return self.tally.status()

    def status(self) -> MixerStatus:
        capabilities = probe_backend()
        renderer = "cefsrc" if capabilities["cefsrc"] else "pillow-fallback"
        stinger_state = (
            self.stinger_player.snapshot()
            if self.stinger_player
            else {
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
            }
        )
        return MixerStatus(
            state=self.state,
            backend=self.backend,
            program_input_id=self.program_input_id,
            preview_input_id=self.preview_input_id,
            last_live_input_id=self.last_live_input_id,
            program_bus=self.program_bus,
            overlay=OverlayStatus(
                enabled=self.overlay.enabled,
                url=self.overlay.url,
                title=self.overlay.title,
                subtitle=self.overlay.subtitle,
                renderer=renderer,
            ),
            stinger=stinger_state,
            outputs=self.outputs,
            raster=self.settings.raster,
            frame_rate=self.settings.frame_rate,
            video_format=self.settings.video_media_type,
            audio_format=self.settings.audio_media_type,
            pipeline=self.pipeline,
            error=self.error,
            workspace=self.workspace.model_dump(),
            panels=[panel.model_dump() for panel in self.panels],
            keyers=[keyer.model_dump() for keyer in self.keyers],
            stinger_slots=[slot.model_dump() for slot in self.stinger_slots],
            webrtc_enabled=webrtc_available(),
            wipe_armed=bool(self.panels[0].wipe_armed) if self.panels else False,
            last_transition=self.last_transition,
        )

    def domain_flows(self):
        return list_flows(self.settings.mxl_domain)
