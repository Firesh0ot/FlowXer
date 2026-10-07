"""Thin ffmpeg/ffprobe abstraction so unit tests can run without binaries."""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

log = logging.getLogger(__name__)

# runner(cmd, timeout=..., cancel=...) -> RunResult
Runner = Callable[..., "RunResult"]

PROBE_TIMEOUT_S = 60.0
_LOW_PRIORITY: list[str] | None = None


@dataclass
class RunResult:
    code: int
    stdout: str = ""
    stderr: str = ""
    cancelled: bool = False

    @property
    def output(self) -> str:
        return (self.stdout or "") + (self.stderr or "")


@dataclass
class ProbeStream:
    codec_type: str = ""
    codec_name: str = ""
    width: int = 0
    height: int = 0
    pix_fmt: str = ""
    r_frame_rate: str = ""
    avg_frame_rate: str = ""
    nb_frames: str = ""
    field_order: str = ""
    color_space: str = ""
    channels: int = 0
    channel_layout: str = ""
    sample_rate: str = ""
    tags: dict = field(default_factory=dict)


@dataclass
class ProbeResult:
    streams: list[ProbeStream] = field(default_factory=list)
    duration: float = 0.0
    format_name: str = ""
    size: int = 0


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def low_priority_prefix() -> list[str]:
    """nice -n 10 and ionice -c3 (idle I/O) where the tools exist: ingest must not take
    CPU or disk time from the live mixer."""
    global _LOW_PRIORITY
    if _LOW_PRIORITY is None:
        prefix: list[str] = []
        if shutil.which("nice"):
            prefix += ["nice", "-n", "10"]
        if shutil.which("ionice"):
            prefix += ["ionice", "-c3"]
        _LOW_PRIORITY = prefix
    return list(_LOW_PRIORITY)


def default_runner(
    cmd: Sequence[str],
    timeout: float | None = None,
    cancel: threading.Event | None = None,
) -> RunResult:
    """Run at low priority. A set cancel event or the timeout kills the process."""
    try:
        proc = subprocess.Popen(
            low_priority_prefix() + list(cmd),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
        )
    except OSError as exc:
        return RunResult(code=127, stderr=str(exc))
    deadline = time.monotonic() + timeout if timeout else None
    while True:
        try:
            out, err = proc.communicate(timeout=0.5)
            return RunResult(code=proc.returncode, stdout=out or "", stderr=err or "")
        except subprocess.TimeoutExpired:
            cancelled = cancel is not None and cancel.is_set()
            if not cancelled and (deadline is None or time.monotonic() < deadline):
                continue
            proc.kill()
            try:
                out, err = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:  # a grandchild still holds the pipes
                out, err = "", ""
            reason = "cancelled" if cancelled else f"timed out after {timeout:.0f} s"
            return RunResult(code=-9, stdout=out or "", stderr=f"{err or ''}\n{reason}", cancelled=cancelled)


class FFmpeg:
    """Build and run ffmpeg/ffprobe commands."""

    def __init__(self, runner: Runner | None = None) -> None:
        self.runner = runner or default_runner

    def which(self) -> bool:
        return ffmpeg_available()

    def probe(self, path: Path) -> ProbeResult:
        cmd = [
            "ffprobe",
            "-v",
            "error",
            "-protocol_whitelist",
            "file",
            "-show_entries",
            "format=duration,format_name,size:stream=index,codec_type,codec_name,"
            "width,height,pix_fmt,r_frame_rate,avg_frame_rate,nb_frames,field_order,"
            "color_space,channels,channel_layout,sample_rate",
            "-of",
            "json",
            str(path),
        ]
        result = self.runner(cmd, timeout=PROBE_TIMEOUT_S)
        if result.code != 0:
            raise RuntimeError(f"ffprobe failed: {result.stderr.strip() or result.code}")
        data = json.loads(result.stdout or "{}")
        streams = []
        for raw in data.get("streams") or []:
            streams.append(
                ProbeStream(
                    codec_type=str(raw.get("codec_type") or ""),
                    codec_name=str(raw.get("codec_name") or ""),
                    width=int(raw.get("width") or 0),
                    height=int(raw.get("height") or 0),
                    pix_fmt=str(raw.get("pix_fmt") or ""),
                    r_frame_rate=str(raw.get("r_frame_rate") or ""),
                    avg_frame_rate=str(raw.get("avg_frame_rate") or ""),
                    nb_frames=str(raw.get("nb_frames") or ""),
                    field_order=str(raw.get("field_order") or ""),
                    color_space=str(raw.get("color_space") or ""),
                    channels=int(raw.get("channels") or 0),
                    channel_layout=str(raw.get("channel_layout") or ""),
                    sample_rate=str(raw.get("sample_rate") or ""),
                    tags=dict(raw.get("tags") or {}),
                )
            )
        fmt = data.get("format") or {}
        return ProbeResult(
            streams=streams,
            duration=float(fmt.get("duration") or 0),
            format_name=str(fmt.get("format_name") or ""),
            size=int(fmt.get("size") or 0),
        )

    def run(
        self,
        cmd: Sequence[str],
        *,
        timeout: float | None = None,
        cancel: threading.Event | None = None,
    ) -> RunResult:
        log.debug("ffmpeg cmd: %s", " ".join(cmd))
        return self.runner(cmd, timeout=timeout, cancel=cancel)


def parse_frame_rate(value: str | None, default: float = 50.0) -> float:
    if not value:
        return default
    if "/" in value:
        num, den = value.split("/", 1)
        try:
            return float(num) / max(float(den), 1.0)
        except ValueError:
            return default
    try:
        return float(value)
    except ValueError:
        return default


def has_alpha_pix_fmt(pix_fmt: str) -> bool:
    name = (pix_fmt or "").lower()
    return any(token in name for token in ("a", "rgba", "argb", "bgra", "abgr", "yuva", "gbra"))


def build_video_filter(
    *,
    width: int,
    height: int,
    fps_num: int,
    fps_den: int,
    fit: str = "fit",
    fps_mode: str = "drop",
    src_interlaced: bool = False,
    color_space: str = "",
    dst_pix_fmt: str = "yuv422p10le",
) -> str:
    """Build an ffmpeg -vf chain matching mxl-test-player conversion semantics."""
    parts: list[str] = []
    if src_interlaced:
        parts.append("bwdif=mode=send_frame")
    cs = (color_space or "").lower()
    if cs in {"smpte170m", "bt470bg", "smpte240m"}:
        parts.append("colorspace=all=bt709:iall=bt601")
    if fit == "fill":
        parts.append(
            f"scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height}"
        )
    elif fit in {"center", "1:1"}:
        parts.append(
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black,"
            f"crop={width}:{height}"
        )
    else:
        parts.append(
            f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black"
        )
    fps = f"{fps_num}/{fps_den}"
    if fps_mode == "motion":
        parts.append(f"minterpolate=fps={fps}:mi_mode=mci")
    else:
        parts.append(f"fps={fps}")
    parts.append(f"format={dst_pix_fmt}")
    return ",".join(parts)


_FFMPEG = ["ffmpeg", "-hide_banner", "-nostdin", "-nostats", "-y"]
# Inputs come from uploads: local files only, no network protocols.
_INPUT = ["-protocol_whitelist", "file"]


def conversion_timeout(duration_s: float, *, motion: bool = False) -> float:
    """Kill a stuck ffmpeg: 5 min plus 10x (motion interpolation: 60x) the input length."""
    return 300.0 + max(duration_s, 0.0) * (60.0 if motion else 10.0)


def build_clip_video_command(*, original: Path, video_tmp: Path, vf: str) -> list[str]:
    """Clip picture to ProRes 422 HQ; the sound is conformed by its own command."""
    return [
        *_FFMPEG,
        *_INPUT,
        "-i",
        str(original),
        "-an",
        "-sn",
        "-dn",
        "-vf",
        vf,
        "-c:v",
        "prores_ks",
        "-profile:v",
        "3",
        "-pix_fmt",
        "yuv422p10le",
        "-f",
        "mov",
        str(video_tmp),
    ]


def channel_map_filter(src_channels: int, dst_channels: int) -> str:
    """pan to dst_channels: source channel n on output n, the other outputs silent."""
    used = max(1, min(src_channels, dst_channels))
    return "pan=" + "|".join([f"{dst_channels}c", *(f"c{n}=c{n}" for n in range(used))])


def channel_remix_filter(src_channels: int, src_layout: str, dst_channels: int) -> str:
    """To dst_channels (1 or 2): a proper downmix (or mono to both sides) when the source
    layout is known, else the first channels one to one."""
    if src_channels == dst_channels:
        return ""
    if src_layout and src_layout != "unknown":
        return f"aformat=channel_layouts={'mono' if dst_channels == 1 else 'stereo'}"
    return channel_map_filter(src_channels, dst_channels)


def build_clip_audio_command(
    *,
    original: Path | None,
    audio_tmp: Path,
    af: str,
    src_channels: int,
    dst_channels: int,
    samples: int,
    src_layout: str = "",
) -> list[str]:
    """The clip's sound as raw float32, remixed to dst_channels and padded or cut to exactly
    samples (whole frames). ffmpeg streams it to disk, so the mixer process never holds the
    sound in memory. Without an original it writes silence of that length."""
    conform = f"apad=whole_len={samples},atrim=end_sample={samples}"
    if original is None:
        source = ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono"]
        chain = ",".join(f for f in (channel_map_filter(1, dst_channels) if dst_channels > 1 else "", conform) if f)
    else:
        source = [*_INPUT, "-i", str(original), "-map", "0:a:0"]
        remix = channel_remix_filter(src_channels, src_layout, dst_channels)
        chain = ",".join(f for f in (af, remix, conform) if f)
    return [
        *_FFMPEG,
        *source,
        "-vn",
        "-sn",
        "-dn",
        "-af",
        chain,
        "-ar",
        "48000",
        "-c:a",
        "pcm_f32le",
        "-f",
        "f32le",
        str(audio_tmp),
    ]


def build_clip_mux_command(*, video_tmp: Path, audio_tmp: Path, audio_channels: int, mezz: Path) -> list[str]:
    """ProRes plus the conformed sound. The sound is stored big-endian: GStreamer's qtdemux
    reads MOV float PCM as F32BE whatever ffmpeg marks (little-endian plays as silence)."""
    return [
        *_FFMPEG,
        "-i",
        str(video_tmp),
        "-f",
        "f32le",
        "-ar",
        "48000",
        "-ac",
        str(max(1, audio_channels)),
        "-i",
        str(audio_tmp),
        "-map",
        "0:v",
        "-map",
        "1:a",
        "-c:v",
        "copy",
        "-c:a",
        "pcm_f32be",
        "-shortest",
        "-f",
        "mov",
        str(mezz),
    ]


def build_thumb_command(*, mezz: Path, thumb: Path) -> list[str]:
    return [
        *_FFMPEG,
        "-i",
        str(mezz),
        "-vf",
        "scale=320:-1",
        "-frames:v",
        "1",
        "-f",
        "image2",
        str(thumb),
    ]


def build_stinger_mezz_command(*, input_args: list[str], mezz: Path, vf: str) -> list[str]:
    """ProRes 4444 mezzanine for stingers (preserves alpha when present). No sound: the
    mixer plays only a stinger's picture."""
    return [
        *_FFMPEG,
        *_INPUT,
        *input_args,
        "-vf",
        vf,
        "-c:v",
        "prores_ks",
        "-profile:v",
        "4",
        "-pix_fmt",
        "yuva444p10le",
        "-an",
        "-f",
        "mov",
        str(mezz),
    ]
