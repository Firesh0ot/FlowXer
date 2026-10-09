"""The saturating mallinfo() the image preloads for libcef (docker/mallinfo-shim.c)."""

import ctypes
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SHIM = ROOT / "docker" / "mallinfo-shim.c"
FIELDS = ("arena", "ordblks", "smblks", "hblks", "hblkhd", "usmblks", "fsmblks", "uordblks",
          "fordblks", "keepcost")


class Mallinfo(ctypes.Structure):
    _fields_ = [(name, ctypes.c_int) for name in FIELDS]


def _int32(value: int) -> int:
    """What C computes for int + int (wraps)."""
    return (value + 2**31) % 2**32 - 2**31


def test_image_builds_and_preloads_the_shim() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "-o /opt/gstcef/libmallinfo-shim.so /tmp/mallinfo-shim.c" in dockerfile
    assert "ENV LD_PRELOAD=/opt/gstcef/libmallinfo-shim.so" in dockerfile


@pytest.mark.skipif(
    not sys.platform.startswith("linux") or shutil.which("cc") is None,
    reason="needs glibc and a C compiler",
)
def test_mallinfo_stays_in_int_range_above_2_gib(tmp_path: Path) -> None:
    lib = tmp_path / "libmallinfo-shim.so"
    subprocess.run(
        ["cc", "-O2", "-Wall", "-Werror", "-shared", "-fPIC", "-o", str(lib), str(SHIM)],
        check=True,
    )
    libc = ctypes.CDLL("libc.so.6")
    shim = ctypes.CDLL(str(lib))
    libc.mallinfo.restype = Mallinfo
    shim.mallinfo.restype = Mallinfo
    libc.malloc.restype = ctypes.c_void_p
    libc.malloc.argtypes = [ctypes.c_size_t]
    libc.free.argtypes = [ctypes.c_void_p]

    # 2.5 GiB from malloc, never touched: one mmapped chunk, no resident memory.
    block = libc.malloc(5 << 29)
    assert block
    try:
        legacy = libc.mallinfo()
        capped = shim.mallinfo()
    finally:
        libc.free(block)

    # Chromium's MallocDumpProvider: checked_cast<size_t>(info.arena + info.hblkhd).
    assert _int32(legacy.arena + legacy.hblkhd) < 0  # glibc's own mallinfo() wraps
    assert capped.arena + capped.hblkhd == 2**31 - 1
    assert all(getattr(capped, name) >= 0 for name in FIELDS)
    assert capped.hblks == legacy.hblks  # values below the cap pass unchanged
