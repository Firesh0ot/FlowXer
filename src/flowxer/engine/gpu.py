"""Optional GPU media path (FLOWXER_GPU): OpenGL on an NVIDIA GPU through EGL.

Every video source is uploaded to the GPU once and stays there as 8-bit Y'CbCr
4:4:4 with alpha in an RGBA texture (R = Y', G = Cb, B = Cr): the samples the CPU
path's AYUV compositor works on, so Program goes through no RGB matrix either.
`glvideomixerelement` composites with the compositor's pad properties (alpha,
zorder), Program is packed back to v210 on the GPU and downloaded once.

GStreamer 1.24's GL elements do not know v210. Its bytes travel as an RGBA
texture a quarter of the line stride wide (one texel is one 32-bit v210 word,
`capssetter` relabels the buffer without a copy), and `glshader` programs unpack
and pack the 10-bit fields.
"""

from __future__ import annotations

import ctypes
import glob
import os
import struct
from dataclasses import dataclass
from pathlib import Path


MEDIA_PATH_GPU = "gpu"
MEDIA_PATH_CPU = "cpu"

# glshader element names start with these; the runtime sets each one's fragment
# shader after parsing (fragment_for).
UNPACK_PREFIX = "gpu_unpack_"
YUV_PREFIX = "gpu_yuv_"
BGRA_PREFIX = "gpu_bgra_"
PACK_PREFIX = "gpu_pack"
MONITOR_SHADER_PREFIX = "gpu_mon_"

REQUIRED_ELEMENTS = ("capssetter", "glupload", "glshader", "glvideomixerelement", "gldownload")
EGL_VENDOR_DIRS = ("/usr/share/glvnd/egl_vendor.d", "/etc/glvnd/egl_vendor.d")
PROBE_TIMEOUT_S = 10.0

# BT.709, the colorimetry of the mixer's v210 caps.
KR = 0.2126
KB = 0.0722


def v210_stride(width: int) -> int:
    """Bytes per v210 line: 48-pixel blocks of 128 bytes."""
    return (width + 47) // 48 * 128


def proxy_width(width: int) -> int:
    """Width of the RGBA texture that carries a v210 line (one texel per word)."""
    return v210_stride(width) // 4


def gl_caps(width: int, height: int, frame_rate: str | None = None) -> str:
    rate = f",framerate={frame_rate}" if frame_rate else ""
    return f"video/x-raw(memory:GLMemory),format=RGBA,width={width},height={height}{rate}"


def proxy_caps(width: int, height: int, frame_rate: str) -> str:
    """The caps of a v210 frame relabelled as its RGBA word texture."""
    return (
        f"video/x-raw,format=RGBA,width={proxy_width(width)},height={height},"
        f"framerate={frame_rate},pixel-aspect-ratio=1/1,interlace-mode=progressive"
    )


_HEADER = """
#ifdef GL_ES
precision highp float;
#endif
varying vec2 v_texcoord;
uniform sampler2D tex;
"""

# 10-bit codes are kept as code / 1020 in the 8-bit texture: 8-bit = 10-bit / 4,
# so 64 (black), 512 and 940 (white) come back exactly.
_CODE_SCALE = "1020.0"


def unpack_v210(width: int) -> str:
    """Fragment: v210 words (RGBA bytes, proxy width) to Y'CbCr + alpha, `width` wide."""
    return _HEADER + f"""
const float W = {float(width)};
const float PW = {float(proxy_width(width))};

vec3 fields(float word, float row) {{
    vec4 t = texture2D(tex, vec2((word + 0.5) / PW, row));
    vec4 b = floor(t * 255.0 + 0.5);
    return vec3(
        b.r + mod(b.g, 4.0) * 256.0,
        floor(b.g / 4.0) + mod(b.b, 16.0) * 64.0,
        floor(b.b / 16.0) + mod(b.a, 64.0) * 16.0);
}}

void main() {{
    float x = floor(v_texcoord.x * W);
    float group = floor(x / 6.0);
    float phase = x - group * 6.0;
    float row = v_texcoord.y;
    vec3 w0 = fields(group * 4.0, row);
    vec3 w1 = fields(group * 4.0 + 1.0, row);
    vec3 w2 = fields(group * 4.0 + 2.0, row);
    vec3 w3 = fields(group * 4.0 + 3.0, row);
    vec3 ycc;
    if (phase < 0.5) ycc = vec3(w0.y, w0.x, w0.z);
    else if (phase < 1.5) ycc = vec3(w1.x, w0.x, w0.z);
    else if (phase < 2.5) ycc = vec3(w1.z, w1.y, w2.x);
    else if (phase < 3.5) ycc = vec3(w2.y, w1.y, w2.x);
    else if (phase < 4.5) ycc = vec3(w3.x, w2.z, w3.y);
    else ycc = vec3(w3.z, w2.z, w3.y);
    gl_FragColor = vec4(clamp(ycc / {_CODE_SCALE}, 0.0, 1.0), 1.0);
}}
"""


# Composited Y'CbCr is "over transparent": where nothing was drawn (alpha < 1) the
# rest is legal black.
_OVER_BLACK = """
const vec3 BLACK = vec3(16.0, 128.0, 128.0) / 255.0;

vec3 ycc_at(vec2 uv) {
    vec4 c = texture2D(tex, uv);
    return c.rgb + (1.0 - c.a) * BLACK;
}
"""


def pack_v210(width: int) -> str:
    """Fragment: composited Y'CbCr (`width` wide) to v210 words as RGBA bytes (proxy width)."""
    return _HEADER + _OVER_BLACK + f"""
const float W = {float(width)};
const float PW = {float(proxy_width(width))};

vec3 code(float x, float row) {{
    return floor(ycc_at(vec2((min(x, W - 1.0) + 0.5) / W, row)) * 255.0 + 0.5) * 4.0;
}}

float chroma(float a, float b) {{
    return floor((a + b) * 0.5);
}}

void main() {{
    float word = floor(v_texcoord.x * PW);
    float group = floor(word / 4.0);
    float index = word - group * 4.0;
    float row = v_texcoord.y;
    float x = group * 6.0;
    vec3 p0 = code(x, row);
    vec3 p1 = code(x + 1.0, row);
    vec3 p2 = code(x + 2.0, row);
    vec3 p3 = code(x + 3.0, row);
    vec3 p4 = code(x + 4.0, row);
    vec3 p5 = code(x + 5.0, row);
    vec3 f;
    if (index < 0.5) f = vec3(chroma(p0.y, p1.y), p0.x, chroma(p0.z, p1.z));
    else if (index < 1.5) f = vec3(p1.x, chroma(p2.y, p3.y), p2.x);
    else if (index < 2.5) f = vec3(chroma(p2.z, p3.z), p3.x, chroma(p4.y, p5.y));
    else f = vec3(p4.x, chroma(p4.z, p5.z), p5.x);
    f = clamp(f, 4.0, 1019.0);
    gl_FragColor = vec4(
        mod(f.x, 256.0),
        floor(f.x / 256.0) + mod(f.y, 64.0) * 4.0,
        floor(f.y / 64.0) + mod(f.z, 16.0) * 16.0,
        floor(f.z / 16.0)) / 255.0;
}}
"""


def _to_yuv(texel: str) -> str:
    """Fragment: RGB + alpha (the texel read by `texel`) to Y'CbCr + alpha."""
    return _HEADER + f"""
void main() {{
    vec4 c = {texel};
    float y = {KR} * c.r + {1.0 - KR - KB:.4f} * c.g + {KB} * c.b;
    gl_FragColor = vec4(
        (16.0 + 219.0 * y) / 255.0,
        (128.0 + 224.0 * (c.b - y) / {2.0 * (1.0 - KB):.4f}) / 255.0,
        (128.0 + 224.0 * (c.r - y) / {2.0 * (1.0 - KR):.4f}) / 255.0,
        c.a);
}}
"""


RGB_TO_YUV = _to_yuv("texture2D(tex, v_texcoord)")
# The HTML keyer's BGRA bytes, uploaded as an RGBA image: red and blue are swapped back.
BGRA_TO_YUV = _to_yuv("texture2D(tex, v_texcoord).bgra")


def monitor_rgb(width: int, height: int) -> str:
    """Fragment: composited Y'CbCr to RGB for a `width` x `height` GUI monitor. Four
    bilinear taps a quarter monitor pixel apart average the area it covers."""
    return _HEADER + _OVER_BLACK + f"""
const vec2 STEP = vec2({0.25 / width!r}, {0.25 / height!r});

void main() {{
    vec3 ycc = (ycc_at(v_texcoord - STEP) + ycc_at(v_texcoord + STEP)
        + ycc_at(v_texcoord + vec2(STEP.x, -STEP.y)) + ycc_at(v_texcoord + vec2(-STEP.x, STEP.y))) * 0.25;
    float y = (ycc.x * 255.0 - 16.0) / 219.0;
    float pb = (ycc.y * 255.0 - 128.0) / 224.0;
    float pr = (ycc.z * 255.0 - 128.0) / 224.0;
    float r = y + {2.0 * (1.0 - KR):.4f} * pr;
    float b = y + {2.0 * (1.0 - KB):.4f} * pb;
    float g = (y - {KR} * r - {KB} * b) / {1.0 - KR - KB:.4f};
    gl_FragColor = vec4(clamp(vec3(r, g, b), 0.0, 1.0), 1.0);
}}
"""


def fragment_for(name: str, width: int) -> str | None:
    """The fragment shader of the glshader element `name` at the mixer raster."""
    if name.startswith(UNPACK_PREFIX):
        return unpack_v210(width)
    if name.startswith(YUV_PREFIX):
        return RGB_TO_YUV
    if name.startswith(BGRA_PREFIX):
        return BGRA_TO_YUV
    if name.startswith(PACK_PREFIX):
        return pack_v210(width)
    if name.startswith(MONITOR_SHADER_PREFIX):
        from flowxer.engine.pipeline import MONITOR_HEIGHT, MONITOR_WIDTH

        return monitor_rgb(MONITOR_WIDTH, MONITOR_HEIGHT)
    return None


# ── selection ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class MediaPath:
    """The media path the mixer builds its pipeline for, and why."""

    path: str
    reason: str

    @property
    def gpu(self) -> bool:
        return self.path == MEDIA_PATH_GPU


NO_NVIDIA_DEVICE = "no NVIDIA device (/dev/nvidiactl, /dev/nvidia<N>)"


class GpuUnavailableError(RuntimeError):
    """FLOWXER_GPU=on, but the GPU path does not work here (exit 78)."""


def select_media_path(mode: str) -> MediaPath:
    """FLOWXER_GPU: `off` is the CPU path; `auto` the GPU path when it works, else
    the CPU path; `on` the GPU path or GpuUnavailableError."""
    if mode == "off":
        return MediaPath(MEDIA_PATH_CPU, "FLOWXER_GPU=off")
    renderer, problem = probe_gpu()
    if problem is None:
        return MediaPath(MEDIA_PATH_GPU, f"OpenGL through EGL on {renderer}")
    if mode == "on":
        raise GpuUnavailableError(f"FLOWXER_GPU=on, but the GPU media path does not work: {problem}")
    return MediaPath(MEDIA_PATH_CPU, problem)


def probe_gpu() -> tuple[str, str | None]:
    """(GL renderer, None) when an NVIDIA GPU runs the GL elements and the v210
    shaders give back the exact frame, else ("", the reason).

    Sets the GStreamer GL environment for EGL without a display server (the image
    runs an Xvfb for the HTML keyer, whose GLX is software): GST_GL_PLATFORM=egl,
    GST_GL_WINDOW=egl-device and only NVIDIA's EGL vendor library, so a Mesa
    software renderer cannot stand in. Variables already set are kept."""
    if not _nvidia_device():
        return "", NO_NVIDIA_DEVICE
    vendor = _nvidia_egl_vendor_file()
    if vendor is None:
        return "", f"no glvnd EGL vendor file for libEGL_nvidia.so.0 in {', '.join(EGL_VENDOR_DIRS)}"
    try:
        ctypes.CDLL("libEGL_nvidia.so.0")
    except OSError:
        return "", "libEGL_nvidia.so.0 cannot be loaded (NVIDIA_DRIVER_CAPABILITIES needs graphics)"
    try:
        import gi

        gi.require_version("Gst", "1.0")
        from gi.repository import Gst

        Gst.init(None)
    except Exception:
        return "", "GStreamer with PyGObject is not installed"
    missing = [name for name in REQUIRED_ELEMENTS if Gst.ElementFactory.find(name) is None]
    if missing:
        return "", f"missing GStreamer elements: {', '.join(missing)} (gstreamer1.0-gl)"
    saved = {name: os.environ.get(name) for name in _GL_ENVIRONMENT}
    for name, value in _GL_ENVIRONMENT.items():
        os.environ.setdefault(name, value or vendor)
    try:
        return _round_trip(Gst), None
    except Exception as exc:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        return "", f"GL test frame failed: {exc}"


_GL_ENVIRONMENT = {
    "GST_GL_PLATFORM": "egl",
    "GST_GL_WINDOW": "egl-device",
    # Empty: the NVIDIA vendor file that was found.
    "__EGL_VENDOR_LIBRARY_FILENAMES": "",
}


def _nvidia_device() -> bool:
    return Path("/dev/nvidiactl").exists() and bool(glob.glob("/dev/nvidia[0-9]*"))


def _nvidia_egl_vendor_file() -> str | None:
    for directory in EGL_VENDOR_DIRS:
        for path in sorted(Path(directory).glob("*.json")):
            try:
                if "libEGL_nvidia" in path.read_text(encoding="utf-8"):
                    return str(path)
            except OSError:
                continue
    return None


# The test frame: two 48-pixel blocks, codes that 8 bits hold exactly.
_TEST_WIDTH = 96
_TEST_HEIGHT = 2


def _test_frame() -> bytes:
    words = []
    for row in range(_TEST_HEIGHT):
        for group in range(_TEST_WIDTH // 6):
            luma = [4 * (16 + (row * 97 + group * 13 + i * 31) % 219) for i in range(6)]
            cb = [4 * (16 + (row * 53 + group * 7 + i * 41) % 224) for i in range(3)]
            cr = [4 * (16 + (row * 29 + group * 11 + i * 23) % 224) for i in range(3)]
            for a, b, c in (
                (cb[0], luma[0], cr[0]),
                (luma[1], cb[1], luma[2]),
                (cr[1], luma[3], cb[2]),
                (luma[4], cr[2], luma[5]),
            ):
                words.append(a | b << 10 | c << 20)
    return struct.pack(f"<{len(words)}I", *words)


def _round_trip(Gst) -> str:
    """Unpack and pack one v210 frame on the GPU; returns the GL renderer."""
    width, height = _TEST_WIDTH, _TEST_HEIGHT
    v210 = f"video/x-raw,format=v210,width={width},height={height},framerate=50/1,interlace-mode=progressive"
    pipeline = Gst.parse_launch(
        f'appsrc name=src format=time caps="{v210}" '
        f'! capssetter replace=true caps="{proxy_caps(width, height, "50/1")}" '
        f"! glupload ! glshader name={UNPACK_PREFIX}probe ! {gl_caps(width, height)} "
        f"! glshader name={PACK_PREFIX} ! {gl_caps(proxy_width(width), height)} "
        f'! gldownload ! video/x-raw,format=RGBA ! capssetter replace=true caps="{v210}" '
        "! appsink name=out sync=false"
    )
    for name in (f"{UNPACK_PREFIX}probe", PACK_PREFIX):
        pipeline.get_by_name(name).set_property("fragment", fragment_for(name, width))
    frame = _test_frame()
    try:
        if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            raise RuntimeError(_bus_error(pipeline) or "the pipeline did not start")
        buffer = Gst.Buffer.new_wrapped(frame)
        buffer.pts = 0
        buffer.duration = 20_000_000
        pipeline.get_by_name("src").emit("push-buffer", buffer)
        sample = pipeline.get_by_name("out").emit("try-pull-sample", int(PROBE_TIMEOUT_S * Gst.SECOND))
        if sample is None:
            raise RuntimeError(_bus_error(pipeline) or "no frame came back")
        out = sample.get_buffer()
        ok, mapped = out.map(Gst.MapFlags.READ)
        if not ok:
            raise RuntimeError("the frame cannot be read")
        data = bytes(mapped.data)
        out.unmap(mapped)
        if data != frame:
            raise RuntimeError("the v210 shaders changed the frame")
        return _renderer(pipeline.get_by_name(PACK_PREFIX)) or "an NVIDIA GPU"
    finally:
        pipeline.set_state(Gst.State.NULL)


def _bus_error(pipeline) -> str:
    from gi.repository import Gst

    message = pipeline.get_bus().pop_filtered(Gst.MessageType.ERROR)
    return message.parse_error()[0].message if message is not None else ""


def _renderer(element) -> str:
    """GL_RENDERER of the element's GL context (best effort)."""
    try:
        import gi

        gi.require_version("GstGL", "1.0")
        from gi.repository import GstGL  # noqa: F401  (GstGLContext.thread_add)

        context = element.get_property("context")
        gles = ctypes.CDLL("libGLESv2.so.2")
        gles.glGetString.restype = ctypes.c_char_p
        gles.glGetString.argtypes = [ctypes.c_uint]
        found: list[str] = []
        context.thread_add(lambda _context, *_: found.append(gles.glGetString(0x1F01).decode()), None)
        return found[0] if found else ""
    except Exception:
        return ""
