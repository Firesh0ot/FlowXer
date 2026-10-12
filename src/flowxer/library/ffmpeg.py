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
        self._nvenc: bool | None = None

    def which(self) -> bool:
        return ffmpeg_available()

    def has_nvenc(self) -> bool:
        """NVENC opens for HEVC 4:4:4 10-bit (tried once, with a tiny picture); the result is
        logged once."""
        if self._nvenc is None:
            result = self.runner(_NVENC_PROBE, timeout=30.0)
            self._nvenc = result.code == 0
            if self._nvenc:
                log.info("NVENC available: clip mezzanines are encoded on the GPU")
            else:
                reason = (result.stderr or "").strip().splitlines()[-1:] or [str(result.code)]
                log.warning("NVENC cannot be opened (%s): clip mezzanines are encoded with x265", reason[0])
        return self._nvenc

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
    """A pixel format with an alpha channel (yuva*, rgba*, argb, gbrap*, ya*, ...)."""
    name = (pix_fmt or "").lower()
    return name.startswith(("yuva", "rgba", "bgra", "argb", "abgr", "gbrap", "ya"))


def is_rgb_pix_fmt(pix_fmt: str) -> bool:
    """An R'G'B' pixel format (it needs the BT.709 matrix on the way to Y'CbCr)."""
    name = (pix_fmt or "").lower()
    return name.startswith(("rgb", "bgr", "argb", "abgr", "gbr", "0rgb", "0bgr", "pal8"))


def _fit_filter(width: int, height: int, fit: str, pad: str = "black") -> str:
    """Scale (and pad or crop) to the raster: fit (letterbox), fill (crop) or center (1:1).
    `pad` is the colour of the bars (a stinger's are transparent: black@0)."""
    if fit == "fill":
        return f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height}"
    if fit in {"center", "1:1"}:
        return f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:{pad},crop={width}:{height}"
    return f"scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:{pad}"


def build_still_filter(*, width: int, height: int, fit: str = "fit") -> str:
    """A still picture at the raster as limited-range BT.709 10-bit 4:2:2 (an RGB image or a
    full-range JPEG is converted)."""
    return f"{_fit_filter(width, height, fit)},scale=out_color_matrix=bt709:out_range=tv,format=yuv422p10le"


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
    rgb: bool = False,
    pad: str = "black",
) -> str:
    """Build an ffmpeg -vf chain matching mxl-test-player conversion semantics. An R'G'B' source
    (`rgb`: TGA, PNG, QuickTime Animation) is converted with the BT.709 matrix to limited range."""
    parts: list[str] = []
    if src_interlaced:
        parts.append("bwdif=mode=send_frame")
    cs = (color_space or "").lower()
    if cs in {"smpte170m", "bt470bg", "smpte240m"}:
        parts.append("colorspace=all=bt709:iall=bt601")
    parts.append(_fit_filter(width, height, fit, pad))
    fps = f"{fps_num}/{fps_den}"
    if fps_mode == "motion":
        parts.append(f"minterpolate=fps={fps}:mi_mode=mci")
    else:
        parts.append(f"fps={fps}")
    if rgb:
        parts.append("scale=out_color_matrix=bt709:out_range=tv")
    parts.append(f"format={dst_pix_fmt}")
    return ",".join(parts)


# Filters on one thread, and every input decoded on one thread (below): the encoders get the
# conversion's threads (conversion_threads). A decoder's own threading started a burst that got
# the container throttled (the clip's thumbnail: HEVC 4:4:4 with 16 frame threads).
_FFMPEG = ["ffmpeg", "-hide_banner", "-nostdin", "-nostats", "-y", "-filter_threads", "1"]
# Inputs come from uploads: local files only, no network protocols.
_INPUT = ["-protocol_whitelist", "file", "-threads", "1"]


def conversion_threads(concurrency: int = 1, cgroup: Path = Path("/sys/fs/cgroup")) -> int:
    """Threads for one conversion: the container's CPU limit (cgroup v2 cpu.max) less two cores
    for the engine and the control plane, shared by the conversions that run at once; 8 without
    a limit. A conversion that fills the limit gets the whole container throttled, the engine
    with it (lab: a 10 s clip under a 6-core limit cost 8 skipped and 45 late grains with x265's
    own threading, none with 4 threads)."""
    budget = 8
    try:
        quota, period = (cgroup / "cpu.max").read_text().split()[:2]
        if quota != "max":
            budget = max(1, int(int(quota) / int(period)) - 2)
    except (OSError, ValueError):
        pass
    return max(1, budget // max(1, concurrency))


def conversion_timeout(duration_s: float, *, motion: bool = False) -> float:
    """Kill a stuck ffmpeg: 5 min plus 10x (motion interpolation: 60x) the input length."""
    return 300.0 + max(duration_s, 0.0) * (60.0 if motion else 10.0)


# The clip mezzanine: HEVC Main 4:4:4 10, which NVDEC decodes (it has no 4:2:2 HEVC). The 4:2:2
# picture is widened by repeating each chroma sample, so the engine's 4:4:4 -> 4:2:2 (the mean of
# each pair) gives the 4:2:2 picture back. No B-frames and a key frame every second: a cue decodes
# at most one second. NVENC with constant QP 8, else x265 with CRF 8: at least 56 dB PSNR against
# the 4:2:2 source on the lab's clips (48-52 dB on heavy grain).
CLIP_NVENC_QP = 8
CLIP_CRF = 8
# NVENC opens for HEVC 4:4:4 10-bit (FFmpeg's yuv444p16le holds 10 bits in the high bits).
_NVENC_PROBE = [
    "ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=256x144:r=50:d=0.1",
    "-vf", "format=yuv444p16le", "-c:v", "hevc_nvenc", "-profile:v", "rext", "-f", "null", "-",
]


def build_clip_video_command(
    *, original: Path | None, video_tmp: Path, vf: str, keyint: int, threads: int, nvenc: bool, input_args: list[str] | None = None
) -> list[str]:
    """Clip picture to the HEVC 4:4:4 10-bit mezzanine (Matroska); the sound is conformed by its
    own command into a raw file next to it. `threads` is the conversion's CPU budget
    (conversion_threads). NVENC encodes on the GPU, so decoding and scaling get the budget
    (half each). x265 gets it for its workers; decoding and scaling, on one thread each, keep up
    with it. `input_args` replace "-i original" (a stinger's image sequence)."""
    if nvenc:
        helpers = str(max(1, threads // 2))
        # After _INPUT: these override its one-thread defaults.
        threading_args = ["-filter_threads", helpers, "-threads", helpers]
        picture = "yuv444p16le"
        encoder = ["-c:v", "hevc_nvenc", "-profile:v", "rext", "-preset", "p5", "-tune", "hq", "-rc", "constqp",
                   "-qp", str(CLIP_NVENC_QP), "-bf", "0", "-g", str(keyint)]
    else:
        params = f"crf={CLIP_CRF}:keyint={keyint}:min-keyint={keyint}:bframes=0:scenecut=0:pools={threads}:log-level=error"
        threading_args = []
        picture = "yuv444p10le"
        encoder = ["-c:v", "libx265", "-profile:v", "main444-10", "-preset", "medium", "-x265-params", params]
    return [
        *_FFMPEG,
        *_INPUT,
        *threading_args,
        *(input_args or ["-i", str(original)]),
        "-an",
        "-sn",
        "-dn",
        "-vf",
        f"{vf},scale=flags=neighbor,format={picture}",
        *encoder,
        "-f",
        "matroska",
        str(video_tmp),
    ]


def build_still_command(*, original: Path, still_tmp: Path, vf: str | None) -> list[str]:
    """The first picture of an image (or video) as one raw v210 picture: the clip player
    uploads it once and shows it as a frame memory. Without `vf` (a frame grab's lossless
    original at its own raster) the samples stay as they are."""
    return [
        *_FFMPEG,
        *_INPUT,
        "-i",
        str(original),
        "-an",
        "-sn",
        "-dn",
        "-frames:v",
        "1",
        *(["-vf", vf] if vf else []),
        "-c:v",
        "v210",
        "-f",
        "rawvideo",
        str(still_tmp),
    ]


def build_grab_original_command(*, picture: Path, original_tmp: Path, width: int, height: int) -> list[str]:
    """A frame grab's raw v210 picture as its lossless original: FFV1 10-bit 4:2:2 in Matroska (as
    the colour bars), which decodes to the same samples."""
    return [
        *_FFMPEG,
        *_INPUT,
        "-f",
        "v210",
        "-video_size",
        f"{width}x{height}",
        "-i",
        str(picture),
        "-frames:v",
        "1",
        "-c:v",
        "ffv1",
        "-level",
        "3",
        "-pix_fmt",
        "yuv422p10le",
        "-f",
        "matroska",
        str(original_tmp),
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
    """The clip's sound as raw float32 (interleaved), remixed to dst_channels and padded or cut
    to exactly samples (whole frames). ffmpeg streams it to disk; the engine reads each frame's
    sound from that file. Without an original it writes silence of that length."""
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


def build_thumb_command(*, mezz: Path, thumb: Path) -> list[str]:
    return [
        *_FFMPEG,
        *_INPUT,
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


def build_stinger_alpha_command(
    *, input_args: list[str], alpha_tmp: Path, vf: str, width: int, height: int, fps: str, keyint: int, threads: int, nvenc: bool
) -> list[str]:
    """A stinger's alpha as the luma of HEVC 4:2:0 10-bit (Matroska; chroma 512, no range
    conversion), lossless, since NVDEC decodes no alpha and no 4:0:0 (docs/engine-p3.md §6). `vf`
    ends in yuva444p10le; `fps` is the format's rate (n/d). NVENC lossless, else x265 lossless."""
    if nvenc:
        encoder = ["-c:v", "hevc_nvenc", "-profile:v", "main10", "-preset", "p5", "-tune", "lossless", "-bf", "0", "-g", str(keyint)]
    else:
        params = f"lossless=1:keyint={keyint}:min-keyint={keyint}:bframes=0:scenecut=0:pools={threads}:log-level=error"
        encoder = ["-c:v", "libx265", "-profile:v", "main10", "-preset", "medium", "-x265-params", params]
    grey = f"color=c=0x808080:s={width // 2}x{height // 2}:r={fps},format=gray10le,setsar=1"
    return [
        *_FFMPEG,
        *_INPUT,
        *input_args,
        "-f",
        "lavfi",
        "-i",
        grey,
        "-filter_complex",
        f"[0:v]{vf},extractplanes=a,setsar=1[a];[a][1:v][1:v]mergeplanes=0x001020:yuv420p10le[alpha]",
        "-map",
        "[alpha]",
        "-shortest",
        "-an",
        "-sn",
        "-dn",
        *encoder,
        "-f",
        "matroska",
        str(alpha_tmp),
    ]
