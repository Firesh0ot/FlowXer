"""Non-destructive import of legacy storage/clips and storage/stingers.

Legacy files are referenced in place (item.json names their absolute path), never copied:
the import only writes item.json files, so it is quick, needs no disk space, and an
interrupted run leaves nothing half done. Conversions are queued at low priority.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Iterable
from pathlib import Path

from flowxer.engine.stinger import inspect_stinger
from flowxer.library.convert import ConversionQueue
from flowxer.library.models import ConvertOptions, LibraryItem, LibraryKind
from flowxer.library.store import LibraryStore, new_item_id

log = logging.getLogger(__name__)

CLIP_SUFFIXES = {".mp4", ".mov", ".mkv", ".ts", ".mxf", ".wav", ".m4a", ".webm", ".m4v"}


def _known_legacy_paths(store: LibraryStore) -> set[str]:
    return {item.legacy_path for item in store.list_items() if item.legacy_path}


def import_legacy_clips(
    store: LibraryStore,
    clips_dir: Path,
    queue: ConversionQueue,
    format_id: str,
    *,
    skip: Iterable[str] = (),
    stop: threading.Event | None = None,
) -> list[LibraryItem]:
    imported: list[LibraryItem] = []
    if not clips_dir.is_dir():
        return imported
    known = _known_legacy_paths(store) | set(skip)
    for path in sorted(clips_dir.rglob("*")):
        if stop is not None and stop.is_set():
            break
        if not path.is_file() or path.suffix.lower() not in CLIP_SUFFIXES:
            continue
        rel = str(path.relative_to(clips_dir).as_posix())
        if rel in known:
            continue
        item = LibraryItem(
            id=new_item_id(),
            kind=LibraryKind.clip,
            name=path.name,
            created_at=time.time(),
            updated_at=time.time(),
            original={"path": str(path.resolve()), "source_kind": "video", "legacy": rel},
            options=ConvertOptions(),
            source="legacy_clip",
            legacy_path=rel,
        )
        store.save(item)
        queue.enqueue(item.id, format_id, low_priority=True)
        imported.append(item)
        log.info("imported legacy clip %s as library item %s", rel, item.id)
    return imported


def import_legacy_stingers(
    store: LibraryStore,
    stingers_dir: Path,
    queue: ConversionQueue,
    format_id: str,
    fps: float = 50.0,
    *,
    skip: Iterable[str] = (),
    stop: threading.Event | None = None,
) -> list[LibraryItem]:
    imported: list[LibraryItem] = []
    if not stingers_dir.is_dir():
        return imported
    known = _known_legacy_paths(store) | set(skip)
    for path in sorted(stingers_dir.iterdir()):
        if stop is not None and stop.is_set():
            break
        if not path.is_dir():
            continue
        stinger_id = path.name
        legacy_key = f"stingers/{stinger_id}"
        if legacy_key in known:
            continue
        info = inspect_stinger(stingers_dir, stinger_id, fps=fps)
        if info is None:
            continue
        if info.kind == "sequence":
            original = {
                "path": str(path.resolve()),
                "source_kind": "sequence",
                "frame_count": info.frame_count,
                "width": info.width,
                "height": info.height,
                "has_alpha": info.has_alpha,
                "pattern": info.pattern,
                "fps": info.fps,
            }
        else:
            media = Path(info.media_path) if info.media_path else None
            if media is None or not media.is_file():
                # Prefer a video file inside the stinger directory.
                videos = [p for p in path.iterdir() if p.suffix.lower() in CLIP_SUFFIXES]
                media = videos[0] if videos else None
            if media is None:
                continue
            original = {
                "path": str(media.resolve()),
                "source_kind": "video",
                "frame_count": info.frame_count,
                "width": info.width,
                "height": info.height,
                "has_alpha": info.has_alpha,
                "fps": info.fps,
            }
        options = ConvertOptions(
            sequence_fps=info.fps,
            cut_frame=info.cut_frame,
            cut_ms=info.cut_ms,
        )
        item = LibraryItem(
            id=new_item_id(),
            kind=LibraryKind.stinger,
            name=stinger_id,
            created_at=time.time(),
            updated_at=time.time(),
            original=original,
            options=options,
            cut_frame=info.cut_frame,
            cut_ms=info.cut_ms,
            has_alpha=info.has_alpha,
            source="legacy_stinger",
            legacy_path=legacy_key,
            tags=[stinger_id],
        )
        store.save(item)
        queue.enqueue(item.id, format_id, low_priority=True)
        imported.append(item)
        log.info("imported legacy stinger %s as library item %s", stinger_id, item.id)
    return imported
