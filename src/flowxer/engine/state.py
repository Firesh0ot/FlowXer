"""Mixer state file: STATE_DIR/state.json, written after each change and read on start."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# The export/import document and the state file share this format.
STATE_FORMAT = "flowxer-config/1"


class StateStore:
    def __init__(self, directory: Path) -> None:
        self.path = directory / "state.json"
        self._written: str | None = None

    def load(self) -> dict[str, Any] | None:
        """The saved state, or None when there is none or it cannot be used."""
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as exc:
            log.error("cannot read %s, starting with defaults: %s", self.path, exc)
            return None
        try:
            data = json.loads(text)
        except ValueError as exc:
            log.error("%s is not valid JSON, starting with defaults: %s", self.path, exc)
            return None
        if not isinstance(data, dict) or data.get("format") != STATE_FORMAT:
            log.error("%s is not a %s document, starting with defaults", self.path, STATE_FORMAT)
            return None
        self._written = text
        return data

    def save(self, data: dict[str, Any]) -> None:
        """Write atomically (temp file + rename); skip when nothing changed."""
        text = json.dumps(data, indent=2, sort_keys=True)
        if text == self._written:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError as exc:
            log.warning("cannot write %s: %s", self.path, exc)
            return
        self._written = text
