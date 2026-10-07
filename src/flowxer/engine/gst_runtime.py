from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from flowxer.engine.pipeline import (
    AUDIO_MAP_PREFIX,
    MONITOR_PREFIX,
    PAD_MIX,
    PAD_PROGRAM,
    STINGER_ZORDER,
    UNROUTED_FLOW,
)

log = logging.getLogger(__name__)

# A stinger is linked this far ahead of the current running time, so its decoder
# has started before the compositor needs the first frame.
STINGER_LEAD_NS = 100_000_000
# Frames Program keeps the incoming source on both buses before the B bus is hidden.
MIX_HANDOVER_FRAMES = 3
# Program audio buffers may start this much before the end of the last one (timestamp rounding).
AUDIO_OVERLAP_TOLERANCE_NS = 100_000
# Dropped Program buffers logged per start; the counters keep counting.
DROP_LOG_LIMIT = 5
# A source restart (IS-05 retarget, recovery) waits this long for the source's state changes.
# Stopping an mxlsrc can take up to 5 s (its grain read times out after 5 s).
RESTART_TIMEOUT_S = 10.0
# Program stop waits this long for the pipeline to reach NULL.
STOP_TIMEOUT_S = 15.0
# A failed MXL source is started again after these pauses (s); the last one repeats. A source
# that ran for RECOVERY_RESET_S after it was started again starts over at the first pause (an
# invalid grain now and then costs a 1 s freeze; a source that fails at once backs off).
RECOVERY_BACKOFF_S = (1.0, 2.0, 5.0, 10.0, 30.0)
RECOVERY_RESET_S = 3.0
MXL_SOURCE_PREFIXES = ("vsrc_", "asrc_")

# Pipelines whose stop did not finish: kept referenced, a pipeline must not be finalized while
# its threads still run.
_abandoned: list = []


class SourceRestartTimeout(RuntimeError):
    """A source did not get through its restart in time; its thread may still be blocked."""


def run_bounded(work: Callable[[], object], timeout_s: float, what: str, watchdog=None) -> bool:
    """Run `work` in its own thread and wait at most `timeout_s`. True when it finished.

    A GStreamer state change can block for good (a streaming thread waiting in a serialized
    query that nobody answers). The caller then goes on and releases its locks; the thread stays
    registered with the control-plane watchdog until it ends, so /livez reports it."""
    done = threading.Event()
    errors: list[BaseException] = []
    token = watchdog.begin(what) if watchdog is not None else None

    def run() -> None:
        try:
            work()
        except BaseException as exc:  # noqa: BLE001 (handed to the caller)
            errors.append(exc)
        finally:
            done.set()
            if token is not None:
                watchdog.end(token)

    threading.Thread(target=run, name=f"bounded: {what}", daemon=True).start()
    if not done.wait(timeout_s):
        log.error("%s did not finish within %.0f s", what, timeout_s)
        return False
    if errors:
        raise errors[0]
    return True


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
        # Program buffers dropped because they went back in time, per essence.
        self.program_dropped = {"video": 0, "audio": 0}
        # ControlPlaneWatchdog for the bounded state changes (set by the mixer).
        self.watchdog = None
        # Called with (source element name, reason) when a failed MXL source is started again.
        self.on_source_restart: Callable[[str, str], None] | None = None
        # Per MXL source: (failures in a row, monotonic time of the last failure).
        self._failures: dict[str, tuple[int, float]] = {}
        self._recovering: set[str] = set()
        self._lock = threading.Lock()
        self._mix: _Mix | None = None
        self._stinger = None
        # Newest GUI monitor picture per appsink name: (width, height, RGB bytes).
        self.monitors: dict[str, tuple[int, int, bytes]] = {}

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
                self._recover_source(message.src, err.message)
            elif message.type == Gst.MessageType.EOS:
                log.info("GStreamer EOS")

        bus.connect("message", on_message)
        self._gst = Gst
        self._glib = GLib
        self.pipeline = pipeline
        self._guard_program_output()
        iterator = pipeline.iterate_recurse()
        while True:
            result, element = iterator.next()
            if result != Gst.IteratorResult.OK:
                break
            if element.get_name().startswith(AUDIO_MAP_PREFIX):
                map_audio_channels(element, audio_channels)
            elif element.get_name().startswith(MONITOR_PREFIX):
                element.set_property("emit-signals", True)
                element.connect("new-sample", self._on_monitor_sample)
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

    def stop(self) -> bool:
        """Take the pipeline to NULL. False when that did not finish within STOP_TIMEOUT_S: the
        pipeline is then left behind (its threads are blocked) and the watchdog reports it."""
        stopped = True
        if self.pipeline is not None and self._gst is not None:
            pipeline, null = self.pipeline, self._gst.State.NULL
            stopped = run_bounded(lambda: pipeline.set_state(null), STOP_TIMEOUT_S, "Program stop", self.watchdog)
            if not stopped:
                _abandoned.append(pipeline)
        if self.loop is not None and self.loop.is_running():
            self.loop.quit()
        self.pipeline = None
        self.loop = None
        self._mix = None
        self._stinger = None
        return stopped

    def _on_monitor_sample(self, sink) -> object:
        sample = sink.emit("pull-sample")
        if sample is not None:
            structure = sample.get_caps().get_structure(0)
            buffer = sample.get_buffer()
            ok, mapped = buffer.map(self._gst.MapFlags.READ)
            if ok:
                picture = bytes(mapped.data)
                buffer.unmap(mapped)
                self.monitors[sink.get_name()] = (
                    structure.get_int("width")[1],
                    structure.get_int("height")[1],
                    picture,
                )
        return self._gst.FlowReturn.OK

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

    def _guard_program_output(self) -> None:
        """Keep Program going forward in time, and count its frames.

        mxlsink writes each buffer at the MXL index of its timestamp. In the first frames after a
        start the compositor and the audiomixer can start their output over at 0 (seen at the
        sinks: video [0.04, 0.06) then [0, 0.08); audio [0, 0.01) then [0, 0.02)). mxlsink cannot
        write behind what it wrote, returns an error without a message, the error runs upstream and
        stops that essence for good: Program audio stayed silent, or Program video stopped after a
        few frames. Such a buffer is dropped before it reaches the sink: a video frame whose
        timestamp is not after the last one, audio that starts before the last buffer ended.
        """
        Gst = self._gst
        for name, essence in (("vout", "video"), ("aout", "audio")):
            sink = self.pipeline.get_by_name(name)
            pad = sink.get_static_pad("sink") if sink is not None else None
            if pad is not None:
                pad.add_probe(Gst.PadProbeType.BUFFER, self._program_timeline_probe(essence))

    def _program_timeline_probe(self, essence: str):
        Gst = self._gst
        none = Gst.CLOCK_TIME_NONE
        last = {"pts": none, "end": none}

        def probe(_pad, info):
            buffer = info.get_buffer()
            pts, duration = buffer.pts, buffer.duration
            if pts != none and last["pts"] != none:
                if essence == "video":
                    back = pts <= last["pts"]
                else:
                    back = last["end"] != none and pts + AUDIO_OVERLAP_TOLERANCE_NS < last["end"]
                if back:
                    self.program_dropped[essence] += 1
                    if self.program_dropped[essence] <= DROP_LOG_LIMIT:
                        log.warning(
                            "Program %s buffer at %.3f s dropped: it is behind the last one (%.3f-%.3f s)",
                            essence,
                            pts / 1e9,
                            last["pts"] / 1e9,
                            (last["end"] if last["end"] != none else last["pts"]) / 1e9,
                        )
                    return Gst.PadProbeReturn.DROP
            if pts != none:
                last["pts"] = pts
                last["end"] = pts + duration if duration != none else none
            if essence == "video":
                self.program_frames += 1
            return Gst.PadProbeReturn.OK

        return probe

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

        Returns False without touching an element that is not an mxlsrc: an
        input whose kind changed on air keeps its earlier source until the
        next start. Taking such a source to NULL and failing on the missing
        property left it stopped, the input-selectors then held the other
        inputs' streaming threads, and the next retarget blocked the API.
        """
        if self.pipeline is None or self._gst is None:
            return False
        element = self.pipeline.get_by_name(element_name)
        flow_property = "video-flow-id" if role == "video" else "audio-flow-id"
        if element is None or element.find_property(flow_property) is None:
            return False

        def apply() -> None:
            # Not routed: a flow id that never exists, so the source waits like for a missing flow
            # (an empty id is not a UUID and made mxlsrc fail).
            element.set_property(flow_property, flow_id or UNROUTED_FLOW)
            if domain:
                element.set_property("domain", domain)

        if not run_bounded(
            lambda: self._restart_source(element, apply), RESTART_TIMEOUT_S, f"retarget of {element_name}", self.watchdog
        ):
            raise SourceRestartTimeout(f"{element_name} did not restart within {RESTART_TIMEOUT_S:.0f} s")
        self._failures.pop(element_name, None)
        return True

    def _restart_source(self, element, apply: Callable[[], None] | None = None) -> None:
        """Take a source to NULL and back to PLAYING, changing its properties in between.

        Its branch is flushed first. A source that starts again sends a serialized allocation
        query; when the queue after it cannot hand the query on (the queue's thread waits
        downstream, e.g. in an input-selector whose active input stopped), the source's thread
        waits with its stream lock held, and every later state change of the source blocked for
        good (an IS-05 route hung the control plane on 10.17.40). FLUSH_START answers the
        waiting query and wakes the blocked threads; FLUSH_STOP makes the branch usable again."""
        Gst = self._gst
        src = element.get_static_pad("src")
        peer = src.get_peer() if src is not None else None
        if src is not None:
            src.push_event(Gst.Event.new_flush_start())
        element.set_state(Gst.State.NULL)
        if peer is not None:
            # reset_time false: a running-time reset makes the sinks reset the pipeline's time,
            # which held Program for seconds.
            peer.send_event(Gst.Event.new_flush_stop(False))
        if apply is not None:
            apply()
        element.set_state(Gst.State.PLAYING)

    def _recover_source(self, element, reason: str) -> None:
        """A failed MXL source stays stopped (its input froze on the last picture): start it again
        after a pause that grows while it keeps failing. mxlsrc stops for good on a grain marked
        invalid (an ST 2110 gateway writes incomplete frames that way)."""
        name = element.get_name() if element is not None else ""
        if (
            self.pipeline is None
            or not name.startswith(MXL_SOURCE_PREFIXES)
            or element.find_property("domain") is None
            or name in self._recovering
        ):
            return
        failures, started = self._failures.get(name, (0, 0.0))
        if time.monotonic() - started > RECOVERY_RESET_S:
            failures = 0
        delay = RECOVERY_BACKOFF_S[min(failures, len(RECOVERY_BACKOFF_S) - 1)]
        self._failures[name] = (failures + 1, started)
        self._recovering.add(name)
        log.warning("MXL source %s failed (%s); starting it again in %.0f s", name, reason, delay)
        pipeline = self.pipeline

        def restart() -> None:
            try:
                if self.pipeline is pipeline:
                    run_bounded(lambda: self._restart_source(element), RESTART_TIMEOUT_S, f"restart of {name}", self.watchdog)
                    self._failures[name] = (self._failures.get(name, (1, 0.0))[0], time.monotonic())
                    if self.on_source_restart is not None:
                        self.on_source_restart(name, reason)
            except Exception:
                log.exception("starting %s again failed", name)
            finally:
                self._recovering.discard(name)

        def later() -> bool:
            threading.Thread(target=restart, name=f"recover {name}", daemon=True).start()
            return False

        self._glib.timeout_add(int(delay * 1000), later)


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
