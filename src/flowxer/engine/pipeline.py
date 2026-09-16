from __future__ import annotations

from flowxer.api.schemas import InputKind, LogicalInput
from flowxer.settings import Settings


V210_CAPS = (
    "video/x-raw,format=v210,width={width},height={height},"
    "framerate={fps},interlace-mode=progressive,colorimetry=bt709"
)
BGRA_CAPS = "video/x-raw,format=BGRA,width={width},height={height},framerate={fps}"
AUDIO_CAPS = "audio/x-raw,format=F32LE,layout=interleaved,rate={rate},channels={channels}"


def _v210(settings: Settings) -> str:
    return V210_CAPS.format(
        width=settings.width, height=settings.height, fps=settings.frame_rate
    )


def _bgra(settings: Settings) -> str:
    return BGRA_CAPS.format(
        width=settings.width, height=settings.height, fps=settings.frame_rate
    )


def _audio(settings: Settings) -> str:
    return AUDIO_CAPS.format(rate=settings.audio_rate, channels=settings.audio_channels)


def _video_source_bin(inp: LogicalInput, settings: Settings, domain: str) -> str:
    caps = _v210(settings)
    bgra = _bgra(settings)
    if inp.kind == InputKind.mxl_live:
        flow_id = str(inp.video.flow_id) if inp.video and inp.video.flow_id else "UNBOUND"
        return (
            f"mxlsrc name=vsrc_{inp.id} video-flow-id={flow_id} domain={domain} "
            f"! queue max-size-buffers=2 leaky=downstream "
            f"! videoconvert ! {caps} ! queue ! vsel.sink_{inp.slot}"
        )
    if inp.kind == InputKind.black:
        return (
            f"videotestsrc name=vsrc_{inp.id} pattern=black is-live=true "
            f"! {caps} ! queue ! vsel.sink_{inp.slot}"
        )
    if inp.kind == InputKind.test:
        return (
            f"videotestsrc name=vsrc_{inp.id} pattern=smpte is-live=true "
            f"! timeoverlay ! {caps} ! queue ! vsel.sink_{inp.slot}"
        )
    # file / replay: decode any container, convert to uncompressed v210, run as live.
    location = inp.file_path or "_unassigned"
    if location.endswith("_unassigned") or location == "_unassigned":
        return (
            f"videotestsrc name=vsrc_{inp.id} pattern=black is-live=true "
            f"! {caps} ! queue ! vsel.sink_{inp.slot}"
        )
    return (
        f'filesrc name=vsrc_{inp.id} location="{location}" '
        f"! decodebin name=vdec_{inp.id} "
        f"! videoconvert ! videoscale ! videorate "
        f"! {bgra} ! videoconvert ! {caps} "
        f"! identity sync=true ! queue ! vsel.sink_{inp.slot}"
    )


def _audio_source_bin(inp: LogicalInput, settings: Settings, domain: str) -> str:
    caps = _audio(settings)
    if inp.kind == InputKind.mxl_live:
        flow_id = str(inp.audio.flow_id) if inp.audio and inp.audio.flow_id else "UNBOUND"
        return (
            f"mxlsrc name=asrc_{inp.id} audio-flow-id={flow_id} domain={domain} "
            f"! queue max-size-buffers=2 leaky=downstream "
            f"! audioconvert ! audioresample ! {caps} ! queue ! asel.sink_{inp.slot}"
        )
    if inp.kind in {InputKind.black, InputKind.test}:
        wave = "silence" if inp.kind == InputKind.black else "ticks"
        return (
            f"audiotestsrc name=asrc_{inp.id} wave={wave} is-live=true "
            f"! {caps} ! queue ! asel.sink_{inp.slot}"
        )
    location = inp.file_path or "_unassigned"
    if location.endswith("_unassigned") or location == "_unassigned":
        return (
            f"audiotestsrc name=asrc_{inp.id} wave=silence is-live=true "
            f"! {caps} ! queue ! asel.sink_{inp.slot}"
        )
    return (
        f'filesrc name=asrc_{inp.id} location="{location}" '
        f"! decodebin name=adec_{inp.id} "
        f"! audioconvert ! audioresample ! {caps} "
        f"! identity sync=true ! queue ! asel.sink_{inp.slot}"
    )


def build_pipeline_description(
    *,
    settings: Settings,
    inputs: list[LogicalInput],
    overlay_url: str,
    overlay_enabled: bool,
    stinger: dict | None,
    output_video_flow_id: str,
    output_audio_flow_id: str,
    domain: str,
    use_mxl_sink: bool,
    use_cefsrc: bool,
) -> str:
    """
    Build a GStreamer gst-launch-style description:

      sources → input-selector → compositor (HTML5 + TGA stinger) → v210 mxlsink
      sources → input-selector → float32 mxlsink
    """
    if not inputs:
        raise ValueError("at least one logical input is required")

    bgra = _bgra(settings)
    v210 = _v210(settings)
    audio = _audio(settings)
    overlay_alpha = "1.0" if overlay_enabled else "0.0"
    kind = (stinger or {}).get("kind") or "sequence"
    if stinger and kind == "video":
        stinger_bin = (
            f'filesrc name=stinger location="{stinger.get("media_path") or stinger["path"]}" '
            f"! decodebin name=stingerdec ! videoconvert ! videoscale ! {bgra} "
            f"! queue name=stingerq ! comp.sink_2"
        )
    else:
        stinger_location = (
            f"{stinger['path']}/{stinger['pattern']}" if stinger else "/dev/null/frame_%05d.tga"
        )
        stinger_stop = (stinger["frame_count"] - 1) if stinger else 0
        stinger_bin = (
            f"multifilesrc name=stinger location={stinger_location} index=0 "
            f"stop-index={stinger_stop} loop=false caps=image/x-tga "
            f"! decodebin ! videoconvert ! videoscale ! {bgra} "
            f"! queue name=stingerq ! comp.sink_2"
        )

    video_sources = "\n".join(_video_source_bin(i, settings, domain) for i in inputs)
    audio_sources = "\n".join(_audio_source_bin(i, settings, domain) for i in inputs)

    if use_cefsrc:
        overlay_bin = (
            f"cefsrc name=html5 url={overlay_url} "
            f"! {bgra} ! videorate ! {bgra} ! queue name=html5q "
            f"! comp.sink_1"
        )
    else:
        overlay_bin = (
            f"videotestsrc name=html5 pattern=black is-live=true "
            f"! {bgra} ! gdkpixbufoverlay name=html5fallback "
            f"! queue name=html5q ! comp.sink_1"
        )

    if use_mxl_sink:
        video_sink = (
            f"videoconvert ! {v210} ! queue ! "
            f"mxlsink name=vout flow-id={output_video_flow_id} domain={domain}"
        )
        audio_sink = (
            f"queue ! {audio} ! "
            f"mxlsink name=aout flow-id={output_audio_flow_id} domain={domain}"
        )
    else:
        video_sink = f"videoconvert ! {v210} ! queue ! fakesink name=vout sync=true"
        audio_sink = f"queue ! {audio} ! fakesink name=aout sync=true"

    return f"""
input-selector name=vsel sync-streams=true cache-buffers=true
input-selector name=asel sync-streams=true cache-buffers=true
compositor name=comp zero-size-is-unconfigured=false
  sink_0::zorder=0 sink_0::alpha=1.0
  sink_1::zorder=1 sink_1::alpha={overlay_alpha} sink_1::sync=false
  sink_2::zorder=2 sink_2::alpha=0.0 sink_2::sync=false

{video_sources}

vsel. ! videoconvert ! {bgra} ! queue ! comp.sink_0

{overlay_bin}

{stinger_bin}

comp. ! identity name=ptsfix ! {video_sink}

{audio_sources}

asel. ! audioconvert ! audioresample ! {audio_sink}
""".strip()
