"""The T-bar as a real lever (ping-pong): `lever` is where it physically is, the mixer keeps the end it
rests at (panel.tbar_end) and flips it when the T-bar, an Auto or a Cut of a held transition
completes; `position` keeps its old meaning (the transition's progress)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from flowxer.api.schemas import MixerStartRequest
from flowxer.engine.mixer import VisionMixer


def _start(mixer: VisionMixer) -> None:
    mixer.start(MixerStartRequest(program_input_id="cam-1", preview_input_id="cam-2"))


def _bus(mixer: VisionMixer) -> tuple[str | None, str | None]:
    panel = mixer.get_panel("me-1")
    return panel.program_input_id, panel.preview_input_id


def test_a_drag_down_completes_and_the_next_one_goes_up(mixer: VisionMixer) -> None:
    _start(mixer)
    assert mixer.tbar_state()["me-1"] == {"end": "top", "position": 0.0, "lever": 0.0}
    mixer.set_lever("me-1", 0.4)
    assert mixer.tbar_state()["me-1"] == {"end": "top", "position": 0.4, "lever": 0.4}
    mixer.set_lever("me-1", 1.0)
    # Complete: the buses swapped, the lever rests at the bottom (no jump back to the top).
    assert _bus(mixer) == ("cam-2", "cam-1")
    assert mixer.tbar_state()["me-1"] == {"end": "bottom", "position": 0.0, "lever": 1.0}
    # Wiggling at the bottom (end-of-travel) starts nothing.
    mixer.set_lever("me-1", 0.99)
    assert mixer.tbar_state()["me-1"]["position"] == 0.0 and _bus(mixer) == ("cam-2", "cam-1")
    # The reverse drag: up from the bottom makes the next transition.
    mixer.set_lever("me-1", 0.7)
    assert mixer.tbar_state()["me-1"] == {"end": "bottom", "position": 0.30000000000000004, "lever": 0.7}
    mixer.set_lever("me-1", 0.0)
    assert _bus(mixer) == ("cam-1", "cam-2")
    assert mixer.tbar_state()["me-1"] == {"end": "top", "position": 0.0, "lever": 0.0}


def test_back_to_the_start_ends_it_without_the_swap(mixer: VisionMixer) -> None:
    _start(mixer)
    mixer.set_lever("me-1", 0.5)
    mixer.set_lever("me-1", 0.0)
    assert _bus(mixer) == ("cam-1", "cam-2") and mixer.tbar_state()["me-1"]["end"] == "top"


def test_auto_and_cut_leave_the_lever_at_the_right_end(mixer: VisionMixer) -> None:
    _start(mixer)
    mixer.auto("me-1")
    assert mixer.get_panel("me-1").tbar_end == "bottom"
    # From a held position, Auto runs the rest: the lever ends at the far end.
    mixer.set_lever("me-1", 0.6)  # from the bottom: 40 % of the next transition
    assert mixer.tbar_state()["me-1"]["position"] == 0.4
    mixer.auto("me-1")
    assert mixer.tbar_state()["me-1"] == {"end": "top", "position": 0.0, "lever": 0.0}
    # A Cut at rest leaves the lever where it is; a Cut of a held transition completes it.
    mixer.cut("me-1")
    assert mixer.get_panel("me-1").tbar_end == "top"
    mixer.set_lever("me-1", 0.3)
    mixer.cut("me-1")
    assert mixer.tbar_state()["me-1"] == {"end": "bottom", "position": 0.0, "lever": 1.0}


def test_the_api_takes_lever_or_position_and_the_console_shows_the_tbar(client: TestClient, mixer: VisionMixer) -> None:
    _start(mixer)
    assert client.post("/api/v1/mixer/position", json={"panel_id": "me-1", "lever": 1.0}).status_code == 200
    assert client.get("/api/v1/console").json()["tbar"]["me-1"] == {"end": "bottom", "position": 0.0, "lever": 1.0}
    # `position` keeps its meaning (the progress, whatever end the lever rests at).
    assert client.post("/api/v1/mixer/position", json={"panel_id": "me-1", "position": 0.25}).status_code == 200
    assert client.get("/api/v1/console").json()["tbar"]["me-1"] == {"end": "bottom", "position": 0.25, "lever": 0.75}
    assert client.post("/api/v1/mixer/position", json={"panel_id": "me-1"}).status_code == 422
    assert client.post("/api/v1/mixer/position", json={"panel_id": "me-1", "position": 0.5, "lever": 0.5}).status_code == 422
    # The resting end is part of the saved state.
    client.post("/api/v1/mixer/position", json={"panel_id": "me-1", "position": 1.0})
    assert client.get("/api/v1/config/export").json()["panels"][0]["tbar_end"] == "top"
