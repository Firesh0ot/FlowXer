"""The real media pipeline on GStreamer (without the MXL plugins: fakesink outputs).

Skipped where GStreamer and PyGObject are not installed; CI runs these in the
"Pytest (GStreamer)" job with the packages of the mixer image.
"""

from __future__ import annotations

import struct
import threading
import time
from uuid import uuid4

import pytest

from flowxer.engine.capabilities import gstreamer_available
from flowxer.engine.mixer import VisionMixer
from flowxer.engine.gst_runtime import first_channels_matrix, map_audio_channels
from flowxer.engine.pipeline import PAD_MIX, PAD_PROGRAM
from flowxer.api.schemas import InputKind, LogicalInputUpdate, MixerStartRequest
from flowxer.settings import Settings

pytestmark = pytest.mark.skipif(not gstreamer_available(), reason="GStreamer with PyGObject is not installed")


def _wait(predicate, what, timeout: float = 5.0) -> None:
    """`what` is a text or a callable giving one (evaluated on timeout)."""
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what() if callable(what) else what}")
        time.sleep(0.01)


@pytest.fixture
def live(settings: Settings):
    settings.simulate = False
    settings.gst_mode = "auto"
    settings.width = 320
    settings.height = 180
    mixer = VisionMixer(settings)
    # Keyer off: Program is only the selected source.
    mixer.update_keyer("dsk-1", enabled=False)
    yield mixer
    mixer.stop()


class ProgramTap:
    """Mean luma (10-bit) of the latest Program frame at the video sink: black is
    64, colour bars are far brighter."""

    def __init__(self, mixer: VisionMixer) -> None:
        from gi.repository import Gst

        self._lock = threading.Lock()
        self._luma = -1.0
        pad = mixer.gst.pipeline.get_by_name("vout").get_static_pad("sink")
        pad.add_probe(Gst.PadProbeType.BUFFER, self._probe)

    def _probe(self, _pad, info):
        from array import array

        from gi.repository import Gst

        buffer = info.get_buffer()
        ok, mapped = buffer.map(Gst.MapFlags.READ)
        if ok:
            words = array("I", bytes(mapped.data))
            buffer.unmap(mapped)
            # v210 words: Cb Y0 Cr | Y1 Cb Y2 | Cr Y3 Cb | Y4 Cr Y5 (10 bits each).
            luma = [w >> 10 & 0x3FF for w in words[0::4]] + [w >> 10 & 0x3FF for w in words[2::4]]
            luma += [w & 0x3FF for w in words[1::2]] + [w >> 20 & 0x3FF for w in words[1::2]]
            with self._lock:
                self._luma = sum(luma) / len(luma)
        return Gst.PadProbeReturn.OK

    @property
    def luma(self) -> float:
        with self._lock:
            return self._luma


def test_program_runs_at_the_mixer_rate(live: VisionMixer) -> None:
    status = live.start(MixerStartRequest(program_input_id="cam-1"))
    assert status.backend == "gstreamer", status.error
    _wait(lambda: live.frames_rendered >= 10, "the first Program frames")
    first = live.frames_rendered
    time.sleep(1.0)
    rate = live.frames_rendered - first
    assert 40 <= rate <= 60, rate


def test_cut_changes_the_program_picture(live: VisionMixer) -> None:
    live.start(MixerStartRequest(program_input_id="black", preview_input_id="cam-1"))
    tap = ProgramTap(live)
    _wait(lambda: 0 <= tap.luma < 100, lambda: f"black on Program (mean luma {tap.luma:.0f})")
    live.cut()
    _wait(lambda: tap.luma > 200, lambda: f"colour bars on Program (mean luma {tap.luma:.0f})")
    assert live.program_input_id == "cam-1"


def test_fade_dissolves_then_hands_program_to_the_a_bus(live: VisionMixer) -> None:
    live.start(MixerStartRequest(program_input_id="black", preview_input_id="cam-1"))
    tap = ProgramTap(live)
    _wait(lambda: 0 <= tap.luma < 100, lambda: f"black on Program (mean luma {tap.luma:.0f})")
    comp = live.gst.pipeline.get_by_name("comp")
    amix = live.gst.pipeline.get_by_name("amix")
    incoming = comp.get_static_pad(PAD_MIX)
    live.fade(duration_ms=600)
    levels = []
    deadline = time.monotonic() + 0.6
    while time.monotonic() < deadline:
        levels.append(incoming.get_property("alpha"))
        time.sleep(0.02)
    # A dissolve, not a cut: the B bus passes through intermediate levels.
    assert any(0.1 < level < 0.9 for level in levels), levels
    # Then the A bus shows the new source and the B bus is hidden again.
    _wait(lambda: incoming.get_property("alpha") == 0.0, "the B bus hidden after the mix")
    vsel = live.gst.pipeline.get_by_name("vsel")
    assert vsel.get_property("active-pad").get_name() == f"sink_{live.get_input('cam-1').slot}"
    assert amix.get_static_pad(PAD_PROGRAM).get_property("volume") == 1.0
    assert amix.get_static_pad(PAD_MIX).get_property("volume") == 0.0
    assert tap.luma > 200
    assert live.program_input_id == "cam-1"


def test_a_cut_during_a_fade_wins(live: VisionMixer) -> None:
    live.start(MixerStartRequest(program_input_id="black", preview_input_id="cam-1"))
    incoming = live.gst.pipeline.get_by_name("comp").get_static_pad(PAD_MIX)
    live.fade(duration_ms=2000)
    _wait(lambda: incoming.get_property("alpha") > 0.05, "the mix has started")
    live.take("cam-2")
    time.sleep(0.2)
    assert incoming.get_property("alpha") == 0.0
    vsel = live.gst.pipeline.get_by_name("vsel")
    assert vsel.get_property("active-pad").get_name() == f"sink_{live.get_input('cam-2').slot}"


def test_a_stinger_plays_from_its_own_frames_every_time(live: VisionMixer) -> None:
    live.start(MixerStartRequest(program_input_id="cam-1", preview_input_id="cam-2"))
    comp = live.gst.pipeline.get_by_name("comp")
    pads_before = len(comp.sinkpads)
    frames: list[int] = []
    original = live._stinger_frame

    def counted(player):
        frames.append(player.frame)
        original(player)

    live._stinger_frame = counted
    for target in ("cam-2", "cam-1"):
        frames.clear()
        live.play_stinger("replay-wipe", target, direction="to_live")
        _wait(lambda: live.stinger_player is None, f"the stinger to {target} to finish")
        # Driven by the frames that reached the compositor, not by a timer or EOS.
        assert len(frames) >= live.settings.stinger_frame_count - 1, frames
        assert live.program_input_id == target
        _wait(lambda: len(comp.sinkpads) == pads_before, "the stinger pad released")


def _mean(image) -> float:
    from PIL import ImageStat

    return sum(ImageStat.Stat(image).mean) / 3


def test_monitors_show_the_pictures_of_the_pipeline(live: VisionMixer) -> None:
    from flowxer.engine.preview import render_monitor

    live.start(MixerStartRequest(program_input_id="cam-1", preview_input_id="black"))
    names = {"mon_cam-1", "mon_black", "mon__program"}
    _wait(lambda: names <= set(live.gst.monitors), lambda: f"monitor pictures ({sorted(live.gst.monitors)})")
    # Live pictures: the test source's time overlay changes them, a card would not.
    first = render_monitor(live, "source:cam-1").tobytes()
    time.sleep(0.5)
    assert render_monitor(live, "source:cam-1").tobytes() != first
    assert _mean(render_monitor(live, "source:black")) < 10
    assert _mean(render_monitor(live, "panel:me-1:pgm")) > 50
    # The Program monitor shows the mixed output: it follows a cut.
    live.take("black")
    _wait(lambda: _mean(render_monitor(live, "panel:me-1:pgm")) < 10, "black on the Program monitor")


def _first_frame(caps: str, samples: bytes, out_channels: int) -> tuple[float, ...]:
    """Push one buffer through the MXL audio channel map and return the first output frame."""
    import gi

    gi.require_version("Gst", "1.0")
    from gi.repository import Gst

    Gst.init(None)
    pipeline = Gst.parse_launch(
        f'appsrc name=src format=time caps="{caps}" ! audioconvert name=amap ! audioconvert '
        f"! audio/x-raw,format=F32LE,layout=interleaved,rate=48000,channels={out_channels} "
        "! appsink name=out sync=false"
    )
    map_audio_channels(pipeline.get_by_name("amap"), out_channels)
    pipeline.set_state(Gst.State.PLAYING)
    buffer = Gst.Buffer.new_wrapped(samples)
    buffer.pts = 0
    pipeline.get_by_name("src").emit("push-buffer", buffer)
    pipeline.get_by_name("src").emit("end-of-stream")
    sample = pipeline.get_by_name("out").emit("try-pull-sample", 5 * Gst.SECOND)
    error = pipeline.get_bus().pop_filtered(Gst.MessageType.ERROR)
    pipeline.set_state(Gst.State.NULL)
    assert sample is not None, error.parse_error() if error else "no output"
    out = sample.get_buffer()
    ok, mapped = out.map(Gst.MapFlags.READ)
    data = bytes(mapped.data)
    out.unmap(mapped)
    return struct.unpack(f"<{out_channels}f", data[: 4 * out_channels])


def test_routing_an_input_whose_kind_changed_on_air_does_not_block(live: VisionMixer) -> None:
    # 8.15.31: the inputs still had their test sources in the running pipeline; routing them took
    # the source to NULL, failed on the missing flow-id property and left it stopped, and the next
    # route blocked (and with it the REST, NMOS and GUI API) while Program ran on.
    from gi.repository import Gst

    live.start(MixerStartRequest(program_input_id="cam-1", preview_input_id="cam-2"))
    _wait(lambda: live.frames_rendered >= 10, "the first Program frames")
    for input_id in ("cam-1", "cam-2"):
        live.update_input(input_id, LogicalInputUpdate(kind=InputKind.mxl_live, group_hint=f"player-{input_id}"))
    routed = threading.Event()

    def route() -> None:
        for input_id in ("cam-1", "cam-2"):
            for role in ("video", "audio"):
                live.apply_nmos_receiver(input_id, role, domain_id=None, flow_id=str(uuid4()), enabled=True)
        routed.set()

    threading.Thread(target=route, daemon=True).start()
    assert routed.wait(5), "routing the inputs blocked"
    for name in ("vsrc_cam-1", "asrc_cam-1", "vsrc_cam-2", "asrc_cam-2"):
        _, state, _ = live.gst.pipeline.get_by_name(name).get_state(0)
        assert state == Gst.State.PLAYING, f"{name} is {state.value_nick}"
    first = live.frames_rendered
    _wait(lambda: live.frames_rendered >= first + 10, "Program frames after the routes")


def test_first_channels_matrix() -> None:
    assert first_channels_matrix(3, 2) == (
        "<<(float)1.0, (float)0.0, (float)0.0>, <(float)0.0, (float)1.0, (float)0.0>>"
    )


@pytest.mark.parametrize("mask", ["0xffff", "0x0"])
def test_mxl_audio_takes_the_first_channels_without_a_downmix(mask: str) -> None:
    # Channel 1 = 0.5, channel 2 = -0.5, channels 3-16 = 0.25; mxlsrc's caps (0xffff)
    # and unpositioned ones.
    frame = struct.pack("<16f", 0.5, -0.5, *([0.25] * 14))
    caps = f"audio/x-raw,format=F32LE,layout=interleaved,rate=48000,channels=16,channel-mask=(bitmask){mask}"
    assert _first_frame(caps, frame * 480, 2) == pytest.approx((0.5, -0.5))


def test_mxl_stereo_audio_passes_unchanged() -> None:
    frame = struct.pack("<2f", 0.5, -0.5)
    caps = "audio/x-raw,format=F32LE,layout=interleaved,rate=48000,channels=2,channel-mask=(bitmask)0x3"
    assert _first_frame(caps, frame * 480, 2) == pytest.approx((0.5, -0.5))


def _push(source, pts_ms: float, duration_ms: float, size: int) -> None:
    from gi.repository import Gst

    buffer = Gst.Buffer.new_wrapped(bytes(size))
    buffer.pts = int(pts_ms * 1_000_000)
    buffer.duration = int(duration_ms * 1_000_000)
    source.emit("push-buffer", buffer)


def test_program_buffers_that_go_back_in_time_are_dropped() -> None:
    # 10.17.40 on the lab and the platform: right after a start the compositor sent [0.04, 0.06) and
    # then [0, 0.08), the audiomixer [0, 0.01) and then [0, 0.02). mxlsink cannot write behind
    # what it wrote, failed silently, and Program video or audio stopped for good.
    from flowxer.engine.gst_runtime import GstRuntime

    runtime = GstRuntime()
    runtime.start(
        'appsrc name=v format=time caps="video/x-raw,format=v210,width=48,height=2,framerate=50/1" '
        "! fakesink name=vout sync=false "
        'appsrc name=a format=time caps="audio/x-raw,format=F32LE,layout=interleaved,rate=48000,channels=2" '
        "! fakesink name=aout sync=false"
    )
    try:
        video, audio = runtime.pipeline.get_by_name("v"), runtime.pipeline.get_by_name("a")
        for pts, duration in ((0, 20), (20, 20), (40, 20), (0, 80), (80, 20), (100, 20)):
            _push(video, pts, duration, 256)
        for pts, duration in ((0, 10), (0, 20), (20, 10), (30, 10)):
            _push(audio, pts, duration, 3840)
        _wait(lambda: runtime.program_frames == 5, lambda: f"5 Program frames ({runtime.program_frames})")
        _wait(lambda: runtime.program_dropped == {"video": 1, "audio": 1}, lambda: str(runtime.program_dropped))
    finally:
        runtime.stop()


def test_a_source_waiting_in_an_allocation_query_can_be_started_again() -> None:
    # 10.17.40: a retargeted mxlsrc sent its allocation query into its queue, whose thread waited
    # downstream (an input-selector whose active input had stopped). The source's thread then held
    # its stream lock, and the next route took the source to NULL and blocked for good under the
    # NMOS lock. A blocking probe stands in for the waiting input-selector here.
    from gi.repository import Gst

    from flowxer.engine.gst_runtime import GstRuntime, run_bounded

    runtime = GstRuntime()
    runtime.start(
        "videotestsrc name=src is-live=true ! video/x-raw,width=64,height=36,framerate=50/1 "
        "! queue name=q max-size-buffers=2 leaky=downstream ! identity name=gate ! fakesink sync=false"
    )
    try:
        source = runtime.pipeline.get_by_name("src")
        blocked = threading.Event()

        def hold(_pad, _info):
            blocked.set()
            return Gst.PadProbeReturn.OK

        runtime.pipeline.get_by_name("gate").get_static_pad("sink").add_probe(
            Gst.PadProbeType.BLOCK | Gst.PadProbeType.BUFFER, hold
        )
        assert blocked.wait(5), "the branch did not block"
        # The first restart leaves the source in its allocation query, the second one has to
        # take it down again. Both must come back.
        for _ in range(2):
            assert run_bounded(lambda: runtime._restart_source(source), 5, "restart")
            time.sleep(0.3)
    finally:
        runtime.stop()
