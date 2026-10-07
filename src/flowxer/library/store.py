from __future__ import annotations

import json
import logging
import shutil
import time
import uuid
from pathlib import Path

from flowxer.engine.security import SecurityError, require_safe_id
from flowxer.library.models import ConversionInfo, ConversionStatus, LibraryItem, LibraryKind

log = logging.getLogger(__name__)


def new_item_id() -> str:
    return uuid.uuid4().hex[:12]


class LibraryStore:
    """On-disk library layout under ``library_dir``."""

    def __init__(self, library_dir: Path) -> None:
        self.root = Path(library_dir)
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "_uploads").mkdir(parents=True, exist_ok=True)

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
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return LibraryItem.model_validate(data)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            log.warning("failed to load library item %s: %s", item_id, exc)
            return None

    def save(self, item: LibraryItem) -> LibraryItem:
        directory = self.item_dir(item.id)
        directory.mkdir(parents=True, exist_ok=True)
        item.updated_at = time.time()
        if not item.created_at:
            item.created_at = item.updated_at
        path = self.item_json_path(item.id)
        path.write_text(item.model_dump_json(indent=2), encoding="utf-8")
        return item

    def delete(self, item_id: str) -> None:
        directory = self.item_dir(item_id)
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

    def set_conversion(self, item: LibraryItem, format_id: str, info: ConversionInfo) -> LibraryItem:
        item.conversions[format_id] = info
        return self.save(item)

    def mark_converting(self, item: LibraryItem, format_id: str) -> LibraryItem:
        current = item.conversion_for(format_id)
        current.status = ConversionStatus.converting
        current.error = None
        return self.set_conversion(item, format_id, current)

    def find_by_legacy_path(self, legacy_path: str) -> LibraryItem | None:
        for item in self.list_items():
            if item.legacy_path == legacy_path:
                return item
        return None
