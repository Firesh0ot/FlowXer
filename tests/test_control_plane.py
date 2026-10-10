"""The control plane and the media engine: the /livez watchdog, the engine as a supervised child
(start, state push, commands, restart after a crash, a start that fails), IS-05 routes reaching
the engine."""

from __future__ import annotations

import json
import socket
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from flowxer.api.schemas import MixerStartRequest
from flowxer.engine import runtime as engine_runtime
from flowxer.engine.mixer import MixerError, VisionMixer
from flowxer.engine.watchdog import ControlPlaneWatchdog

FAKE_ENGINE = Path(__file__).with_name("fake_engine.py")
FLOW = "11111111-1111-1111-1111-111111111111"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _commands(log: Path) -> list[dict]:
    if not log.is_file():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]


def _wait(predicate, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


@pytest.fixture
def engine_mixer(settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A mixer that runs the fake engine (tests/fake_engine.py) as its media engine."""
    log = tmp_path / "engine-commands.jsonl"
    monkeypatch.setenv("FAKE_ENGINE_LOG", str(log))
    settings.simulate = False
    settings.engine_bin = f'"{Path(sys.executable).as_posix()}" "{FAKE_ENGINE.as_posix()}"'
    settings.engine_port = _free_port()
    settings.nmos_enable = True
    mixer = VisionMixer(settings)
    yield mixer, log
    mixer.stop()


def test_watchdog_reports_an_operation_that_runs_too_long() -> None:
    watchdog = ControlPlaneWatchdog(stuck_after_s=0.05)
    assert watchdog.oldest() is None and watchdog.stuck() is None
    with watchdog.busy("IS-05 activation"):
        assert watchdog.stuck() is None
        time.sleep(0.1)
        operation, seconds = watchdog.stuck()
        assert operation == "IS-05 activation" and seconds >= 0.05
    assert watchdog.oldest() is None


def _activate(mixer: VisionMixer, input_id: str, role: str, flow_id: str | None, enable: bool = True) -> None:
    receiver = next(
        r for r in mixer.nmos.status()["receivers"] if r["input_id"] == input_id and r["role"] == role
    )
    mixer.nmos.patch_staged(
        receiver["receiver_id"],
        "receivers",
        {
            "master_enable": enable,
            "activation": {"mode": "activate_immediate"},
            "transport_params": [{"mxl_flow_id": flow_id}],
        },
    )


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
    settings.nmos_enable = True
    mixer = VisionMixer(settings)
    seen: list = []
    monkeypatch.setattr(mixer, "apply_nmos_receiver", lambda *a, **k: seen.append(mixer.watchdog.oldest()))
    _activate(mixer, "cam-1", "video", FLOW)
    assert seen and seen[0][0] == "IS-05 activation"
    assert mixer.watchdog.oldest() is None


def test_the_engine_starts_gets_the_state_and_the_commands(engine_mixer, settings) -> None:
    mixer, log = engine_mixer
    status = mixer.start(MixerStartRequest(program_input_id="cam-1", preview_input_id="cam-2"))
    assert status.state.value == "running" and status.backend == "engine"
    assert mixer.engine is not None and mixer.engine.ready
    config = json.loads(mixer.engine.config_path.read_text(encoding="utf-8"))
    live = [item for item in mixer.list_inputs() if item.kind.value == "mxl_live"]
    assert [entry["label"] for entry in config["FLOWXER_ENGINE_INPUTS"]] == [item.label for item in live]
    assert config["FLOWXER_ENGINE_LATENCY"] == settings.latency
    assert config["FLOWXER_ENGINE_MES"] == mixer.render_count
    outputs = config["FLOWXER_ENGINE_OUTPUTS"][0]
    assert outputs["pgm_video"] == mixer.outputs.video_flow_id and outputs["pgm_audio"] == mixer.outputs.audio_flow_id
    commands = _commands(log)
    # The state push on ready: every live input's route, ME 1's buses.
    assert sum(1 for c in commands if c["path"].startswith("/input/")) == len(live)
    assert {"method": "POST", "path": "/me/1/program", "body": {"source": "in1"}} in [
        {k: c[k] for k in ("method", "path", "body")} for c in commands
    ]
    assert any(c["path"] == "/me/1/preview" and c["body"] == {"source": "in2"} for c in commands)
    # A cut: Program in2, Preview in1, at the next grain.
    mixer.cut("me-1")
    assert _commands(log)[-1]["path"] == "/me/1/take"
    assert _commands(log)[-1]["body"] == {"source": "in2", "frames": 0, "preview": "in1"}
    # A fade of 400 ms at 50p: 20 grains.
    mixer.fade("me-1", duration_ms=400)
    assert _commands(log)[-1]["body"] == {"source": "in1", "frames": 20, "preview": "in2"}
    # Fade to Black: Program to black, Preview stays.
    mixer.fade_to_black("me-1", duration_ms=600)
    assert _commands(log)[-1]["body"] == {"source": "black", "frames": 30}
    # Frames come from the engine's status.
    assert _wait(lambda: mixer.frames_rendered > 0)
    assert mixer.status().engine["state"] == "running"


def test_an_is05_route_reaches_the_engine(engine_mixer, settings) -> None:
    mixer, log = engine_mixer
    domain = settings.mxl_root / "cams"
    (domain / f"{FLOW}.mxl-flow").mkdir(parents=True)
    (domain / "domain_def.json").write_text(json.dumps({"id": "c0ffee00-0000-4000-8000-000000000000"}), encoding="utf-8")
    mixer.start()
    _activate(mixer, "cam-2", "video", FLOW)
    route = _commands(log)[-1]
    assert route["path"] == "/input/2"
    assert route["body"]["video"] == {"domain": str(domain.resolve()), "flow": FLOW}
    assert route["body"]["audio"] == {"domain": "", "flow": ""}
    # master_enable false closes the reader.
    _activate(mixer, "cam-2", "video", FLOW, enable=False)
    assert _commands(log)[-1]["body"]["video"] == {"domain": "", "flow": ""}


def test_the_engine_is_restarted_and_gets_the_state_again(engine_mixer, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(engine_runtime, "RESTART_BACKOFF_S", (0.1,))
    mixer, log = engine_mixer
    mixer.start(MixerStartRequest(program_input_id="cam-1", preview_input_id="cam-2"))
    mixer.cut("me-1")
    first = mixer.engine._process.pid
    mixer.engine._process.kill()
    assert _wait(lambda: mixer.engine.restarts >= 1 and mixer.engine.ready and mixer.engine._process.pid != first)
    # The new engine got the buses as they are now (after the cut).
    pushed = [c for c in _commands(log) if c["pid"] != first]
    assert {"path": "/me/1/program", "body": {"source": "in2"}} in [{k: c[k] for k in ("path", "body")} for c in pushed]
    assert mixer.state.value == "running"


def test_a_start_that_fails_reports_the_engine_error(engine_mixer, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_ENGINE_FAIL", "75")
    mixer, _log = engine_mixer
    with pytest.raises(MixerError) as failure:
        mixer.start()
    assert "no CUDA device" in str(failure.value)
    assert mixer.state.value == "error"
    assert mixer.engine is None


def test_preview_tiles_name_the_console_pictures(engine_mixer) -> None:
    from flowxer.engine.preview import preview_info

    mixer, _log = engine_mixer
    assert preview_info(mixer)["available"] is False
    mixer.start()
    info = preview_info(mixer)
    assert info["available"] and info["whep_url"] is None and info["whep_port"] == 8989
    ids = [tile["id"] for tile in info["tiles"]]
    assert ids[0] == "source:cam-1"
    assert "panel:me-1:pgm" in ids and "panel:me-1:pvw" in ids
