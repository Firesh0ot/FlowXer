from __future__ import annotations

import logging

log = logging.getLogger(__name__)


class GstRuntime:
    """Optional live GStreamer backend. Failures fall back to the simulator."""

    def __init__(self) -> None:
        self.pipeline = None
        self.loop = None
        self.thread = None
        self._gst = None

    def start(self, description: str) -> None:
        import threading

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
                log.error("GStreamer error: %s (%s)", err, debug)
            elif message.type == Gst.MessageType.EOS:
                log.info("GStreamer EOS")

        bus.connect("message", on_message)
        ret = pipeline.set_state(Gst.State.PLAYING)
        if ret == Gst.StateChangeReturn.FAILURE:
            raise RuntimeError("failed to set GStreamer pipeline to PLAYING")

        self._gst = Gst
        self.pipeline = pipeline
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


def try_start_gst(description: str) -> GstRuntime | None:
    try:
        runtime = GstRuntime()
        runtime.start(description)
        return runtime
    except Exception as exc:
        log.warning("GStreamer backend unavailable, simulating mixer: %s", exc)
        return None
