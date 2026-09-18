from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from flowxer.api.schemas import (
    ConsoleState,
    DomainInfo,
    DownstreamKeyer,
    ErrorBody,
    FlowDescriptor,
    HealthResponse,
    KeyerUpdate,
    LogicalInput,
    LogicalInputCreate,
    LogicalInputUpdate,
    MixerCommandResponse,
    MixerStartRequest,
    MixerStatus,
    OverlayStatus,
    OverlayUpdate,
    PanelTransitionRequest,
    PreviewRequest,
    ReplayLoadRequest,
    ReplayTransitionRequest,
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
from flowxer.domain.mxl_domain import load_domain_info
from flowxer.engine.capabilities import probe_backend
from flowxer.engine.formats import VIDEO_FORMATS
from flowxer.engine.mixer import MixerError, VisionMixer
from flowxer.engine.preview import render_jpeg
from flowxer.engine.resources import collect_resources
from flowxer.engine.tally import TALLY_PRESETS
from flowxer.engine.webrtc import create_whep_answer, webrtc_available
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
    caps = probe_backend()
    return HealthResponse(
        status="ok",
        service="flowxer-vision-mixer",
        version=settings.version,
        mxl_domain=str(settings.mxl_domain),
        gstreamer=bool(caps["gstreamer"]),
        mxl_plugins=bool(caps["mxl_plugins"]),
        simulate=mixer.backend == "simulate" or settings.simulate,
    )


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
        "mxl_domain": str(settings.mxl_domain),
        "storage_root": str(settings.storage_root),
        "group_hint": settings.group_hint,
        "default_stinger": settings.default_stinger,
        "overlay_url": settings.overlay_url,
    }


@router.get(
    "/domain",
    response_model=DomainInfo,
    tags=["mxl"],
    summary="Inspect the mounted MXL domain",
)
def domain(
    mixer: VisionMixer = Depends(get_mixer),
    settings: Settings = Depends(get_settings),
) -> DomainInfo:
    return load_domain_info(settings.mxl_domain)


@router.get(
    "/domain/flows",
    response_model=list[FlowDescriptor],
    tags=["mxl"],
    summary="List MXL essences (video/v210 and audio/float32 flows)",
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
    summary="Update label, kind, essences, clip path, or auto-stinger on a logical input",
)
def patch_input(
    input_id: str, payload: LogicalInputUpdate, mixer: VisionMixer = Depends(get_mixer)
) -> LogicalInput:
    try:
        return mixer.update_input(input_id, payload)
    except MixerError as exc:
        code = status.HTTP_404_NOT_FOUND if "unknown" in str(exc) else status.HTTP_409_CONFLICT
        raise _http(exc, code)


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
    summary="Program, preview, overlay, stinger and output flow status",
)
def mixer_status(mixer: VisionMixer = Depends(get_mixer)) -> MixerStatus:
    return mixer.status()


@router.post(
    "/mixer/start",
    response_model=MixerCommandResponse,
    tags=["mixer"],
    summary="Start the GStreamer vision mixer and publish PGM v210 + float32 flows",
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
    summary="Stop the GStreamer pipeline and take the mixer off-air",
)
def mixer_stop(mixer: VisionMixer = Depends(get_mixer)) -> MixerCommandResponse:
    return MixerCommandResponse(status="stopped", mixer=mixer.stop())


@router.post(
    "/mixer/take",
    response_model=MixerCommandResponse,
    tags=["mixer"],
    summary="Take a logical input to program (cut, mix or stinger)",
)
def mixer_take(
    payload: TakeRequest, mixer: VisionMixer = Depends(get_mixer)
) -> MixerCommandResponse:
    try:
        body = mixer.take(payload.input_id, payload.transition, payload.stinger_id, payload.panel_id)
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
    summary="Cut Preview to Program. Auto-stinger on the Preview source, or an armed Wipe, plays a stinger instead.",
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
    summary="Arm Wipe so the next Cut plays the default stinger (toggle if armed is omitted)",
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
    "/overlay",
    response_model=OverlayStatus,
    tags=["overlay"],
    summary="HTML5 graphics keyer status",
)
def overlay_status(mixer: VisionMixer = Depends(get_mixer)) -> OverlayStatus:
    return mixer.status().overlay


@router.post(
    "/overlay",
    response_model=MixerCommandResponse,
    tags=["overlay"],
    summary="Enable/disable the HTML5 overlay or change its URL and ident text",
)
def overlay_update(
    payload: OverlayUpdate, mixer: VisionMixer = Depends(get_mixer)
) -> MixerCommandResponse:
    body = mixer.set_overlay(
        enabled=payload.enabled,
        url=payload.url,
        title=payload.title,
        subtitle=payload.subtitle,
    )
    return MixerCommandResponse(status="overlay", mixer=body)


@router.get(
    "/storage/clips",
    response_model=list[StorageClip],
    tags=["storage"],
    summary="List playable video files in mixer storage",
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
    "/replay/load",
    response_model=LogicalInput,
    tags=["replay"],
    summary="Load a stored clip onto the replay logical input",
)
def replay_load(
    payload: ReplayLoadRequest, mixer: VisionMixer = Depends(get_mixer)
) -> LogicalInput:
    try:
        return mixer.load_clip(payload.input_id, payload.file_path)
    except MixerError as exc:
        raise _http(exc, status.HTTP_404_NOT_FOUND if "not found" in str(exc) else status.HTTP_409_CONFLICT)


@router.post(
    "/replay/take",
    response_model=MixerCommandResponse,
    tags=["replay"],
    summary="Stinger from live program into replay",
)
def replay_take(
    payload: ReplayTransitionRequest, mixer: VisionMixer = Depends(get_mixer)
) -> MixerCommandResponse:
    try:
        body = mixer.take_replay(payload.stinger_id, payload.input_id)
    except MixerError as exc:
        raise _http(exc)
    return MixerCommandResponse(status="to_replay", mixer=body)


@router.post(
    "/replay/return",
    response_model=MixerCommandResponse,
    tags=["replay"],
    summary="Stinger from replay back to the previous live input",
)
def replay_return(
    payload: ReplayTransitionRequest, mixer: VisionMixer = Depends(get_mixer)
) -> MixerCommandResponse:
    try:
        body = mixer.return_live(payload.stinger_id, payload.input_id)
    except MixerError as exc:
        raise _http(exc)
    return MixerCommandResponse(status="to_live", mixer=body)


@router.post(
    "/stinger/play",
    response_model=MixerCommandResponse,
    tags=["stinger"],
    summary="Play a TGA or video stinger and cut Program at the cut frame; flip_flop swaps Preview/Program",
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
    "/stinger/tick",
    response_model=MixerCommandResponse,
    tags=["stinger"],
    summary="Advance the stinger by N frames (used by tests and the simulate backend)",
)
def stinger_tick(
    frames: int = 1, mixer: VisionMixer = Depends(get_mixer)
) -> MixerCommandResponse:
    return MixerCommandResponse(status="tick", mixer=mixer.advance_stinger(frames))


@router.get(
    "/console",
    response_model=ConsoleState,
    tags=["gui"],
    summary="One-shot operator console snapshot (workspace, buses, resources, WebRTC)",
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
        webrtc={"enabled": webrtc_available(), "protocol": "WHEP"},
        clips=[StorageClip(**item) for item in mixer.list_clips()],
        stingers=mixer.list_stingers(),
        tally=TallyConfig(receivers=mixer.tally.status(), presets=TALLY_PRESETS),
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
)
def put_workspace(
    payload: WorkspaceUpdate, mixer: VisionMixer = Depends(get_mixer)
) -> WorkspaceConfig:
    try:
        return mixer.apply_workspace(payload)
    except (MixerError, ValueError) as exc:
        raise _http(MixerError(str(exc)))


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
    tags=["overlay"],
    summary="Configure a downstream keyer (HTML5 graphics)",
)
def patch_keyer(
    keyer_id: str, payload: KeyerUpdate, mixer: VisionMixer = Depends(get_mixer)
) -> DownstreamKeyer:
    try:
        return mixer.update_keyer(keyer_id, **payload.model_dump(exclude_unset=True))
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


@router.get(
    "/preview/jpeg/{stream_id:path}",
    tags=["gui"],
    summary="JPEG snapshot of a source or ME bus (WebRTC fallback)",
)
def preview_jpeg(stream_id: str, mixer: VisionMixer = Depends(get_mixer)) -> Response:
    payload = render_jpeg(mixer, stream_id)
    return Response(content=payload, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@router.post(
    "/webrtc/whep/{stream_id:path}",
    tags=["gui"],
    summary="WHEP: browser sends SDP offer, mixer answers with a WebRTC video preview",
)
async def webrtc_whep(
    stream_id: str, request: Request, mixer: VisionMixer = Depends(get_mixer)
) -> Response:
    if not webrtc_available():
        raise HTTPException(
            status_code=501,
            detail="WebRTC preview requires aiortc. JPEG fallback is at /api/v1/preview/jpeg/{stream_id}",
        )
    offer = (await request.body()).decode("utf-8")
    if not offer.strip():
        raise HTTPException(status_code=400, detail="SDP offer required")
    try:
        answer = await create_whep_answer(mixer, stream_id, offer)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"WebRTC negotiation failed: {exc}") from exc
    return Response(content=answer, media_type="application/sdp")
