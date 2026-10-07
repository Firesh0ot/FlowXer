"""High-level library orchestration used by the API and mixer."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import threading
import time
from collections.abc import Callable
from pathlib import Path

from flowxer.engine.security import SecurityError
from flowxer.library.convert import ConversionQueue
from flowxer.library.legacy import import_legacy_clips, import_legacy_stingers
from flowxer.library.models import (
    ConversionStatus,
    ConvertJob,
    ConvertOptions,
    LibraryItem,
    LibraryKind,
    UploadMode,
    UploadSession,
)
from flowxer.library.sequence import inspect_tga_zip, validate_sequence
from flowxer.library.store import LibraryStore, new_item_id
from flowxer.library.uploads import UploadManager
from flowxer.settings import Settings

log = logging.getLogger(__name__)

# Legacy paths of imported items that were deleted: never imported again.
_LEGACY_DELETED = "_legacy_deleted.json"
# Import-dir files ingested (or failed) while the dir could not be written: name|size|mtime.
_IMPORT_SEEN = "_import_seen.json"


def _safe_suffix(name: str) -> str:
    suffix = Path(name).suffix.lower()
    return suffix if re.fullmatch(r"\.[a-z0-9]{1,10}", suffix) else ""


def _place(source: Path, dest: Path, *, move: bool) -> None:
    """Move (a rename on the same volume) or copy a file or folder into the item."""
    if move:
        try:
            os.replace(source, dest)
        except OSError:
            shutil.move(str(source), str(dest))
    elif source.is_dir():
        shutil.copytree(source, dest)
    else:
        shutil.copy2(source, dest)


def _sequence_original(info, fps: float, path: str = "sequence") -> dict:
    return {
        "path": path,
        "source_kind": "sequence",
        "frame_count": info.frame_count,
        "width": info.width,
        "height": info.height,
        "has_alpha": info.has_alpha,
        "pattern": info.pattern,
        "start_number": info.start_number,
        "fps": fps,
    }


class LibraryService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.store = LibraryStore(settings.resolved_library_dir)
        self.uploads = UploadManager(
            settings.resolved_library_dir / "_uploads",
            settings.upload_limit_bytes,
        )
        self.queue = ConversionQueue(
            self.store,
            concurrency=settings.convert_concurrency,
            ram_clip_max_s=settings.ram_clip_max_s,
            ram_budget_mb=settings.ram_budget_mb,
            on_update=self._job_update,
        )
        # Set by the mixer: the inputs/slots that use an item (derived from the mixer state,
        # so it is right after a restart or a config import).
        self.consumers_fn: Callable[[str], list[str]] = lambda _item_id: []
        self._listeners: list[Callable[[ConvertJob], None]] = []
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._import_thread: threading.Thread | None = None
        self._legacy_thread: threading.Thread | None = None
        self.legacy_import_done = threading.Event()
        self._format_id = "1080p50"
        self._fps = settings.fps

    # ── lifecycle ────────────────────────────────────────────────────────────

    def start(self, format_id: str, fps: float | None = None) -> None:
        self._format_id = format_id
        if fps is not None:
            self._fps = fps
        self._stop.clear()
        self.queue.start()
        self._resume_pending()
        # Legacy storage is imported after start: the API must answer (liveness probe) first.
        self._legacy_thread = threading.Thread(target=self._legacy_loop, name="flowxer-legacy-import", daemon=True)
        self._legacy_thread.start()
        self._start_import_watcher()

    def stop(self) -> None:
        self._stop.set()
        deadline = time.monotonic() + 2.0
        for thread in (self._import_thread, self._legacy_thread):
            if thread and thread.is_alive():
                thread.join(timeout=max(0.0, deadline - time.monotonic()))
        self.queue.stop(timeout=3.0)

    def add_listener(self, listener: Callable[[ConvertJob], None]) -> None:
        self._listeners.append(listener)

    def _job_update(self, job: ConvertJob) -> None:
        for listener in list(self._listeners):
            try:
                listener(job)
            except Exception:  # pragma: no cover - listener errors must not kill workers
                log.exception("library listener failed")

    def _resume_pending(self) -> None:
        """Conversions cut short by a restart are queued again."""
        for item in self.store.list_items():
            status = item.conversion_for(self._format_id).status
            if status in {ConversionStatus.queued, ConversionStatus.converting}:
                self.queue.enqueue(item.id, self._format_id, low_priority=item.source != "upload")

    def set_format(self, format_id: str, fps: float | None = None) -> list[ConvertJob]:
        """Re-convert all items when the mixer format changes (caller ensures off-air)."""
        self._format_id = format_id
        if fps is not None:
            self._fps = fps
        jobs: list[ConvertJob] = []
        for item in self.store.list_items():
            conv = item.conversion_for(format_id)
            if conv.status == ConversionStatus.ready and self.store.mezz_path(item.id, format_id).is_file():
                continue
            jobs.append(self.queue.enqueue(item.id, format_id))
        return jobs

    def ensure_format(self, format_id: str) -> None:
        self._format_id = format_id

    # ── usage tracking ───────────────────────────────────────────────────────

    def consumers(self, item_id: str) -> list[str]:
        return sorted(self.consumers_fn(item_id))

    def is_in_use(self, item_id: str) -> bool:
        return bool(self.consumers(item_id))

    # ── queries ──────────────────────────────────────────────────────────────

    def list_items(self, kind: LibraryKind | None = None, query: str = "") -> list[LibraryItem]:
        return self.store.list_items(kind=kind, query=query)

    def get(self, item_id: str) -> LibraryItem | None:
        try:
            return self.store.load(item_id)
        except SecurityError:
            return None  # not a valid id: no such item

    def patch(self, item_id: str, *, name: str | None = None, tags: list[str] | None = None,
              cut_frame: int | None = None, cut_ms: int | None = None) -> LibraryItem:
        from flowxer.engine.stinger import cut_frame_from_ms, cut_ms_from_frame

        def change(item: LibraryItem) -> None:
            nonlocal cut_frame
            if name is not None:
                item.name = name
            if tags is not None:
                item.tags = tags
            if cut_frame is not None or cut_ms is not None:
                if item.kind != LibraryKind.stinger:
                    raise ValueError("cut_frame/cut_ms only apply to stingers")
                conv = item.conversion_for(self._format_id)
                frame_count = conv.frames or int(item.original.get("frame_count") or 0)
                fps = self._fps
                if cut_ms is not None and cut_frame is None:
                    cut_frame = cut_frame_from_ms(cut_ms, fps, max(frame_count, 1))
                if frame_count > 0:
                    cut_frame = max(0, min(cut_frame, frame_count - 1))
                item.cut_frame = cut_frame
                item.cut_ms = cut_ms_from_frame(cut_frame, fps)
                item.options.cut_frame = item.cut_frame
                item.options.cut_ms = item.cut_ms
                if conv.status == ConversionStatus.ready:
                    conv.cut_frame = item.cut_frame
                    conv.cut_ms = item.cut_ms
                    item.conversions[self._format_id] = conv

        if self.get(item_id) is None:
            raise KeyError(item_id)
        updated = self.store.update(item_id, change)
        if updated is None:
            raise KeyError(item_id)
        return updated

    def delete(self, item_id: str) -> None:
        item = self.get(item_id)
        if item is None:
            raise KeyError(item_id)
        if self.is_in_use(item_id):
            raise PermissionError(f"item in use by: {', '.join(self.consumers(item_id))}")
        # A running conversion is killed first, so it cannot write the item back.
        self.queue.cancel_item(item_id, wait_s=5.0)
        if item.legacy_path:
            self._remember(_LEGACY_DELETED, item.legacy_path)
        self.store.delete(item_id)

    def reconvert(self, item_id: str, options: ConvertOptions | None = None) -> ConvertJob:
        if self.get(item_id) is None:
            raise KeyError(item_id)
        return self.queue.enqueue(item_id, self._format_id, options)

    def mezzanine_path(self, item_id: str, format_id: str | None = None) -> Path | None:
        fmt = format_id or self._format_id
        item = self.get(item_id)
        if item is None or not item.is_ready(fmt):
            return None
        path = self.store.mezz_path(item_id, fmt)
        return path if path.is_file() else None

    def playback_mode(self, item_id: str, format_id: str | None = None) -> str:
        fmt = format_id or self._format_id
        item = self.get(item_id)
        if item is None:
            return "unknown"
        return item.conversion_for(fmt).playback

    # ── small persistent sets in the library dir ─────────────────────────────

    def _remembered(self, name: str) -> set[str]:
        try:
            return set(json.loads((self.store.root / name).read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError):
            return set()

    def _remember(self, name: str, value: str) -> None:
        with self._lock:
            values = self._remembered(name) | {value}
            path = self.store.root / name
            tmp = path.with_name(f"{name}.tmp")
            tmp.write_text(json.dumps(sorted(values)), encoding="utf-8")
            os.replace(tmp, path)

    # ── upload / ingest ──────────────────────────────────────────────────────

    def begin_upload(
        self,
        *,
        name: str,
        size: int,
        kind: LibraryKind,
        mode: UploadMode,
        options: ConvertOptions | None = None,
    ) -> UploadSession:
        return self.uploads.create(name=name, size=size, kind=kind, mode=mode, options=options)

    def complete_upload(self, upload_id: str) -> LibraryItem:
        """Move the uploaded data into a new item (a rename) and queue its conversion.
        A repeated call returns the same item."""
        session, data = self.uploads.begin_complete(upload_id)
        if data is None:
            item = self.get(session.item_id or "")
            if item is None:
                raise KeyError(upload_id)
            return item
        try:
            item = self.ingest_path(
                data,
                kind=session.kind,
                mode=session.mode,
                name=session.name,
                options=session.options,
                move=True,
            )
        except Exception:
            self.uploads.cleanup(upload_id)
            raise
        self.uploads.finish_complete(upload_id, item.id)
        return item

    def ingest_path(
        self,
        path: Path,
        *,
        kind: LibraryKind,
        mode: UploadMode = UploadMode.video,
        name: str | None = None,
        options: ConvertOptions | None = None,
        move: bool = False,
    ) -> LibraryItem:
        """Make a library item from a file or TGA folder. Only quick checks run here (a ZIP
        is unpacked by its conversion job). On any error no item directory is left."""
        options = options or ConvertOptions()
        display_name = name or path.name
        zipped = path.is_file() and (
            mode == UploadMode.zip
            or (path.suffix.lower() == ".zip" and (kind == LibraryKind.stinger or mode == UploadMode.image_sequence))
        )
        if (zipped or mode != UploadMode.video) and kind != LibraryKind.stinger:
            raise ValueError("TGA sequences and ZIPs are stingers")
        item_id = new_item_id()
        directory = self.store.item_dir(item_id)
        directory.mkdir(parents=True)
        try:
            if zipped:
                inspect_tga_zip(path)
                _place(path, directory / "original.zip", move=move)
                original = {"path": "original.zip", "source_kind": "zip"}
                options.sequence_fps = options.sequence_fps or self._fps
            elif mode != UploadMode.video:
                if not path.is_dir():
                    raise ValueError("image_sequence upload requires a folder or zip")
                info = validate_sequence(path)
                if not info.ok:
                    errors = "; ".join(i.message for i in info.issues if i.level == "error")
                    raise ValueError(errors or "invalid sequence")
                _place(path, directory / "sequence", move=move)
                options.sequence_fps = options.sequence_fps or self._fps
                original = _sequence_original(info, options.sequence_fps)
            else:
                # The client's name is only shown: the file is stored as original<ext>.
                rel = "original" + _safe_suffix(display_name)
                _place(path, directory / rel, move=move)
                original = {"path": rel, "source_kind": "video"}
            item = LibraryItem(
                id=item_id,
                kind=kind,
                name=display_name,
                created_at=time.time(),
                updated_at=time.time(),
                original=original,
                options=options,
                tags=list(options.tags),
                cut_frame=options.cut_frame,
                cut_ms=options.cut_ms,
                has_alpha=bool(original.get("has_alpha", kind == LibraryKind.stinger)),
                source="upload",
            )
            self.store.save(item)
        except BaseException:
            shutil.rmtree(directory, ignore_errors=True)
            raise
        self.queue.enqueue(item.id, self._format_id)
        return item

    def ingest_sequence_dir(self, stage: Path, *, name: str, options: ConvertOptions | None = None) -> LibraryItem:
        """A TGA folder written by POST /uploads/sequence (moved, not copied)."""
        return self.ingest_path(
            stage,
            kind=LibraryKind.stinger,
            mode=UploadMode.image_sequence,
            name=name,
            options=options,
            move=True,
        )

    # ── legacy / import dir ──────────────────────────────────────────────────

    def import_legacy(self) -> dict[str, int]:
        deleted = self._remembered(_LEGACY_DELETED)
        clips = import_legacy_clips(
            self.store, self.settings.clips_dir, self.queue, self._format_id, skip=deleted, stop=self._stop
        )
        stingers = import_legacy_stingers(
            self.store,
            self.settings.stingers_dir,
            self.queue,
            self._format_id,
            fps=self._fps,
            skip=deleted,
            stop=self._stop,
        )
        return {"clips": len(clips), "stingers": len(stingers)}

    def _legacy_loop(self) -> None:
        try:
            counts = self.import_legacy()
            if any(counts.values()):
                log.info("legacy storage imported: %s", counts)
        except Exception:
            log.exception("legacy import failed")
        finally:
            self.legacy_import_done.set()

    def _start_import_watcher(self) -> None:
        import_dir = self.settings.import_dir
        if not import_dir:
            return
        import_dir.mkdir(parents=True, exist_ok=True)
        processing = import_dir / ".processing"
        if processing.is_dir():
            # Left by a process that stopped during an ingest: try them again.
            for leftover in processing.iterdir():
                try:
                    os.replace(leftover, import_dir / leftover.name)
                except OSError:
                    log.warning("cannot return %s to the import dir", leftover)

        def _loop() -> None:
            seen: dict[str, tuple[int, int]] = {}
            while not self._stop.is_set():
                try:
                    self._scan_import_dir(import_dir, seen)
                except Exception:
                    log.exception("import watcher failed")
                self._stop.wait(2.0)

        self._import_thread = threading.Thread(target=_loop, name="flowxer-import", daemon=True)
        self._import_thread.start()

    def _scan_import_dir(self, import_dir: Path, seen: dict[str, tuple[int, int]]) -> None:
        """Ingest each file once its size and mtime held still for one scan. A writable
        dir: the file moves to .processing/ and then into the library, or to .failed/.
        A read-only dir: the file is copied and remembered (name, size, mtime), so it is
        never imported again, also after a failure."""
        done = None
        for path in sorted(import_dir.iterdir()):
            if path.name.startswith(".") or not path.is_file():
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            key = (stat.st_size, stat.st_mtime_ns)
            if seen.get(path.name) != key:
                seen[path.name] = key
                continue
            seen.pop(path.name, None)
            if done is None:
                done = self._remembered(_IMPORT_SEEN)
            remembered = f"{path.name}|{key[0]}|{key[1]}"
            if remembered in done:
                continue
            self._import_one(import_dir, path, remembered)

    def _import_one(self, import_dir: Path, path: Path, remembered: str) -> None:
        zipped = path.suffix.lower() == ".zip"
        kind = LibraryKind.stinger if zipped else LibraryKind.clip
        mode = UploadMode.zip if zipped else UploadMode.video
        claimed = import_dir / ".processing" / path.name
        try:
            claimed.parent.mkdir(exist_ok=True)
            os.replace(path, claimed)
            movable = True
        except OSError:
            claimed, movable = path, False
        try:
            item = self.ingest_path(claimed, kind=kind, mode=mode, name=path.name, move=movable)
            log.info("imported %s from the import dir as library item %s", path.name, item.id)
        except Exception as exc:
            log.warning("cannot import %s from the import dir: %s", path.name, exc)
            if movable:
                failed = import_dir / ".failed" / path.name
                try:
                    failed.parent.mkdir(exist_ok=True)
                    if failed.exists():
                        failed = failed.with_name(f"{int(time.time())}-{path.name}")
                    os.replace(claimed, failed)
                except OSError:
                    log.exception("cannot move %s to .failed", claimed)
        if not movable:
            self._remember(_IMPORT_SEEN, remembered)
