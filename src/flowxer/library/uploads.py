"""Chunked uploads. Each chunk is streamed into its place in one data file, so completing
an upload is a rename, not a copy, and no chunk is ever held in memory."""

from __future__ import annotations

import logging
import math
import shutil
import threading
import time
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import anyio

from flowxer.library.models import ConvertOptions, LibraryKind, UploadMode, UploadSession

log = logging.getLogger(__name__)

# Free space that must remain on the library volume after an upload is accepted: the
# conversion writes the mezzanine next to the original.
DISK_RESERVE_BYTES = 512 << 20
# An upload without activity for this long is dropped together with its data.
SESSION_IDLE_S = 3600.0
_WRITE_BLOCK = 1 << 20


class UploadError(ValueError):
    """The request does not fit the upload (the API answers 422)."""


class UploadTooLarge(UploadError):
    """More bytes than declared or allowed (413)."""


class UploadNoSpace(UploadError):
    """Not enough free disk space for the upload (507)."""


class UploadBusy(UploadError):
    """The upload is being completed by another request (409)."""


def safe_upload_name(name: str) -> str:
    """The base name of a client file name; never empty, '.' or '..'."""
    base = Path(str(name).replace("\\", "/")).name.strip()
    if base in {"", ".", ".."}:
        return "upload.bin"
    return base[:255]


class UploadManager:
    CHUNK_SIZE = 8 * 1024 * 1024

    def __init__(
        self,
        uploads_dir: Path,
        upload_limit_bytes: int,
        *,
        chunk_size: int | None = None,
        idle_s: float = SESSION_IDLE_S,
    ) -> None:
        self.root = Path(uploads_dir)
        self.upload_limit_bytes = upload_limit_bytes
        self.chunk_size = chunk_size or self.CHUNK_SIZE
        self.idle_s = idle_s
        self._sessions: dict[str, UploadSession] = {}
        self._lock = threading.Lock()
        # Sessions live in memory: data left by an earlier process can never be completed.
        if self.root.is_dir():
            for leftover in self.root.iterdir():
                if leftover.is_dir():
                    shutil.rmtree(leftover, ignore_errors=True)
                else:
                    leftover.unlink(missing_ok=True)
        self.root.mkdir(parents=True, exist_ok=True)

    # ── sessions ─────────────────────────────────────────────────────────────

    def data_path(self, upload_id: str) -> Path:
        return self.root / upload_id / "data"

    def chunk_count(self, session: UploadSession) -> int:
        return math.ceil(session.size / session.chunk_size)

    def chunk_length(self, session: UploadSession, index: int) -> int:
        last = self.chunk_count(session) - 1
        return session.chunk_size if index < last else session.size - last * session.chunk_size

    def _received_bytes(self, session: UploadSession) -> int:
        return sum(self.chunk_length(session, index) for index in session.received)

    def _check_space(self, size: int) -> None:
        """Bytes still to arrive for open uploads count as used."""
        pending = sum(
            s.size - self._received_bytes(s) for s in self._sessions.values() if s.state == "receiving"
        )
        free = shutil.disk_usage(self.root).free
        if size + pending + DISK_RESERVE_BYTES > free:
            raise UploadNoSpace(
                f"not enough free disk space for {size} bytes ({free} free, {pending} bytes of "
                f"other uploads pending, {DISK_RESERVE_BYTES} bytes kept free)"
            )

    def _prune_locked(self) -> None:
        cutoff = time.time() - self.idle_s
        for upload_id, session in list(self._sessions.items()):
            if session.state != "completing" and session.updated_at < cutoff:
                log.info("dropping idle upload %s (%s)", upload_id, session.name)
                self._sessions.pop(upload_id, None)
                shutil.rmtree(self.root / upload_id, ignore_errors=True)

    def create(
        self,
        *,
        name: str,
        size: int,
        kind: LibraryKind,
        mode: UploadMode,
        options: ConvertOptions | None = None,
    ) -> UploadSession:
        if size <= 0:
            raise UploadError("size must be > 0")
        if size > self.upload_limit_bytes:
            raise UploadTooLarge(f"upload exceeds limit of {self.upload_limit_bytes} bytes")
        with self._lock:
            self._prune_locked()
            self._check_space(size)
            now = time.time()
            session = UploadSession(
                id=uuid.uuid4().hex,
                name=safe_upload_name(name),
                size=size,
                kind=kind,
                mode=mode,
                options=options or ConvertOptions(),
                chunk_size=self.chunk_size,
                created_at=now,
                updated_at=now,
            )
            (self.root / session.id).mkdir(parents=True)
            with open(self.data_path(session.id), "wb") as handle:
                handle.truncate(size)
            self._sessions[session.id] = session
            return session.model_copy(deep=True)

    def get(self, upload_id: str) -> UploadSession | None:
        with self._lock:
            self._prune_locked()
            session = self._sessions.get(upload_id)
            return session.model_copy(deep=True) if session else None

    def new_stage(self, expected_bytes: int = 0) -> Path:
        """A scratch directory for a multipart TGA upload (same volume as the library)."""
        if expected_bytes > self.upload_limit_bytes:
            raise UploadTooLarge(f"upload exceeds limit of {self.upload_limit_bytes} bytes")
        with self._lock:
            self._check_space(expected_bytes)
        stage = self.root / f"seq-{uuid.uuid4().hex}"
        stage.mkdir(parents=True)
        return stage

    # ── chunks ───────────────────────────────────────────────────────────────

    async def receive_chunk(
        self,
        upload_id: str,
        index: int,
        body: AsyncIterator[bytes],
        declared_length: int | None = None,
    ) -> UploadSession:
        """Stream one chunk body into its place in the data file. Every chunk but the last
        has exactly ``chunk_size`` bytes; a retried chunk overwrites the same range."""
        with self._lock:
            session = self._sessions.get(upload_id)
            if session is None:
                raise KeyError(upload_id)
            if session.state != "receiving":
                raise UploadBusy("upload is already being completed")
            count = self.chunk_count(session)
            if not 0 <= index < count:
                raise UploadError(f"chunk index must be 0..{count - 1}")
            expected = self.chunk_length(session, index)
            offset = index * session.chunk_size
            session.updated_at = time.time()
        if declared_length is not None and declared_length != expected:
            error = UploadTooLarge if declared_length > expected else UploadError
            raise error(f"chunk {index} must be {expected} bytes, not {declared_length}")
        written = 0
        try:
            handle = open(self.data_path(upload_id), "r+b")
        except FileNotFoundError as exc:
            raise KeyError(upload_id) from exc
        with handle:
            handle.seek(offset)
            block = bytearray()
            async for piece in body:
                written += len(piece)
                if written > expected:
                    raise UploadTooLarge(f"chunk {index} is longer than {expected} bytes")
                block += piece
                if len(block) >= _WRITE_BLOCK:
                    await anyio.to_thread.run_sync(handle.write, bytes(block))
                    block.clear()
            if block:
                await anyio.to_thread.run_sync(handle.write, bytes(block))
        if written != expected:
            raise UploadError(f"chunk {index} has {written} bytes, expected {expected}")
        with self._lock:
            session = self._sessions.get(upload_id)
            if session is None:
                raise KeyError(upload_id)
            if index not in session.received:
                session.received.append(index)
                session.received.sort()
            session.updated_at = time.time()
            return session.model_copy(deep=True)

    # ── completion ───────────────────────────────────────────────────────────

    def begin_complete(self, upload_id: str) -> tuple[UploadSession, Path | None]:
        """Claim the upload for completion. Returns the data file, or None when an earlier
        request completed it already (the session then names the item)."""
        with self._lock:
            session = self._sessions.get(upload_id)
            if session is None:
                raise KeyError(upload_id)
            if session.state == "done":
                return session.model_copy(deep=True), None
            if session.state == "completing":
                raise UploadBusy("upload is already being completed")
            missing = sorted(set(range(self.chunk_count(session))) - set(session.received))
            if missing:
                preview = ", ".join(str(n) for n in missing[:8])
                more = "" if len(missing) <= 8 else f" (+{len(missing) - 8} more)"
                raise UploadError(f"missing chunks: {preview}{more}")
            session.state = "completing"
            session.updated_at = time.time()
            return session.model_copy(deep=True), self.data_path(upload_id)

    def finish_complete(self, upload_id: str, item_id: str) -> None:
        """Keep a small 'done' record (until it idles out) so a retried complete returns the item."""
        with self._lock:
            session = self._sessions.get(upload_id)
            if session is not None:
                session.state = "done"
                session.item_id = item_id
                session.updated_at = time.time()
        shutil.rmtree(self.root / upload_id, ignore_errors=True)

    def cleanup(self, upload_id: str) -> None:
        with self._lock:
            self._sessions.pop(upload_id, None)
        shutil.rmtree(self.root / upload_id, ignore_errors=True)
