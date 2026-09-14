from __future__ import annotations

import logging
from pathlib import Path

from flowxer.api.schemas import (
    InputKind,
    LogicalInput,
    LogicalInputCreate,
    LogicalInputUpdate,
    MixerStartRequest,
    MixerState,
    MixerStatus,
    OutputFlows,
    OverlayStatus,
    ProgramBus,
    StingerInfo,
    TransitionType,
)
from flowxer.domain import nmos
from flowxer.domain.mxl_domain import flows_by_group_hint, list_flows
from flowxer.engine.capabilities import probe_backend
from flowxer.engine.gst_runtime import GstRuntime, try_start_gst
from flowxer.engine.overlay import Html5Overlay
from flowxer.engine.pipeline import build_pipeline_description
from flowxer.engine.stinger import StingerPlayer, generate_replay_wipe, inspect_stinger, list_stingers
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
        self.overlay = Html5Overlay(
            url=settings.overlay_url,
            cache_dir=settings.graphics_dir,
            width=settings.width,
            height=settings.height,
        )
        ensure_storage(settings)
        self.overlay.render_fallback_png()
        self._ensure_default_stinger()
        self._seed_default_inputs()

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

    def _seed_default_inputs(self) -> None:
        if self.inputs:
            return
        self.register_input(
            LogicalInputCreate(
                id="cam-1",
                label="Camera 1",
                kind=InputKind.test,
            )
        )
        self.register_input(
            LogicalInputCreate(
                id="cam-2",
                label="Camera 2",
                kind=InputKind.test,
            )
        )
        self.register_input(
            LogicalInputCreate(id="black", label="Black", kind=InputKind.black)
        )
        self.register_input(
            LogicalInputCreate(id="replay", label="Replay", kind=InputKind.replay)
        )

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
        if self.state == MixerState.running and payload.file_path is None:
            # Live metadata updates are allowed; topology changes are not.
            data = current.model_dump()
            if payload.label is not None:
                data["label"] = payload.label
            if payload.video is not None:
                data["video"] = payload.video
            if payload.audio is not None:
                data["audio"] = payload.audio
            if payload.group_hint is not None:
                data["group_hint"] = payload.group_hint
            updated = LogicalInput(**data)
            self.inputs[input_id] = updated
            return updated
        if self.state == MixerState.running and payload.file_path is not None:
            if current.kind not in {InputKind.file, InputKind.replay}:
                raise MixerError("only file/replay inputs can change clip while on-air")
            return self.load_clip(input_id, payload.file_path)
        data = current.model_dump()
        patch = payload.model_dump(exclude_unset=True)
        data.update(patch)
        updated = LogicalInput(**data)
        if updated.kind in {InputKind.file, InputKind.replay} and updated.file_path:
            updated.file_path = self._resolve_clip(updated.file_path)
        self.inputs[input_id] = updated
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
        info = inspect_stinger(self.settings.stingers_dir, stinger_id)
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
        stinger = self.get_stinger(self.settings.default_stinger).model_dump()
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
        self._apply_program()
        self._apply_overlay_alpha()
        return self.status()

    def stop(self) -> MixerStatus:
        if self.gst is not None:
            self.gst.stop()
            self.gst = None
        self.state = MixerState.idle
        self.backend = "idle"
        self.stinger_player = None
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

    def take(self, input_id: str, transition: TransitionType = TransitionType.cut, stinger_id: str | None = None) -> MixerStatus:
        self._require_running()
        target = self.get_input(input_id)
        if transition == TransitionType.stinger:
            return self.play_stinger(stinger_id or self.settings.default_stinger, input_id)
        self.program_input_id = target.id
        if target.kind not in {InputKind.replay, InputKind.file}:
            self.last_live_input_id = target.id
            self.program_bus = ProgramBus.live
        else:
            self.program_bus = ProgramBus.replay
        self._apply_program()
        return self.status()

    def set_preview(self, input_id: str) -> MixerStatus:
        self._require_running()
        self.get_input(input_id)
        self.preview_input_id = input_id
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
        return self.play_stinger(stinger_id, replay_id, direction="to_replay")

    def return_live(self, stinger_id: str, input_id: str | None = None) -> MixerStatus:
        live_id = input_id or self.last_live_input_id or self._default_program_id()
        return self.play_stinger(stinger_id, live_id, direction="to_live")

    def play_stinger(self, stinger_id: str, target_input_id: str, direction: str | None = None) -> MixerStatus:
        self._require_running()
        if self.stinger_player and not self.stinger_player.done:
            raise MixerError("a stinger is already playing")
        info = self.get_stinger(stinger_id)
        self.get_input(target_input_id)
        if direction is None:
            direction = "to_replay" if self._is_replay(target_input_id) else "to_live"
        self.stinger_player = StingerPlayer(info, target_input_id, direction)
        if self.gst is not None:
            self.gst.set_compositor_alpha("sink_2", 1.0)
        # Advance to first frame so status reports "playing".
        self.advance_stinger(1)
        return self.status()

    def advance_stinger(self, frames: int = 1) -> MixerStatus:
        """Advance the TGA stinger. Called by the GST pad probe or tests."""
        if self.stinger_player is None:
            return self.status()
        snapshot = self.stinger_player.advance(frames)
        if "cut" in snapshot["events"]:
            self.program_input_id = self.stinger_player.target_input_id
            if self.stinger_player.direction == "to_live":
                self.last_live_input_id = self.program_input_id
                self.program_bus = ProgramBus.live
            else:
                self.program_bus = ProgramBus.replay
            self._apply_program()
        if "complete" in snapshot["events"]:
            if self.gst is not None:
                self.gst.set_compositor_alpha("sink_2", 0.0)
            self.stinger_player = None
        return self.status()

    def set_overlay(self, **kwargs) -> MixerStatus:
        self.overlay.update(**kwargs)
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
        )

    def domain_flows(self):
        return list_flows(self.settings.mxl_domain)
