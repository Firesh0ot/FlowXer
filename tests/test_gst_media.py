"""The real media pipeline on GStreamer (without the MXL plugins: fakesink outputs).

Skipped where GStreamer and PyGObject are not installed; CI runs these in the
"Pytest (GStreamer)" job with the packages of the mixer image.
"""

from __future__ import annotations

import threading
import time

import pytest

from flowxer.engine.capabilities import gstreamer_available
from flowxer.engine.mixer import VisionMixer
from flowxer.engine.pipeline import PAD_MIX, PAD_PROGRAM, mxl_audio_adapter
from flowxer.api.schemas import MixerStartRequest
from flowxer.settings import Settings

pytestmark = pytest.mark.skipif(not gstreamer_available(), reason="GStreamer with PyGObject is not installed")


def _wait(predicate, what: str, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, f"timed out waiting for {what}"
        time.sleep(0.01)


@pytest.fixture
def live(settings: Settings):
    settings.simulate = False
    settings.gst_mode = "auto"
    settings.width = 320
    settings.height = 180
    mixer = VisionMixer(settings)
    yield mixer
    mixer.stop()


class ProgramTap:
    """The latest Program frame at the video sink, as a count of distinct v210 words:
    a black frame has a handful, colour bars hundreds."""

    def __init__(self, mixer: VisionMixer) -> None:
        from gi.repository import Gst

        self._lock = threading.Lock()
        self._words = 0
        pad = mixer.gst.pipeline.get_by_name("vout").get_static_pad("sink")
        pad.add_probe(Gst.PadProbeType.BUFFER, self._probe)

    def _probe(self, _pad, info):
        from gi.repository import Gst

        buffer = info.get_buffer()
        ok, mapped = buffer.map(Gst.MapFlags.READ)
        if ok:
            data = bytes(mapped.data)
            buffer.unmap(mapped)
            words = {data[i : i + 4] for i in range(0, len(data), 4)}
            with self._lock:
                self._words = len(words)
        return Gst.PadProbeReturn.OK

    @property
    def words(self) -> int:
        with self._lock:
            return self._words


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
    _wait(lambda: 0 < tap.words < 16, "black on Program")
    live.cut()
    _wait(lambda: tap.words > 50, "colour bars on Program")
    assert live.program_input_id == "cam-1"


def test_fade_dissolves_then_hands_program_to_the_a_bus(live: VisionMixer) -> None:
    live.start(MixerStartRequest(program_input_id="black", preview_input_id="cam-1"))
    tap = ProgramTap(live)
    _wait(lambda: 0 < tap.words < 16, "black on Program")
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
    assert tap.words > 50
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


def test_mxl_audio_with_sixteen_channels_reaches_program(settings: Settings) -> None:
    import gi

    gi.require_version("Gst", "1.0")
    from gi.repository import Gst

    Gst.init(None)
    pipeline = Gst.parse_launch(
        "audiotestsrc num-buffers=5 "
        "! audio/x-raw,format=F32LE,layout=interleaved,rate=48000,channels=16,channel-mask=(bitmask)0x0 "
        f"! {mxl_audio_adapter(settings)} "
        f"! audio/x-raw,format=F32LE,layout=interleaved,rate={settings.audio_rate},channels={settings.audio_channels} "
        "! fakesink"
    )
    pipeline.set_state(Gst.State.PLAYING)
    message = pipeline.get_bus().timed_pop_filtered(5 * Gst.SECOND, Gst.MessageType.EOS | Gst.MessageType.ERROR)
    pipeline.set_state(Gst.State.NULL)
    assert message is not None, "no EOS"
    assert message.type == Gst.MessageType.EOS, message.parse_error()
