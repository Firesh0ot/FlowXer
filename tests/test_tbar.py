"""The T-bar as a real lever (ping-pong): `lever` is where it physically is, the mixer keeps the end it
rests at (panel.tbar_end) and flips it when the T-bar, an Auto or a Cut of a held transition
completes; `position` keeps its old meaning (the transition's progress)."""

from __future__ import annotations

from fastapi.testclient import TestClient

import pytest

from flowxer.api.schemas import MixerStartRequest, PanelTransitionUpdate
from flowxer.engine.mixer import MixerError
from flowxer.engine.mixer import VisionMixer


def _start(mixer: VisionMixer) -> None:
    mixer.start(MixerStartRequest(program_input_id="cam-1", preview_input_id="cam-2"))


def _bus(mixer: VisionMixer) -> tuple[str | None, str | None]:
    panel = mixer.get_panel("me-1")
    return panel.program_input_id, panel.preview_input_id


def test_a_drag_down_completes_and_the_next_one_goes_up(mixer: VisionMixer) -> None:
    _start(mixer)
    assert mixer.tbar_state()["me-1"] == {"end": "top", "position": 0.0, "lever": 0.0}
    mixer.set_lever("me-1", 0.0)  # a hardware lever picks up at the resting end first
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
    mixer.set_lever("me-1", 0.6, "bottom")  # from the bottom: 40 % of the next transition
    assert mixer.tbar_state()["me-1"]["position"] == 0.4
    mixer.auto("me-1")
    assert mixer.tbar_state()["me-1"] == {"end": "top", "position": 0.0, "lever": 0.0}
    # A Cut at rest leaves the lever where it is; a Cut of a held transition completes it.
    mixer.cut("me-1")
    assert mixer.get_panel("me-1").tbar_end == "top"
    mixer.set_lever("me-1", 0.3, "top")
    mixer.cut("me-1")
    assert mixer.tbar_state()["me-1"] == {"end": "bottom", "position": 0.0, "lever": 1.0}


def test_the_api_takes_lever_or_position_and_the_console_shows_the_tbar(client: TestClient, mixer: VisionMixer) -> None:
    _start(mixer)
    assert client.post("/api/v1/mixer/position", json={"panel_id": "me-1", "lever": 1.0, "from": "top"}).status_code == 200
    assert client.get("/api/v1/console").json()["tbar"]["me-1"] == {"end": "bottom", "position": 0.0, "lever": 1.0}
    # `position` keeps its meaning (the progress, whatever end the lever rests at).
    assert client.post("/api/v1/mixer/position", json={"panel_id": "me-1", "position": 0.25}).status_code == 200
    assert client.get("/api/v1/console").json()["tbar"]["me-1"] == {"end": "bottom", "position": 0.25, "lever": 0.75}
    assert client.post("/api/v1/mixer/position", json={"panel_id": "me-1"}).status_code == 422
    assert client.post("/api/v1/mixer/position", json={"panel_id": "me-1", "position": 0.5, "lever": 0.5}).status_code == 422
    # The resting end is part of the saved state.
    client.post("/api/v1/mixer/position", json={"panel_id": "me-1", "position": 1.0})
    assert client.get("/api/v1/config/export").json()["panels"][0]["tbar_end"] == "top"


def test_a_drag_from_an_end_that_flipped_under_it_is_ignored(mixer: VisionMixer, client: TestClient) -> None:
    # Console A holds the lever at 0.4 (from the top); console B's Auto completes it.
    _start(mixer)
    mixer.set_lever("me-1", 0.4, "top")
    mixer.auto("me-1")
    assert _bus(mixer) == ("cam-2", "cam-1") and mixer.get_panel("me-1").tbar_end == "bottom"
    # A drags on: counted from the new end it would start a transition back; it is ignored.
    for lever in (0.5, 0.7, 1.0):
        mixer.set_lever("me-1", lever, "top")
    assert _bus(mixer) == ("cam-2", "cam-1") and mixer.tbar_state()["me-1"]["position"] == 0.0
    # The next drag from the new end works.
    mixer.set_lever("me-1", 0.5, "bottom")
    assert mixer.tbar_state()["me-1"]["position"] == 0.5
    # Through the API the same, with "from".
    mixer.auto("me-1")
    assert client.post("/api/v1/mixer/position", json={"panel_id": "me-1", "lever": 0.6, "from": "bottom"}).status_code == 200
    assert mixer.tbar_state()["me-1"]["position"] == 0.0


def test_a_hardware_lever_picks_up_after_another_console_flipped_the_end(mixer: VisionMixer) -> None:
    _start(mixer)
    # The hardware lever (no from) rests at the top; a console's Auto flips the end to the bottom.
    mixer.set_lever("me-1", 0.0)
    mixer.auto("me-1")
    # The hardware lever moves: ignored until it has come down to the new resting end ...
    for lever in (0.1, 0.5, 0.9):
        mixer.set_lever("me-1", lever)
    assert mixer.tbar_state()["me-1"]["position"] == 0.0 and _bus(mixer) == ("cam-2", "cam-1")
    mixer.set_lever("me-1", 0.99)  # picked up at the bottom
    # ... then it makes the next transition going up.
    mixer.set_lever("me-1", 0.6)
    assert mixer.tbar_state()["me-1"]["position"] == 0.4
    mixer.set_lever("me-1", 0.0)
    assert _bus(mixer) == ("cam-1", "cam-2") and mixer.get_panel("me-1").tbar_end == "top"
    # Its own completion leaves it at the new end: the next move up counts at once (no pickup).
    mixer.set_lever("me-1", 0.3)
    assert mixer.tbar_state()["me-1"]["position"] == 0.3


def test_keys_only_fires_one_auto_per_drag(mixer: VisionMixer) -> None:
    _start(mixer)
    mixer.update_transition("me-1", PanelTransitionUpdate(ties=["me-1-key-1"], background=False))
    key = lambda: mixer.get_keyer("me-1-key-1").enabled
    # A GUI drag from the top: the first move fires the keyer's Auto, the rest of the drag nothing.
    for lever in (0.05, 0.2, 0.6, 1.0):
        mixer.set_lever("me-1", lever, "top")
    assert key() is True and mixer.get_panel("me-1").tbar_end == "bottom"
    # The next drag (from the bottom) takes it off again: once.
    for lever in (0.9, 0.5, 0.0):
        mixer.set_lever("me-1", lever, "bottom")
    assert key() is False and mixer.get_panel("me-1").tbar_end == "top"
    # A hardware lever (picked up at the top): one Auto, then nothing until it has come to the
    # other end (pickup).
    for lever in (0.0, 0.05, 0.3, 0.7):
        mixer.set_lever("me-1", lever)
    assert key() is True
    for lever in (0.99, 0.6):
        mixer.set_lever("me-1", lever)
    assert key() is False


def test_a_failed_auto_does_not_move_the_lever(mixer: VisionMixer) -> None:
    _start(mixer)
    mixer.get_panel("me-1").preview_input_id = None
    with pytest.raises(MixerError, match="Preview"):
        mixer.auto("me-1")
    assert mixer.get_panel("me-1").tbar_end == "top"


def test_a_held_lever_snaps_at_both_ends(mixer: VisionMixer) -> None:
    _start(mixer)
    mixer.set_lever("me-1", 0.5, "top")
    mixer.set_lever("me-1", 0.01, "top")  # within 2 % of the start: back, without the swap
    assert mixer.tbar_state()["me-1"]["position"] == 0.0 and _bus(mixer) == ("cam-1", "cam-2")
    mixer.set_lever("me-1", 0.5, "top")
    mixer.set_lever("me-1", 0.995, "top")  # within 2 % of the end: complete
    assert _bus(mixer) == ("cam-2", "cam-1") and mixer.get_panel("me-1").tbar_end == "bottom"


def test_position_one_from_rest_completes_the_transition(client: TestClient, mixer: VisionMixer) -> None:
    _start(mixer)
    response = client.post("/api/v1/mixer/position", json={"panel_id": "me-1", "position": 1.0})
    assert response.status_code == 200 and response.json()["status"] == "position"
    assert _bus(mixer) == ("cam-2", "cam-1") and mixer.get_panel("me-1").tbar_end == "bottom"


def test_a_hardware_lever_that_has_not_picked_up_cannot_complete_a_held_transition(mixer: VisionMixer) -> None:
    _start(mixer)
    mixer.auto("me-1")  # the lever rests at the bottom
    mixer.set_lever("me-1", 0.6, "bottom")  # a console holds the next transition
    # A hardware lever (no from) somewhere near the top jitters: ignored, also while held.
    status, ignored = mixer.set_lever("me-1", 0.01)
    assert ignored == "pickup" and mixer.tbar_state()["me-1"]["position"] == 0.4 and _bus(mixer) == ("cam-2", "cam-1")


def test_hardware_levers_pick_up_after_start_restart_and_a_loaded_state(mixer: VisionMixer, settings) -> None:
    _start(mixer)
    mixer.auto("me-1")  # rests at the bottom; saved
    mixer.persist()
    again = VisionMixer(settings)
    again.start(MixerStartRequest())
    assert again.get_panel("me-1").tbar_end == "bottom"
    # A hardware panel connecting with its lever at the top: nothing taken until it is at the bottom.
    assert again.set_lever("me-1", 0.0)[1] == "pickup"
    assert again.tbar_state()["me-1"]["position"] == 0.0
    assert again.set_lever("me-1", 1.0)[1] is None  # picked up
    # The engine restarts (or a state is loaded): picked up again first.
    again._arm_lever_pickup()
    assert again.set_lever("me-1", 0.5)[1] == "pickup"


def test_a_stale_from_cannot_move_a_transition_held_from_the_other_end(mixer: VisionMixer) -> None:
    _start(mixer)
    mixer.auto("me-1")
    mixer.set_lever("me-1", 0.6, "bottom")
    status, ignored = mixer.set_lever("me-1", 0.99, "top")
    assert ignored == "stale_from" and mixer.tbar_state()["me-1"]["position"] == 0.4


def test_two_hardware_levers_are_told_apart_by_source(mixer: VisionMixer) -> None:
    _start(mixer)
    assert mixer.set_lever("me-1", 0.0, source="panel-a")[1] is None  # A picks up at the top
    assert mixer.set_lever("me-1", 0.3, source="panel-b")[1] == "pickup"  # B has not
    assert mixer.set_lever("me-1", 0.3, source="panel-a")[1] is None
    assert mixer.tbar_state()["me-1"]["position"] == 0.3


def test_ignored_lever_values_say_why(client: TestClient, mixer: VisionMixer) -> None:
    _start(mixer)
    response = client.post("/api/v1/mixer/position", json={"panel_id": "me-1", "lever": 0.5})
    assert response.status_code == 200 and response.json()["status"] == "ignored" and response.json()["reason"] == "pickup"
    response = client.post("/api/v1/mixer/position", json={"panel_id": "me-1", "lever": 0.5, "from": "bottom"})
    assert response.json()["status"] == "ignored" and response.json()["reason"] == "stale_from"
    response = client.post("/api/v1/mixer/position", json={"panel_id": "me-1", "lever": 0.5, "from": "top"})
    assert response.json()["status"] == "position" and response.json()["reason"] is None


def test_the_snap_edges_are_inclusive(mixer: VisionMixer) -> None:
    _start(mixer)
    assert mixer.set_lever("me-1", 0.02, "top")[1] is None and mixer.tbar_state()["me-1"]["position"] == 0.0  # 2 %: nothing starts
    mixer.set_lever("me-1", 0.5, "top")
    mixer.set_lever("me-1", 0.98, "top")  # exactly 2 % from the end: complete
    assert _bus(mixer) == ("cam-2", "cam-1")
    mixer.set_lever("me-1", 0.5, "bottom")
    mixer.set_lever("me-1", 0.98, "bottom")  # exactly 2 % from the start: back
    assert mixer.tbar_state()["me-1"]["position"] == 0.0 and _bus(mixer) == ("cam-2", "cam-1")


def test_at_most_64_hardware_levers_per_me_are_kept(mixer: VisionMixer) -> None:
    from flowxer.engine.mixer import TBAR_SOURCES

    _start(mixer)
    for n in range(TBAR_SOURCES + 10):
        mixer.set_lever("me-1", 0.0, source=f"panel-{n}")
    assert len([item for item in mixer._lever_picked if item[0] == "me-1"]) == TBAR_SOURCES
    # The oldest went (it picks up again); the newest are kept.
    assert mixer.set_lever("me-1", 0.5, source="panel-0")[1] == "pickup"
    assert mixer.set_lever("me-1", 0.5, source=f"panel-{TBAR_SOURCES + 9}")[1] is None


def test_the_source_cap_keeps_recent_levers_and_the_one_that_drives(mixer: VisionMixer) -> None:
    from flowxer.engine.mixer import TBAR_SOURCES

    _start(mixer)
    # panel-0 picks up and holds a transition at 0.4.
    mixer.set_lever("me-1", 0.0, source="panel-0")
    mixer.set_lever("me-1", 0.4, source="panel-0")
    # 64 other levers pick up where the lever is now (0.4): panel-0 is now the least recently
    # used, but it drives the transition, so the others go first.
    for n in range(1, TBAR_SOURCES + 1):
        assert mixer.set_lever("me-1", 0.4, source=f"panel-{n}")[1] is None
    assert len([item for item in mixer._lever_picked if item[0] == "me-1"]) == TBAR_SOURCES
    assert mixer.set_lever("me-1", 0.6, source="panel-0")[1] is None and mixer.tbar_state()["me-1"]["position"] == 0.6
    assert mixer.set_lever("me-1", 0.9, source="panel-1")[1] == "pickup"  # the least recently used went
    # Every accepted value refreshes recency: panel-2 (now the oldest) sends one and survives.
    assert mixer.set_lever("me-1", 0.6, source="panel-2")[1] is None
    for n in range(100, 103):
        mixer.set_lever("me-1", 0.6, source=f"panel-{n}")
    assert mixer.set_lever("me-1", 0.6, source="panel-2")[1] is None
    assert mixer.set_lever("me-1", 0.9, source="panel-3")[1] == "pickup"


def test_a_lever_picks_up_where_a_held_transition_is(mixer: VisionMixer) -> None:
    _start(mixer)
    mixer.set_lever("me-1", 0.5, "top")  # a console holds 50 %
    # A hardware lever connecting at the top would pull it back: ignored until it reaches 0.5.
    assert mixer.set_lever("me-1", 0.0, source="hw")[1] == "pickup"
    assert mixer.tbar_state()["me-1"]["position"] == 0.5
    assert mixer.set_lever("me-1", 0.51, source="hw")[1] is None  # caught: it takes over
    assert mixer.set_lever("me-1", 0.8, source="hw")[1] is None and mixer.tbar_state()["me-1"]["position"] == 0.8
