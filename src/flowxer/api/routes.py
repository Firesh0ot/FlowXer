from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Path, status
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import ValidationError

from flowxer.api.schemas import (
    AudioStripUpdate,
    AutoRequest,
    ConsoleState,
    ConvertJobOut,
    Dme,
    DmePresetInfo,
    DomainInfo,
    Keyer,
    ErrorBody,
    FlowDescriptor,
    HealthResponse,
    InputAudioUpdate,
    KeyerAutoRequest,
    KeyerPresetRequest,
    KeyerRunRequest,
    KeyerUpdate,
    LogicalInput,
    LogicalInputCreate,
    LogicalInputUpdate,
    MixerCommandResponse,
    MixerStartRequest,
    MixerState,
    MixerStatus,
    PanelAudioUpdate,
    PanelTransition,
    PanelTransitionRequest,
    PanelTransitionUpdate,
    PlayerCueRequest,
    PositionRequest,
    PreviewRequest,
    ResourceInfo,
    Snapshot,
    SnapshotOptionsUpdate,
    SnapshotRecallResult,
    SnapshotStartup,
    SnapshotStore,
    SnapshotUpdate,
    StingerInfo,
    StingerPlayRequest,
    StingerSlot,
    StingerSlotUpdate,
    StorageClip,
    TakeRequest,
    TallyConfig,
    TallyReceiversUpdate,
    WorkspaceConfig,
    WorkspaceUpdate,
)
from flowxer import __version__
from flowxer.api.metrics import live_payload, ready_payload, render_prometheus
from flowxer.domain.mxl_domain import load_domain_info, scan_domains
from flowxer.engine.formats import VIDEO_FORMATS
from flowxer.engine.mixer import MixerError, VisionMixer
from flowxer.engine.preview import preview_info
from flowxer.engine.resources import collect_resources
from flowxer.engine.tally import TALLY_PRESETS
from flowxer.settings import Settings, get_settings

router = APIRouter()


def get_mixer() -> VisionMixer:
    raise RuntimeError("mixer dependency is overridden in app startup")


def _http(exc: MixerError, code: int = status.HTTP_409_CONFLICT) -> HTTPException:
    return HTTPException(status_code=code, detail=str(exc))


@router.get(
    "/health",
    response_model=HealthResponse,
    tags=["system"],
    summary="Service health and backend capabilities",
)
def health(
    mixer: VisionMixer = Depends(get_mixer),
    settings: Settings = Depends(get_settings),
) -> HealthResponse:
    return HealthResponse(
        status="ok",
        service="flowxer-vision-mixer",
        version=settings.version,
        mxl_domain=str(settings.output_domain),
        mxl_root=str(settings.mxl_root),
        mxl_output_domain_id=settings.resolved_output_domain_id,
        simulate=settings.simulate,
        engine=mixer.engine.state if mixer.engine is not None else ("simulate" if mixer.backend == "simulate" else "stopped"),
        mxl_revision=settings.resolved_mxl_revision,
    )


@router.get(
    "/metrics",
    tags=["system"],
    summary="Prometheus metrics (also served at GET /metrics)",
)
def api_metrics(mixer: VisionMixer = Depends(get_mixer)) -> PlainTextResponse:
    return PlainTextResponse(
        render_prometheus(mixer),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )


@router.get("/livez", tags=["system"], summary="Liveness probe: 503 when the control plane is stuck")
async def api_livez(mixer: VisionMixer = Depends(get_mixer)) -> JSONResponse:
    # async: answered on the event loop, also when the worker threads are all blocked.
    code, body = live_payload(mixer)
    return JSONResponse(status_code=code, content=body)


@router.get("/readyz", tags=["system"], summary="Readiness: MXL root and output domain")
def api_readyz(mixer: VisionMixer = Depends(get_mixer)):
    code, body = ready_payload(mixer)
    return JSONResponse(status_code=code, content=body)


@router.get(
    "/config",
    tags=["system"],
    summary="Effective mixer raster, format and storage paths",
)
def config(settings: Settings = Depends(get_settings)) -> dict:
    return {
        "raster": settings.raster,
        "frame_rate": settings.frame_rate,
        "video_media_type": settings.video_media_type,
        "audio_media_type": settings.audio_media_type,
        "audio_rate": settings.audio_rate,
        "audio_channels": settings.audio_channels,
        "mxl_domain": str(settings.output_domain),
        "mxl_root": str(settings.mxl_root),
        "mxl_output_domain_id": settings.resolved_output_domain_id,
        "storage_root": str(settings.storage_root),
        "group_hint": settings.group_hint,
        "default_stinger": settings.default_stinger,
        "latency_grains": settings.latency,
        "render_mes": settings.render_mes,
        "pvw_outputs": settings.pvw_outputs,
    }


@router.get(
    "/engine",
    tags=["system"],
    summary="The media engine's status: timing per stage, per input arrival lag, clip players, outputs",
    description=(
        "The engine's own status (refreshed once a second while Program is on air). "
        "`engine.recommended_latency_grains` is the latency that covers the latest input arrival "
        "seen since each input was routed; compare it with FLOWXER_LATENCY."
    ),
)
def engine_status(mixer: VisionMixer = Depends(get_mixer)) -> dict:
    status_body = mixer.engine_status()
    return {
        "process": mixer.engine.summary() if mixer.engine is not None else {"state": mixer.backend},
        "status": status_body,
    }


@router.get(
    "/preview",
    tags=["gui"],
    summary="The preview mosaic stream (WebRTC WHEP / HLS) and where each picture is in it",
    description=(
        "One stream holds every picture of the console: the sources, and each rendering ME's "
        "Program and Preview. `tiles` gives each picture's rectangle in the stream with its "
        "id (`source:<input id>` or `panel:<panel id>:pgm|pvw`). With a shared MediaMTX "
        "(PREVIEW_WHEP_URL) `whep_url` is absolute; with the built-in one the player uses "
        "`whep_port` on the page's host."
    ),
)
def preview(mixer: VisionMixer = Depends(get_mixer)) -> dict:
    return preview_info(mixer)


@router.get(
    "/widgets",
    tags=["gui"],
    summary="The operator-screen widgets flowxer-gui serves at /widget/<id>, with their parameters",
    description=(
        "flowxer-gui serves this list at `/widgets` (with CORS for the origins in "
        "WIDGET_FRAME_ANCESTORS) and the widget pages at `/widget/<id>?<params>[&theme=dark|light|"
        "transparent]`, which only those origins may frame. `bus`: the Program and Preview rows, "
        "Cut/Auto and the transition type (`transition=false` hides it) of one ME. `keys`: one ME's "
        "upstream keyers and DSKs with On/Off and Auto. `dme`: one upstream keyer's box: On/Off, "
        "Auto, Run back and forth, and the presets. `audio`: one ME's audio strips (meter, MUTE, AFV, and the "
        "fader unless `faders=false`) and its master meter with the loudness. `snapshots`: recall buttons for "
        "one ME's snapshots (`me`) and, unless `all=false`, the snapshots of all MEs."
    ),
)
def widgets(mixer: VisionMixer = Depends(get_mixer)) -> list[dict]:
    me = {"type": "integer", "minimum": 1, "maximum": len(mixer.panels), "title": "ME"}
    transition = {"type": "boolean", "default": True, "title": "Transition type"}
    faders = {"type": "boolean", "default": True, "title": "Faders"}
    all_mes = {"type": "boolean", "default": True, "title": "Snapshots of all MEs"}
    keyer = {"type": "integer", "minimum": 1, "maximum": max(1, mixer.workspace.keyers_per_me), "title": "Upstream keyer"}
    return [
        {
            "id": "bus",
            "title": "FlowXer bus",
            "params": {"type": "object", "properties": {"me": me, "transition": transition}, "required": ["me"]},
            "min_size": {"w": 480, "h": 150},
            "version": __version__,
        },
        {
            "id": "keys",
            "title": "FlowXer keys",
            "params": {"type": "object", "properties": {"me": me}, "required": ["me"]},
            "min_size": {"w": 240, "h": 150},
            "version": __version__,
        },
        {
            "id": "dme",
            "title": "FlowXer DME",
            "params": {"type": "object", "properties": {"me": me, "keyer": keyer}, "required": ["me", "keyer"]},
            "min_size": {"w": 320, "h": 120},
            "version": __version__,
        },
        {
            "id": "audio",
            "title": "FlowXer audio",
            "params": {"type": "object", "properties": {"me": me, "faders": faders}, "required": ["me"]},
            "min_size": {"w": 480, "h": 240},
            "version": __version__,
        },
        {
            "id": "snapshots",
            "title": "FlowXer snapshots",
            "params": {"type": "object", "properties": {"me": me, "all": all_mes}, "required": []},
            "min_size": {"w": 240, "h": 120},
            "version": __version__,
        },
        {
            "id": "tbar",
            "title": "FlowXer T-bar",
            "params": {
                "type": "object",
                "properties": {"me": {**me, "default": 1}, "buttons": {"type": "boolean", "default": True, "title": "Cut and Auto"}},
                "required": [],
            },
            "min_size": {"w": 120, "h": 240},
            "version": __version__,
        },
        {
            "id": "multiview",
            "title": "FlowXer multiview",
            "params": {
                "type": "object",
                "properties": {"me": {**me, "title": "Tally of ME", "default": 1}, "pvw": {"type": "boolean", "default": False, "title": "Previews too"}},
                "required": [],
            },
            "min_size": {"w": 480, "h": 270},
            "version": __version__,
        },
    ]


@router.get(
    "/domain",
    response_model=DomainInfo,
    tags=["mxl"],
    summary="Inspect FlowXer's output MXL domain",
)
def domain(
    mixer: VisionMixer = Depends(get_mixer),
    settings: Settings = Depends(get_settings),
) -> DomainInfo:
    return load_domain_info(settings.output_domain)


@router.get(
    "/domains",
    response_model=list[DomainInfo],
    tags=["mxl"],
    summary="List MXL domains under FLOWXER_MXL_ROOT (including mirror domains)",
)
def domains(
    mixer: VisionMixer = Depends(get_mixer),
    settings: Settings = Depends(get_settings),
) -> list[DomainInfo]:
    return scan_domains(settings.mxl_root)


@router.get(
    "/domain/flows",
    response_model=list[FlowDescriptor],
    tags=["mxl"],
    summary="List MXL essences (video/v210 and audio/float32 flows) in every scanned domain",
)
def domain_flows(mixer: VisionMixer = Depends(get_mixer)) -> list[FlowDescriptor]:
    return mixer.domain_flows()


@router.get(
    "/inputs",
    response_model=list[LogicalInput],
    tags=["inputs"],
    summary="List logical inputs (bundled audio + video essences)",
)
def list_inputs(mixer: VisionMixer = Depends(get_mixer)) -> list[LogicalInput]:
    return mixer.list_inputs()


@router.post(
    "/inputs",
    response_model=LogicalInput,
    status_code=status.HTTP_201_CREATED,
    tags=["inputs"],
    summary="Register a logical input that bundles video and audio essences",
    responses={409: {"model": ErrorBody}},
)
def create_input(
    payload: LogicalInputCreate, mixer: VisionMixer = Depends(get_mixer)
) -> LogicalInput:
    try:
        return mixer.register_input(payload)
    except MixerError as exc:
        raise _http(exc)


@router.get(
    "/inputs/{input_id}",
    response_model=LogicalInput,
    tags=["inputs"],
    summary="Read one logical input",
    responses={404: {"model": ErrorBody}},
)
def get_input(input_id: str, mixer: VisionMixer = Depends(get_mixer)) -> LogicalInput:
    try:
        return mixer.get_input(input_id)
    except MixerError as exc:
        raise _http(exc, status.HTTP_404_NOT_FOUND)


@router.patch(
    "/inputs/{input_id}",
    response_model=LogicalInput,
    tags=["inputs"],
    summary="Update label, kind, essences, the clip player's media, loop and play-on-cut, or auto-stinger",
)
def patch_input(
    input_id: str, payload: LogicalInputUpdate, mixer: VisionMixer = Depends(get_mixer)
) -> LogicalInput:
    try:
        return mixer.update_input(input_id, payload)
    except MixerError as exc:
        code = status.HTTP_404_NOT_FOUND if "unknown" in str(exc) else status.HTTP_409_CONFLICT
        raise _http(exc, code)
    except ValidationError as exc:
        # The patch is merged into the stored input; the merged result can still be
        # invalid (e.g. mxl_live without essences or group hint).
        raise HTTPException(
            status_code=422,
            detail=exc.errors(include_url=False, include_context=False),
        ) from exc


@router.delete(
    "/inputs/{input_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["inputs"],
    summary="Unregister a logical input (mixer must be off-air)",
)
def delete_input(input_id: str, mixer: VisionMixer = Depends(get_mixer)) -> None:
    try:
        mixer.delete_input(input_id)
    except MixerError as exc:
        code = status.HTTP_404_NOT_FOUND if "unknown" in str(exc) else status.HTTP_409_CONFLICT
        raise _http(exc, code)


@router.get(
    "/mixer",
    response_model=MixerStatus,
    tags=["mixer"],
    summary="Program, preview, every ME's output flows and the media engine's state",
)
def mixer_status(mixer: VisionMixer = Depends(get_mixer)) -> MixerStatus:
    return mixer.status()


@router.post(
    "/mixer/start",
    response_model=MixerCommandResponse,
    tags=["mixer"],
    summary="Take the mixer on air: start the media engine, which writes every rendering ME's PGM (v210 + float32) and PVW flows",
)
def mixer_start(
    payload: MixerStartRequest | None = None, mixer: VisionMixer = Depends(get_mixer)
) -> MixerCommandResponse:
    try:
        status_body = mixer.start(payload)
    except MixerError as exc:
        raise _http(exc)
    return MixerCommandResponse(status="started", mixer=status_body)


@router.post(
    "/mixer/stop",
    response_model=MixerCommandResponse,
    tags=["mixer"],
    summary="Take the mixer off-air: stop the media engine",
)
def mixer_stop(mixer: VisionMixer = Depends(get_mixer)) -> MixerCommandResponse:
    return MixerCommandResponse(status="stopped", mixer=mixer.stop())


@router.post(
    "/mixer/take",
    response_model=MixerCommandResponse,
    tags=["mixer"],
    summary="Take a logical input (or me-<n>, re-entry) to Program: cut (the input's auto-stinger plays), mix, wipe or stinger",
)
def mixer_take(
    payload: TakeRequest, mixer: VisionMixer = Depends(get_mixer)
) -> MixerCommandResponse:
    try:
        body = mixer.take(
            payload.input_id,
            payload.transition,
            payload.stinger_id,
            payload.panel_id,
            payload.duration_ms,
            payload.frames,
        )
    except MixerError as exc:
        raise _http(exc)
    return MixerCommandResponse(status="taken", mixer=body)


@router.post(
    "/mixer/preview",
    response_model=MixerCommandResponse,
    tags=["mixer"],
    summary="Arm a logical input on preview",
)
def mixer_preview(
    payload: PreviewRequest, mixer: VisionMixer = Depends(get_mixer)
) -> MixerCommandResponse:
    try:
        body = mixer.set_preview(payload.input_id, payload.panel_id)
    except MixerError as exc:
        raise _http(exc)
    return MixerCommandResponse(status="preview", mixer=body)


@router.post(
    "/mixer/cut",
    response_model=MixerCommandResponse,
    tags=["mixer"],
    summary="Cut Preview to Program: Preview's auto-stinger plays, else an armed Wipe plays the stinger slot by direction",
)
def mixer_cut(
    payload: PanelTransitionRequest | None = None, mixer: VisionMixer = Depends(get_mixer)
) -> MixerCommandResponse:
    payload = payload or PanelTransitionRequest()
    try:
        body = mixer.cut(payload.panel_id)
    except MixerError as exc:
        raise _http(exc)
    return MixerCommandResponse(status="cut", mixer=body)


@router.post(
    "/mixer/auto",
    response_model=MixerCommandResponse,
    tags=["mixer"],
    summary="Run the ME's next transition (mix, wipe or stinger) from Preview to Program",
)
def mixer_auto(payload: AutoRequest | None = None, mixer: VisionMixer = Depends(get_mixer)) -> MixerCommandResponse:
    payload = payload or AutoRequest()
    try:
        body = mixer.auto(payload.panel_id, payload.frames)
    except MixerError as exc:
        raise _http(exc)
    return MixerCommandResponse(status="auto", mixer=body)


@router.post(
    "/mixer/position",
    response_model=MixerCommandResponse,
    tags=["mixer"],
    summary="T-bar: hold the ME's transition at a position (0..1); 1 completes it, 0 ends it without the swap",
)
def mixer_position(payload: PositionRequest, mixer: VisionMixer = Depends(get_mixer)) -> MixerCommandResponse:
    try:
        body = (
            mixer.set_position(payload.panel_id, payload.position)
            if payload.position is not None
            else mixer.set_lever(payload.panel_id, payload.lever)
        )
    except MixerError as exc:
        raise _http(exc)
    return MixerCommandResponse(status="position", mixer=body)


@router.patch(
    "/panels/{panel_id}/transition",
    response_model=PanelTransition,
    tags=["mixer"],
    summary="The ME's next transition: type (mix, wipe, stinger), rate in frames, wipe pattern and edge, stinger slot",
    responses={404: {"model": ErrorBody}, 409: {"model": ErrorBody}},
)
def patch_panel_transition(
    panel_id: str, payload: PanelTransitionUpdate, mixer: VisionMixer = Depends(get_mixer)
) -> PanelTransition:
    try:
        return mixer.update_transition(panel_id, payload)
    except MixerError as exc:
        code = status.HTTP_404_NOT_FOUND if str(exc).startswith("unknown") else status.HTTP_409_CONFLICT
        raise _http(exc, code)


@router.post(
    "/mixer/fade",
    response_model=MixerCommandResponse,
    tags=["mixer"],
    summary="Fade (mix) Preview onto Program",
)
def mixer_fade(
    payload: PanelTransitionRequest | None = None, mixer: VisionMixer = Depends(get_mixer)
) -> MixerCommandResponse:
    payload = payload or PanelTransitionRequest()
    try:
        body = mixer.fade(payload.panel_id, payload.duration_ms)
    except MixerError as exc:
        raise _http(exc)
    return MixerCommandResponse(status="fade", mixer=body)


@router.post(
    "/mixer/fade-to-black",
    response_model=MixerCommandResponse,
    tags=["mixer"],
    summary="Fade Program to the Black source, or fade up from Black onto Preview",
)
def mixer_fade_to_black(
    payload: PanelTransitionRequest | None = None, mixer: VisionMixer = Depends(get_mixer)
) -> MixerCommandResponse:
    payload = payload or PanelTransitionRequest()
    try:
        body = mixer.fade_to_black(payload.panel_id, payload.duration_ms)
    except MixerError as exc:
        raise _http(exc)
    return MixerCommandResponse(status="fade_to_black", mixer=body)


@router.post(
    "/mixer/wipe",
    response_model=MixerCommandResponse,
    tags=["mixer"],
    summary="Arm Wipe (toggle if armed is omitted): the next Cut plays the stinger slot by direction (FlowXer 14)",
)
def mixer_wipe(
    payload: PanelTransitionRequest | None = None, mixer: VisionMixer = Depends(get_mixer)
) -> MixerCommandResponse:
    payload = payload or PanelTransitionRequest()
    try:
        body = mixer.set_wipe(payload.panel_id, payload.armed)
    except MixerError as exc:
        raise _http(exc)
    return MixerCommandResponse(status="wipe", mixer=body)


@router.get(
    "/storage/clips",
    response_model=list[StorageClip],
    tags=["storage"],
    summary="List the library clips and stills a clip player can play",
)
def storage_clips(mixer: VisionMixer = Depends(get_mixer)) -> list[StorageClip]:
    return [StorageClip(**item) for item in mixer.list_clips()]


@router.get(
    "/storage/stingers",
    response_model=list[StingerInfo],
    tags=["storage"],
    summary="List TGA-sequence and video stingers",
)
def storage_stingers(mixer: VisionMixer = Depends(get_mixer)) -> list[StingerInfo]:
    return mixer.list_stingers()


@router.post(
    "/stinger/play",
    response_model=MixerCommandResponse,
    tags=["stinger"],
    summary="Stinger take on the GPU (a cut while the stinger converts); flip_flop swaps Preview/Program",
)
def stinger_play(
    payload: StingerPlayRequest, mixer: VisionMixer = Depends(get_mixer)
) -> MixerCommandResponse:
    try:
        body = mixer.play_stinger(
            payload.stinger_id,
            payload.target_input_id,
            payload.direction,
            flip_flop=payload.flip_flop,
            panel_id=payload.panel_id,
        )
    except MixerError as exc:
        raise _http(exc)
    return MixerCommandResponse(status="stinger", mixer=body)


@router.post(
    "/players/{input_id}/{action}",
    response_model=LogicalInput,
    tags=["inputs"],
    summary="Clip player transport: cue, play, pause, stop",
    description=(
        "`cue` shows a frame and holds (body `{\"frame\": n}`, default the first); `play` plays "
        "from there; `pause` holds the current frame; `stop` goes back to the first frame and "
        "holds. Loop and play-on-cut are set on the input (`PATCH /inputs/{id}`)."
    ),
    responses={404: {"model": ErrorBody}, 409: {"model": ErrorBody}},
)
def player_command(
    input_id: str,
    action: str,
    payload: PlayerCueRequest | None = None,
    mixer: VisionMixer = Depends(get_mixer),
) -> LogicalInput:
    try:
        return mixer.player_command(input_id, action, (payload or PlayerCueRequest()).frame)
    except MixerError as exc:
        code = status.HTTP_404_NOT_FOUND if str(exc).startswith("unknown") else status.HTTP_409_CONFLICT
        raise _http(exc, code)


@router.get(
    "/console",
    response_model=ConsoleState,
    tags=["gui"],
    summary="One-shot operator console snapshot (workspace, buses, resources, preview mosaic, per-source state)",
)
def console(mixer: VisionMixer = Depends(get_mixer)) -> ConsoleState:
    mixer_status = mixer.status()
    keyers = mixer.keyer_views()
    return ConsoleState(
        workspace=mixer.workspace,
        formats=[
            {
                "id": fmt.id,
                "label": fmt.label,
                "width": fmt.width,
                "height": fmt.height,
                "frame_rate": fmt.frame_rate,
            }
            for fmt in VIDEO_FORMATS.values()
        ],
        inputs=mixer.list_inputs(),
        panels=mixer.panels,
        keyers=keyers,
        on_air=mixer.program_visibility(keyers),
        tbar=mixer.tbar_state(),
        stinger_slots=mixer.stinger_slots,
        mixer=mixer_status,
        resources=ResourceInfo.model_validate(collect_resources(mixer)),
        preview=preview_info(mixer),
        sources=mixer.source_runtime(),
        clips=[StorageClip(**item) for item in mixer.list_clips()],
        stingers=mixer.list_stingers(),
        library=[mixer.library_item_out(item) for item in mixer.library.list_items()],
        jobs=[ConvertJobOut(**job.model_dump()) for job in mixer.library.queue.list_jobs()],
        tally=TallyConfig(receivers=mixer.tally.status(), presets=TALLY_PRESETS),
        nmos=mixer.nmos.status(),
        audio=mixer.audio_state(),
        snapshots=mixer.snapshots_view(),
        pinned={
            field: variables for field, (_, variables) in mixer.settings.pinned_workspace.items()
        },
    )


@router.get(
    "/tally",
    response_model=TallyConfig,
    tags=["tally"],
    summary="TSL 5.0 tally/UMD receivers, send status, and device presets",
)
def tally_state(mixer: VisionMixer = Depends(get_mixer)) -> TallyConfig:
    return TallyConfig(receivers=mixer.tally.status(), presets=TALLY_PRESETS)


@router.put(
    "/tally/receivers",
    response_model=TallyConfig,
    tags=["tally"],
    summary="Replace the TSL 5.0 tally receiver list (Companion, VSM, BFE, Riedel HI, custom)",
)
def put_tally_receivers(
    payload: TallyReceiversUpdate, mixer: VisionMixer = Depends(get_mixer)
) -> TallyConfig:
    try:
        receivers = mixer.replace_tally_receivers(payload.receivers)
    except MixerError as exc:
        raise _http(exc)
    return TallyConfig(receivers=receivers, presets=TALLY_PRESETS)


@router.post(
    "/tally/refresh",
    response_model=TallyConfig,
    tags=["tally"],
    summary="Re-send Program/Preview tally and UMD labels to every enabled TSL receiver",
)
def tally_refresh(mixer: VisionMixer = Depends(get_mixer)) -> TallyConfig:
    return TallyConfig(receivers=mixer.publish_tally(), presets=TALLY_PRESETS)


@router.get(
    "/workspace",
    response_model=WorkspaceConfig,
    tags=["gui"],
    summary="Read console layout: format, source-tile aspect, source count, MEs, stingers, DSKs",
)
def get_workspace(mixer: VisionMixer = Depends(get_mixer)) -> WorkspaceConfig:
    return mixer.workspace


@router.put(
    "/workspace",
    response_model=WorkspaceConfig,
    tags=["gui"],
    summary="Apply Settings: raster, source tiles, logical sources, mixer panels, stingers, downstream keyers",
    description=(
        "Fields the environment sets (`GET /console` `pinned`) can only be sent with their "
        "current value; another value is 409."
    ),
    responses={409: {"model": ErrorBody}},
)
def put_workspace(
    payload: WorkspaceUpdate, mixer: VisionMixer = Depends(get_mixer)
) -> WorkspaceConfig:
    try:
        return mixer.apply_workspace(payload)
    except (MixerError, ValueError) as exc:
        raise _http(MixerError(str(exc)))


@router.get(
    "/config/export",
    tags=["gui"],
    summary="Export the configuration (inputs, layout, keyers, stingers, tally, IS-05 routes) as one JSON document",
)
def config_export(mixer: VisionMixer = Depends(get_mixer)) -> dict:
    return mixer.export_state()


@router.post(
    "/config/import",
    tags=["gui"],
    summary="Restore an exported configuration; the mixer must be stopped",
    responses={409: {"model": ErrorBody}, 422: {"model": ErrorBody}},
)
def config_import(payload: dict = Body(...), mixer: VisionMixer = Depends(get_mixer)) -> dict:
    try:
        return mixer.import_state(payload)
    except MixerError as exc:
        code = status.HTTP_409_CONFLICT if mixer.state == MixerState.running else 422
        raise _http(exc, code)


@router.get(
    "/resources",
    response_model=ResourceInfo,
    tags=["gui"],
    summary="Container CPU/memory and mixer issues for the operator status chip",
)
def resources(mixer: VisionMixer = Depends(get_mixer)) -> ResourceInfo:
    return ResourceInfo.model_validate(collect_resources(mixer))


@router.get(
    "/keyers",
    response_model=list[Keyer],
    tags=["keyers"],
    summary="The upstream keyers and DSKs of every rendering ME, in order",
)
def list_keyers(mixer: VisionMixer = Depends(get_mixer)) -> list[Keyer]:
    return mixer.keyer_views()


@router.patch(
    "/keyers/{keyer_id}",
    response_model=Keyer,
    tags=["keyers"],
    summary="Keyer on or off (at the next frame), its rate and label, and its key: mode (linear, luma, pip), source, clip, soft, gain, invert, box (dme)",
)
def patch_keyer(
    keyer_id: str, payload: KeyerUpdate, mixer: VisionMixer = Depends(get_mixer)
) -> Keyer:
    try:
        return mixer.update_keyer(keyer_id, **payload.model_dump(exclude_unset=True))
    except MixerError as exc:
        code = (
            status.HTTP_404_NOT_FOUND
            if str(exc).startswith("unknown")
            else status.HTTP_409_CONFLICT
        )
        raise _http(exc, code)


@router.post(
    "/keyers/{keyer_id}/auto",
    response_model=Keyer,
    tags=["keyers"],
    summary="Fade the keyer on (or off when it is on); with fly, fly its box from keyframe 0 to the last (or back)",
)
def keyer_auto(
    keyer_id: str, payload: KeyerAutoRequest | None = None, mixer: VisionMixer = Depends(get_mixer)
) -> Keyer:
    try:
        return mixer.keyer_auto(keyer_id, (payload or KeyerAutoRequest()).duration_ms)
    except MixerError as exc:
        raise _http(exc, status.HTTP_404_NOT_FOUND)


def _keyer_error(exc: MixerError) -> HTTPException:
    return _http(exc, status.HTTP_404_NOT_FOUND if str(exc).startswith("unknown") else status.HTTP_409_CONFLICT)


@router.put(
    "/keyers/{keyer_id}/dme",
    response_model=Keyer,
    tags=["keyers"],
    summary="Replace the keyer's box (missing fields: defaults; PATCH /keyers/{id} merges); stops a run",
)
def put_keyer_dme(keyer_id: str, payload: Dme, mixer: VisionMixer = Depends(get_mixer)) -> Keyer:
    try:
        return mixer.replace_keyer_box(keyer_id, payload)
    except MixerError as exc:
        raise _keyer_error(exc)


@router.put(
    "/keyers/{keyer_id}/keyframes",
    response_model=Keyer,
    tags=["keyers"],
    summary="Replace the keyer's keyframes (up to 8): box fields (missing: the keyer's box), frames and ease each",
    description=(
        "Each keyframe is a box state (see DmeKeyframe: x, y, size, soft, crop, border width, soft "
        "and opacity, shadow) with `frames` (1..1000: the time from the keyframe before) and `ease` "
        "(linear, ease, ease-in, ease-out, hold). Size is interpolated in log space. A run stops "
        "where the box is."
    ),
)
def put_keyer_keyframes(
    keyer_id: str, payload: list[dict[str, Any]] = Body(...), mixer: VisionMixer = Depends(get_mixer)
) -> Keyer:
    try:
        return mixer.set_keyframes(keyer_id, payload)
    except MixerError as exc:
        raise _keyer_error(exc)


@router.post(
    "/keyers/{keyer_id}/run",
    response_model=Keyer,
    tags=["keyers"],
    summary="Run the box from where it is to a keyframe (default the last; 0 with reverse), loop (wrap, pingpong), or jump (frames 0)",
)
def run_keyer(keyer_id: str, payload: KeyerRunRequest | None = None, mixer: VisionMixer = Depends(get_mixer)) -> Keyer:
    request = payload or KeyerRunRequest()
    try:
        return mixer.run_keyer(keyer_id, to=request.to, reverse=request.reverse, loop=request.loop, frames=request.frames)
    except MixerError as exc:
        raise _keyer_error(exc)


@router.post(
    "/keyers/{keyer_id}/stop",
    response_model=Keyer,
    tags=["keyers"],
    summary="Stop the box's run: it holds where it is",
)
def stop_keyer(keyer_id: str, mixer: VisionMixer = Depends(get_mixer)) -> Keyer:
    try:
        return mixer.stop_keyer(keyer_id)
    except MixerError as exc:
        raise _keyer_error(exc)


@router.post(
    "/keyers/{keyer_id}/preset",
    response_model=list[Keyer],
    tags=["keyers"],
    summary="Write a DME preset (box and keyframes) into the keyer (split and quad: the ME's keyers 1-2 and 1-4); returns the keyers written",
)
def keyer_preset(keyer_id: str, payload: KeyerPresetRequest, mixer: VisionMixer = Depends(get_mixer)) -> list[Keyer]:
    try:
        return mixer.apply_preset(keyer_id, payload.name)
    except MixerError as exc:
        raise _keyer_error(exc)


@router.get(
    "/dme/presets",
    response_model=list[DmePresetInfo],
    tags=["keyers"],
    summary="The DME presets: corner PiPs, split, quad, full",
)
def dme_presets(mixer: VisionMixer = Depends(get_mixer)) -> list[DmePresetInfo]:
    return mixer.dme_presets()


def _audio_error(exc: MixerError) -> HTTPException:
    """Unknown input, panel or strip: 404; a panel that does not render: 409; a bad value: 422."""
    text = str(exc)
    if text.startswith("unknown"):
        return _http(exc, status.HTTP_404_NOT_FOUND)
    return _http(exc, status.HTTP_409_CONFLICT if "does not render" in text else 422)


@router.get(
    "/audio",
    tags=["audio"],
    summary="Every audio setting: per live input its delay and channel map, per ME its master and strips",
    description=(
        "`inputs`: per live input id its `delay_ms` (audio later than its video), `channel_map` (per PGM "
        "channel the input channel, 1-based, 0 silent; empty 1:1) and its flow's `channels`. `panels`: per "
        "rendering ME (panel id) its `master_db` and its `strips` in slot order (live inputs, clip players, "
        "the Programs of higher MEs, `stinger`), each with `fader_db` (-90 off .. +10), `mute` and `afv` "
        "(audio follows video; none on the stinger). `channels`: the PGM's; `max_delay_ms`: "
        "FLOWXER_AUDIO_MAX_DELAY_MS."
    ),
)
def get_audio(mixer: VisionMixer = Depends(get_mixer)) -> dict:
    return mixer.audio_state()


@router.patch(
    "/audio/inputs/{input_id}",
    tags=["audio"],
    summary="A live input's audio delay (ms, whole samples) and channel map, for every ME",
    responses={404: {"model": ErrorBody}, 422: {"model": ErrorBody}},
)
def patch_audio_input(input_id: str, payload: InputAudioUpdate, mixer: VisionMixer = Depends(get_mixer)) -> dict:
    try:
        return mixer.update_input_audio(input_id, payload)
    except MixerError as exc:
        raise _audio_error(exc)


@router.patch(
    "/audio/panels/{panel_id}",
    tags=["audio"],
    summary="An ME's master fader (-90 off .. +10 dB); it ramps over one frame",
    responses={404: {"model": ErrorBody}, 409: {"model": ErrorBody}},
)
def patch_audio_panel(panel_id: str, payload: PanelAudioUpdate, mixer: VisionMixer = Depends(get_mixer)) -> dict:
    try:
        return mixer.update_panel_audio(panel_id, payload)
    except MixerError as exc:
        raise _audio_error(exc)


@router.patch(
    "/audio/panels/{panel_id}/strips/{strip_id}",
    tags=["audio"],
    summary="A strip of an ME's mix: fader, mute, AFV (strip: an input or clip player id, a higher panel id, or stinger)",
    responses={404: {"model": ErrorBody}, 409: {"model": ErrorBody}, 422: {"model": ErrorBody}},
)
def patch_audio_strip(
    panel_id: str, strip_id: str, payload: AudioStripUpdate, mixer: VisionMixer = Depends(get_mixer)
) -> dict:
    try:
        return mixer.update_audio_strip(panel_id, strip_id, payload)
    except MixerError as exc:
        raise _audio_error(exc)


@router.post(
    "/audio/panels/{panel_id}/loudness/reset",
    tags=["audio"],
    summary="The ME's integrated loudness (EBU R 128) from now on",
    responses={404: {"model": ErrorBody}, 409: {"model": ErrorBody}},
)
def reset_audio_loudness(panel_id: str, mixer: VisionMixer = Depends(get_mixer)) -> dict:
    try:
        return mixer.reset_loudness(panel_id)
    except MixerError as exc:
        raise _audio_error(exc)


@router.get(
    "/audio/meters",
    tags=["audio"],
    summary="The meters of the last 100 ms block: per source peak and RMS, per ME PGM also true peak, loudness, overs and its strips",
    description=(
        "`sources`: live inputs (after their delay and channel map, before the fader) and clip players by id, "
        "stinger players as `stinger-<k>`: `peak_dbfs` and `rms_dbfs` per channel. `panels`: per rendering ME "
        "its PGM (what goes to MXL): `peak_dbfs`, `true_peak_dbtp` (BS.1770-4, 4x), `rms_dbfs` per channel, "
        "`loudness` per channel pair (`momentary` 400 ms, `short_term` 3 s, `integrated` since the start or the "
        "last reset; LUFS, EBU R 128), `overs` (samples at or above 0 dBFS since the engine started) and "
        "`strips`: per strip its `gain_db` in PGM now and whether it is `heard`. `block`: the 100 ms block's "
        "number (its first 48 kHz sample index / 4800); null off-air. Values below -120 dB are -120. Poll at "
        "10 Hz at most: the answer changes every 100 ms."
    ),
)
def get_audio_meters(mixer: VisionMixer = Depends(get_mixer)) -> dict:
    return mixer.audio_meters()


@router.patch(
    "/stinger-slots/{slot_id}",
    response_model=StingerSlot,
    tags=["stinger"],
    summary="Set stinger media (TGA sequence or video) and the Program cut frame for a slot",
)
def patch_stinger_slot(
    slot_id: str, payload: StingerSlotUpdate, mixer: VisionMixer = Depends(get_mixer)
) -> StingerSlot:
    try:
        return mixer.configure_stinger_slot(slot_id, payload)
    except MixerError as exc:
        raise _http(exc, status.HTTP_404_NOT_FOUND)


# ── snapshots (docs/snapshots.md) ────────────────────────────────────────────

SnapshotNumber = Path(ge=1, le=99, description="The snapshot's number (1..99)")


def _snapshot_error(exc: MixerError) -> HTTPException:
    """Unknown snapshot or panel: 404; everything else (no free number, an empty name, an ME the
    console no longer has): 409."""
    return _http(exc, status.HTTP_404_NOT_FOUND if str(exc).startswith("unknown") else status.HTTP_409_CONFLICT)


@router.get(
    "/snapshots",
    tags=["snapshots"],
    summary="Every snapshot (number, name, ME or all, the recall's defaults, when stored) and the start-up snapshot",
)
def list_snapshots(mixer: VisionMixer = Depends(get_mixer)) -> dict:
    return mixer.snapshots_view()


@router.post(
    "/snapshots",
    response_model=Snapshot,
    tags=["snapshots"],
    summary="Store the current state of one ME (panel_id me-<n>) or of all MEs (all) as a snapshot",
    description=(
        "A snapshot holds per ME its Program and Preview, FTB, its transition (type, rate, wipe, stinger slot, "
        "ties, BKGD), its keyers and DSKs (on air, rate, key, box, keyframes, fly) and its audio (master and "
        "strips). Not the structure, the inputs, the clip players' media, NMOS routes or input delays and maps. "
        "`number` defaults to the lowest free one; a used number is stored over (its name and options stay "
        "unless given)."
    ),
    responses={404: {"model": ErrorBody}, 409: {"model": ErrorBody}},
)
def store_snapshot(payload: SnapshotStore, mixer: VisionMixer = Depends(get_mixer)) -> Snapshot:
    try:
        return mixer.store_snapshot(payload)
    except MixerError as exc:
        raise _snapshot_error(exc)


@router.put(
    "/snapshots/startup",
    tags=["snapshots"],
    summary="The snapshot recalled (with its options) whenever the mixer starts; number null: none",
    responses={404: {"model": ErrorBody}},
)
def put_startup_snapshot(payload: SnapshotStartup, mixer: VisionMixer = Depends(get_mixer)) -> dict:
    try:
        return mixer.set_startup_snapshot(payload.number)
    except MixerError as exc:
        raise _snapshot_error(exc)


@router.get(
    "/snapshots/{number}",
    response_model=Snapshot,
    tags=["snapshots"],
    summary="A snapshot with its contents",
    responses={404: {"model": ErrorBody}},
)
def get_snapshot(number: int = SnapshotNumber, mixer: VisionMixer = Depends(get_mixer)) -> Snapshot:
    try:
        return mixer.get_snapshot(number)
    except MixerError as exc:
        raise _snapshot_error(exc)


@router.patch(
    "/snapshots/{number}",
    response_model=Snapshot,
    tags=["snapshots"],
    summary="Rename a snapshot or change the defaults of its recall (program, audio)",
    responses={404: {"model": ErrorBody}, 409: {"model": ErrorBody}},
)
def patch_snapshot(payload: SnapshotUpdate, number: int = SnapshotNumber, mixer: VisionMixer = Depends(get_mixer)) -> Snapshot:
    try:
        return mixer.update_snapshot(number, payload)
    except MixerError as exc:
        raise _snapshot_error(exc)


@router.delete(
    "/snapshots/{number}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["snapshots"],
    summary="Delete a snapshot (also as the start-up snapshot)",
    responses={404: {"model": ErrorBody}},
)
def delete_snapshot(number: int = SnapshotNumber, mixer: VisionMixer = Depends(get_mixer)) -> None:
    try:
        mixer.delete_snapshot(number)
    except MixerError as exc:
        raise _snapshot_error(exc)


@router.post(
    "/snapshots/{number}/recall",
    response_model=SnapshotRecallResult,
    tags=["snapshots"],
    summary="Recall a snapshot; program and audio override its defaults for this recall",
    description=(
        "Always: each ME's Preview, its transition and the settings of its keyers that are off air. "
        "`program` (Program/on-air): also Program (a cut), the keyers' and DSKs' on or off (cuts), the settings "
        "of keyers on air and FTB (a cut). `audio`: also the strips and the master (each ramps over one grain). "
        "What no longer fits the console is skipped and listed in `skipped`. Off-air only the state changes."
    ),
    responses={404: {"model": ErrorBody}, 409: {"model": ErrorBody}},
)
def recall_snapshot(
    payload: SnapshotOptionsUpdate | None = None, number: int = SnapshotNumber, mixer: VisionMixer = Depends(get_mixer)
) -> dict:
    try:
        return mixer.recall_snapshot(number, payload)
    except MixerError as exc:
        raise _snapshot_error(exc)
