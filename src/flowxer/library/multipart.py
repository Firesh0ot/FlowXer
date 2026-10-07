"""Streaming multipart/form-data reader: file parts go straight to disk under a byte cap.

Starlette's form parser spools every part to the system temp directory without a total
limit (and stops at 1000 files); a TGA folder upload is written here part by part instead.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import BinaryIO

import anyio

try:
    from python_multipart.multipart import MultipartParser, parse_options_header
except ImportError:  # python-multipart < 0.0.13
    from multipart.multipart import MultipartParser, parse_options_header

from flowxer.library.uploads import UploadError, UploadTooLarge, safe_upload_name

FIELD_LIMIT = 64 * 1024
_FEED_BLOCK = 1 << 20


class _Collector:
    def __init__(self, dest: Path, *, max_bytes: int, max_files: int, keep: Callable[[str], bool]) -> None:
        self.dest = dest
        self.max_bytes = max_bytes
        self.max_files = max_files
        self.keep = keep
        self.fields: dict[str, str] = {}
        self.files: list[str] = []
        self.total = 0
        self._headers: dict[bytes, bytes] = {}
        self._name = b""
        self._value = b""
        self._out: BinaryIO | None = None
        self._field: str | None = None
        self._is_file = False
        self._buffer = bytearray()

    def callbacks(self) -> dict:
        return {
            "on_part_begin": self.on_part_begin,
            "on_header_field": self.on_header_field,
            "on_header_value": self.on_header_value,
            "on_header_end": self.on_header_end,
            "on_headers_finished": self.on_headers_finished,
            "on_part_data": self.on_part_data,
            "on_part_end": self.on_part_end,
        }

    def on_part_begin(self) -> None:
        self._headers = {}
        self._name = self._value = b""

    def on_header_field(self, data: bytes, start: int, end: int) -> None:
        self._name += data[start:end]

    def on_header_value(self, data: bytes, start: int, end: int) -> None:
        self._value += data[start:end]

    def on_header_end(self) -> None:
        self._headers[self._name.lower()] = self._value
        self._name = self._value = b""

    def on_headers_finished(self) -> None:
        _, options = parse_options_header(self._headers.get(b"content-disposition", b""))
        filename = options.get(b"filename")
        self._is_file = filename is not None
        self._field = None
        self._out = None
        if not self._is_file:
            self._field = options.get(b"name", b"").decode("utf-8", "replace")
            self._buffer = bytearray()
            return
        name = safe_upload_name(filename.decode("utf-8", "replace"))
        if not self.keep(name):
            return
        if len(self.files) >= self.max_files:
            raise UploadError(f"more than {self.max_files} files")
        if name in self.files:
            raise UploadError(f"duplicate file name: {name}")
        self._out = open(self.dest / name, "xb")
        self.files.append(name)

    def on_part_data(self, data: bytes, start: int, end: int) -> None:
        if self._is_file:
            self.total += end - start
            if self.total > self.max_bytes:
                raise UploadTooLarge(f"upload exceeds limit of {self.max_bytes} bytes")
            if self._out is not None:
                self._out.write(data[start:end])
        elif self._field is not None:
            if len(self._buffer) + (end - start) > FIELD_LIMIT:
                raise UploadError(f"form field {self._field} is too long")
            self._buffer += data[start:end]

    def on_part_end(self) -> None:
        if self._out is not None:
            self._out.close()
            self._out = None
        elif self._field is not None:
            self.fields[self._field] = self._buffer.decode("utf-8", "replace")
            self._field = None

    def close(self) -> None:
        if self._out is not None:
            self._out.close()
            self._out = None


async def receive_files(
    body: AsyncIterator[bytes],
    content_type: str,
    dest: Path,
    *,
    max_bytes: int,
    max_files: int,
    keep: Callable[[str], bool],
) -> tuple[dict[str, str], list[str]]:
    """Write the kept file parts of a multipart body into ``dest`` (base names only).
    Returns the text fields and the stored file names."""
    kind, params = parse_options_header(content_type)
    boundary = params.get(b"boundary")
    if kind != b"multipart/form-data" or not boundary:
        raise UploadError("expected multipart/form-data")
    collector = _Collector(dest, max_bytes=max_bytes, max_files=max_files, keep=keep)
    parser = MultipartParser(boundary, collector.callbacks())
    pending = bytearray()
    try:
        # Parse (and write) in a worker thread, so a slow disk does not stall the event loop.
        async for piece in body:
            pending += piece
            if len(pending) >= _FEED_BLOCK:
                await anyio.to_thread.run_sync(parser.write, bytes(pending))
                pending.clear()
        if pending:
            await anyio.to_thread.run_sync(parser.write, bytes(pending))
        parser.finalize()
    finally:
        collector.close()
    return collector.fields, collector.files
