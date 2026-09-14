from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def gstreamer_available() -> bool:
    try:
        import gi

        gi.require_version("Gst", "1.0")
        from gi.repository import Gst

        Gst.init(None)
        return True
    except Exception:
        return False


def plugin_available(name: str) -> bool:
    try:
        import gi

        gi.require_version("Gst", "1.0")
        from gi.repository import Gst

        Gst.init(None)
        registry = Gst.Registry.get()
        return registry.find_plugin(name) is not None or registry.find_feature(
            name, Gst.ElementFactory.__gtype__  # type: ignore[attr-defined]
        ) is not None
    except Exception:
        return False


def element_available(factory_name: str) -> bool:
    try:
        import gi

        gi.require_version("Gst", "1.0")
        from gi.repository import Gst

        Gst.init(None)
        return Gst.ElementFactory.find(factory_name) is not None
    except Exception:
        return False


def probe_backend() -> dict[str, bool | str]:
    gst = gstreamer_available()
    mxlsrc = element_available("mxlsrc") if gst else False
    mxlsink = element_available("mxlsink") if gst else False
    cefsrc = element_available("cefsrc") if gst else False
    return {
        "gstreamer": gst,
        "mxlsrc": mxlsrc,
        "mxlsink": mxlsink,
        "cefsrc": cefsrc,
        "mxl_plugins": bool(mxlsrc and mxlsink),
    }
