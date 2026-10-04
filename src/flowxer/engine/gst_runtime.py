from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass

from flowxer.engine.pipeline import AUDIO_MAP_PREFIX, PAD_MIX, PAD_PROGRAM, STINGER_ZORDER

log = logging.getLogger(__name__)

# A stinger is linked this far ahead of the current running time, so its decoder
# has started before the compositor needs the first frame.
STINGER_LEAD_NS = 100_000_000
# Frames Program keeps the incoming source on both buses before the B bus is hidden.
MIX_HANDOVER_FRAMES = 3


def first_channels_matrix(in_channels: int, out_channels: int) -> str:
    """audioconvert mix-matrix (one row per output channel) that takes the first
    input channels one to one and drops the rest."""
    rows = []
    for out in range(out_channels):
        row = ", ".join("(float)1.0" if i == out else "(float)0.0" for i in range(in_channels))
        rows.append(f"<{row}>")
    return "<" + ", ".join(rows) + ">"


def map_audio_channels(element, out_channels: int) -> None:
    """Give `element` (an audioconvert) a first-channels mix-matrix for every caps
    its input announces. MXL audio channels are separate signals, and mxlsrc gives
    an N-channel flow the first N speaker positions: without the matrix,
    audioconvert downmixed all of them into Program (16 on the lab test player).
    GStreamer 1.24 cannot drop channels any other way."""
    from gi.repository import Gst

    def probe(_pad, info):
        event = info.get_event()
        if event is not None and event.type == Gst.EventType.CAPS:
            ok, channels = event.parse_caps().get_structure(0).get_int("channels")
            if ok:
                Gst.util_set_object_arg(element, "mix-matrix", first_channels_matrix(channels, out_channels))
        return Gst.PadProbeReturn.OK

    element.get_static_pad("sink").add_probe(Gst.PadProbeType.EVENT_DOWNSTREAM, probe)


@dataclass
class _Mix:
    slot: int
    start: int
    end: int
    switched: bool = False
    handover: int = 0


class GstRuntime:
    """Live GStreamer backend. `on_error` receives every pipeline error message."""

    def __init__(self, on_error: Callable[[str], None] | None = None) -> None:
        self.pipeline = None
        self.loop = None
        self.thread = None
        self._gst = None
        self._glib = None
        self._on_error = on_error
        # Buffers that reached the Program video sink.
        self.program_frames = 0
        self._lock = threading.Lock()
        self._mix: _Mix | None = None
        self._stinger = None

    def start(self, description: str, audio_channels: int = 2) -> None:
        import gi

        gi.require_version("Gst", "1.0")
        gi.require_version("GLib", "2.0")
        from gi.repository import GLib, Gst

        Gst.init(None)
        pipeline = Gst.parse_launch(description)
        bus = pipeline.get_bus()
        bus.add_signal_watch()

        loop = GLib.MainLoop()

        def on_message(_bus, message) -> None:
            if message.type == Gst.MessageType.ERROR:
                err, debug = message.parse_error()
                if self._from_stinger(message.src):
                    # A broken stinger ends that stinger, not the program.
                    log.error("stinger failed: %s (%s)", err, debug)
                    self._end_stinger()
                    return
                log.error("GStreamer error: %s (%s)", err, debug)
                if self._on_error is not None:
                    source = message.src.get_name() if message.src is not None else "pipeline"
                    self._on_error(f"{source}: {err.message}")
            elif message.type == Gst.MessageType.EOS:
                log.info("GStreamer EOS")

        bus.connect("message", on_message)
        self._gst = Gst
        self._glib = GLib
        self.pipeline = pipeline
        self._count_program_frames()
        iterator = pipeline.iterate_recurse()
        while True:
            result, element = iterator.next()
            if result != Gst.IteratorResult.OK:
                break
            if element.get_name().startswith(AUDIO_MAP_PREFIX):
                map_audio_channels(element, audio_channels)
        for name in ("comp", "amix"):
            element = pipeline.get_by_name(name)
            if element is not None:
                element.connect("samples-selected", self._on_samples_selected)
        ret = pipeline.set_state(Gst.State.PLAYING)
        if ret == Gst.StateChangeReturn.FAILURE:
            raise RuntimeError("failed to set GStreamer pipeline to PLAYING")

        self.loop = loop
        self.thread = threading.Thread(target=loop.run, name="gst-mainloop", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        if self.pipeline is not None and self._gst is not None:
            self.pipeline.set_state(self._gst.State.NULL)
        if self.loop is not None and self.loop.is_running():
            self.loop.quit()
        self.pipeline = None
        self.loop = None
        self._mix = None
        self._stinger = None

    def running_time(self) -> int:
        clock = self.pipeline.get_clock() if self.pipeline is not None else None
        if clock is None:
            return 0
        return clock.get_time() - self.pipeline.get_base_time()

    def frame_ns(self) -> int:
        comp = self.pipeline.get_by_name("comp") if self.pipeline is not None else None
        caps = comp.get_static_pad("src").get_current_caps() if comp is not None else None
        if caps is not None:
            ok, num, den = caps.get_structure(0).get_fraction("framerate")
            if ok and num > 0:
                return 1_000_000_000 * den // num
        return 20_000_000

    def _count_program_frames(self) -> None:
        vout = self.pipeline.get_by_name("vout")
        pad = vout.get_static_pad("sink") if vout is not None else None
        if pad is None:
            return

        def count(_pad, _info):
            self.program_frames += 1
            return self._gst.PadProbeReturn.OK

        pad.add_probe(self._gst.PadProbeType.BUFFER, count)

    def set_active_slot(self, selector_name: str, slot: int) -> None:
        if self.pipeline is None:
            return
        selector = self.pipeline.get_by_name(selector_name)
        if selector is None:
            raise RuntimeError(f"missing selector {selector_name}")
        pad = selector.get_static_pad(f"sink_{slot}")
        if pad is None:
            raise RuntimeError(f"{selector_name} has no sink_{slot}")
        selector.set_property("active-pad", pad)

    def set_program(self, slot: int) -> None:
        """Cut: Program shows `slot` now. A mix still running is cut short."""
        with self._lock:
            self._mix = None
        self.set_active_slot("vsel", slot)
        self.set_active_slot("asel", slot)
        self._hide_mix_bus()

    def mix(self, slot: int, duration_ns: int) -> None:
        """Dissolve Program to `slot`: the B bus shows it with rising alpha and audio
        level; at the end the A bus takes it over and the B bus is hidden again."""
        with self._lock:
            previous = self._mix
        if previous is not None:
            # A mix still running ends on its target first.
            self.set_program(previous.slot)
        self.set_active_slot("vselb", slot)
        self.set_active_slot("aselb", slot)
        start = self.running_time() + self.frame_ns()
        with self._lock:
            self._mix = _Mix(slot=slot, start=start, end=start + max(duration_ns, 1))

    def _on_samples_selected(self, aggregator, segment, pts, _dts, _duration, _info) -> None:
        """Once per output frame (compositor) or buffer (audiomixer): set this
        output's mix level, so the dissolve follows the output timeline exactly."""
        program = aggregator.get_static_pad(PAD_PROGRAM)
        incoming = aggregator.get_static_pad(PAD_MIX)
        now = segment.to_running_time(self._gst.Format.TIME, pts)
        # Under the lock: a mix that ended or was cut must not set its level again.
        with self._lock:
            mix = self._mix
            if mix is None:
                return
            level = min(max((now - mix.start) / (mix.end - mix.start), 0.0), 1.0)
            if aggregator.get_name() == "comp":
                incoming.set_property("alpha", level)
            else:
                program.set_property("volume", 1.0 - level)
                incoming.set_property("volume", level)
        if level >= 1.0 and aggregator.get_name() == "comp":
            self._advance_mix(mix)

    def _advance_mix(self, mix: _Mix) -> None:
        # Compositor thread, once per frame after the dissolve: hand Program over to
        # the A bus, then hide the B bus once A shows the same source.
        if not mix.switched:
            mix.switched = True
            self._glib.idle_add(self._switch_program, mix)
            return
        mix.handover += 1
        if mix.handover == MIX_HANDOVER_FRAMES:
            self._glib.idle_add(self._finish_mix, mix)

    def _switch_program(self, mix: _Mix) -> bool:
        if self._mix is mix:
            self.set_active_slot("vsel", mix.slot)
            self.set_active_slot("asel", mix.slot)
        return False

    def _finish_mix(self, mix: _Mix) -> bool:
        with self._lock:
            if self._mix is not mix:
                return False
            self._mix = None
        self._hide_mix_bus()
        return False

    def _hide_mix_bus(self) -> None:
        comp = self.pipeline.get_by_name("comp") if self.pipeline is not None else None
        amix = self.pipeline.get_by_name("amix") if self.pipeline is not None else None
        if comp is not None:
            comp.get_static_pad(PAD_MIX).set_property("alpha", 0.0)
        if amix is not None:
            amix.get_static_pad(PAD_PROGRAM).set_property("volume", 1.0)
            amix.get_static_pad(PAD_MIX).set_property("volume", 0.0)

    def play_stinger(
        self,
        description: str,
        on_frame: Callable[[], None],
        on_end: Callable[[], None],
    ) -> None:
        """Play one stinger: a new bin on a new compositor pad above the keyer.

        `on_frame` runs (main loop) for every stinger frame that reaches the
        compositor, `on_end` once when the stinger is over or failed.
        """
        Gst = self._gst
        self._end_stinger()
        comp = self.pipeline.get_by_name("comp")
        # Ghost only the last queue: decodebin's output pad appears later, and an
        # automatic ghost of the then-unlinked videoconvert sink would take its link.
        stinger = Gst.parse_bin_from_description(description, False)
        stinger.add_pad(Gst.GhostPad.new("src", stinger.get_by_name("stingerq").get_static_pad("src")))
        pad = comp.request_pad_simple("sink_%u")
        pad.set_property("zorder", STINGER_ZORDER)
        src = stinger.get_static_pad("src")
        src.set_offset(self.running_time() + STINGER_LEAD_NS)
        state = {"bin": stinger, "pad": pad, "on_end": on_end}

        def probe(_pad, info):
            if info.type & Gst.PadProbeType.BUFFER:
                self._glib.idle_add(lambda: (on_frame(), False)[1])
                return Gst.PadProbeReturn.OK
            event = info.get_event()
            if event is not None and event.type == Gst.EventType.EOS:
                # The compositor pad must not see EOS: the bin is removed instead.
                self._glib.idle_add(lambda: (self._end_stinger(state), False)[1])
                return Gst.PadProbeReturn.DROP
            return Gst.PadProbeReturn.OK

        src.add_probe(Gst.PadProbeType.BUFFER | Gst.PadProbeType.EVENT_DOWNSTREAM, probe)
        self.pipeline.add(stinger)
        src.link(pad)
        with self._lock:
            self._stinger = state
        stinger.sync_state_with_parent()

    def _from_stinger(self, element) -> bool:
        state = self._stinger
        return state is not None and element is not None and element.has_as_ancestor(state["bin"])

    def _end_stinger(self, state=None) -> None:
        with self._lock:
            current = self._stinger
            if current is None or (state is not None and state is not current):
                return
            self._stinger = None
        # Releasing the compositor pad first flushes it, which wakes a stinger thread
        # blocked on it; then the bin can stop.
        self.pipeline.get_by_name("comp").release_request_pad(current["pad"])
        stinger = current["bin"]
        stinger.set_locked_state(True)
        stinger.set_state(self._gst.State.NULL)
        self.pipeline.remove(stinger)
        current["on_end"]()

    def set_compositor_alpha(self, sink: str, alpha: float) -> None:
        if self.pipeline is None:
            return
        comp = self.pipeline.get_by_name("comp")
        if comp is None:
            return
        pad = comp.get_static_pad(sink)
        if pad is not None:
            pad.set_property("alpha", float(alpha))

    def set_overlay_png(self, path) -> None:
        if self.pipeline is None:
            return
        overlay = self.pipeline.get_by_name("html5fallback")
        if overlay is not None:
            overlay.set_property("location", str(path))

    def retarget_mxl_source(
        self,
        element_name: str,
        flow_id: str | None,
        domain: str | None,
        role: str,
    ) -> bool:
        """Retarget one mxlsrc without restarting the mixer pipeline.

        Properties are mutable_ready: the element is taken to NULL, updated,
        then PLAYING again. Program continues on the other selector sinks.
        """
        if self.pipeline is None or self._gst is None:
            return False
        element = self.pipeline.get_by_name(element_name)
        if element is None:
            return False
        Gst = self._gst
        element.set_state(Gst.State.NULL)
        if role == "video":
            element.set_property("video-flow-id", flow_id or "")
        else:
            element.set_property("audio-flow-id", flow_id or "")
        if domain:
            element.set_property("domain", domain)
        element.set_state(Gst.State.PLAYING)
        return True


def try_start_gst(
    description: str, on_error: Callable[[str], None] | None = None, audio_channels: int = 2
) -> tuple[GstRuntime | None, str]:
    """Start the pipeline. Returns the runtime, or None and the reason it failed."""
    try:
        runtime = GstRuntime(on_error)
        runtime.start(description, audio_channels)
        return runtime, ""
    except Exception as exc:
        log.error("GStreamer pipeline failed to start: %s", exc)
        return None, str(exc)
