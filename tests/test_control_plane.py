"""The control plane never hangs on the media plane: bounded source restarts and stops, the
/livez watchdog, and the unrouted flow id (platform 10.17.40: an IS-05 route hung the node)."""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from flowxer.engine import gst_runtime
from flowxer.engine.gst_runtime import GstRuntime, SourceRestartTimeout, run_bounded
from flowxer.engine.mixer import VisionMixer
from flowxer.engine.pipeline import UNROUTED_FLOW
from flowxer.engine.watchdog import ControlPlaneWatchdog


class FakeElement:
    """Enough of an mxlsrc for retarget_mxl_source: properties, state changes, no pads."""

    def __init__(self, name: str, block: threading.Event | None = None) -> None:
        self.name = name
        self.properties: dict[str, str] = {}
        self.states: list[str] = []
        self.block = block

    def get_name(self) -> str:
        return self.name

    def find_property(self, name: str):
        return object() if name in {"video-flow-id", "audio-flow-id", "domain"} else None

    def set_property(self, name: str, value) -> None:
        self.properties[name] = value

    def set_state(self, state) -> None:
        if self.block is not None:
            self.block.wait()
        self.states.append(state)

    def get_static_pad(self, _name: str):
        return None


def _runtime_with(element: FakeElement) -> GstRuntime:
    runtime = GstRuntime()
    runtime._gst = SimpleNamespace(State=SimpleNamespace(NULL="null", PLAYING="playing"))
    runtime.pipeline = SimpleNamespace(get_by_name=lambda name: element if name == element.name else None)
    runtime.watchdog = ControlPlaneWatchdog()
    return runtime


def test_watchdog_reports_an_operation_that_runs_too_long() -> None:
    watchdog = ControlPlaneWatchdog(stuck_after_s=0.05)
    assert watchdog.oldest() is None and watchdog.stuck() is None
    with watchdog.busy("IS-05 activation"):
        assert watchdog.stuck() is None
        time.sleep(0.1)
        operation, seconds = watchdog.stuck()
        assert operation == "IS-05 activation" and seconds >= 0.05
    assert watchdog.oldest() is None


def test_run_bounded_gives_up_on_a_blocked_call_and_keeps_it_registered() -> None:
    watchdog = ControlPlaneWatchdog()
    release = threading.Event()
    assert run_bounded(lambda: None, 1.0, "quick", watchdog)
    assert not run_bounded(release.wait, 0.1, "blocked", watchdog)
    # The blocked thread stays registered until it ends.
    assert watchdog.oldest()[0] == "blocked"
    release.set()
    deadline = time.monotonic() + 2
    while watchdog.oldest() is not None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert watchdog.oldest() is None
    with pytest.raises(ValueError):
        run_bounded(lambda: int("x"), 1.0, "failing")


def test_unrouting_gives_mxlsrc_the_unrouted_flow_id() -> None:
    # An empty id is not a UUID and made mxlsrc fail at start.
    element = FakeElement("asrc_cam-3")
    runtime = _runtime_with(element)
    assert runtime.retarget_mxl_source("asrc_cam-3", None, "/Volumes/mxl/b", "audio")
    assert element.properties == {"audio-flow-id": UNROUTED_FLOW, "domain": "/Volumes/mxl/b"}
    assert element.states == ["null", "playing"]
    runtime.retarget_mxl_source("asrc_cam-3", "11111111-1111-1111-1111-111111111111", None, "audio")
    assert element.properties["audio-flow-id"] == "11111111-1111-1111-1111-111111111111"


def test_a_retarget_that_blocks_returns_after_the_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gst_runtime, "RESTART_TIMEOUT_S", 0.2)
    release = threading.Event()
    element = FakeElement("vsrc_cam-1", block=release)
    runtime = _runtime_with(element)
    started = time.monotonic()
    with pytest.raises(SourceRestartTimeout):
        runtime.retarget_mxl_source("vsrc_cam-1", "11111111-1111-1111-1111-111111111111", "/d", "video")
    assert time.monotonic() - started < 1.0
    assert runtime.watchdog.oldest()[0] == "retarget of vsrc_cam-1"
    release.set()


def _activate(mixer: VisionMixer, input_id: str, role: str, flow_id: str) -> None:
    receiver = next(
        r for r in mixer.nmos.status()["receivers"] if r["input_id"] == input_id and r["role"] == role
    )
    mixer.nmos.patch_staged(
        receiver["receiver_id"],
        "receivers",
        {
            "master_enable": True,
            "activation": {"mode": "activate_immediate"},
            "transport_params": [{"mxl_flow_id": flow_id}],
        },
    )


def test_an_activation_that_cannot_retarget_releases_the_node(settings, monkeypatch: pytest.MonkeyPatch) -> None:
    # 10.17.40: the retarget blocked under the NMOS lock; GET /mixer, stop and every later route hung.
    from flowxer.api.schemas import InputKind, LogicalInputUpdate

    monkeypatch.setattr(gst_runtime, "RESTART_TIMEOUT_S", 0.2)
    settings.nmos_enable = True
    mixer = VisionMixer(settings)
    mixer.update_input("cam-1", LogicalInputUpdate(kind=InputKind.mxl_live, group_hint="cam-1"))
    release = threading.Event()
    mixer.gst = _runtime_with(FakeElement("vsrc_cam-1", block=release))
    mixer.gst.watchdog = mixer.watchdog
    started = time.monotonic()
    _activate(mixer, "cam-1", "video", "11111111-1111-1111-1111-111111111111")
    assert time.monotonic() - started < 2
    assert "did not restart" in mixer.error
    # The NMOS lock is free again: status, routes and stop work.
    assert mixer.nmos.lock.acquire(timeout=0.5)
    mixer.nmos.lock.release()
    mixer.nmos.status()
    release.set()
    mixer.gst = None


def test_stop_that_does_not_finish_reports_an_error(settings) -> None:
    mixer = VisionMixer(settings)
    mixer.gst = SimpleNamespace(
        program_frames=5, program_dropped={"video": 1, "audio": 2}, late_frames=3, stop=lambda: False
    )
    status = mixer.stop()
    assert status.state.value == "error"
    assert "did not stop" in status.error
    assert mixer.gst is None
    assert mixer.program_dropped == {"video": 1, "audio": 2}
    assert mixer.frames_dropped == 3


def test_livez_fails_while_the_control_plane_is_stuck(client: TestClient, mixer: VisionMixer) -> None:
    assert client.get("/livez").status_code == 200
    assert client.get("/api/v1/livez").status_code == 200
    mixer.watchdog.stuck_after_s = 0.05
    token = mixer.watchdog.begin("IS-05 activation")
    time.sleep(0.1)
    for path in ("/livez", "/api/v1/livez"):
        response = client.get(path)
        assert response.status_code == 503
        assert "IS-05 activation" in response.json()["reason"]
    busy = next(
        line for line in client.get("/metrics").text.splitlines() if line.startswith("flowxer_control_plane_busy_seconds ")
    )
    assert float(busy.split()[1]) >= 0.05
    mixer.watchdog.end(token)
    assert client.get("/livez").status_code == 200


def test_an_is05_activation_is_watched(settings, monkeypatch: pytest.MonkeyPatch) -> None:
    from flowxer.api.schemas import InputKind, LogicalInputUpdate

    settings.nmos_enable = True
    mixer = VisionMixer(settings)
    seen: list = []
    monkeypatch.setattr(mixer, "apply_nmos_receiver", lambda *a, **k: seen.append(mixer.watchdog.oldest()))
    mixer.update_input("cam-1", LogicalInputUpdate(kind=InputKind.mxl_live, group_hint="cam-1"))
    _activate(mixer, "cam-1", "video", "11111111-1111-1111-1111-111111111111")
    assert seen and seen[0][0] == "IS-05 activation"
    assert mixer.watchdog.oldest() is None


def test_a_failed_mxl_source_is_started_again(monkeypatch: pytest.MonkeyPatch) -> None:
    # Platform: gateway RX flows mark incomplete frames invalid, mxlsrc stops for good on one and
    # the input froze. The source is started again after a pause.
    element = FakeElement("vsrc_cam-3")
    runtime = _runtime_with(element)
    scheduled: list = []
    runtime._glib = SimpleNamespace(timeout_add=lambda ms, fn: scheduled.append((ms, fn)))
    restarted = threading.Event()
    runtime.on_source_restart = lambda name, reason: restarted.set()
    runtime._recover_source(element, "Internal data stream error.")
    runtime._recover_source(element, "Internal data stream error.")  # the same failure, reported twice
    assert [ms for ms, _ in scheduled] == [1000]
    scheduled[0][1]()
    assert restarted.wait(2)
    assert element.states == ["null", "playing"]
    # Failing again at once backs off.
    deadline = time.monotonic() + 2
    while "vsrc_cam-3" in runtime._recovering and time.monotonic() < deadline:
        time.sleep(0.01)
    runtime._recover_source(element, "Internal data stream error.")
    assert scheduled[-1][0] == 2000
    # Test sources are not restarted.
    runtime._recover_source(FakeElement("comp"), "x")
    assert len(scheduled) == 2
