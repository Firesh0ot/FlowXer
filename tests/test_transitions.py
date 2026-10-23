"""P3: the transition type per ME (mix, wipe, stinger), Auto, takes with a type, flip-flop, the
T-bar, the engine commands and the tally of wipes (M1, docs/engine-p3.md §3, §4); the keyers per ME
from the environment and the P2 state (M2, §5). P4 M3: keyer ties and BKGD (docs/engine-p4.md §5)."""

import time

import pytest
from fastapi.testclient import TestClient

from flowxer.api.schemas import MixerStartRequest, PanelTransitionUpdate, TransitionType, WipeSettingsUpdate, WorkspaceUpdate
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
        "ties": [],
        "background": True,
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
        {"type": "wipe", "frames": 30, "pattern": 201, "reverse": False, "soft": 0.1, "border": 0.02, "border_color": "#00ff00", "ties": []},
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
    # After its 30 frames at 50 fps (the mixer's own clock decides what is visible).
    outgoing, incoming, _end = mixer._mixes["me-1"]
    mixer._mixes["me-1"] = (outgoing, incoming, time.monotonic() - 0.01)
    assert lit(mixer) == ({"cam-2"}, {"cam-1"})
    mixer.stop()


def test_a_bad_transition_is_refused(mixer: VisionMixer) -> None:
    try:
        mixer.update_transition("me-1", PanelTransitionUpdate(wipe=WipeSettingsUpdate(pattern=999)))
    except MixerError as exc:
        assert "pattern" in str(exc)
    else:
        raise AssertionError("pattern 999 was accepted")
    assert mixer.panels[0].transition.wipe.pattern == 1


def test_the_t_bar(client: TestClient, mixer: VisionMixer) -> None:
    mixer.start(MixerStartRequest(program_input_id="cam-1", preview_input_id="cam-2"))
    calls = _record_engine(mixer)
    _wipe(mixer, pattern=119, alternate=True)
    # Off 0: the ME's wipe starts and is held; both sources are on air until it ends.
    assert client.post("/api/v1/mixer/position", json={"position": 0.3}).status_code == 200
    assert calls[-1] == ("POST", "/me/1/position", {"t": 0.3})
    assert mixer.program_input_id == "cam-1" and lit(mixer)[0] == {"cam-1", "cam-2"}
    assert lit(mixer, now=time.monotonic() + 60)[0] == {"cam-1", "cam-2"}
    mixer.set_position("me-1", 0.7)
    # 1 completes it: the buses swap, flip-flop reverses the next wipe.
    mixer.set_position("me-1", 1.0)
    assert ("POST", "/me/1/position", {"t": 1.0}) in calls
    assert not any(path == "/me/1/take" for _method, path, _body in calls)
    assert mixer.program_input_id == "cam-2" and mixer.preview_input_id == "cam-1"
    assert mixer.panels[0].transition.wipe.reverse and mixer.last_transition == "wipe"
    time.sleep(0.05)  # its last frame (the mixer's own clock decides what is visible)
    assert lit(mixer) == ({"cam-2"}, {"cam-1"})
    # Back to 0 ends it without the swap.
    mixer.set_position("me-1", 0.4)
    mixer.set_position("me-1", 0.0)
    assert calls[-1] == ("POST", "/me/1/position", {"t": 0.0})
    assert mixer.program_input_id == "cam-2" and lit(mixer) == ({"cam-2"}, {"cam-1"})
    # Auto from a held position: the engine runs the rest (0.5 of 30 frames).
    mixer.set_position("me-1", 0.5)
    mixer.auto("me-1")
    assert calls[-1][1] == "/me/1/transition" and calls[-2] == ("POST", "/me/1/auto", {"frames": 30})
    assert mixer.program_input_id == "cam-1" and mixer.preview_input_id == "cam-2"
    assert lit(mixer)[0] == {"cam-1", "cam-2"}
    # Its rest (15 frames, 0.3 s) over (the mixer's own clock decides what is visible).
    outgoing, incoming, _end = mixer._mixes["me-1"]
    mixer._mixes["me-1"] = (outgoing, incoming, time.monotonic() - 0.01)
    assert lit(mixer)[0] == {"cam-1"}
    # A Cut at a held position completes it (the engine ends it as if it had run to its end).
    mixer.set_position("me-1", 0.5)
    mixer.cut("me-1")
    assert calls[-1] == ("POST", "/me/1/take", {"source": "in2", "frames": 0})
    assert mixer.program_input_id == "cam-2" and mixer.preview_input_id == "cam-1"
    # A stinger does not scrub: off 0 it runs as an Auto (a cut in this version).
    mixer.update_transition("me-1", PanelTransitionUpdate(type=TransitionType.stinger))
    mixer.set_position("me-1", 0.2)
    assert calls[-1] == ("POST", "/me/1/take", {"source": "in1", "frames": 0, "preview": "in2"})
    assert client.post("/api/v1/mixer/position", json={"position": 1.2}).status_code == 422
    mixer.stop()


def test_a_p2_state_keeps_dsk_1(settings) -> None:
    # P2 saved one DSK on ME 1 (downstream_keyer_count, a keyer without ME or stage).
    mixer = VisionMixer(settings)
    state = mixer.export_state()
    state["workspace"].pop("keyers_per_me")
    state["workspace"].pop("dsks_per_me")
    state["workspace"]["downstream_keyer_count"] = 1
    state["keyers"] = [{"id": "dsk-1", "label": "Graphics", "enabled": True, "rate_ms": 800}]
    again = VisionMixer(settings)
    again._apply_state(state, checked=False)
    assert again.workspace.dsks_per_me == 1 and again.workspace.keyers_per_me == 2
    dsk = again.get_keyer("dsk-1")
    assert (dsk.panel_id, dsk.stage.value, dsk.label, dsk.enabled, dsk.rate_ms) == ("me-1", "downstream", "Graphics", True, 800)
    assert [keyer.id for keyer in again.keyers] == ["me-1-key-1", "me-1-key-2", "dsk-1"]


@pytest.mark.parametrize("on", [False, True])
def test_the_saved_p2_state_keeps_dsk_1_on_or_off(settings, on: bool) -> None:
    # small's test-all: the state.json P2 (14.21.49) wrote, DSK 1 not routed; P3 starts on it with
    # FLOWXER_KEYERS_PER_ME=2, FLOWXER_DSKS_PER_ME=1. DSK 1 stays as it was, in FlowXer and on the
    # engine; the keyers P2 did not have start off.
    settings.keyers_per_me, settings.dsks_per_me, settings.stinger_players = 2, 1, 1
    p2 = VisionMixer(settings)
    state = p2.export_state()
    state["flowxer_version"] = "14.21.49"
    state["workspace"] = {
        "downstream_keyer_count": 1, "format_id": "1080p50", "logical_source_count": 7, "mixer_panel_count": 1,
        "source_tile_aspect": "16:9", "stinger_count": 1, "stinger_mode": "shared",
    }
    state["panels"] = [
        {"id": "me-1", "label": "ME 1", "last_transition": "cut", "preview_input_id": "cam-2", "program_input_id": "cam-1", "wipe_armed": False}
    ]
    state["keyers"] = [{"enabled": on, "id": "dsk-1", "label": "DSK 1", "rate_ms": 500}]
    p2._store.save(state)
    p3 = VisionMixer(settings)
    assert [(keyer.id, keyer.enabled) for keyer in p3.keyers] == [("me-1-key-1", False), ("me-1-key-2", False), ("dsk-1", on)]
    calls = _record_engine(p3)
    p3._on_engine_ready(None)
    assert [(path, body) for _, path, body in calls if path.startswith("/keyer/") and "on" in body] == [
        ("/keyer/1", {"on": False}), ("/keyer/2", {"on": False}), ("/keyer/3", {"on": on}),
    ]


def test_keyer_counts_from_the_environment(settings) -> None:
    settings.panels = 2
    settings.keyers_per_me = 1
    settings.dsks_per_me = 2
    mixer = VisionMixer(settings)
    assert [keyer.id for keyer in mixer.keyers] == ["me-1-key-1", "dsk-1", "dsk-2", "me-2-key-1", "me-2-dsk-1", "me-2-dsk-2"]
    assert [keyer.receiver_label for keyer in mixer.keyers] == [
        "ME 1 Key 1", "DSK 1 Key", "DSK 2 Key", "ME 2 Key 1", "ME 2 DSK 1 Key", "ME 2 DSK 2 Key",
    ]
    # Pinned: the counts and the labels.
    with pytest.raises(MixerError, match="FLOWXER_KEYERS_PER_ME"):
        mixer.apply_workspace(WorkspaceUpdate(keyers_per_me=3))
    with pytest.raises(MixerError, match="labels are fixed"):
        mixer.update_keyer("dsk-1", label="Graphics")
    config = mixer._engine_config("test")["FLOWXER_ENGINE_KEYS"]
    assert config[4] == {"label": "ME 2 DSK 1 Key", "me": 2, "stage": "downstream"}


# ---- keyer ties (P4 M3) ----------------------------------------------------------------------


def _ties(mixer: VisionMixer, *ties: str, background: bool = True) -> None:
    mixer.update_transition("me-1", PanelTransitionUpdate(ties=list(ties), background=background))


def test_keyer_ties_api(client: TestClient, mixer: VisionMixer) -> None:
    calls = _record_engine(mixer)
    mixer.start(MixerStartRequest(program_input_id="cam-1", preview_input_id="cam-2"))
    # The ME's upstream keyers, in their order; the engine gets their numbers.
    reply = client.patch("/api/v1/panels/me-1/transition", json={"ties": ["me-1-key-2", "me-1-key-1", "me-1-key-2"]})
    assert reply.status_code == 200 and reply.json()["ties"] == ["me-1-key-1", "me-1-key-2"] and reply.json()["background"]
    assert calls[-1][1] == "/me/1/transition" and calls[-1][2]["ties"] == [1, 2]
    # A partial update keeps them; BKGD off needs a tie; DSKs and unknown keyers are refused.
    assert client.patch("/api/v1/panels/me-1/transition", json={"background": False}).json()["ties"] == ["me-1-key-1", "me-1-key-2"]
    for bad in ({"ties": ["dsk-1"]}, {"ties": ["me-2-key-1"]}, {"ties": []}):
        assert client.patch("/api/v1/panels/me-1/transition", json=bad).status_code == 409, bad
    assert client.patch("/api/v1/panels/me-1/transition", json={"ties": [], "background": True}).json()["ties"] == []
    # Ties are part of the saved state; keyers that go take their ties with them.
    _ties(mixer, "me-1-key-2", background=False)
    mixer.stop()
    again = VisionMixer(mixer.settings)
    again._apply_state(mixer.export_state(), checked=False)
    assert again.panels[0].transition.ties == ["me-1-key-2"] and not again.panels[0].transition.background
    again.apply_workspace(WorkspaceUpdate(keyers_per_me=1))
    assert again.panels[0].transition.ties == [] and again.panels[0].transition.background


def test_cut_and_auto_take_the_tied_keyers_along(mixer: VisionMixer) -> None:
    mixer.start(MixerStartRequest(program_input_id="cam-1", preview_input_id="cam-2"))
    mixer.update_keyer("me-1-key-1", mode="pip", source_id="cam-3")
    mixer.update_keyer("me-1-key-2", mode="pip", source_id="cam-4", fly=True)
    mixer.set_keyframes("me-1-key-2", [{"x": -0.3}, {"x": 0.6, "frames": 20}])
    _ties(mixer, "me-1-key-1", "me-1-key-2")
    calls = _record_engine(mixer)
    # Auto: one take with ties; the keyers are on (the fly at its last keyframe), lit at once.
    mixer.auto("me-1")
    assert calls[-1] == ("POST", "/me/1/take", {"source": "in2", "frames": 25, "preview": "in1", "ties": True})
    one, two = mixer.get_keyer("me-1-key-1"), mixer.get_keyer("me-1-key-2")
    assert one.enabled and two.enabled and two.dme.x == 0.6
    assert {"cam-3", "cam-4"} <= lit(mixer)[0]
    # Auto again: off, still lit for the 25 frames of the mix, the fly at keyframe 0.
    mixer.auto("me-1")
    assert not one.enabled and not two.enabled and two.dme.x == -0.3 and {"cam-3", "cam-4"} <= lit(mixer)[0]
    assert 0.4 < mixer._key_until["me-1-key-1"] - time.monotonic() <= 0.5
    for keyer_id in ("me-1-key-1", "me-1-key-2"):
        mixer._key_until[keyer_id] = time.monotonic() - 0.01
    assert not {"cam-3", "cam-4"} & lit(mixer)[0]
    # Cut: a take with ties, the keyers on at once; a direct take leaves them alone.
    mixer.cut("me-1")
    assert calls[-1] == ("POST", "/me/1/take", {"source": "in2", "frames": 0, "preview": "in1", "ties": True})
    assert one.enabled and two.enabled and "me-1-key-1" not in mixer._key_until
    mixer.take("cam-5")
    assert calls[-1] == ("POST", "/me/1/take", {"source": "in5", "frames": 0}) and one.enabled
    # Without ties nothing changes.
    _ties(mixer)
    mixer.cut("me-1")
    assert "ties" not in calls[-1][2] and one.enabled
    mixer.stop()


def test_key_only_and_the_t_bar_with_ties(mixer: VisionMixer) -> None:
    mixer.start(MixerStartRequest(program_input_id="cam-1", preview_input_id="cam-2"))
    mixer.update_keyer("me-1-key-1", mode="pip", source_id="cam-3", rate_ms=200)
    calls = _record_engine(mixer)
    # BKGD off: Cut turns the tied keyer on at the next frame, Auto runs its own Auto (its rate);
    # the buses stay.
    _ties(mixer, "me-1-key-1", background=False)
    mixer.cut("me-1")
    assert calls[-1] == ("POST", "/keyer/1", {"on": True}) and mixer.get_keyer("me-1-key-1").enabled
    mixer.auto("me-1")
    assert calls[-1] == ("POST", "/keyer/1/auto", {"frames": 10}) and not mixer.get_keyer("me-1-key-1").enabled
    assert not any(path == "/me/1/take" for _method, path, _body in calls) and mixer.program_input_id == "cam-1"
    # Key only, the T-bar off 0 runs it as an Auto.
    mixer.set_position("me-1", 0.4)
    assert calls[-1] == ("POST", "/keyer/1/auto", {"frames": 10}) and mixer.get_keyer("me-1-key-1").enabled
    mixer.update_keyer("me-1-key-1", enabled=False)
    # With BKGD the engine runs the tied keyer with the T-bar: on air while it is held, on when it
    # completes; back at 0 as it was.
    _ties(mixer, "me-1-key-1")
    mixer.set_position("me-1", 0.3)
    assert calls[-1] == ("POST", "/me/1/position", {"t": 0.3}) and not mixer.get_keyer("me-1-key-1").enabled
    assert "cam-3" in lit(mixer)[0]
    mixer.set_position("me-1", 0.0)
    assert "cam-3" not in lit(mixer)[0] and not mixer.get_keyer("me-1-key-1").enabled
    mixer.set_position("me-1", 0.6)
    mixer.set_position("me-1", 1.0)
    assert mixer.get_keyer("me-1-key-1").enabled and mixer.program_input_id == "cam-2" and "cam-3" in lit(mixer)[0]
    # Auto from a held position: the keyer goes off with the rest (0.5 of 25 frames).
    mixer.set_position("me-1", 0.5)
    mixer.auto("me-1")
    assert calls[-1] == ("POST", "/me/1/auto", {"frames": 25}) and not mixer.get_keyer("me-1-key-1").enabled
    assert 0.2 < mixer._key_until["me-1-key-1"] - time.monotonic() <= 0.26
    # A take while the T-bar holds it ends it as at its end (the keyer turns on).
    mixer.set_position("me-1", 0.5)
    mixer.take("cam-4")
    assert mixer.get_keyer("me-1-key-1").enabled and mixer.panels[0].program_input_id == "cam-4"
    mixer.stop()
