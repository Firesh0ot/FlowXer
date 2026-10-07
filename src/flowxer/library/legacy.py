"""Non-destructive import of legacy storage/clips and storage/stingers."""

from __future__ import annotations

import logging
import time
from pathlib import Path

from flowxer.engine.stinger import inspect_stinger
from flowxer.library.convert import ConversionQueue, copy_original_into_item
from flowxer.library.models import ConvertOptions, LibraryItem, LibraryKind
from flowxer.library.store import LibraryStore, new_item_id

log = logging.getLogger(__name__)

CLIP_SUFFIXES = {".mp4", ".mov", ".mkv", ".ts", ".mxf", ".wav", ".m4a", ".webm", ".m4v"}


def import_legacy_clips(
    store: LibraryStore,
    clips_dir: Path,
    queue: ConversionQueue,
    format_id: str,
) -> list[LibraryItem]:
    imported: list[LibraryItem] = []
    if not clips_dir.is_dir():
        return imported
    for path in sorted(clips_dir.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in CLIP_SUFFIXES:
            continue
        rel = str(path.relative_to(clips_dir))
        existing = store.find_by_legacy_path(rel)
        if existing:
            continue
        item_id = new_item_id()
        rel_original = copy_original_into_item(store, item_id, path, preferred_name=path.name)
        item = LibraryItem(
            id=item_id,
            kind=LibraryKind.clip,
            name=path.name,
            created_at=time.time(),
            updated_at=time.time(),
            original={"path": rel_original, "legacy": rel},
            options=ConvertOptions(),
            source="legacy_clip",
            legacy_path=rel,
        )
        store.save(item)
        queue.enqueue(item.id, format_id)
        imported.append(item)
        log.info("imported legacy clip %s as library item %s", rel, item_id)
    return imported


def import_legacy_stingers(
    store: LibraryStore,
    stingers_dir: Path,
    queue: ConversionQueue,
    format_id: str,
    fps: float = 50.0,
) -> list[LibraryItem]:
    imported: list[LibraryItem] = []
    if not stingers_dir.is_dir():
        return imported
    for path in sorted(stingers_dir.iterdir()):
        if not path.is_dir():
            continue
        stinger_id = path.name
        info = inspect_stinger(stingers_dir, stinger_id, fps=fps)
        if info is None:
            continue
        legacy_key = f"stingers/{stinger_id}"
        existing = store.find_by_legacy_path(legacy_key)
        if existing:
            continue
        # Also skip if an item already uses this id as name/legacy.
        for item in store.list_items(kind=LibraryKind.stinger):
            if item.name == stinger_id or item.legacy_path == legacy_key:
                existing = item
                break
        if existing:
            continue

        item_id = new_item_id()
        if info.kind == "sequence":
            rel_original = copy_original_into_item(store, item_id, path)
            source_kind = "sequence"
            original = {
                "path": rel_original,
                "source_kind": source_kind,
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
                videos = [p for p in path.iterdir() if p.suffix.lower() in CLIP_SUFFIXES | {".webm", ".m4v"}]
                media = videos[0] if videos else None
            if media is None:
                continue
            rel_original = copy_original_into_item(store, item_id, media)
            original = {
                "path": rel_original,
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
            id=item_id,
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
        queue.enqueue(item.id, format_id)
        imported.append(item)
        log.info("imported legacy stinger %s as library item %s", stinger_id, item_id)
    return imported
