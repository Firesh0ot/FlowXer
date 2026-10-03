from __future__ import annotations

import logging
from collections.abc import Callable

log = logging.getLogger(__name__)


class GstRuntime:
    """Live GStreamer backend. `on_error` receives every pipeline error message."""

    def __init__(self, on_error: Callable[[str], None] | None = None) -> None:
        self.pipeline = None
        self.loop = None
        self.thread = None
        self._gst = None
        self._on_error = on_error

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
                if self._on_error is not None:
                    source = message.src.get_name() if message.src is not None else "pipeline"
                    self._on_error(f"{source}: {err.message}")
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
    description: str, on_error: Callable[[str], None] | None = None
) -> tuple[GstRuntime | None, str]:
    """Start the pipeline. Returns the runtime, or None and the reason it failed."""
    try:
        runtime = GstRuntime(on_error)
        runtime.start(description)
        return runtime, ""
    except Exception as exc:
        log.error("GStreamer pipeline failed to start: %s", exc)
        return None, str(exc)
