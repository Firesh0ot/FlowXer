"""The media engine (flowxer-engine, C++/CUDA) as a supervised child process.

FlowXer owns the state and the decisions; the engine renders, reads and writes MXL and
publishes the preview mosaic. `EngineRuntime` starts the engine with a JSON configuration (the
structure: inputs, clip players, key inputs, MEs, output flows), restarts it with a backoff when
it exits (as mxl-webrtc-monitor does with its MediaMTX child), calls `on_ready` after every
(re)start so the mixer can push its state (routes, buses, players, keyers), and talks to the
engine's control API on 127.0.0.1 only.
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

log = logging.getLogger("flowxer.engine")

# Pauses (s) before the engine is started again after it exited; the last one repeats. An engine
# that ran for RESET_AFTER_S starts the backoff over.
RESTART_BACKOFF_S = (1.0, 2.0, 5.0, 10.0)
RESET_AFTER_S = 60.0
# How long a start waits for the control API to answer (GPU, MXL flows and MediaMTX come up).
READY_TIMEOUT_S = 20.0
# The engine's status is fetched this often while it runs.
POLL_S = 1.0
# A control call that does not answer within this time fails (the engine is local).
CALL_TIMEOUT_S = 2.0
STOP_TIMEOUT_S = 8.0


class EngineError(RuntimeError):
    """The engine did not start, or refused a command."""


class EngineClient:
    """JSON over HTTP to the engine's control API on localhost (no proxy)."""

    def __init__(self, port: int, host: str = "127.0.0.1") -> None:
        self.base = f"http://{host}:{port}"
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def request(self, method: str, path: str, body: dict[str, Any] | None = None, timeout: float = CALL_TIMEOUT_S) -> Any:
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            self.base + path, data=data, method=method, headers={"Content-Type": "application/json"}
        )
        try:
            with self._opener.open(request, timeout=timeout) as response:
                text = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            try:
                detail = json.loads(detail).get("error", detail)
            except ValueError:
                pass
            raise EngineError(f"engine {method} {path}: {exc.code} {detail}") from exc
        except OSError as exc:
            raise EngineError(f"engine {method} {path}: {exc}") from exc
        if not text:
            return None
        if path == "/metrics" or path == "/livez":
            return text
        return json.loads(text)

    def get(self, path: str, timeout: float = CALL_TIMEOUT_S) -> Any:
        return self.request("GET", path, timeout=timeout)

    def post(self, path: str, body: dict[str, Any] | None = None) -> Any:
        return self.request("POST", path, body or {})

    def put(self, path: str, body: dict[str, Any]) -> Any:
        return self.request("PUT", path, body)


class EngineRuntime:
    """Starts, supervises and drives one engine process."""

    def __init__(
        self,
        *,
        binary: str | list[str],
        port: int,
        state_dir: Path,
        on_ready: Callable[[EngineClient], None],
        env: dict[str, str] | None = None,
    ) -> None:
        # The engine binary (a command line: tests run a stand-in through Python).
        self.command = shlex.split(binary) if isinstance(binary, str) else list(binary)
        self.binary = self.command[0] if self.command else ""
        self.port = port
        self.client = EngineClient(port)
        self.config_path = state_dir / "engine.json"
        self._on_ready = on_ready
        self._env = env
        self._lock = threading.RLock()
        self._wanted = False
        self._process: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._poller: threading.Thread | None = None
        # For the API and the metrics.
        self.state = "stopped"
        self.restarts = 0
        self.starts = 0
        self.last_exit: int | None = None
        self.last_error: str | None = None
        self.started_at = 0.0
        self.status: dict[str, Any] | None = None
        self.status_at = 0.0
        # The preview mosaic's tile map (fixed while the engine runs).
        self.mosaic_map: dict[str, Any] | None = None

    # ── lifecycle ────────────────────────────────────────────────────────────

    def start(self, config: dict[str, Any], timeout: float = READY_TIMEOUT_S) -> None:
        """Start the engine and wait until it answers and on_ready has pushed the state.
        Raises EngineError when it exits or does not answer in time (it is stopped then)."""
        with self._lock:
            if self._wanted:
                raise EngineError("the engine is already running")
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.config_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(config, indent=2), encoding="utf-8")
            os.replace(tmp, self.config_path)
            self._wanted = True
            self._stop.clear()
            self._ready.clear()
            self.last_error = None
            self.status = None
            self._thread = threading.Thread(target=self._supervise, name="engine-supervisor", daemon=True)
            self._thread.start()
            self._poller = threading.Thread(target=self._poll, name="engine-status", daemon=True)
            self._poller.start()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._ready.wait(0.1):
                return
            if self.state == "restarting" and self.starts >= 1:
                # The first start failed: report why instead of waiting for the retries.
                break
        reason = self.last_error or f"no answer from the engine within {timeout:.0f} s"
        self.stop()
        raise EngineError(f"the media engine did not start: {reason}")

    def stop(self) -> None:
        with self._lock:
            self._wanted = False
            self._stop.set()
            process = self._process
        if process is not None:
            self._terminate(process)
        for thread in (self._thread, self._poller):
            if thread is not None and thread is not threading.current_thread():
                thread.join(timeout=STOP_TIMEOUT_S + 2)
        self._thread = None
        self._poller = None
        self._ready.clear()
        self.state = "stopped"
        self.status = None

    @property
    def running(self) -> bool:
        return self._wanted

    @property
    def ready(self) -> bool:
        return self._ready.is_set()

    def _child_env(self) -> dict[str, str]:
        # The configuration file is authoritative: no FLOWXER_ENGINE_* from the pod's
        # environment (FLOWXER_TALLY_TSL and the like are FlowXer's, not the engine's).
        env = {key: value for key, value in (self._env or os.environ).items() if not key.startswith("FLOWXER_ENGINE_")}
        env["FLOWXER_ENGINE_CONFIG"] = str(self.config_path)
        return env

    def _spawn(self) -> subprocess.Popen:
        return subprocess.Popen(
            self.command,
            env=self._child_env(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            start_new_session=True,
        )

    def _supervise(self) -> None:
        attempt = 0
        while not self._stop.is_set():
            self.state = "starting"
            try:
                process = self._spawn()
            except OSError as exc:
                self.last_error = f"cannot start {self.binary}: {exc}"
                log.error("%s", self.last_error)
                process = None
            self.starts += 1
            if process is not None:
                with self._lock:
                    self._process = process
                self.started_at = time.monotonic()
                reader = threading.Thread(target=self._forward_log, args=(process,), name="engine-log", daemon=True)
                reader.start()
                self._wait_ready(process)
                code = process.wait()
                # The engine's own children (its MediaMTX) are in its process group: a crashed
                # engine must not leave them holding the ports of the next one.
                self._kill_group(process)
                reader.join(timeout=2)
                with self._lock:
                    self._process = None
                self._ready.clear()
                self.last_exit = code
                if self._stop.is_set():
                    break
                if time.monotonic() - self.started_at > RESET_AFTER_S:
                    attempt = 0
                log.error("the media engine exited (code %s)%s", code, f": {self.last_error}" if self.last_error else "")
            if self._stop.is_set():
                break
            self.state = "restarting"
            pause = RESTART_BACKOFF_S[min(attempt, len(RESTART_BACKOFF_S) - 1)]
            attempt += 1
            self.restarts += 1
            if self._stop.wait(pause):
                break
        self.state = "stopped"

    def _wait_ready(self, process: subprocess.Popen) -> bool:
        """Wait for the control API, then push the state. False when the process ended first."""
        deadline = time.monotonic() + READY_TIMEOUT_S
        while time.monotonic() < deadline and not self._stop.is_set():
            if process.poll() is not None:
                return False
            try:
                self.client.get("/livez", timeout=0.5)
            except EngineError:
                time.sleep(0.1)
                continue
            try:
                self._on_ready(self.client)
            except Exception as exc:  # the state push must not kill the supervisor
                log.warning("pushing the mixer state to the engine failed: %s", exc)
            try:
                self.mosaic_map = self.client.get("/mosaic/map")
            except (EngineError, ValueError) as exc:
                log.warning("the engine's mosaic map: %s", exc)
            self.state = "running"
            self._ready.set()
            log.info("media engine running (pid %s, start %d)", process.pid, self.starts)
            return True
        if process.poll() is None and not self._stop.is_set():
            self.last_error = f"no answer from the engine's control API within {READY_TIMEOUT_S:.0f} s"
            log.error("%s; stopping it", self.last_error)
            self._terminate(process)
        return False

    def _terminate(self, process: subprocess.Popen) -> None:
        if process.poll() is not None:
            return
        try:
            process.send_signal(signal.SIGTERM)
            process.wait(timeout=STOP_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            log.warning("the media engine did not stop within %.0f s; killing it", STOP_TIMEOUT_S)
            process.kill()
            process.wait(timeout=5)
        except OSError:
            pass

    @staticmethod
    def _kill_group(process: subprocess.Popen) -> None:
        """Ends what is left of the engine's process group (it was started in a session of its own)."""
        if not hasattr(os, "killpg"):
            return
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

    def _forward_log(self, process: subprocess.Popen) -> None:
        """The engine logs one JSON object per line on stderr: forward it, keep the last error."""
        assert process.stderr is not None
        for line in process.stderr:
            line = line.rstrip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                log.info("%s", line)
                continue
            level = str(record.pop("level", "info"))
            event = str(record.pop("event", ""))
            text = event + (" " + json.dumps(record, separators=(",", ":")) if record else "")
            if level == "error":
                self.last_error = f"{event}: {record.get('error', record)}"
                log.error("%s", text)
            elif level == "warn":
                log.warning("%s", text)
            else:
                log.info("%s", text)

    def _poll(self) -> None:
        while not self._stop.wait(POLL_S):
            if not self._ready.is_set():
                continue
            try:
                self.status = self.client.get("/status")
                self.status_at = time.monotonic()
            except (EngineError, ValueError):
                pass

    # ── commands ─────────────────────────────────────────────────────────────

    def call(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        """A control command. Raises EngineError while the engine is not up (the state is pushed
        again when it is) or when it refuses the command."""
        if not self._ready.is_set():
            raise EngineError("the media engine is not running")
        return self.client.request(method, path, body if body is not None else ({} if method != "GET" else None))

    def metrics(self) -> str:
        if not self._ready.is_set():
            return ""
        try:
            return self.client.get("/metrics")
        except EngineError:
            return ""

    def summary(self) -> dict[str, Any]:
        """For GET /mixer: the process state and the headline numbers of the last status."""
        status = self.status or {}
        output = status.get("output") or {}
        engine = status.get("engine") or {}
        return {
            "state": self.state,
            "restarts": self.restarts,
            "last_exit": self.last_exit,
            "last_error": self.last_error,
            "fps": output.get("fps"),
            "skipped": output.get("skipped"),
            "lag_grains": output.get("lag_grains"),
            "latency_grains": engine.get("latency_grains"),
            "recommended_latency_grains": engine.get("recommended_latency_grains"),
            "gpu": (engine.get("gpu") or {}).get("name"),
            "version": engine.get("version"),
        }
