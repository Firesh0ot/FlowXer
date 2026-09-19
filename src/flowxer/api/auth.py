"""Optional Bearer token for the mixer HTTP control plane."""

from __future__ import annotations

import secrets

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

_OPEN_PATHS = frozenset({"/api/v1/health", "/favicon.ico", "/"})
_OPEN_PREFIXES = ("/static/", "/graphics/")


def extract_bearer_token(request: Request) -> str:
    header = request.headers.get("authorization") or ""
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return (request.headers.get("x-flowxer-token") or "").strip()


class ApiTokenMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        settings = getattr(request.app.state, "settings", None)
        expected = (getattr(settings, "api_token", "") or "").strip()
        if not expected:
            return await call_next(request)
        path = request.url.path
        if path in _OPEN_PATHS or any(path.startswith(prefix) for prefix in _OPEN_PREFIXES):
            return await call_next(request)
        provided = extract_bearer_token(request)
        if not provided or not secrets.compare_digest(provided, expected):
            return JSONResponse(
                {"detail": "Not authenticated"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
        return await call_next(request)
