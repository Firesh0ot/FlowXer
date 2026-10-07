"""High-level library orchestration used by the API and mixer."""

from __future__ import annotations

import logging
import shutil
import threading
import time
from pathlib import Path

from flowxer.library.convert import ConversionQueue, copy_original_into_item
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
from flowxer.library.sequence import safe_extract_tga_zip, validate_sequence
from flowxer.library.store import LibraryStore, new_item_id
from flowxer.library.uploads import UploadManager
from flowxer.settings import Settings

log = logging.getLogger(__name__)


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
        )
        self._in_use: dict[str, set[str]] = {}
        self._lock = threading.RLock()
        self._import_stop = threading.Event()
        self._import_thread: threading.Thread | None = None
        self._format_id = "1080p50"
        self._fps = settings.fps

    # ── lifecycle ────────────────────────────────────────────────────────────

    def start(self, format_id: str, fps: float | None = None) -> None:
        self._format_id = format_id
        if fps is not None:
            self._fps = fps
        self.queue.start()
        self.import_legacy()
        self._start_import_watcher()

    def stop(self) -> None:
        self._import_stop.set()
        if self._import_thread and self._import_thread.is_alive():
            self._import_thread.join(timeout=2)
        self.queue.stop()

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

    def mark_in_use(self, item_id: str, consumer: str) -> None:
        with self._lock:
            self._in_use.setdefault(item_id, set()).add(consumer)

    def mark_free(self, item_id: str, consumer: str) -> None:
        with self._lock:
            users = self._in_use.get(item_id)
            if not users:
                return
            users.discard(consumer)
            if not users:
                self._in_use.pop(item_id, None)

    def consumers(self, item_id: str) -> list[str]:
        with self._lock:
            return sorted(self._in_use.get(item_id, set()))

    def is_in_use(self, item_id: str) -> bool:
        return bool(self.consumers(item_id))

    # ── queries ──────────────────────────────────────────────────────────────

    def list_items(self, kind: LibraryKind | None = None, query: str = "") -> list[LibraryItem]:
        return self.store.list_items(kind=kind, query=query)

    def get(self, item_id: str) -> LibraryItem | None:
        return self.store.load(item_id)

    def patch(self, item_id: str, *, name: str | None = None, tags: list[str] | None = None,
              cut_frame: int | None = None, cut_ms: int | None = None) -> LibraryItem:
        item = self.store.load(item_id)
        if item is None:
            raise KeyError(item_id)
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
                from flowxer.engine.stinger import cut_frame_from_ms

                cut_frame = cut_frame_from_ms(cut_ms, fps, max(frame_count, 1))
            if cut_frame is not None:
                if frame_count > 0:
                    cut_frame = max(0, min(cut_frame, frame_count - 1))
                from flowxer.engine.stinger import cut_ms_from_frame

                item.cut_frame = cut_frame
                item.cut_ms = cut_ms_from_frame(cut_frame, fps)
                item.options.cut_frame = item.cut_frame
                item.options.cut_ms = item.cut_ms
                if conv.status == ConversionStatus.ready:
                    conv.cut_frame = item.cut_frame
                    conv.cut_ms = item.cut_ms
                    item.conversions[self._format_id] = conv
        return self.store.save(item)

    def delete(self, item_id: str) -> None:
        if self.is_in_use(item_id):
            raise PermissionError(f"item in use by: {', '.join(self.consumers(item_id))}")
        self.store.delete(item_id)

    def reconvert(self, item_id: str, options: ConvertOptions | None = None) -> ConvertJob:
        item = self.store.load(item_id)
        if item is None:
            raise KeyError(item_id)
        return self.queue.enqueue(item_id, self._format_id, options)

    def mezzanine_path(self, item_id: str, format_id: str | None = None) -> Path | None:
        fmt = format_id or self._format_id
        item = self.store.load(item_id)
        if item is None or not item.is_ready(fmt):
            return None
        path = self.store.mezz_path(item_id, fmt)
        return path if path.is_file() else None

    def playback_mode(self, item_id: str, format_id: str | None = None) -> str:
        fmt = format_id or self._format_id
        item = self.store.load(item_id)
        if item is None:
            return "unknown"
        return item.conversion_for(fmt).playback

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

    def write_chunk(self, upload_id: str, index: int, data: bytes) -> UploadSession:
        return self.uploads.write_chunk(upload_id, index, data)

    def complete_upload(self, upload_id: str) -> LibraryItem:
        session = self.uploads.get(upload_id)
        if session is None:
            raise KeyError(upload_id)
        assembled = self.uploads.assemble(upload_id)
        try:
            item = self.ingest_path(
                assembled,
                kind=session.kind,
                mode=session.mode,
                name=session.name,
                options=session.options,
            )
        finally:
            self.uploads.cleanup(upload_id)
        return item

    def ingest_path(
        self,
        path: Path,
        *,
        kind: LibraryKind,
        mode: UploadMode = UploadMode.video,
        name: str | None = None,
        options: ConvertOptions | None = None,
    ) -> LibraryItem:
        options = options or ConvertOptions()
        item_id = new_item_id()
        directory = self.store.item_dir(item_id)
        directory.mkdir(parents=True, exist_ok=True)
        display_name = name or path.name

        if mode == UploadMode.zip or (mode == UploadMode.video and path.suffix.lower() == ".zip" and kind == LibraryKind.stinger):
            seq_dir = directory / "sequence"
            extracted = safe_extract_tga_zip(path, seq_dir.parent / "_zip_stage")
            # Move extracted tree to sequence/
            if seq_dir.exists():
                shutil.rmtree(seq_dir)
            shutil.move(str(extracted), str(seq_dir))
            stage = directory / "_zip_stage"
            if stage.exists():
                shutil.rmtree(stage, ignore_errors=True)
            info = validate_sequence(seq_dir)
            if not info.ok:
                shutil.rmtree(directory, ignore_errors=True)
                errors = "; ".join(i.message for i in info.issues if i.level == "error")
                raise ValueError(errors or "invalid sequence in zip")
            fps = options.sequence_fps or self._fps
            original = {
                "path": "sequence",
                "source_kind": "sequence",
                "frame_count": info.frame_count,
                "width": info.width,
                "height": info.height,
                "has_alpha": info.has_alpha,
                "pattern": info.pattern,
                "fps": fps,
            }
            options.sequence_fps = fps
        elif mode == UploadMode.image_sequence:
            # Assembled upload is expected to be a directory already staged, or a zip.
            if path.is_file() and path.suffix.lower() == ".zip":
                return self.ingest_path(path, kind=kind, mode=UploadMode.zip, name=name, options=options)
            if path.is_file():
                # Single file is invalid for image_sequence unless it's already a folder copy.
                raise ValueError("image_sequence upload requires a folder or zip")
            rel = copy_original_into_item(self.store, item_id, path)
            info = validate_sequence(directory / rel)
            if not info.ok:
                shutil.rmtree(directory, ignore_errors=True)
                errors = "; ".join(i.message for i in info.issues if i.level == "error")
                raise ValueError(errors or "invalid sequence")
            fps = options.sequence_fps or self._fps
            original = {
                "path": rel,
                "source_kind": "sequence",
                "frame_count": info.frame_count,
                "width": info.width,
                "height": info.height,
                "has_alpha": info.has_alpha,
                "pattern": info.pattern,
                "fps": fps,
            }
            options.sequence_fps = fps
        else:
            rel = copy_original_into_item(self.store, item_id, path, preferred_name=Path(display_name).name)
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
        self.queue.enqueue(item.id, self._format_id)
        return item

    def ingest_sequence_files(
        self,
        files: list[tuple[str, bytes]],
        *,
        name: str,
        options: ConvertOptions | None = None,
    ) -> LibraryItem:
        """Ingest a browser folder-drop (list of relative path + bytes)."""
        options = options or ConvertOptions()
        item_id = new_item_id()
        seq_dir = self.store.item_dir(item_id) / "sequence"
        seq_dir.mkdir(parents=True, exist_ok=True)
        for rel, data in files:
            rel_path = Path(rel)
            if ".." in rel_path.parts:
                raise ValueError(f"unsafe path: {rel}")
            if rel_path.suffix.lower() != ".tga":
                continue
            target = seq_dir / rel_path.name
            target.write_bytes(data)
        info = validate_sequence(seq_dir)
        if not info.ok:
            shutil.rmtree(self.store.item_dir(item_id), ignore_errors=True)
            errors = "; ".join(i.message for i in info.issues if i.level == "error")
            raise ValueError(errors or "invalid sequence")
        fps = options.sequence_fps or self._fps
        options.sequence_fps = fps
        item = LibraryItem(
            id=item_id,
            kind=LibraryKind.stinger,
            name=name,
            created_at=time.time(),
            updated_at=time.time(),
            original={
                "path": "sequence",
                "source_kind": "sequence",
                "frame_count": info.frame_count,
                "width": info.width,
                "height": info.height,
                "has_alpha": info.has_alpha,
                "pattern": info.pattern,
                "fps": fps,
            },
            options=options,
            tags=list(options.tags),
            cut_frame=options.cut_frame,
            cut_ms=options.cut_ms,
            has_alpha=info.has_alpha,
            source="upload",
        )
        self.store.save(item)
        self.queue.enqueue(item.id, self._format_id)
        return item

    # ── legacy / import dir ──────────────────────────────────────────────────

    def import_legacy(self) -> dict[str, int]:
        clips = import_legacy_clips(self.store, self.settings.clips_dir, self.queue, self._format_id)
        stingers = import_legacy_stingers(
            self.store,
            self.settings.stingers_dir,
            self.queue,
            self._format_id,
            fps=self._fps,
        )
        return {"clips": len(clips), "stingers": len(stingers)}

    def _start_import_watcher(self) -> None:
        import_dir = self.settings.import_dir
        if not import_dir:
            return
        import_dir.mkdir(parents=True, exist_ok=True)
        self._import_stop.clear()

        def _loop() -> None:
            seen: dict[str, tuple[int, float]] = {}
            while not self._import_stop.is_set():
                try:
                    self._scan_import_dir(import_dir, seen)
                except Exception:
                    log.exception("import watcher failed")
                self._import_stop.wait(2.0)

        self._import_thread = threading.Thread(target=_loop, name="flowxer-import", daemon=True)
        self._import_thread.start()

    def _scan_import_dir(self, import_dir: Path, seen: dict[str, tuple[int, float]]) -> None:
        for path in sorted(import_dir.iterdir()):
            if not path.is_file():
                continue
            if path.name.startswith("."):
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            key = str(path)
            prev = seen.get(key)
            if prev and prev[0] == stat.st_size and time.time() - prev[1] >= 1.5:
                # Stable size — ingest and remove from import dir.
                kind = LibraryKind.stinger if path.suffix.lower() == ".zip" else LibraryKind.clip
                mode = UploadMode.zip if path.suffix.lower() == ".zip" else UploadMode.video
                try:
                    self.ingest_path(path, kind=kind, mode=mode, name=path.name)
                    path.unlink(missing_ok=True)
                except Exception:
                    log.exception("failed to ingest %s", path)
                seen.pop(key, None)
            else:
                seen[key] = (stat.st_size, time.time() if not prev else prev[1])
