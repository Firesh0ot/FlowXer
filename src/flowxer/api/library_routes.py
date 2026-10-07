"""Media library HTTP surface: items, uploads, conversion jobs."""

from __future__ import annotations

import shutil

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import FileResponse
from starlette.concurrency import run_in_threadpool

from flowxer.api.schemas import (
    ConvertJobOut,
    ConvertOptionsIn,
    LibraryItemOut,
    LibraryItemPatch,
    ReconvertRequest,
    UploadInitRequest,
    UploadInitResponse,
)
from flowxer.engine.mixer import VisionMixer
from flowxer.library.models import ConvertOptions, FitMode, FpsMode, LibraryKind, UploadMode
from flowxer.library.multipart import receive_files
from flowxer.library.uploads import UploadBusy, UploadError, UploadNoSpace, UploadTooLarge

router = APIRouter(tags=["library"])

# Frames of one TGA folder upload.
MAX_SEQUENCE_FILES = 10_000


def get_mixer() -> VisionMixer:
    raise RuntimeError("mixer dependency is overridden in app startup")


def _upload_http(exc: ValueError) -> HTTPException:
    if isinstance(exc, UploadTooLarge):
        code = 413
    elif isinstance(exc, UploadNoSpace):
        code = 507
    elif isinstance(exc, UploadBusy):
        code = status.HTTP_409_CONFLICT
    else:
        code = 422
    return HTTPException(code, detail=str(exc))


def _content_length(request: Request) -> int | None:
    value = request.headers.get("content-length", "")
    return int(value) if value.isdigit() else None


def _options(payload: ConvertOptionsIn | None) -> ConvertOptions:
    if payload is None:
        return ConvertOptions()
    return ConvertOptions(
        fit=FitMode(payload.fit) if payload.fit in FitMode._value2member_map_ else FitMode.fit,
        fps_mode=FpsMode(payload.fps_mode) if payload.fps_mode in FpsMode._value2member_map_ else FpsMode.drop,
        loudness=payload.loudness,
        crossfade_ms=payload.crossfade_ms,
        map_channels=payload.map_channels,
        sequence_fps=payload.sequence_fps,
        cut_frame=payload.cut_frame,
        cut_ms=payload.cut_ms,
        tags=list(payload.tags),
    )


def _item_out(mixer: VisionMixer, item) -> LibraryItemOut:
    return mixer.library_item_out(item)


@router.get(
    "/library",
    response_model=list[LibraryItemOut],
    summary="List media library items",
)
def list_library(
    kind: str | None = None,
    q: str = "",
    mixer: VisionMixer = Depends(get_mixer),
) -> list[LibraryItemOut]:
    parsed: LibraryKind | None = None
    if kind:
        try:
            parsed = LibraryKind(kind)
        except ValueError as exc:
            raise HTTPException(422, detail="kind must be clip or stinger") from exc
    return [_item_out(mixer, item) for item in mixer.library.list_items(kind=parsed, query=q)]


@router.get(
    "/library/{item_id}",
    response_model=LibraryItemOut,
    summary="Get one library item",
)
def get_library_item(item_id: str, mixer: VisionMixer = Depends(get_mixer)) -> LibraryItemOut:
    item = mixer.library.get(item_id)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="unknown library item")
    return _item_out(mixer, item)


@router.patch(
    "/library/{item_id}",
    response_model=LibraryItemOut,
    summary="Rename, retag, or update stinger cut frame",
)
def patch_library_item(
    item_id: str,
    payload: LibraryItemPatch,
    mixer: VisionMixer = Depends(get_mixer),
) -> LibraryItemOut:
    try:
        item = mixer.library.patch(
            item_id,
            name=payload.name,
            tags=payload.tags,
            cut_frame=payload.cut_frame,
            cut_ms=payload.cut_ms,
        )
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="unknown library item") from exc
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from exc
    mixer.refresh_library_bindings()
    return _item_out(mixer, item)


@router.delete(
    "/library/{item_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a library item (409 if in use)",
)
def delete_library_item(item_id: str, mixer: VisionMixer = Depends(get_mixer)) -> None:
    try:
        mixer.library.delete(item_id)
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="unknown library item") from exc
    except PermissionError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.post(
    "/library/{item_id}/reconvert",
    response_model=ConvertJobOut,
    summary="Re-queue conversion for the current mixer format",
)
def reconvert_library_item(
    item_id: str,
    payload: ReconvertRequest | None = None,
    mixer: VisionMixer = Depends(get_mixer),
) -> ConvertJobOut:
    try:
        job = mixer.library.reconvert(item_id, _options(payload.options if payload else None))
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="unknown library item") from exc
    return ConvertJobOut(**job.model_dump())


@router.get(
    "/library/{item_id}/thumb.jpg",
    summary="Thumbnail JPEG for a library item",
)
def library_thumb(item_id: str, mixer: VisionMixer = Depends(get_mixer)) -> FileResponse:
    if mixer.library.get(item_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="unknown library item")
    path = mixer.library.store.thumb_path(item_id)
    if not path.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="thumbnail not ready")
    return FileResponse(path, media_type="image/jpeg")


@router.get(
    "/jobs",
    response_model=list[ConvertJobOut],
    summary="List conversion jobs",
)
def list_jobs(mixer: VisionMixer = Depends(get_mixer)) -> list[ConvertJobOut]:
    return [ConvertJobOut(**job.model_dump()) for job in mixer.library.queue.list_jobs()]


@router.post(
    "/jobs/{job_id}/cancel",
    response_model=ConvertJobOut,
    summary="Cancel a queued or running conversion job",
)
def cancel_job(job_id: str, mixer: VisionMixer = Depends(get_mixer)) -> ConvertJobOut:
    job = mixer.library.queue.cancel(job_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="unknown job")
    return ConvertJobOut(**job.model_dump())


@router.post(
    "/uploads",
    response_model=UploadInitResponse,
    summary="Start a chunked upload (413 over FLOWXER_UPLOAD_LIMIT_GB, 507 without disk space)",
)
def upload_init(payload: UploadInitRequest, mixer: VisionMixer = Depends(get_mixer)) -> UploadInitResponse:
    try:
        kind = LibraryKind(payload.kind)
        mode = UploadMode(payload.mode)
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from exc
    try:
        session = mixer.library.begin_upload(
            name=payload.name,
            size=payload.size,
            kind=kind,
            mode=mode,
            options=_options(payload.options),
        )
    except ValueError as exc:
        raise _upload_http(exc) from exc
    return UploadInitResponse(id=session.id, chunk_size=session.chunk_size, received=session.received)


@router.put(
    "/uploads/{upload_id}/chunks/{index}",
    response_model=UploadInitResponse,
    summary="Upload one chunk (raw body): chunk_size bytes, the last one the rest",
)
async def upload_chunk(
    upload_id: str,
    index: int,
    request: Request,
    mixer: VisionMixer = Depends(get_mixer),
) -> UploadInitResponse:
    try:
        session = await mixer.library.uploads.receive_chunk(
            upload_id, index, request.stream(), _content_length(request)
        )
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="unknown upload") from exc
    except ValueError as exc:
        raise _upload_http(exc) from exc
    return UploadInitResponse(id=session.id, chunk_size=session.chunk_size, received=session.received)


@router.get(
    "/uploads/{upload_id}",
    response_model=UploadInitResponse,
    summary="Upload session status",
)
def upload_status(upload_id: str, mixer: VisionMixer = Depends(get_mixer)) -> UploadInitResponse:
    session = mixer.library.uploads.get(upload_id)
    if session is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="unknown upload")
    return UploadInitResponse(id=session.id, chunk_size=session.chunk_size, received=session.received)


@router.post(
    "/uploads/{upload_id}/complete",
    response_model=LibraryItemOut,
    status_code=status.HTTP_201_CREATED,
    summary="Turn the uploaded chunks into a library item and queue its conversion",
)
def upload_complete(upload_id: str, mixer: VisionMixer = Depends(get_mixer)) -> LibraryItemOut:
    try:
        item = mixer.library.complete_upload(upload_id)
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="unknown upload") from exc
    except ValueError as exc:
        raise _upload_http(exc) from exc
    return _item_out(mixer, item)


@router.post(
    "/uploads/sequence",
    response_model=LibraryItemOut,
    status_code=status.HTTP_201_CREATED,
    summary="Upload a TGA folder (multipart: name, sequence_fps, cut_frame, fit, files) as a stinger",
)
async def upload_sequence(request: Request, mixer: VisionMixer = Depends(get_mixer)) -> LibraryItemOut:
    library = mixer.library
    stage = None
    try:
        stage = library.uploads.new_stage(_content_length(request) or 0)
        fields, files = await receive_files(
            request.stream(),
            request.headers.get("content-type", ""),
            stage,
            max_bytes=library.uploads.upload_limit_bytes,
            max_files=MAX_SEQUENCE_FILES,
            keep=lambda name: name.lower().endswith(".tga"),
        )
        if not files:
            raise UploadError("no .tga files in the upload")
        fit = fields.get("fit", "fit")
        options = ConvertOptions(
            fit=FitMode(fit) if fit in FitMode._value2member_map_ else FitMode.fit,
            sequence_fps=float(fields["sequence_fps"]) if fields.get("sequence_fps") else None,
            cut_frame=int(fields["cut_frame"]) if fields.get("cut_frame") else None,
        )
        item = await run_in_threadpool(
            library.ingest_sequence_dir, stage, name=fields.get("name") or "sequence", options=options
        )
    except ValueError as exc:
        raise _upload_http(exc) from exc
    finally:
        if stage is not None:
            shutil.rmtree(stage, ignore_errors=True)
    return _item_out(mixer, item)
