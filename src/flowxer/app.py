from __future__ import annotations

import logging
import socket
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from flowxer import __version__
from flowxer.api.auth import ApiTokenMiddleware
from flowxer.api.library_routes import get_mixer as get_library_mixer
from flowxer.api.library_routes import router as library_router
from flowxer.api.metrics import ready_payload, render_prometheus
from flowxer.api.routes import get_mixer, router as api_router
from flowxer.domain.mxl_domain import DomainError
from flowxer.engine.mixer import VisionMixer
from flowxer.listen import bind_listener
from flowxer.settings import Settings, get_settings

log = logging.getLogger(__name__)

# Exit codes of the platform contract: invalid configuration, port taken.
EXIT_CONFIG = 78
EXIT_PORT = 75

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
        "description": (
            "Start/stop the GStreamer vision mixer, arm Preview, take sources to Program, "
            "and run Cut / Fade / Fade to Black / Wipe on a mixer panel (ME)."
        ),
    },
    {
        "name": "overlay",
        "description": "HTML5 graphics keyer (cefsrc when the plugin is in the image, Pillow fallback otherwise).",
    },
    {
        "name": "storage",
        "description": "Legacy clip store plus TGA-sequence and video stingers.",
    },
    {
        "name": "library",
        "description": (
            "Media library: chunked upload, background conversion to intra-frame "
            "mezzanine, RAM/decode-ahead playback metadata, and conversion jobs."
        ),
    },
    {
        "name": "replay",
        "description": "Load a stored clip and stinger in/out of replay.",
    },
    {
        "name": "stinger",
        "description": (
            "Play a TGA sequence or video stinger, cut Program at the chosen frame, "
            "and configure per-slot media. flip_flop swaps Preview and Program like Cut."
        ),
    },
    {
        "name": "tally",
        "description": (
            "TSL UMD Protocol 5.0 tally lamps and under-monitor labels. "
            "Receivers include Bitfocus Companion, Lawo VSM, BFE Commander, "
            "Riedel HI, and any custom TSL 5.0 listener."
        ),
    },
    {
        "name": "gui",
        "description": (
            "Operator console snapshot, workspace layout (including source-tile aspect), "
            "container resources for the status chip, JPEG/WebRTC monitors."
        ),
    },
]


def create_app(
    settings: Settings | None = None,
    mixer: VisionMixer | None = None,
    nmos_listener: socket.socket | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    mixer = mixer or VisionMixer(settings)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        mixer.nmos.boot(nmos_listener)
        yield
        mixer.shutdown()

    app = FastAPI(
        title=settings.title,
        version=settings.version,
        lifespan=lifespan,
        description=(
            "FlowXer is a Dynamic Media Facility (DMF) vision mixer media function. "
            "It is controlled entirely over HTTP, documents itself with OpenAPI, and "
            "exchanges uncompressed media on an MXL domain: **video/v210 (VP210)** plus "
            "**audio/float32** at 48 kHz.\n\n"
            "The media plane is GStreamer. FastAPI is the control plane; "
            "`mxlsrc`/`mxlsink` carry MXL when the plugin is present, with an HTML5 "
            "keyer and a file player with storage access. "
            "Clips and stingers go through a media library (upload → convert → mezzanine). "
            "Stingers may also be TGA sequences or video files; Program cuts at a chosen frame. "
            "The operator GUI on port 9620 is a thin client of this API. "
            "TSL UMD 5.0 carries Program/Preview tally and source labels to "
            "Companion, VSM, BFE, Riedel HI, and other listeners. "
            "When FLOWXER_NMOS_ENABLE is true, an IS-04/IS-05 node on "
            "FLOWXER_NMOS_PORT (default 3252) advertises live-input receivers "
            "and program senders for BCP-007-03 MXL routing."
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
    @app.middleware("http")
    async def persist_changes(request: Request, call_next):
        # Every successful change through the API lands in STATE_DIR/state.json.
        response = await call_next(request)
        if (
            request.method in {"POST", "PUT", "PATCH", "DELETE"}
            and request.url.path.startswith("/api/v1/")
            and response.status_code < 400
        ):
            await run_in_threadpool(mixer.persist)
        return response

    # Auth is inner; CORS is added last so it is outermost (preflight stays unauthenticated).
    app.add_middleware(ApiTokenMiddleware)
    origins = settings.cors_origin_list
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_methods=["*"],
        allow_headers=["*"],
        allow_credentials=origins != ["*"],
    )
    app.state.settings = settings
    app.state.mixer = mixer
    app.dependency_overrides[get_mixer] = lambda: app.state.mixer
    app.dependency_overrides[get_library_mixer] = lambda: app.state.mixer
    app.dependency_overrides[get_settings] = lambda: settings
    app.include_router(api_router, prefix="/api/v1")
    app.include_router(library_router, prefix="/api/v1")

    @app.get("/metrics", include_in_schema=False)
    def metrics_root() -> PlainTextResponse:
        return PlainTextResponse(
            render_prometheus(mixer),
            media_type="text/plain; version=0.0.4; charset=utf-8",
        )

    @app.get("/livez", include_in_schema=False)
    def livez() -> dict:
        return {"status": "live"}

    @app.get("/readyz", include_in_schema=False)
    def readyz():
        code, body = ready_payload(mixer)
        return JSONResponse(status_code=code, content=body)
    if not (settings.api_token or "").strip():
        log.warning(
            "FLOWXER_API_TOKEN is unset; the HTTP control plane is unauthenticated. "
            "Set a token before exposing FlowXer on a public or staging network."
        )

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


class _Server(uvicorn.Server):
    """Remembers the signal that stopped it."""

    signal = 0

    def handle_exit(self, sig, frame) -> None:
        self.signal = sig
        super().handle_exit(sig, frame)


def run() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        settings = get_settings()
    except ValidationError as exc:
        # Without the input values: they may hold the API token.
        problems = "; ".join(
            f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
            for error in exc.errors(include_input=False, include_url=False)
        )
        log.error("invalid configuration: %s", problems)
        sys.exit(EXIT_CONFIG)
    # Bind both ports before anything starts, so a taken port is exit 75 and not a
    # half-started mixer.
    try:
        listener = bind_listener(settings.host, settings.port)
        nmos_listener = (
            bind_listener("0.0.0.0", settings.nmos_port)
            if settings.nmos_enable and settings.nmos_bind
            else None
        )
    except OSError as exc:
        log.error("cannot listen: %s", exc)
        sys.exit(EXIT_PORT)
    try:
        app = create_app(settings, nmos_listener=nmos_listener)
    except DomainError as exc:
        log.error("invalid MXL output domain: %s", exc)
        sys.exit(EXIT_CONFIG)
    log.info("FlowXer %s listening on %s:%s", __version__, settings.host, settings.port)
    # Open requests get half of SHUTDOWN_TIMEOUT_S; the rest is for stopping media,
    # deregistering and removing the output domain.
    server = _Server(
        uvicorn.Config(app, timeout_graceful_shutdown=max(1, settings.shutdown_timeout_s // 2))
    )
    server.run(sockets=[listener])
    # uvicorn raises the signal again after shutting down. As PID 1 in a container
    # the kernel ignores that, so exit with 128 + signal here (SIGTERM: 143).
    sys.exit(128 + server.signal if server.signal else 1)


if __name__ == "__main__":
    run()
