from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException, status
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import ValidationError

from flowxer.api.schemas import (
    AutoRequest,
    ConsoleState,
    ConvertJobOut,
    DomainInfo,
    DownstreamKeyer,
    ErrorBody,
    FlowDescriptor,
    HealthResponse,
    KeyerAutoRequest,
    KeyerUpdate,
    LogicalInput,
    LogicalInputCreate,
    LogicalInputUpdate,
    MixerCommandResponse,
    MixerStartRequest,
    MixerState,
    MixerStatus,
    PanelTransition,
    PanelTransitionRequest,
    PanelTransitionUpdate,
    PlayerCueRequest,
    PreviewRequest,
    ResourceInfo,
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
        "transparent]`, which only those origins may frame. `bus`: the Program and Preview rows "
        "and Cut/Auto of one ME."
    ),
)
def widgets(mixer: VisionMixer = Depends(get_mixer)) -> list[dict]:
    me = {"type": "integer", "minimum": 1, "maximum": len(mixer.panels), "title": "ME"}
    return [
        {
            "id": "bus",
            "title": "FlowXer bus",
            "params": {"type": "object", "properties": {"me": me}, "required": ["me"]},
            "min_size": {"w": 480, "h": 150},
            "version": __version__,
        }
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
    summary="Take a logical input (or me-<n>, re-entry) to Program: cut, mix or wipe (a stinger is a cut in this version)",
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
    summary="Cut Preview to Program (auto-stingers and Wipe come back in P3)",
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
    summary="Run the ME's next transition (mix or wipe; a stinger is a cut in this version) from Preview to Program",
)
def mixer_auto(payload: AutoRequest | None = None, mixer: VisionMixer = Depends(get_mixer)) -> MixerCommandResponse:
    payload = payload or AutoRequest()
    try:
        body = mixer.auto(payload.panel_id, payload.frames)
    except MixerError as exc:
        raise _http(exc)
    return MixerCommandResponse(status="auto", mixer=body)


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
    summary="Arm Wipe (toggle if armed is omitted); wipes come in P3, the next Cut is a cut",
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
    summary="Stinger take (a cut in this version; stingers come back in P3); flip_flop swaps Preview/Program",
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
        keyers=mixer.keyers,
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


@router.patch(
    "/keyers/{keyer_id}",
    response_model=DownstreamKeyer,
    tags=["keyers"],
    summary="Downstream keyer on or off (at the next frame), or its label",
)
def patch_keyer(
    keyer_id: str, payload: KeyerUpdate, mixer: VisionMixer = Depends(get_mixer)
) -> DownstreamKeyer:
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
    response_model=DownstreamKeyer,
    tags=["keyers"],
    summary="Fade the downstream keyer on (or off when it is on)",
)
def keyer_auto(
    keyer_id: str, payload: KeyerAutoRequest | None = None, mixer: VisionMixer = Depends(get_mixer)
) -> DownstreamKeyer:
    try:
        return mixer.keyer_auto(keyer_id, (payload or KeyerAutoRequest()).duration_ms)
    except MixerError as exc:
        raise _http(exc, status.HTTP_404_NOT_FOUND)


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
