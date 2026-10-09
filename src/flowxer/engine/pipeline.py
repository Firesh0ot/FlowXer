from __future__ import annotations

from flowxer.api.schemas import InputKind, LogicalInput
from flowxer.engine import gpu as gpu_path
from flowxer.settings import Settings


V210_CAPS = (
    "video/x-raw,format=v210,width={width},height={height},"
    "framerate={fps},interlace-mode=progressive,colorimetry=bt709"
)
BGRA_CAPS = "video/x-raw,format=BGRA,width={width},height={height},framerate={fps}"
# GPU path: sources that are not v210 are uploaded as RGBA.
RGBA_CAPS = "video/x-raw,format=RGBA,width={width},height={height},framerate={fps}"
AUDIO_CAPS = "audio/x-raw,format=F32LE,layout=interleaved,rate={rate},channels={channels}"
# Compositor output: 4:4:4 YUV with alpha, so Program (v210) needs no RGB matrix on
# its way in and out. Keyers and stingers (BGRA) are converted per pad.
MIX_CAPS = "video/x-raw,format=AYUV,width={width},height={height},framerate={fps}"

# Compositor and audiomixer pads. Program is the A bus; during a mix the B bus
# shows the incoming source above it. Every stinger gets a new pad above the keyer.
PAD_PROGRAM = "sink_0"
PAD_MIX = "sink_1"
PAD_KEYER = "sink_2"
STINGER_ZORDER = 3

# GUI monitor pictures: one appsink per source (MONITOR_PREFIX + input id) and
# one for Program, each holding its newest RGB picture.
MONITOR_PREFIX = "mon_"
MONITOR_PROGRAM = "mon__program"
MONITOR_WIDTH = 640
MONITOR_HEIGHT = 360


def _gst_string(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _v210(settings: Settings) -> str:
    return V210_CAPS.format(
        width=settings.width, height=settings.height, fps=settings.frame_rate
    )
    return V210_CAPS.format(
        width=settings.width, height=settings.height, fps=settings.frame_rate
    )


def _bgra(settings: Settings) -> str:
    return BGRA_CAPS.format(
        width=settings.width, height=settings.height, fps=settings.frame_rate
    )


def _rgba(settings: Settings) -> str:
    return RGBA_CAPS.format(
        width=settings.width, height=settings.height, fps=settings.frame_rate
    )


def _gl(settings: Settings) -> str:
    return gpu_path.gl_caps(settings.width, settings.height)


def _audio(settings: Settings) -> str:
    return AUDIO_CAPS.format(rate=settings.audio_rate, channels=settings.audio_channels)


def _mix(settings: Settings) -> str:
    return MIX_CAPS.format(
        width=settings.width, height=settings.height, fps=settings.frame_rate
    )


def _buses(kind: str, inp: LogicalInput, settings: Settings, gpu: bool = False) -> str:
    """A source feeds both selectors of its kind: A (Program) and B (incoming mix),
    and a video source its GUI monitor."""
    tee = f"{kind}t_{inp.id}"
    sel = "vsel" if kind == "v" else "asel"
    chain = (
        f"tee name={tee}\n"
        f"{tee}. ! queue ! {sel}.sink_{inp.slot}\n"
        f"{tee}. ! queue ! {sel}b.sink_{inp.slot}"
    )
    if kind == "v" and settings.monitor_fps:
        tap = gpu_monitor_tap if gpu else monitor_tap
        # A still never changes: its picture is taken once a second.
        fps = 1 if _is_still(inp) else settings.monitor_fps
        chain += f"\n{tee}. ! {tap(MONITOR_PREFIX + inp.id, fps)}"
    return chain


def _is_still(inp: LogicalInput) -> bool:
    """Black, colour bars, and a file or replay input without a clip: one frame, repeated."""
    if inp.kind in {InputKind.black, InputKind.test}:
        return True
    location = inp.file_path or "_unassigned"
    return inp.kind != InputKind.mxl_live and location.endswith("_unassigned")


def monitor_tap(name: str, fps: int) -> str:
    """Branch to a GUI monitor: the rate drops first, then one conversion pass
    scales the full-size frame straight to the small RGB picture."""
    return (
        "queue leaky=downstream max-size-buffers=1 ! videorate drop-only=true "
        f"! videoconvertscale ! video/x-raw,format=RGB,width={MONITOR_WIDTH},height={MONITOR_HEIGHT},"
        f"pixel-aspect-ratio=1/1,framerate={fps}/1 "
        # async=false: a source that never delivers (an MXL flow that is missing or silent) must not
        # keep the pipeline from PLAYING, or the Program sink waits and Program stops after a frame.
        f"! appsink name={name} max-buffers=1 drop=true sync=false async=false"
    )


# mxlsrc of an mxl_live essence without a route: a flow id that never exists, so the source
# waits for a route like for a missing flow. A non-UUID id made mxlsrc fail at start, and the
# failed sound branch kept the pipeline out of PLAYING (Program stopped after one frame).
UNROUTED_FLOW = "00000000-0000-0000-0000-000000000000"


def gpu_monitor_tap(name: str, fps: int) -> str:
    """GPU path: the rate drops first, then a shader makes the small RGB picture
    from the full-size frame on the GPU; only the picture is downloaded."""
    return (
        "queue leaky=downstream max-size-buffers=1 ! videorate drop-only=true "
        f"! glshader name={gpu_path.MONITOR_SHADER_PREFIX}{name} "
        f"! {gpu_path.gl_caps(MONITOR_WIDTH, MONITOR_HEIGHT, f'{fps}/1')} "
        "! gldownload ! videoconvert ! video/x-raw,format=RGB,pixel-aspect-ratio=1/1 "
        f"! appsink name={name} max-buffers=1 drop=true sync=false async=false"
    )


def _gpu_video_source(
    inp: LogicalInput,
    settings: Settings,
    domain: str,
    domain_paths: dict[str, str],
) -> str:
    """GPU path: each source is uploaded once and leaves as Y'CbCr on the GPU. MXL
    v210 goes up as its words (an RGBA texture a quarter of the stride wide) and a
    shader unpacks it; the other sources are made as RGBA and converted by a shader."""
    rgba = _rgba(settings)
    gl = _gl(settings)
    to_gpu = f"glupload ! glshader name={gpu_path.YUV_PREFIX}{inp.id} ! {gl} ! "
    # Black and colour bars are made, uploaded and converted once; imagefreeze repeats the frame.
    still = (
        f"num-buffers=1 ! {rgba} ! {to_gpu}imagefreeze is-live=true "
        f"! {gpu_path.gl_caps(settings.width, settings.height, settings.frame_rate)} ! "
    )
    if inp.kind == InputKind.mxl_live:
        flow_id = str(inp.video.flow_id) if inp.video and inp.video.flow_id else UNROUTED_FLOW
        src_domain = domain_paths.get(f"{inp.id}:video", domain)
        proxy = gpu_path.proxy_caps(settings.width, settings.height, settings.frame_rate)
        # drop-allocation: capssetter passes the allocation query on with the v210 caps, and
        # glupload would answer with a GL buffer pool for v210, which GL cannot make (abort
        # when the source renegotiates, e.g. when a selector switches to it).
        return (
            f"mxlsrc name=vsrc_{inp.id} video-flow-id={flow_id} "
            f"domain={_gst_string(src_domain)} "
            f"! queue max-size-buffers=2 leaky=downstream "
            f"! videoconvert ! {_v210(settings)} "
            f'! capssetter replace=true caps="{proxy}" ! identity drop-allocation=true '
            f"! glupload ! glshader name={gpu_path.UNPACK_PREFIX}{inp.id} ! {gl} ! "
        )
    if inp.kind == InputKind.black:
        return (
            f"videotestsrc name=vsrc_{inp.id} pattern=black "
            f"foreground-color=0xFF000000 background-color=0xFF000000 {still}"
        )
    if inp.kind == InputKind.test:
        return f"videotestsrc name=vsrc_{inp.id} pattern=smpte {still}"
    location = inp.file_path or "_unassigned"
    if location.endswith("_unassigned") or location == "_unassigned":
        return f"videotestsrc name=vsrc_{inp.id} pattern=black {still}"
    return (
        f'filesrc name=vsrc_{inp.id} location="{location}" '
        f"! decodebin name=vdec_{inp.id} "
        f"! videoconvert ! videoscale ! videorate "
        f"! {rgba} ! identity sync=true ! {to_gpu}"
    )


def _gpu_video_source_bin(
    inp: LogicalInput,
    settings: Settings,
    domain: str,
    domain_paths: dict[str, str],
) -> str:
    return _gpu_video_source(inp, settings, domain, domain_paths) + _buses("v", inp, settings, gpu=True)


def _video_source_bin(
    inp: LogicalInput,
    settings: Settings,
    domain: str,
    domain_paths: dict[str, str],
) -> str:
    caps = _v210(settings)
    bgra = _bgra(settings)
    # Black and colour bars are made once; imagefreeze repeats the frame live at the mixer rate.
    still = f"num-buffers=1 ! {caps} ! imagefreeze is-live=true ! {caps} ! "
    if inp.kind == InputKind.mxl_live:
        flow_id = str(inp.video.flow_id) if inp.video and inp.video.flow_id else UNROUTED_FLOW
        src_domain = domain_paths.get(f"{inp.id}:video", domain)
        chain = (
            f"mxlsrc name=vsrc_{inp.id} video-flow-id={flow_id} "
            f"domain={_gst_string(src_domain)} "
            f"! queue max-size-buffers=2 leaky=downstream "
            f"! videoconvert ! {caps} ! "
        )
    elif inp.kind == InputKind.black:
        chain = (
            f"videotestsrc name=vsrc_{inp.id} pattern=black "
            f"foreground-color=0xFF000000 background-color=0xFF000000 {still}"
        )
    elif inp.kind == InputKind.test:
        chain = f"videotestsrc name=vsrc_{inp.id} pattern=smpte {still}"
    else:
        # file / replay: decode any container, convert to uncompressed v210, run as live.
        location = inp.file_path or "_unassigned"
        if location.endswith("_unassigned") or location == "_unassigned":
            chain = f"videotestsrc name=vsrc_{inp.id} pattern=black {still}"
        else:
            chain = (
                f'filesrc name=vsrc_{inp.id} location="{location}" '
                f"! decodebin name=vdec_{inp.id} "
                f"! videoconvert ! videoscale ! videorate "
                f"! {bgra} ! videoconvert ! {caps} "
                f"! identity sync=true ! "
            )
    return chain + _buses("v", inp, settings)


def _audio_source_bin(
    inp: LogicalInput,
    settings: Settings,
    domain: str,
    domain_paths: dict[str, str],
) -> str:
    caps = _audio(settings)
    if inp.kind == InputKind.mxl_live:
        flow_id = str(inp.audio.flow_id) if inp.audio and inp.audio.flow_id else UNROUTED_FLOW
        src_domain = domain_paths.get(f"{inp.id}:audio", domain)
        chain = (
            f"mxlsrc name=asrc_{inp.id} audio-flow-id={flow_id} "
            f"domain={_gst_string(src_domain)} "
            f"! queue max-size-buffers=2 leaky=downstream "
            f"! audioconvert name={AUDIO_MAP_PREFIX}{inp.id} ! audioconvert ! audioresample ! {caps} ! "
        )
    elif inp.kind in {InputKind.black, InputKind.test}:
        wave = "silence" if inp.kind == InputKind.black else "ticks"
        chain = (
            f"audiotestsrc name=asrc_{inp.id} wave={wave} is-live=true "
            f"! {caps} ! "
        )
    else:
        location = inp.file_path or "_unassigned"
        if location.endswith("_unassigned") or location == "_unassigned":
            chain = (
                f"audiotestsrc name=asrc_{inp.id} wave=silence is-live=true "
                f"! {caps} ! "
            )
        else:
            chain = (
                f'filesrc name=asrc_{inp.id} location="{location}" '
                f"! decodebin name=adec_{inp.id} "
                f"! audioconvert ! audioresample ! {caps} "
                f"! identity sync=true ! "
            )
    return chain + _buses("a", inp, settings)


# MXL audio: this audioconvert gets a mix-matrix when the flow's caps arrive
# (GstRuntime.map_audio_channels), so its channel count can be anything.
AUDIO_MAP_PREFIX = "amap_"


def stinger_bin_description(stinger: dict, settings: Settings, gpu: bool = False) -> str:
    """One playback of a stinger: decoded to BGRA at the mixer raster and rate
    (GPU path: RGBA, uploaded and converted to Y'CbCr by a shader).

    The runtime builds a new bin from this for every playback and links it to a
    new compositor pad, so a stinger can play any number of times.
    """
    bgra = _bgra(settings)
    if stinger.get("kind") == "video":
        source = f"filesrc location={_gst_string(stinger.get('media_path') or stinger['path'])}"
    else:
        # A TGA sequence has no rate of its own: give it the mixer rate.
        location = f"{stinger['path']}/{stinger['pattern']}"
        source = (
            f"multifilesrc location={_gst_string(location)} index=0 "
            f"stop-index={stinger['frame_count'] - 1} loop=false "
            f"caps=image/x-tga,framerate={settings.frame_rate}"
        )
    if gpu:
        return (
            f"{source} ! decodebin ! videoconvert ! videoscale ! videorate ! {_rgba(settings)} "
            f"! glupload ! glshader name={gpu_path.YUV_PREFIX}stinger ! {_gl(settings)} ! queue name=stingerq"
        )
    return f"{source} ! decodebin ! videoconvert ! videoscale ! videorate ! {bgra} ! queue name=stingerq"


def build_pipeline_description(
    *,
    settings: Settings,
    inputs: list[LogicalInput],
    overlay_url: str,
    overlay_enabled: bool,
    output_video_flow_id: str,
    output_audio_flow_id: str,
    domain: str,
    use_mxl_sink: bool,
    use_cefsrc: bool,
    domain_paths: dict[str, str] | None = None,
    gpu: bool = False,
) -> str:
    """
    Build a GStreamer gst-launch-style description:

      sources → tee → input-selector A (Program) / B (mix) → compositor (+ HTML5 keyer,
      + stingers added while they play) → v210 mxlsink
      sources → tee → input-selector A / B → audiomixer → float32 mxlsink

    `gpu`: the same graph with the video on the GPU (flowxer.engine.gpu): sources
    uploaded once, glvideomixerelement as the compositor, Program packed to v210
    by a shader and downloaded once.
    """
    if not inputs:
        raise ValueError("at least one logical input is required")

    bgra = _bgra(settings)
    v210 = _v210(settings)
    audio = _audio(settings)
    overlay_alpha = "1.0" if overlay_enabled else "0.0"

    video_bin = _gpu_video_source_bin if gpu else _video_source_bin
    video_sources = "\n".join(
        video_bin(i, settings, domain, domain_paths or {}) for i in inputs
    )
    audio_sources = "\n".join(
        _audio_source_bin(i, settings, domain, domain_paths or {}) for i in inputs
    )

    if use_cefsrc:
        overlay_bin = (
            f'cefsrc name=html5 url="{overlay_url}" '
            f"! {bgra} ! videorate ! {bgra} ! queue name=html5q "
            f"! comp.{PAD_KEYER}"
        )
    else:
        overlay_bin = (
            f"videotestsrc name=html5 pattern=black is-live=true "
            f"! {bgra} ! gdkpixbufoverlay name=html5fallback "
            f"! queue name=html5q ! comp.{PAD_KEYER}"
        )

    if gpu:
        # The keyer is BGRA: its bytes go up as an RGBA image (capssetter, no copy) and the shader
        # swaps red and blue as it converts (a glcolorconvert pass cost the GL thread 7 %, 1080p50).
        overlay_bin = overlay_bin.replace(
            f"! comp.{PAD_KEYER}",
            f'! capssetter replace=true caps="{_rgba(settings)}" ! identity drop-allocation=true '
            f"! glupload ! glshader name={gpu_path.BGRA_PREFIX}html5 ! {_gl(settings)} "
            f"! comp.{PAD_KEYER}",
        )

    tap = gpu_monitor_tap if gpu else monitor_tap
    program_monitor = (
        f"\npgmt. ! {tap(MONITOR_PROGRAM, settings.monitor_fps)}" if settings.monitor_fps else ""
    )

    # Video sink qos=true: it measures how late each frame is; GstRuntime keeps Program on the TAI
    # timeline from that (the compositor skips frames when it is late).
    if use_mxl_sink:
        video_sink = (
            f"videoconvert ! {v210} ! queue ! "
            f"mxlsink name=vout qos=true flow-id={output_video_flow_id} domain={_gst_string(domain)}"
        )
        audio_sink = (
            f"queue ! {audio} ! "
            f"mxlsink name=aout flow-id={output_audio_flow_id} domain={_gst_string(domain)}"
        )
    else:
        video_sink = f"videoconvert ! {v210} ! queue ! fakesink name=vout sync=true qos=true"
        audio_sink = f"queue ! {audio} ! fakesink name=aout sync=true"

    compositor = "compositor name=comp background=black"
    mix_caps = _mix(settings)
    if gpu:
        # Program is packed to v210 words on the GPU, downloaded once and labelled v210.
        # The background is transparent so that the pack shader puts legal black where
        # nothing was drawn.
        compositor = "glvideomixerelement name=comp background=transparent"
        # With the rate fixed like the CPU compositor's: the monitor branch (videorate)
        # would otherwise let the mixer settle on its rate.
        mix_caps = gpu_path.gl_caps(settings.width, settings.height, settings.frame_rate)
        packed = gpu_path.gl_caps(gpu_path.proxy_width(settings.width), settings.height)
        video_sink = video_sink.replace(
            f"videoconvert ! {v210} ! ",
            f"glshader name={gpu_path.PACK_PREFIX} ! {packed} ! gldownload ! video/x-raw,format=RGBA "
            f'! capssetter replace=true caps="{v210}" ! ',
        )

    # The compositor converts each pad itself and skips a pad whose alpha is 0, so
    # the B bus and an idle keyer cost nothing until they are shown.
    return f"""
input-selector name=vsel sync-streams=true cache-buffers=true
input-selector name=vselb sync-streams=true cache-buffers=true
input-selector name=asel sync-streams=true cache-buffers=true
input-selector name=aselb sync-streams=true cache-buffers=true
{compositor} emit-signals=true
  {PAD_PROGRAM}::zorder=0
  {PAD_MIX}::zorder=1 {PAD_MIX}::alpha=0.0
  {PAD_KEYER}::zorder=2 {PAD_KEYER}::alpha={overlay_alpha}
audiomixer name=amix emit-signals=true {PAD_PROGRAM}::volume=1.0 {PAD_MIX}::volume=0.0

{video_sources}

vsel. ! queue ! comp.{PAD_PROGRAM}
vselb. ! queue ! comp.{PAD_MIX}

{overlay_bin}

comp. ! {mix_caps} ! tee name=pgmt ! identity name=ptsfix ! {video_sink}{program_monitor}

{audio_sources}

asel. ! audioconvert ! queue ! amix.{PAD_PROGRAM}
aselb. ! audioconvert ! queue ! amix.{PAD_MIX}
amix. ! audioconvert ! audioresample ! {audio_sink}
""".strip()
