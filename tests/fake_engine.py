"""A stand-in for flowxer-engine in the tests: the control API on the configured port, no media.

It reads FLOWXER_ENGINE_CONFIG like the engine, answers /livez, /status, /metrics, /players and
/mosaic/map, keeps the buses it is told, and appends every command (method, path, body) as one
JSON line to FAKE_ENGINE_LOG. FAKE_ENGINE_FAIL=<code> makes it log an error and exit at once.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

config = json.loads(open(os.environ["FLOWXER_ENGINE_CONFIG"], encoding="utf-8").read())
log_path = os.environ.get("FAKE_ENGINE_LOG")
fail = os.environ.get("FAKE_ENGINE_FAIL")
if fail:
    print(json.dumps({"level": "error", "event": "start", "error": "no CUDA device"}), file=sys.stderr, flush=True)
    sys.exit(int(fail))

inputs = config.get("FLOWXER_ENGINE_INPUTS") or []
players = config.get("FLOWXER_ENGINE_PLAYERS") or []
keys = config.get("FLOWXER_ENGINE_KEYS") or []
mes = int(config.get("FLOWXER_ENGINE_MES") or 1)
state = {
    "committed": 0,
    "mes": {m: {"program": "black", "preview": "black"} for m in range(1, mes + 1)},
    "dsks": {k: False for k in range(1, len(keys) + 1)},
}
lock = threading.Lock()


def record(method: str, path: str, body: dict) -> None:
    if log_path:
        with open(log_path, "a", encoding="utf-8") as out:
            out.write(json.dumps({"method": method, "path": path, "body": body, "pid": os.getpid()}) + "\n")


def status() -> dict:
    with lock:
        state["committed"] += 50
        return {
            "engine": {"version": "fake", "latency_grains": config.get("FLOWXER_ENGINE_LATENCY"), "recommended_latency_grains": 2, "gpu": {"name": "none"}},
            "output": {"fps": 50.0, "committed": state["committed"], "skipped": 0, "lag_grains": {"avg": 4, "max": 4}},
            "inputs": [
                {"input": k, "name": f"in{k}", "state": "running", "late": 0, "missing": 0, "arrival_ms": {"avg": 3.0, "max": 5.0, "peak": 6.0},
                 "audio": {"state": "running", "missing": 0, "lag_ms": {"avg": 10.0, "max": 20.0, "peak": 21.0}}}
                for k in range(1, len(inputs) + 1)
            ],
            "players": [{"player": k, "label": p.get("label"), "state": "empty"} for k, p in enumerate(players, 1)],
            "keys": [{"key": k, "name": f"key{k}", "state": "not_routed", "arrival_ms": {"avg": 0, "max": 0}} for k in range(1, len(keys) + 1)],
            "mes": [{"me": m, **bus} for m, bus in state["mes"].items()],
            "dsks": [{"dsk": k, "on": on} for k, on in state["dsks"].items()],
        }


def audio_meters() -> dict:
    """The engine's /audio/meters with every input at -20 dBFS and loudness -23 LUFS."""
    channels = int(config.get("FLOWXER_ENGINE_AUDIO_CHANNELS") or 2)
    level = {"peak_dbfs": [-20.0] * channels, "rms_dbfs": [-23.0] * channels}
    sources = {f"in{k}": level for k in range(1, len(inputs) + 1)}
    sources.update({f"cp{k}": level for k in range(1, len(players) + 1)})
    mes_out = {}
    for m in range(1, mes + 1):
        strips = {f"in{k}": {"gain_db": 0.0 if k == m else -90.0, "heard": k == m} for k in range(1, len(inputs) + 1)}
        mes_out[str(m)] = {"peak_dbfs": [-20.0] * channels, "true_peak_dbtp": [-19.5] * channels, "rms_dbfs": [-23.0] * channels,
                           "loudness": [{"pair": 1, "momentary": -23.0, "short_term": -23.1, "integrated": -23.0}], "overs": 0, "strips": strips}
    return {"block": 17921903012, "channels": channels, "sources": sources, "mes": mes_out}


def mosaic_map() -> dict:
    tiles = [{"id": f"in{k}", "kind": "input", "source": f"in{k}", "x": 0, "y": 0, "w": 480, "h": 270} for k in range(1, len(inputs) + 1)]
    tiles += [{"id": f"cp{k}", "kind": "player", "source": f"cp{k}", "x": 0, "y": 0, "w": 480, "h": 270} for k in range(1, len(players) + 1)]
    # Each ME's Program is a stream of its own (FLOWXER_ENGINE_PGM_PREVIEW, the default).
    programs = []
    for m in range(1, mes + 1):
        tiles.append({"id": f"me{m}-pvw", "kind": "preview", "source": f"me{m}", "x": 480, "y": 540, "w": 480, "h": 270})
        programs.append({"me": m, "id": f"me{m}-pgm", "source": f"me{m}", "path": f"flowxer/me{m}-pgm", "width": 1280, "height": 720,
                         "fps": 25, "kbps": 4000})
    return {"width": 1920, "height": 1080, "fps": 25, "enabled": True, "path": "flowxer/mosaic", "mode": "own",
            "whep_base": "", "hls_base": "", "whep_port": 8989, "hls_port": 8988, "tiles": tiles, "programs": programs}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:
        pass

    def _reply(self, code: int, body) -> None:
        data = (body if isinstance(body, str) else json.dumps(body)).encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/plain" if isinstance(body, str) else "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        if self.path == "/livez":
            self._reply(200, "ok\n")
        elif self.path == "/status":
            self._reply(200, status())
        elif self.path == "/metrics":
            self._reply(200, "flowxer_engine_output_fps 50\n")
        elif self.path == "/mosaic/map":
            self._reply(200, mosaic_map())
        elif self.path == "/audio/meters":
            self._reply(200, audio_meters())
        elif self.path == "/players":
            with lock:
                index = state["committed"]
            self._reply(200, {"index": index, "players": [{"player": k, "label": p.get("label"), "state": "empty", "end_index": None}
                                                         for k, p in enumerate(players, 1)]})
        else:
            self._reply(404, {"error": "not found"})

    def _command(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        record(self.command, self.path, body)
        parts = self.path.strip("/").split("/")
        with lock:
            if parts[0] == "me" and len(parts) == 3:
                bus = state["mes"].get(int(parts[1]))
                if bus is None:
                    return self._reply(404, {"error": "no ME"})
                if parts[2] in {"program", "preview"}:
                    bus[parts[2]] = body.get("source")
                elif parts[2] == "take":
                    bus["program"] = body.get("source")
                    if body.get("preview"):
                        bus["preview"] = body["preview"]
            elif parts[0] == "dsk" and len(parts) == 2:
                state["dsks"][int(parts[1])] = bool(body.get("on"))
        self._reply(200, {"ok": True})

    do_POST = _command
    do_PUT = _command


server = ThreadingHTTPServer(("127.0.0.1", int(config["FLOWXER_ENGINE_HTTP_PORT"])), Handler)
signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=server.shutdown, daemon=True).start())
print(json.dumps({"level": "info", "event": "http_listen", "port": config["FLOWXER_ENGINE_HTTP_PORT"]}), file=sys.stderr, flush=True)
server.serve_forever()
