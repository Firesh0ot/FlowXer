"""P3 M1: the transition type per ME (mix, wipe, stinger), Auto, takes with a type, flip-flop,
the engine commands and the tally of wipes (docs/engine-p3.md §3, §4)."""

import time

from fastapi.testclient import TestClient

from flowxer.api.schemas import MixerStartRequest, PanelTransitionUpdate, TransitionType, WipeSettingsUpdate
from flowxer.engine.mixer import MixerError, VisionMixer
from test_tally_export import lit


def _record_engine(mixer: VisionMixer) -> list[tuple[str, str, dict]]:
    calls: list[tuple[str, str, dict]] = []
    mixer._engine_call = lambda method, path, body, client=None: calls.append((method, path, body))
    return calls


def _wipe(mixer: VisionMixer, **wipe) -> None:
    mixer.update_transition("me-1", PanelTransitionUpdate(type=TransitionType.wipe, rate_frames=30, wipe=WipeSettingsUpdate(**wipe)))


def test_the_transition_api(client: TestClient) -> None:
    default = client.get("/api/v1/mixer").json()["panels"][0]["transition"]
    assert default == {
        "type": "mix",
        "rate_frames": 25,
        "wipe": {"pattern": 1, "reverse": False, "alternate": False, "soft": 0.0, "border": 0.0, "border_color": "#ffffff"},
        "stinger_slot_id": None,
    }
    reply = client.patch(
        "/api/v1/panels/me-1/transition",
        json={"type": "wipe", "rate_frames": 40, "wipe": {"pattern": 119, "soft": 0.05, "border_color": "#FF8000"}},
    )
    assert reply.status_code == 200
    assert reply.json()["wipe"] == {
        "pattern": 119,
        "reverse": False,
        "alternate": False,
        "soft": 0.05,
        "border": 0.0,
        "border_color": "#FF8000",
    }
    # A partial update keeps the rest.
    client.patch("/api/v1/panels/me-1/transition", json={"wipe": {"reverse": True}})
    panel = client.get("/api/v1/mixer").json()["panels"][0]["transition"]
    assert panel["type"] == "wipe" and panel["rate_frames"] == 40 and panel["wipe"]["pattern"] == 119 and panel["wipe"]["reverse"]
    for bad in ({"wipe": {"pattern": 7}}, {"wipe": {"soft": 1.5}}, {"rate_frames": 0}, {"type": "cut"}, {"wipe": {"border_color": "red"}}):
        assert client.patch("/api/v1/panels/me-1/transition", json=bad).status_code == 409, bad
    assert client.patch("/api/v1/panels/me-1/transition", json={"stinger_slot_id": "nope"}).status_code == 404
    assert client.patch("/api/v1/panels/me-9/transition", json={"type": "mix"}).status_code == 404


def test_auto_runs_the_mes_transition(client: TestClient, mixer: VisionMixer) -> None:
    mixer.start(MixerStartRequest(program_input_id="cam-1", preview_input_id="cam-2"))
    calls = _record_engine(mixer)
    # Mix at the ME's rate (25), or the given frames; the buses swap.
    assert client.post("/api/v1/mixer/auto", json={"panel_id": "me-1"}).status_code == 200
    assert calls[-1] == ("POST", "/me/1/take", {"source": "in2", "frames": 25, "preview": "in1"})
    assert mixer.program_input_id == "cam-2" and mixer.preview_input_id == "cam-1"
    assert mixer.last_transition == "mix"
    client.post("/api/v1/mixer/auto", json={"frames": 8})
    assert calls[-1] == ("POST", "/me/1/take", {"source": "in1", "frames": 8, "preview": "in2"})
    # A wipe: the engine gets the ME's wipe first, then the take with type wipe.
    _wipe(mixer, pattern=201, soft=0.1, border=0.02, border_color="#00ff00")
    assert calls[-1] == (
        "POST",
        "/me/1/transition",
        {"type": "wipe", "frames": 30, "pattern": 201, "reverse": False, "soft": 0.1, "border": 0.02, "border_color": "#00ff00"},
    )
    mixer.auto("me-1")
    assert calls[-1] == ("POST", "/me/1/take", {"source": "in2", "frames": 30, "preview": "in1", "type": "wipe"})
    assert mixer.last_transition == "wipe" and mixer.transition_counts["wipe"] == 1
    # A stinger is a cut in this version.
    mixer.update_transition("me-1", PanelTransitionUpdate(type=TransitionType.stinger))
    mixer.auto("me-1")
    assert calls[-1] == ("POST", "/me/1/take", {"source": "in1", "frames": 0, "preview": "in2"})
    assert mixer.program_input_id == "cam-1"
    assert client.post("/api/v1/mixer/auto", json={"panel_id": "me-9"}).status_code == 409
    mixer.stop()


def test_flip_flop_reverses_the_next_wipe(mixer: VisionMixer) -> None:
    mixer.start(MixerStartRequest(program_input_id="cam-1", preview_input_id="cam-2"))
    calls = _record_engine(mixer)
    _wipe(mixer, pattern=1, alternate=True)
    mixer.auto("me-1")
    # The running wipe keeps its direction; the next one goes back.
    take = calls.index(("POST", "/me/1/take", {"source": "in2", "frames": 30, "preview": "in1", "type": "wipe"}))
    assert calls[take + 1][1] == "/me/1/transition" and calls[take + 1][2]["reverse"] is True
    assert mixer.panels[0].transition.wipe.reverse
    mixer.auto("me-1")
    assert calls[-1][2]["reverse"] is False and not mixer.panels[0].transition.wipe.reverse
    # A mix leaves the direction alone; without alternate it stays.
    mixer.fade("me-1")
    assert not mixer.panels[0].transition.wipe.reverse
    _wipe(mixer, alternate=False, reverse=True)
    mixer.auto("me-1")
    assert mixer.panels[0].transition.wipe.reverse
    mixer.stop()


def test_take_with_a_wipe(mixer: VisionMixer) -> None:
    mixer.start(MixerStartRequest(program_input_id="cam-1", preview_input_id="cam-2"))
    calls = _record_engine(mixer)
    _wipe(mixer, pattern=101)
    # Without a duration: the ME's rate; with frames or duration_ms: those.
    mixer.take("cam-3", TransitionType.wipe)
    assert calls[-1] == ("POST", "/me/1/take", {"source": "in3", "frames": 30, "type": "wipe"})
    mixer.take("cam-4", TransitionType.wipe, frames=12)
    assert calls[-1] == ("POST", "/me/1/take", {"source": "in4", "frames": 12, "type": "wipe"})
    mixer.take("cam-1", TransitionType.wipe, duration_ms=200)
    assert calls[-1] == ("POST", "/me/1/take", {"source": "in1", "frames": 10, "type": "wipe"})
    # A mix take with frames.
    mixer.take("cam-2", TransitionType.mix, frames=7)
    assert calls[-1] == ("POST", "/me/1/take", {"source": "in2", "frames": 7})
    assert mixer.preview_input_id == "cam-2"
    mixer.stop()


def test_the_engine_gets_every_mes_transition_when_it_starts(settings) -> None:
    settings.panels = 2
    mixer = VisionMixer(settings)
    mixer.start(MixerStartRequest(program_input_id="cam-1", preview_input_id="cam-2"))
    mixer.update_transition("me-2", PanelTransitionUpdate(type=TransitionType.wipe, wipe=WipeSettingsUpdate(pattern=45)))
    calls = _record_engine(mixer)
    mixer._on_engine_ready(None)
    pushed = {path: body for _method, path, body in calls if path.endswith("/transition")}
    assert pushed["/me/1/transition"]["type"] == "mix"
    assert pushed["/me/2/transition"]["type"] == "wipe" and pushed["/me/2/transition"]["pattern"] == 45
    mixer.stop()
    # The transition is part of the saved state.
    again = VisionMixer(settings)
    again._apply_state(mixer.export_state(), checked=False)
    assert again.panels[1].transition.wipe.pattern == 45


def test_a_wipe_lights_both_sources_until_it_ends(mixer: VisionMixer) -> None:
    mixer.start(MixerStartRequest(program_input_id="cam-1", preview_input_id="cam-2"))
    _wipe(mixer, pattern=119)
    mixer.auto("me-1")
    assert lit(mixer) == ({"cam-1", "cam-2"}, {"cam-1"})
    # 30 frames at 50 fps.
    assert lit(mixer, now=time.monotonic() + 0.7) == ({"cam-2"}, {"cam-1"})
    mixer.stop()


def test_a_bad_transition_is_refused(mixer: VisionMixer) -> None:
    try:
        mixer.update_transition("me-1", PanelTransitionUpdate(wipe=WipeSettingsUpdate(pattern=999)))
    except MixerError as exc:
        assert "pattern" in str(exc)
    else:
        raise AssertionError("pattern 999 was accepted")
    assert mixer.panels[0].transition.wipe.pattern == 1
