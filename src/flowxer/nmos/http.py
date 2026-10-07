from __future__ import annotations

import re

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from flowxer.nmos.service import TRANSPORT_MXL, NmosActivationError, NmosNode


def _paginate(items: list, request: Request) -> list:
    skip = int(request.query_params.get("skip") or 0)
    limit = request.query_params.get("limit")
    sliced = items[skip:]
    if limit is not None:
        sliced = sliced[: int(limit)]
    return sliced


class _CollapseSlashes:
    """Route `//` like `/`: the device's control href ends in `/`, and controllers that
    append `/single/...` to it send `.../v1.2//single/...`. nmos-cpp nodes accept that."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http" and "//" in scope.get("path", ""):
            path = re.sub(r"/{2,}", "/", scope["path"])
            scope = dict(scope, path=path, raw_path=path.encode())
        await self.app(scope, receive, send)


def create_nmos_app(node: NmosNode) -> FastAPI:
    app = FastAPI(title="FlowXer NMOS Node", docs_url=None, redoc_url=None)
    app.add_middleware(_CollapseSlashes)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def nmos_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("Cache-Control", "no-cache")
        return response

    def _error_body(status_code: int, detail: object) -> dict:
        if isinstance(detail, str) and detail:
            message = detail
        else:
            message = "error"
        return {"code": status_code, "error": message, "debug": None}

    @app.exception_handler(HTTPException)
    @app.exception_handler(StarletteHTTPException)
    async def nmos_http_error(_request: Request, exc: StarletteHTTPException):
        return JSONResponse(
            status_code=exc.status_code,
            content=_error_body(exc.status_code, exc.detail),
        )

    @app.get("/")
    def root() -> list[str]:
        return ["x-nmos/"]

    @app.get("/x-nmos")
    @app.get("/x-nmos/")
    def x_nmos() -> list[str]:
        return ["node/", "connection/"]

    @app.get("/x-nmos/node")
    @app.get("/x-nmos/node/")
    def node_versions() -> list[str]:
        return ["v1.3/"]

    @app.get("/x-nmos/node/v1.3")
    @app.get("/x-nmos/node/v1.3/")
    def node_v13() -> list[str]:
        return [
            "self/",
            "devices/",
            "sources/",
            "flows/",
            "senders/",
            "receivers/",
        ]

    @app.get("/x-nmos/node/v1.3/self")
    @app.get("/x-nmos/node/v1.3/self/")
    def node_self() -> dict:
        return node.self_resource()

    def _collection(name: str, request: Request):
        if name == "devices":
            items = [node.device_resource()]
        elif name == "receivers":
            items = node.receivers()
        elif name == "senders":
            items = node.senders()
        elif name == "sources":
            items = node.sources()
        elif name == "flows":
            items = node.flows()
        else:
            raise HTTPException(status_code=404, detail="Not Found")
        return _paginate(items, request)

    @app.get("/x-nmos/node/v1.3/{collection}")
    @app.get("/x-nmos/node/v1.3/{collection}/")
    def list_collection(collection: str, request: Request):
        return _collection(collection, request)

    @app.get("/x-nmos/node/v1.3/{collection}/{resource_id}")
    @app.get("/x-nmos/node/v1.3/{collection}/{resource_id}/")
    def get_one(collection: str, resource_id: str):
        item = node.get_resource(collection, resource_id)
        if item is None:
            raise HTTPException(status_code=404, detail="Not Found")
        return item

    @app.get("/x-nmos/connection")
    @app.get("/x-nmos/connection/")
    def conn_versions() -> list[str]:
        return ["v1.2/"]

    @app.get("/x-nmos/connection/v1.2")
    @app.get("/x-nmos/connection/v1.2/")
    def conn_v12() -> list[str]:
        return ["single/", "bulk/"]

    @app.get("/x-nmos/connection/v1.2/bulk")
    @app.get("/x-nmos/connection/v1.2/bulk/")
    def conn_bulk() -> list[str]:
        return ["senders/", "receivers/"]

    @app.api_route(
        "/x-nmos/connection/v1.2/bulk/{side}",
        methods=["GET", "PUT", "PATCH", "DELETE", "HEAD"],
    )
    @app.api_route(
        "/x-nmos/connection/v1.2/bulk/{side}/",
        methods=["GET", "PUT", "PATCH", "DELETE", "HEAD"],
    )
    def conn_bulk_side_not_allowed(side: str):
        name = side.rstrip("/")
        if name not in {"senders", "receivers"}:
            raise HTTPException(status_code=404, detail="Not Found")
        return JSONResponse(
            status_code=405,
            headers={"Allow": "POST"},
            content=_error_body(405, "Use POST to stage bulk changes"),
        )

    @app.post("/x-nmos/connection/v1.2/bulk/{side}")
    @app.post("/x-nmos/connection/v1.2/bulk/{side}/")
    async def conn_bulk_post(side: str, request: Request):
        name = side.rstrip("/")
        if name not in {"senders", "receivers"}:
            raise HTTPException(status_code=404, detail="Not Found")
        try:
            body = await request.json()
        except Exception:
            return JSONResponse(status_code=400, content=_error_body(400, "invalid JSON"))
        if not isinstance(body, list):
            return JSONResponse(
                status_code=400,
                content=_error_body(400, "bulk body must be a JSON array"),
            )
        results = []
        for item in body:
            if not isinstance(item, dict) or "id" not in item:
                results.append({"id": None, "code": 400})
                continue
            resource_id = str(item["id"]).rstrip("/")
            if node.get_resource(name, resource_id) is None:
                results.append({"id": resource_id, "code": 404})
                continue
            params = item.get("params") or {}
            if not isinstance(params, dict):
                results.append({"id": resource_id, "code": 400})
                continue
            try:
                node.patch_staged(resource_id, name, params)
                results.append({"id": resource_id, "code": 200})
            except NmosActivationError:
                results.append({"id": resource_id, "code": 400})
        return results

    @app.get("/x-nmos/connection/v1.2/single")
    @app.get("/x-nmos/connection/v1.2/single/")
    def conn_single() -> list[str]:
        return ["senders/", "receivers/"]

    @app.get("/x-nmos/connection/v1.2/single/{side}")
    @app.get("/x-nmos/connection/v1.2/single/{side}/")
    def conn_ids(side: str):
        collection = side.rstrip("/")
        return node.list_ids(collection)

    @app.get("/x-nmos/connection/v1.2/single/{side}/{resource_id}")
    @app.get("/x-nmos/connection/v1.2/single/{side}/{resource_id}/")
    def conn_resource(side: str, resource_id: str) -> list[str]:
        if node.get_resource(side, resource_id) is None:
            raise HTTPException(status_code=404, detail="Not Found")
        paths = ["constraints/", "staged/", "active/", "transporttype/"]
        if side.rstrip("/") == "senders":
            paths.insert(3, "transportfile/")
        return paths

    @app.get("/x-nmos/connection/v1.2/single/{side}/{resource_id}/constraints")
    @app.get("/x-nmos/connection/v1.2/single/{side}/{resource_id}/constraints/")
    def conn_constraints(side: str, resource_id: str):
        if node.get_resource(side, resource_id) is None:
            raise HTTPException(status_code=404, detail="Not Found")
        return [node.constraints(resource_id, side)]

    @app.get("/x-nmos/connection/v1.2/single/{side}/{resource_id}/staged")
    @app.get("/x-nmos/connection/v1.2/single/{side}/{resource_id}/staged/")
    def conn_staged_get(side: str, resource_id: str):
        if node.get_resource(side, resource_id) is None:
            raise HTTPException(status_code=404, detail="Not Found")
        return node.staged(resource_id, side)

    @app.patch("/x-nmos/connection/v1.2/single/{side}/{resource_id}/staged")
    @app.patch("/x-nmos/connection/v1.2/single/{side}/{resource_id}/staged/")
    async def conn_staged_patch(side: str, resource_id: str, request: Request):
        if node.get_resource(side, resource_id) is None:
            raise HTTPException(status_code=404, detail="Not Found")
        try:
            body = await request.json()
        except Exception:
            return JSONResponse(
                status_code=400,
                content=_error_body(400, "invalid JSON"),
            )
        if not isinstance(body, dict):
            return JSONResponse(
                status_code=400,
                content=_error_body(400, "body must be a JSON object"),
            )
        try:
            staged = node.patch_staged(resource_id, side, body)
        except NmosActivationError as exc:
            return JSONResponse(
                status_code=400,
                content=_error_body(400, str(exc)),
            )
        mode = ((staged.get("activation") or {}).get("mode") or "")
        if str(mode).startswith("activate_scheduled"):
            return JSONResponse(status_code=202, content=staged)
        return staged

    @app.get("/x-nmos/connection/v1.2/single/{side}/{resource_id}/active")
    @app.get("/x-nmos/connection/v1.2/single/{side}/{resource_id}/active/")
    def conn_active(side: str, resource_id: str):
        if node.get_resource(side, resource_id) is None:
            raise HTTPException(status_code=404, detail="Not Found")
        return node.active(resource_id, side)

    @app.get("/x-nmos/connection/v1.2/single/{side}/{resource_id}/transporttype")
    @app.get("/x-nmos/connection/v1.2/single/{side}/{resource_id}/transporttype/")
    def conn_transporttype(side: str, resource_id: str) -> str:
        if node.get_resource(side, resource_id) is None:
            raise HTTPException(status_code=404, detail="Not Found")
        return TRANSPORT_MXL

    @app.get("/x-nmos/connection/v1.2/single/{side}/{resource_id}/transportfile")
    @app.get("/x-nmos/connection/v1.2/single/{side}/{resource_id}/transportfile/")
    def conn_transportfile(side: str, resource_id: str):
        if node.get_resource(side, resource_id) is None:
            raise HTTPException(status_code=404, detail="Not Found")
        # BCP-007-03: MXL has no SDP / transport file.
        raise HTTPException(status_code=404, detail="MXL has no transport file")

    return app
