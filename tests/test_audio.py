"""P5 audio in the control plane (docs/engine-p5.md §8): the settings API and its validation, the
saved state (a P4 state loads with the defaults), the push to the engine after a start, and the
saves of a moving fader at most once a second."""

from __future__ import annotations

import json
import socket
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from flowxer.api.schemas import MixerStartRequest, WorkspaceUpdate
from flowxer.app import create_app
from flowxer.engine.mixer import VisionMixer
from flowxer.settings import Settings

FAKE_ENGINE = Path(__file__).with_name("fake_engine.py")


def _commands(log: Path) -> list[dict]:
    if not log.is_file():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]


@pytest.fixture
def engine_mixer(settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A mixer that runs the fake engine (tests/fake_engine.py) as its media engine."""
    log = tmp_path / "engine-commands.jsonl"
    monkeypatch.setenv("FAKE_ENGINE_LOG", str(log))
    settings.simulate = False
    settings.engine_bin = f'"{Path(sys.executable).as_posix()}" "{FAKE_ENGINE.as_posix()}"'
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        settings.engine_port = sock.getsockname()[1]
    mixer = VisionMixer(settings)
    yield mixer, log
    mixer.stop()


def _two_mes(mixer: VisionMixer) -> None:
    mixer.apply_workspace(WorkspaceUpdate(mixer_panel_count=2))


def test_audio_defaults(client: TestClient, mixer: VisionMixer) -> None:
    _two_mes(mixer)
    audio = client.get("/api/v1/audio").json()
    assert audio["channels"] == 2 and audio["max_delay_ms"] == 1000
    # Live inputs only, with their delay, map and channels.
    assert list(audio["inputs"]) == [i.id for i in mixer.list_inputs() if i.kind.value == "mxl_live"]
    assert audio["inputs"]["cam-1"] == {"label": "Camera 1", "delay_ms": 0.0, "channel_map": [], "channels": 2}
    # Every rendering ME: inputs, clip players, the Programs of higher MEs, the stinger.
    me1 = audio["panels"]["me-1"]
    assert me1["master_db"] == 0.0
    assert list(me1["strips"])[-3:] == ["clip-1", "me-2", "stinger"]
    assert me1["strips"]["cam-1"] == {"label": "Camera 1", "kind": "input", "fader_db": 0.0, "mute": False, "afv": True}
    assert me1["strips"]["stinger"]["afv"] is None
    assert "me-1" not in audio["panels"]["me-2"]["strips"] and "black" not in me1["strips"]
    # The console carries the settings.
    assert client.get("/api/v1/console").json()["audio"]["panels"]["me-1"]["strips"]["clip-1"]["kind"] == "clip_player"


def test_input_delay_and_channel_map(client: TestClient) -> None:
    response = client.patch("/api/v1/audio/inputs/cam-2", json={"delay_ms": 40.01})
    # Whole 48 kHz samples: 40.01 ms is 1920 samples.
    assert response.status_code == 200 and response.json()["delay_ms"] == 40.0
    assert client.patch("/api/v1/audio/inputs/cam-2", json={"channel_map": [1, 1]}).json() == {
        "label": "Camera 2", "delay_ms": 40.0, "channel_map": [1, 1], "channels": 2,
    }
    # One entry per PGM channel; channels 1..64 or 0.
    assert client.patch("/api/v1/audio/inputs/cam-2", json={"channel_map": [1, 2, 3]}).status_code == 422
    assert client.patch("/api/v1/audio/inputs/cam-2", json={"channel_map": [65, 1]}).status_code == 422
    assert client.patch("/api/v1/audio/inputs/cam-2", json={"channel_map": [0, 2]}).json()["channel_map"] == [0, 2]
    # FLOWXER_AUDIO_MAX_DELAY_MS (1000) and the schema's 0..5000.
    assert client.patch("/api/v1/audio/inputs/cam-2", json={"delay_ms": 1000.5}).status_code == 422
    assert client.patch("/api/v1/audio/inputs/cam-2", json={"delay_ms": -1}).status_code == 422
    # Live inputs only.
    assert client.patch("/api/v1/audio/inputs/clip-1", json={"delay_ms": 10}).status_code == 422
    assert client.patch("/api/v1/audio/inputs/nope", json={"delay_ms": 10}).status_code == 404
    # Back to the defaults: nothing kept.
    client.patch("/api/v1/audio/inputs/cam-2", json={"delay_ms": 0, "channel_map": []})
    assert "cam-2" not in client.get("/api/v1/config/export").json()["audio"]["inputs"]


def test_master_and_strips(client: TestClient, mixer: VisionMixer) -> None:
    _two_mes(mixer)
    assert client.patch("/api/v1/audio/panels/me-1", json={"master_db": -3.5}).json()["master_db"] == -3.5
    assert client.patch("/api/v1/audio/panels/me-1", json={"master_db": 11}).status_code == 422
    strip = client.patch("/api/v1/audio/panels/me-1/strips/cam-3", json={"fader_db": -6, "afv": False}).json()
    assert strip == {"label": "Camera 3", "kind": "input", "fader_db": -6.0, "mute": False, "afv": False}
    assert client.patch("/api/v1/audio/panels/me-1/strips/cam-3", json={"mute": True}).json()["fader_db"] == -6.0
    # Re-entry: ME 1 has a strip for ME 2, not the other way round.
    assert client.patch("/api/v1/audio/panels/me-1/strips/me-2", json={"fader_db": -10}).status_code == 200
    assert client.patch("/api/v1/audio/panels/me-2/strips/me-1", json={"fader_db": -10}).status_code == 404
    # The stinger strip: fader and mute, no AFV.
    assert client.patch("/api/v1/audio/panels/me-1/strips/stinger", json={"fader_db": -2}).status_code == 200
    assert client.patch("/api/v1/audio/panels/me-1/strips/stinger", json={"afv": False}).status_code == 422
    assert client.patch("/api/v1/audio/panels/me-1/strips/black", json={"mute": True}).status_code == 404
    assert client.patch("/api/v1/audio/panels/me-9/strips/cam-1", json={"mute": True}).status_code == 404
    assert client.patch("/api/v1/audio/panels/me-1/strips/cam-1", json={"fader_db": -91}).status_code == 422
    saved = client.get("/api/v1/config/export").json()["audio"]["panels"]["me-1"]
    assert saved["master_db"] == -3.5
    assert saved["strips"] == {
        "cam-3": {"fader_db": -6.0, "mute": True, "afv": False},
        "me-2": {"fader_db": -10.0, "mute": False, "afv": True},
        "stinger": {"fader_db": -2.0, "mute": False, "afv": True},
    }


def test_a_panel_that_does_not_render_has_no_audio(settings: Settings) -> None:
    settings.render_mes = 1
    mixer = VisionMixer(settings)
    _two_mes(mixer)
    client = TestClient(create_app(settings, mixer))
    assert list(client.get("/api/v1/audio").json()["panels"]) == ["me-1"]
    assert client.patch("/api/v1/audio/panels/me-2", json={"master_db": -6}).status_code == 409


def test_the_audio_state_round_trip_and_a_p4_state(client: TestClient, mixer: VisionMixer, settings: Settings) -> None:
    client.patch("/api/v1/audio/inputs/cam-1", json={"delay_ms": 120, "channel_map": [2, 1]})
    client.patch("/api/v1/audio/panels/me-1/strips/clip-1", json={"afv": False, "fader_db": -12})
    exported = client.get("/api/v1/config/export").json()
    assert exported["audio"] == {
        "inputs": {"cam-1": {"delay_ms": 120.0, "channel_map": [2, 1]}},
        "panels": {"me-1": {"master_db": 0.0, "strips": {"clip-1": {"fader_db": -12.0, "mute": False, "afv": False}}}},
    }
    # A restart restores it (audio changes are saved at most once a second: save now).
    mixer.persist()
    again = VisionMixer(settings)
    assert again.audio_state()["inputs"]["cam-1"]["delay_ms"] == 120.0
    assert again.audio_state()["panels"]["me-1"]["strips"]["clip-1"]["afv"] is False
    # A P4 state (no `audio`) imports with the defaults.
    p4 = {key: value for key, value in exported.items() if key != "audio"}
    assert client.post("/api/v1/config/import", json=p4).status_code == 200
    state = client.get("/api/v1/audio").json()
    assert state["inputs"]["cam-1"]["delay_ms"] == 0.0 and state["panels"]["me-1"]["strips"]["clip-1"]["afv"] is True
    # A bad audio value in an import is refused.
    broken = dict(exported, audio={"inputs": {"cam-1": {"delay_ms": -5}}})
    assert client.post("/api/v1/config/import", json=broken).status_code == 422


def test_audio_of_what_is_gone_is_dropped(client: TestClient, mixer: VisionMixer) -> None:
    _two_mes(mixer)
    client.patch("/api/v1/audio/panels/me-2", json={"master_db": -6})
    client.patch("/api/v1/audio/panels/me-1/strips/me-2", json={"mute": True})
    client.patch("/api/v1/audio/inputs/cam-6", json={"delay_ms": 20})
    mixer.apply_workspace(WorkspaceUpdate(mixer_panel_count=1, logical_source_count=4))
    audio = mixer.export_state()["audio"]
    assert audio == {"inputs": {}, "panels": {}}


def test_the_engine_gets_the_audio(engine_mixer) -> None:
    mixer, log = engine_mixer
    mixer.apply_workspace(WorkspaceUpdate(mixer_panel_count=2))
    from flowxer.api.schemas import AudioStripUpdate, InputAudioUpdate, PanelAudioUpdate

    mixer.update_input_audio("cam-2", InputAudioUpdate(delay_ms=40, channel_map=[2, 1]))
    mixer.update_audio_strip("me-1", "me-2", AudioStripUpdate(fader_db=-6, afv=False))
    mixer.update_audio_strip("me-1", "stinger", AudioStripUpdate(mute=True))
    mixer.update_panel_audio("me-2", PanelAudioUpdate(master_db=-1))
    config_before = len(_commands(log))
    assert config_before == 0
    mixer.start(MixerStartRequest(program_input_id="cam-1", preview_input_id="cam-2"))
    config = json.loads(mixer.engine.config_path.read_text(encoding="utf-8"))
    assert config["FLOWXER_ENGINE_AUDIO_MAX_DELAY_MS"] == 1000
    pushed = {c["path"]: c["body"] for c in _commands(log)}
    assert pushed["/input/2/audio"] == {"delay_ms": 40.0, "map": [2, 1]}
    assert pushed["/input/1/audio"] == {"delay_ms": 0.0, "map": []}
    me1 = pushed["/me/1/audio"]
    assert me1["master_db"] == 0.0 and me1["strips"]["me2"] == {"fader_db": -6.0, "mute": False, "afv": False}
    assert me1["strips"]["stinger"] == {"fader_db": 0.0, "mute": True}
    assert me1["strips"]["in1"] == {"fader_db": 0.0, "mute": False, "afv": True} and "cp1" in me1["strips"]
    assert pushed["/me/2/audio"]["master_db"] == -1.0 and "me1" not in pushed["/me/2/audio"]["strips"]
    # A change while on air: only what changed.
    mixer.update_audio_strip("me-1", "cam-1", AudioStripUpdate(fader_db=-3))
    last = _commands(log)[-1]
    assert last["path"] == "/me/1/audio" and last["body"] == {"strips": {"in1": {"fader_db": -3.0, "mute": False, "afv": True}}}
    mixer.update_input_audio("cam-1", InputAudioUpdate(delay_ms=5))
    assert _commands(log)[-1]["body"] == {"delay_ms": 5.0, "map": []}


def test_a_moving_fader_is_saved_at_most_once_a_second(client: TestClient, settings: Settings) -> None:
    state = Path(settings.state_dir) / "state.json"
    client.patch("/api/v1/audio/panels/me-1/strips/cam-1", json={"fader_db": -1})
    started = time.monotonic()
    for k in range(2, 12):
        client.patch("/api/v1/audio/panels/me-1/strips/cam-1", json={"fader_db": -k})
    # The first change is saved at once, the last one within a second.
    saved = json.loads(state.read_text(encoding="utf-8"))
    assert saved["audio"]["panels"]["me-1"]["strips"]["cam-1"]["fader_db"] in (-1.0, -11.0)
    deadline = started + 2.5
    while time.monotonic() < deadline:
        saved = json.loads(state.read_text(encoding="utf-8"))
        if saved["audio"]["panels"]["me-1"]["strips"]["cam-1"]["fader_db"] == -11.0:
            break
        time.sleep(0.05)
    assert saved["audio"]["panels"]["me-1"]["strips"]["cam-1"]["fader_db"] == -11.0
