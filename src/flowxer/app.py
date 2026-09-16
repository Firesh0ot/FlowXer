from __future__ import annotations

import logging
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from flowxer import __version__
from flowxer.api.routes import get_mixer, router as api_router
from flowxer.engine.mixer import VisionMixer
from flowxer.settings import Settings, get_settings

log = logging.getLogger(__name__)

OPENAPI_TAGS = [
    {"name": "system", "description": "Health, raster and backend capability probes."},
    {
        "name": "mxl",
        "description": (
            "MXL domain inspection. Flows are uncompressed video/v210 (VP210) "
            "and audio/float32 essences."
        ),
    },
    {
        "name": "inputs",
        "description": (
            "Logical inputs virtually bundle a video essence and an audio essence "
            "into one mixer source."
        ),
    },
    {
        "name": "mixer",
        "description": "Start/stop the GStreamer vision mixer and take sources to program/preview.",
    },
    {
        "name": "overlay",
        "description": "HTML5 graphics keyer (cefsrc in production, Pillow fallback in the container).",
    },
    {
        "name": "storage",
        "description": "Clip store and TGA-sequence stingers used for live ↔ replay transitions.",
    },
    {
        "name": "replay",
        "description": "Load a stored clip and stinger in/out of replay.",
    },
    {
        "name": "gui",
        "description": "Operator console layout, container resources, JPEG/WebRTC monitors.",
    },
]


def create_app(settings: Settings | None = None, mixer: VisionMixer | None = None) -> FastAPI:
    settings = settings or get_settings()
    mixer = mixer or VisionMixer(settings)

    app = FastAPI(
        title=settings.title,
        version=settings.version,
        description=(
            "FlowXer is a Dynamic Media Facility (DMF) vision mixer media function. "
            "It is controlled entirely over HTTP, documents itself with OpenAPI, and "
            "exchanges uncompressed media on an MXL domain: **video/v210 (VP210)** plus "
            "**audio/float32** at 48 kHz.\n\n"
            "The media plane is GStreamer. FastAPI is the control plane; "
            "`mxlsrc`/`mxlsink` carry MXL when the plugin is present, with an HTML5 "
            "keyer and a file player with storage access. "
            "A TGA-sequence stinger covers the cut when going to replay and when "
            "returning to live."
        ),
        openapi_tags=OPENAPI_TAGS,
        contact={"name": "FlowXer", "url": "https://github.com/Firesh0ot/FlowXer"},
        license_info={
            "name": "Apache-2.0",
            "url": "https://www.apache.org/licenses/LICENSE-2.0",
        },
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.state.settings = settings
    app.state.mixer = mixer
    app.dependency_overrides[get_mixer] = lambda: app.state.mixer
    app.include_router(api_router, prefix="/api/v1")

    static_dir = Path(__file__).parent / "static"
    graphics_dir = Path(__file__).parent / "graphics"
    if static_dir.is_dir():
        app.mount("/static", StaticFiles(directory=static_dir), name="static")
    if graphics_dir.is_dir():
        app.mount("/graphics", StaticFiles(directory=graphics_dir, html=True), name="graphics")

    @app.get("/", include_in_schema=False)
    def index() -> HTMLResponse:
        index_path = static_dir / "index.html"
        return HTMLResponse(index_path.read_text(encoding="utf-8"))

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon() -> FileResponse:
        icon = static_dir / "favicon.svg"
        return FileResponse(icon, media_type="image/svg+xml")

    return app


def run() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings = get_settings()
    log.info("FlowXer %s listening on %s:%s", __version__, settings.host, settings.port)
    uvicorn.run(
        "flowxer.app:create_app",
        factory=True,
        host=settings.host,
        port=settings.port,
        reload=False,
    )


if __name__ == "__main__":
    run()
