from __future__ import annotations

from flowxer.api.schemas import InputKind, LogicalInput
from flowxer.settings import Settings


V210_CAPS = (
    "video/x-raw,format=v210,width={width},height={height},"
    "framerate={fps},interlace-mode=progressive,colorimetry=bt709"
)
BGRA_CAPS = "video/x-raw,format=BGRA,width={width},height={height},framerate={fps}"
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


def _audio(settings: Settings) -> str:
    return AUDIO_CAPS.format(rate=settings.audio_rate, channels=settings.audio_channels)


def _mix(settings: Settings) -> str:
    return MIX_CAPS.format(
        width=settings.width, height=settings.height, fps=settings.frame_rate
    )


def _buses(kind: str, inp: LogicalInput) -> str:
    """A source feeds both selectors of its kind: A (Program) and B (incoming mix)."""
    tee = f"{kind}t_{inp.id}"
    sel = "vsel" if kind == "v" else "asel"
    return (
        f"tee name={tee}\n"
        f"{tee}. ! queue ! {sel}.sink_{inp.slot}\n"
        f"{tee}. ! queue ! {sel}b.sink_{inp.slot}"
    )


def _video_source_bin(
    inp: LogicalInput,
    settings: Settings,
    domain: str,
    domain_paths: dict[str, str],
) -> str:
    caps = _v210(settings)
    bgra = _bgra(settings)
    if inp.kind == InputKind.mxl_live:
        flow_id = str(inp.video.flow_id) if inp.video and inp.video.flow_id else "UNBOUND"
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
            f"foreground-color=0xFF000000 background-color=0xFF000000 is-live=true "
            f"! {caps} ! "
        )
    elif inp.kind == InputKind.test:
        chain = (
            f"videotestsrc name=vsrc_{inp.id} pattern=smpte is-live=true "
            f"! timeoverlay ! {caps} ! "
        )
    else:
        # file / replay: decode any container, convert to uncompressed v210, run as live.
        location = inp.file_path or "_unassigned"
        if location.endswith("_unassigned") or location == "_unassigned":
            chain = (
                f"videotestsrc name=vsrc_{inp.id} pattern=black is-live=true "
                f"! {caps} ! "
            )
        else:
            chain = (
                f'filesrc name=vsrc_{inp.id} location="{location}" '
                f"! decodebin name=vdec_{inp.id} "
                f"! videoconvert ! videoscale ! videorate "
                f"! {bgra} ! videoconvert ! {caps} "
                f"! identity sync=true ! "
            )
    return chain + _buses("v", inp)


def _audio_source_bin(
    inp: LogicalInput,
    settings: Settings,
    domain: str,
    domain_paths: dict[str, str],
) -> str:
    caps = _audio(settings)
    if inp.kind == InputKind.mxl_live:
        flow_id = str(inp.audio.flow_id) if inp.audio and inp.audio.flow_id else "UNBOUND"
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
    return chain + _buses("a", inp)


# MXL audio: this audioconvert gets a mix-matrix when the flow's caps arrive
# (GstRuntime.map_audio_channels), so its channel count can be anything.
AUDIO_MAP_PREFIX = "amap_"


def stinger_bin_description(stinger: dict, settings: Settings) -> str:
    """One playback of a stinger: decoded to BGRA at the mixer raster and rate.

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
) -> str:
    """
    Build a GStreamer gst-launch-style description:

      sources → tee → input-selector A (Program) / B (mix) → compositor (+ HTML5 keyer,
      + stingers added while they play) → v210 mxlsink
      sources → tee → input-selector A / B → audiomixer → float32 mxlsink
    """
    if not inputs:
        raise ValueError("at least one logical input is required")

    bgra = _bgra(settings)
    v210 = _v210(settings)
    audio = _audio(settings)
    overlay_alpha = "1.0" if overlay_enabled else "0.0"

    video_sources = "\n".join(
        _video_source_bin(i, settings, domain, domain_paths or {}) for i in inputs
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

    if use_mxl_sink:
        video_sink = (
            f"videoconvert ! {v210} ! queue ! "
            f"mxlsink name=vout flow-id={output_video_flow_id} domain={_gst_string(domain)}"
        )
        audio_sink = (
            f"queue ! {audio} ! "
            f"mxlsink name=aout flow-id={output_audio_flow_id} domain={_gst_string(domain)}"
        )
    else:
        video_sink = f"videoconvert ! {v210} ! queue ! fakesink name=vout sync=true"
        audio_sink = f"queue ! {audio} ! fakesink name=aout sync=true"

    # The compositor converts each pad itself and skips a pad whose alpha is 0, so
    # the B bus and an idle keyer cost nothing until they are shown.
    return f"""
input-selector name=vsel sync-streams=true cache-buffers=true
input-selector name=vselb sync-streams=true cache-buffers=true
input-selector name=asel sync-streams=true cache-buffers=true
input-selector name=aselb sync-streams=true cache-buffers=true
compositor name=comp background=black emit-signals=true
  {PAD_PROGRAM}::zorder=0
  {PAD_MIX}::zorder=1 {PAD_MIX}::alpha=0.0
  {PAD_KEYER}::zorder=2 {PAD_KEYER}::alpha={overlay_alpha}
audiomixer name=amix emit-signals=true {PAD_PROGRAM}::volume=1.0 {PAD_MIX}::volume=0.0

{video_sources}

vsel. ! queue ! comp.{PAD_PROGRAM}
vselb. ! queue ! comp.{PAD_MIX}

{overlay_bin}

comp. ! {_mix(settings)} ! identity name=ptsfix ! {video_sink}

{audio_sources}

asel. ! audioconvert ! queue ! amix.{PAD_PROGRAM}
aselb. ! audioconvert ! queue ! amix.{PAD_MIX}
amix. ! audioconvert ! audioresample ! {audio_sink}
""".strip()
