from __future__ import annotations

import json
import logging
import os
import shutil
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path

from flowxer.engine.security import SecurityError, require_safe_id
from flowxer.library.models import LibraryItem, LibraryKind

log = logging.getLogger(__name__)


def new_item_id() -> str:
    return uuid.uuid4().hex[:12]


class LibraryStore:
    """On-disk library layout under ``library_dir``."""

    def __init__(self, library_dir: Path) -> None:
        self.root = Path(library_dir)
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "_uploads").mkdir(parents=True, exist_ok=True)
        # Serialises read-modify-write of item.json between the API and the conversion workers.
        self._lock = threading.RLock()
        # item id -> (item.json mtime_ns, size, parsed item): /console lists every item each poll.
        self._cache: dict[str, tuple[int, int, LibraryItem]] = {}

    def item_dir(self, item_id: str) -> Path:
        require_safe_id(item_id, what="library item id")
        path = (self.root / item_id).resolve()
        if not path.is_relative_to(self.root.resolve()):
            raise SecurityError("invalid library item id")
        return path

    def item_json_path(self, item_id: str) -> Path:
        return self.item_dir(item_id) / "item.json"

    def mezz_path(self, item_id: str, format_id: str) -> Path:
        return self.item_dir(item_id) / f"mezz-{format_id}.mov"

    def thumb_path(self, item_id: str) -> Path:
        return self.item_dir(item_id) / "thumb.jpg"

    def convert_log_path(self, item_id: str) -> Path:
        return self.item_dir(item_id) / "convert.log"

    def list_ids(self) -> list[str]:
        ids = []
        for path in sorted(self.root.iterdir()):
            if path.name.startswith("_"):
                continue
            if path.is_dir() and (path / "item.json").is_file():
                ids.append(path.name)
        return ids

    def load(self, item_id: str) -> LibraryItem | None:
        path = self.item_json_path(item_id)
        try:
            stat = path.stat()
        except OSError:
            self._cache.pop(item_id, None)
            return None
        cached = self._cache.get(item_id)
        if cached and cached[0] == stat.st_mtime_ns and cached[1] == stat.st_size:
            return cached[2].model_copy(deep=True)
        for attempt in range(3):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                item = LibraryItem.model_validate(data)
                break
            except PermissionError:
                # Windows: the file is being replaced by a worker right now.
                if attempt == 2:
                    return None
                time.sleep(0.01)
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                log.warning("failed to load library item %s: %s", item_id, exc)
                return None
        self._cache[item_id] = (stat.st_mtime_ns, stat.st_size, item)
        return item.model_copy(deep=True)

    def save(self, item: LibraryItem) -> LibraryItem:
        with self._lock:
            directory = self.item_dir(item.id)
            directory.mkdir(parents=True, exist_ok=True)
            return self._write(item)

    def _write(self, item: LibraryItem) -> LibraryItem:
        item.updated_at = time.time()
        if not item.created_at:
            item.created_at = item.updated_at
        path = self.item_json_path(item.id)
        # Conversion workers save progress while the API lists items: replace the file in one
        # step, so a reader never sees it half written (it skipped the item).
        temporary = path.with_name(f"{path.name}.{threading.get_ident()}.tmp")
        temporary.write_text(item.model_dump_json(indent=2), encoding="utf-8")
        os.replace(temporary, path)
        self._cache.pop(item.id, None)
        return item

    def update(self, item_id: str, change: Callable[[LibraryItem], None]) -> LibraryItem | None:
        """Load, change and save one item under the store lock. Returns None (and writes
        nothing) when the item is gone, so a worker never brings a deleted item back."""
        with self._lock:
            item = self.load(item_id)
            if item is None:
                return None
            change(item)
            return self._write(item)

    def delete(self, item_id: str) -> None:
        with self._lock:
            directory = self.item_dir(item_id)
            self._cache.pop(item_id, None)
            if directory.is_dir():
                shutil.rmtree(directory)

    def list_items(self, kind: LibraryKind | None = None, query: str = "") -> list[LibraryItem]:
        items: list[LibraryItem] = []
        q = query.strip().lower()
        for item_id in self.list_ids():
            item = self.load(item_id)
            if item is None:
                continue
            if kind is not None and item.kind != kind:
                continue
            if q:
                hay = " ".join([item.name, item.id, *item.tags]).lower()
                if q not in hay:
                    continue
            items.append(item)
        return items

    def find_by_legacy_path(self, legacy_path: str) -> LibraryItem | None:
        for item in self.list_items():
            if item.legacy_path == legacy_path:
                return item
        return None
