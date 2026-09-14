from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from flowxer.api.schemas import (
    DomainInfo,
    ErrorBody,
    FlowDescriptor,
    HealthResponse,
    LogicalInput,
    LogicalInputCreate,
    LogicalInputUpdate,
    MixerCommandResponse,
    MixerStartRequest,
    MixerStatus,
    OverlayStatus,
    OverlayUpdate,
    PreviewRequest,
    ReplayLoadRequest,
    ReplayTransitionRequest,
    StingerInfo,
    StingerPlayRequest,
    StorageClip,
    TakeRequest,
)
from flowxer.domain.mxl_domain import load_domain_info
from flowxer.engine.capabilities import probe_backend
from flowxer.engine.mixer import MixerError, VisionMixer
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
    summary="Update essence mapping or clip path on a logical input",
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
        body = mixer.take(payload.input_id, payload.transition, payload.stinger_id)
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
        body = mixer.set_preview(payload.input_id)
    except MixerError as exc:
        raise _http(exc)
    return MixerCommandResponse(status="preview", mixer=body)


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
    summary="List TGA-sequence stingers (live ↔ replay)",
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
    summary="Play a TGA sequence stinger and cut program at the opaque frame",
)
def stinger_play(
    payload: StingerPlayRequest, mixer: VisionMixer = Depends(get_mixer)
) -> MixerCommandResponse:
    try:
        body = mixer.play_stinger(payload.stinger_id, payload.target_input_id, payload.direction)
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
