"""The real media pipeline on GStreamer (without the MXL plugins: fakesink outputs).

Skipped where GStreamer and PyGObject are not installed; CI runs these in the
"Pytest (GStreamer)" job with the packages of the mixer image.
"""

from __future__ import annotations

import struct
import threading
import time

import pytest

from flowxer.engine.capabilities import gstreamer_available
from flowxer.engine.mixer import VisionMixer
from flowxer.engine.gst_runtime import first_channels_matrix, map_audio_channels
from flowxer.engine.pipeline import PAD_MIX, PAD_PROGRAM
from flowxer.api.schemas import MixerStartRequest
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
