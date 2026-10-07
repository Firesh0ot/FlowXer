"""Thin ffmpeg/ffprobe abstraction so unit tests can run without binaries."""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

log = logging.getLogger(__name__)

Runner = Callable[[Sequence[str], Path | None], "RunResult"]


@dataclass
class RunResult:
    code: int
    stdout: str = ""
    stderr: str = ""

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


def default_runner(cmd: Sequence[str], cwd: Path | None = None) -> RunResult:
    try:
        proc = subprocess.run(
            list(cmd),
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            check=False,
            timeout=3600,
        )
        return RunResult(code=proc.returncode, stdout=proc.stdout or "", stderr=proc.stderr or "")
    except (OSError, subprocess.SubprocessError) as exc:
        return RunResult(code=127, stderr=str(exc))


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
            "-show_entries",
            "format=duration,format_name,size:stream=index,codec_type,codec_name,"
            "width,height,pix_fmt,r_frame_rate,avg_frame_rate,nb_frames,field_order,"
            "color_space,channels,sample_rate",
            "-of",
            "json",
            str(path),
        ]
        result = self.runner(cmd, None)
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

    def run(self, cmd: Sequence[str], cwd: Path | None = None) -> RunResult:
        log.debug("ffmpeg cmd: %s", " ".join(cmd))
        return self.runner(cmd, cwd)


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


def build_clip_mezz_commands(
    *,
    original: Path,
    video_tmp: Path,
    audio_raw: Path,
    audio_conf: Path,
    mezz: Path,
    thumb: Path,
    vf: str,
    af: str,
    audio_channels: int,
    src_audio_channels: int,
) -> list[list[str]]:
    """Return ordered ffmpeg command lists for clip mezzanine conversion."""
    video_cmd = [
        "ffmpeg",
        "-hide_banner",
        "-y",
        "-i",
        str(original),
        "-an",
        "-vf",
        vf,
        "-c:v",
        "prores_ks",
        "-profile:v",
        "3",
        "-pix_fmt",
        "yuv422p10le",
        str(video_tmp),
    ]
    extract_ch = max(1, src_audio_channels) if src_audio_channels > 0 else 1
    audio_cmd = [
        "ffmpeg",
        "-hide_banner",
        "-y",
        "-i",
        str(original),
        "-vn",
        "-af",
        af,
        "-f",
        "f32le",
        "-ac",
        str(extract_ch),
        str(audio_raw),
    ]
    mux_cmd = [
        "ffmpeg",
        "-hide_banner",
        "-y",
        "-i",
        str(video_tmp),
        "-f",
        "f32le",
        "-ar",
        "48000",
        "-ac",
        str(max(1, audio_channels)),
        "-i",
        str(audio_conf),
        "-map",
        "0:v",
        "-map",
        "1:a",
        "-c:v",
        "copy",
        "-c:a",
        "pcm_f32le",
        "-shortest",
        str(mezz),
    ]
    thumb_cmd = [
        "ffmpeg",
        "-hide_banner",
        "-y",
        "-i",
        str(mezz),
        "-vf",
        "scale=320:-1",
        "-frames:v",
        "1",
        str(thumb),
    ]
    return [video_cmd, audio_cmd, mux_cmd, thumb_cmd]


def build_stinger_mezz_commands(
    *,
    input_args: list[str],
    mezz: Path,
    thumb: Path,
    vf: str,
    has_audio: bool,
    af: str = "aresample=48000",
) -> list[list[str]]:
    """ProRes 4444 mezzanine for stingers (preserves alpha when present)."""
    video_cmd = [
        "ffmpeg",
        "-hide_banner",
        "-y",
        *input_args,
        "-vf",
        vf,
        "-c:v",
        "prores_ks",
        "-profile:v",
        "4",
        "-pix_fmt",
        "yuva444p10le",
    ]
    if has_audio:
        video_cmd.extend(["-af", af, "-c:a", "pcm_f32le"])
    else:
        video_cmd.append("-an")
    video_cmd.append(str(mezz))
    thumb_cmd = [
        "ffmpeg",
        "-hide_banner",
        "-y",
        "-i",
        str(mezz),
        "-vf",
        "scale=320:-1",
        "-frames:v",
        "1",
        str(thumb),
    ]
    return [video_cmd, thumb_cmd]
