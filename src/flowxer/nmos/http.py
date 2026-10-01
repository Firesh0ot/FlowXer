from __future__ import annotations

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from flowxer.nmos.service import NmosActivationError, NmosNode


def _paginate(items: list, request: Request) -> list:
    skip = int(request.query_params.get("skip") or 0)
    limit = request.query_params.get("limit")
    sliced = items[skip:]
    if limit is not None:
        sliced = sliced[: int(limit)]
    return sliced


def create_nmos_app(node: NmosNode) -> FastAPI:
    app = FastAPI(title="FlowXer NMOS Node", docs_url=None, redoc_url=None)
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

    @app.get("/")
    def root() -> dict:
        return {"x-nmos/": {}}

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
            raise HTTPException(404)
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
            raise HTTPException(404, detail="Not Found")
        return item

    @app.get("/x-nmos/connection")
    @app.get("/x-nmos/connection/")
    def conn_versions() -> list[str]:
        return ["v1.2/"]

    @app.get("/x-nmos/connection/v1.2")
    @app.get("/x-nmos/connection/v1.2/")
    def conn_v12() -> list[str]:
        return ["single/"]

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
            raise HTTPException(404)
        return ["constraints/", "staged/", "active/", "transportfile/"]

    @app.get("/x-nmos/connection/v1.2/single/{side}/{resource_id}/constraints")
    @app.get("/x-nmos/connection/v1.2/single/{side}/{resource_id}/constraints/")
    def conn_constraints(side: str, resource_id: str):
        if node.get_resource(side, resource_id) is None:
            raise HTTPException(404)
        return [node.constraints(resource_id, side)]

    @app.get("/x-nmos/connection/v1.2/single/{side}/{resource_id}/staged")
    @app.get("/x-nmos/connection/v1.2/single/{side}/{resource_id}/staged/")
    def conn_staged_get(side: str, resource_id: str):
        if node.get_resource(side, resource_id) is None:
            raise HTTPException(404)
        return node.staged(resource_id, side)

    @app.patch("/x-nmos/connection/v1.2/single/{side}/{resource_id}/staged")
    @app.patch("/x-nmos/connection/v1.2/single/{side}/{resource_id}/staged/")
    async def conn_staged_patch(side: str, resource_id: str, request: Request):
        if node.get_resource(side, resource_id) is None:
            raise HTTPException(404)
        try:
            body = await request.json()
        except Exception:
            return JSONResponse(
                status_code=400,
                content={"code": 400, "error": "invalid JSON", "debug": "body must be JSON"},
            )
        if not isinstance(body, dict):
            return JSONResponse(
                status_code=400,
                content={"code": 400, "error": "body must be a JSON object", "debug": ""},
            )
        try:
            return node.patch_staged(resource_id, side, body)
        except NmosActivationError as exc:
            return JSONResponse(
                status_code=400,
                content={"code": 400, "error": str(exc), "debug": str(exc)},
            )

    @app.get("/x-nmos/connection/v1.2/single/{side}/{resource_id}/active")
    @app.get("/x-nmos/connection/v1.2/single/{side}/{resource_id}/active/")
    def conn_active(side: str, resource_id: str):
        if node.get_resource(side, resource_id) is None:
            raise HTTPException(404)
        return node.active(resource_id, side)

    @app.get("/x-nmos/connection/v1.2/single/{side}/{resource_id}/transportfile")
    @app.get("/x-nmos/connection/v1.2/single/{side}/{resource_id}/transportfile/")
    def conn_transportfile(side: str, resource_id: str):
        if node.get_resource(side, resource_id) is None:
            raise HTTPException(404)
        return JSONResponse(content=None, status_code=204)

    return app
