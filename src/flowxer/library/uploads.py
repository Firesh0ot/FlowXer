from __future__ import annotations

import shutil
import time
import uuid
from pathlib import Path

from flowxer.library.models import ConvertOptions, LibraryKind, UploadMode, UploadSession


class UploadManager:
    CHUNK_SIZE = 8 * 1024 * 1024

    def __init__(self, uploads_dir: Path, upload_limit_bytes: int) -> None:
        self.root = Path(uploads_dir)
        self.root.mkdir(parents=True, exist_ok=True)
        self.upload_limit_bytes = upload_limit_bytes
        self._sessions: dict[str, UploadSession] = {}

    def create(
        self,
        *,
        name: str,
        size: int,
        kind: LibraryKind,
        mode: UploadMode,
        options: ConvertOptions | None = None,
    ) -> UploadSession:
        if size < 0:
            raise ValueError("size must be >= 0")
        if size > self.upload_limit_bytes:
            raise ValueError(f"upload exceeds limit of {self.upload_limit_bytes} bytes")
        session = UploadSession(
            id=uuid.uuid4().hex,
            name=Path(name).name or "upload.bin",
            size=size,
            kind=kind,
            mode=mode,
            options=options or ConvertOptions(),
            chunk_size=self.CHUNK_SIZE,
            created_at=time.time(),
        )
        dest = self.root / session.id
        dest.mkdir(parents=True, exist_ok=True)
        self._sessions[session.id] = session
        return session

    def get(self, upload_id: str) -> UploadSession | None:
        return self._sessions.get(upload_id)

    def write_chunk(self, upload_id: str, index: int, data: bytes) -> UploadSession:
        session = self._sessions.get(upload_id)
        if session is None:
            raise KeyError(upload_id)
        if index < 0:
            raise ValueError("chunk index must be >= 0")
        path = self.root / upload_id / f"chunk-{index:08d}"
        path.write_bytes(data)
        if index not in session.received:
            session.received.append(index)
            session.received.sort()
        return session

    def assemble(self, upload_id: str) -> Path:
        session = self._sessions.get(upload_id)
        if session is None:
            raise KeyError(upload_id)
        directory = self.root / upload_id
        chunks = sorted(directory.glob("chunk-*"))
        if not chunks:
            raise ValueError("no chunks received")
        # For image_sequence mode the client may upload a zip of frames or we
        # accept a single assembled blob; always produce one assembled file.
        out = directory / session.name
        with open(out, "wb") as dest:
            for chunk in chunks:
                dest.write(chunk.read_bytes())
        actual = out.stat().st_size
        if session.size and actual != session.size:
            # Allow mismatch for folder-drop zip packaging where size was estimate.
            if session.mode == UploadMode.video and abs(actual - session.size) > 0:
                raise ValueError(f"assembled size {actual} != declared {session.size}")
        return out

    def cleanup(self, upload_id: str) -> None:
        self._sessions.pop(upload_id, None)
        directory = self.root / upload_id
        if directory.is_dir():
            shutil.rmtree(directory, ignore_errors=True)
