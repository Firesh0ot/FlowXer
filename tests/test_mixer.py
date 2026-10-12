import time
from types import SimpleNamespace

import pytest

from flowxer.api.metrics import render_prometheus
from flowxer.api.schemas import (
    Dme,
    DmeLoop,
    DmePreset,
    InputKind,
    Keyer,
    LogicalInputCreate,
    MixerStartRequest,
    MixerState,
    TransitionType,
    WorkspaceUpdate,
)
from flowxer.domain import nmos
from flowxer.engine.mixer import MixerError, VisionMixer


def _record_engine(mixer: VisionMixer, reply: dict | None = None) -> list[tuple[str, str, dict]]:
    """The commands the mixer would send to a running engine (each answered with `reply`)."""
    calls: list[tuple[str, str, dict]] = []

    def call(method, path, body, client=None):
        calls.append((method, path, body))
        return reply

    mixer._engine_call = call
    return calls


def test_start_publishes_uncompressed_output_flows(mixer: VisionMixer) -> None:
    status = mixer.start(MixerStartRequest(program_input_id="cam-1"))
    assert status.state.value == "running"
    assert status.backend == "simulate"
    assert status.outputs is not None
    assert status.outputs.media_type_video == "video/v210"
    assert status.outputs.media_type_audio == "audio/float32"
    assert status.outputs.nmos_video["media_type"] == "video/v210"
    assert status.outputs.nmos_video["frame_width"] == 64
    assert status.outputs.nmos_audio["bit_depth"] == 32
    mixer.stop()


def test_me1_program_keeps_flowxers_flow_ids_and_every_me_has_its_own(settings) -> None:
    # Downstream activations on ME 1 PGM survive the upgrade; ME n no longer advertises ME 1's flow.
    settings.panels = 3
    mixer = VisionMixer(settings)
    status = mixer.start(MixerStartRequest(program_input_id="cam-1"))
    key = dict(seed=settings.resolved_nmos_seed, width=64, height=36, frame_rate_num=50, frame_rate_den=1)
    assert status.outputs.video_flow_id == nmos.flow_uuid(settings.group_hint, "video", **key)
    assert status.outputs.audio_flow_id == nmos.flow_uuid(settings.group_hint, "audio", **key)
    assert [item.panel_id for item in status.me_outputs] == ["me-1", "me-2", "me-3"]
    ids = [flow for item in status.me_outputs for flow in (item.pgm_video_flow_id, item.pgm_audio_flow_id, item.pvw_video_flow_id)]
    assert len(set(ids)) == 9 and None not in ids
    assert status.me_outputs[0].pgm_video_flow_id == status.outputs.video_flow_id
    mixer.stop()


def test_render_mes_and_pvw_outputs(settings) -> None:
    settings.panels = 4
    settings.render_mes = 2
    settings.pvw_outputs = False
    mixer = VisionMixer(settings)
    assert mixer.render_count == 2
    status = mixer.start()
    assert [item.panel_id for item in status.me_outputs] == ["me-1", "me-2"]
    assert all(item.pvw_video_flow_id is None for item in status.me_outputs)
    assert status.render_mes == 2
    mixer.stop()
    # More than there are panels: every panel.
    settings.render_mes = 4
    settings.panels = 2
    assert VisionMixer(settings).render_count == 2


def test_take_switches_program(mixer: VisionMixer) -> None:
    mixer.start(MixerStartRequest(program_input_id="cam-1"))
    calls = _record_engine(mixer)
    mixer.take("cam-2")
    assert mixer.program_input_id == "cam-2"
    assert mixer.program_bus.value == "live"
    assert calls[-1] == ("POST", "/me/1/take", {"source": "in2", "frames": 0})
    mixer.stop()


def test_slot_order_is_live_clip_players_black(settings) -> None:
    settings.live_inputs = 3
    settings.clip_players = 2
    mixer = VisionMixer(settings)
    inputs = mixer.list_inputs()
    assert [(item.id, item.kind.value, item.slot) for item in inputs] == [
        ("cam-1", "mxl_live", 0),
        ("cam-2", "mxl_live", 1),
        ("cam-3", "mxl_live", 2),
        ("clip-1", "clip_player", 3),
        ("clip-2", "clip_player", 4),
        ("black", "black", 5),
    ]
    assert [mixer._engine_source(item.id) for item in inputs] == ["in1", "in2", "in3", "cp1", "cp2", "black"]
    assert inputs[3].label == "Clip 1"


def test_reentry_takes_the_program_of_a_higher_rendering_me(settings) -> None:
    settings.panels = 3
    settings.render_mes = 2
    mixer = VisionMixer(settings)
    mixer.start()
    calls = _record_engine(mixer)
    mixer.set_preview("me-2", panel_id="me-1")
    assert calls[-1] == ("POST", "/me/1/preview", {"source": "me2"})
    mixer.cut("me-1")
    assert mixer.panels[0].program_input_id == "me-2"
    assert calls[-1][2]["source"] == "me2"
    # ME 2 may not take ME 1, and ME 3 does not render: nobody may take it.
    with pytest.raises(MixerError, match="re-entry"):
        mixer.set_preview("me-1", panel_id="me-2")
    with pytest.raises(MixerError, match="re-entry"):
        mixer.take("me-3", panel_id="me-1")
    # A non-rendering ME is controlled (and tallied) without engine commands.
    count = len(calls)
    mixer.take("cam-1", panel_id="me-3")
    assert mixer.panels[2].program_input_id == "cam-1" and len(calls) == count
    mixer.stop()


def test_fade_to_black_and_back(mixer: VisionMixer) -> None:
    mixer.start(MixerStartRequest(program_input_id="cam-1", preview_input_id="cam-2"))
    calls = _record_engine(mixer)
    # The whole output fades (DSKs and sound too); the buses stay.
    mixer.fade_to_black("me-1", duration_ms=600)
    assert mixer.program_input_id == "cam-1" and mixer.preview_input_id == "cam-2" and mixer.panels[0].ftb
    assert calls[-1] == ("POST", "/me/1/ftb/auto", {"frames": 30})
    mixer.fade_to_black("me-1", duration_ms=200)
    assert mixer.program_input_id == "cam-1" and not mixer.panels[0].ftb
    assert calls[-1] == ("POST", "/me/1/ftb/auto", {"frames": 10})
    # The engine gets the state when it starts.
    mixer.fade_to_black("me-1")
    mixer._on_engine_ready(None)
    assert ("POST", "/me/1/ftb", {"on": True}) in calls
    mixer.stop()


def test_a_stinger_is_a_cut_in_this_version(mixer: VisionMixer) -> None:
    mixer.start(MixerStartRequest(program_input_id="cam-1", preview_input_id="cam-2"))
    calls = _record_engine(mixer)
    mixer.play_stinger("replay-wipe", "cam-2", flip_flop=True, panel_id="me-1")
    assert mixer.program_input_id == "cam-2" and mixer.preview_input_id == "cam-1"
    assert calls[-1] == ("POST", "/me/1/take", {"source": "in2", "frames": 0, "preview": "in1"})
    mixer.take("cam-3", transition=TransitionType.stinger)
    assert mixer.program_input_id == "cam-3"
    assert mixer.status().stinger["phase"] == "idle"
    mixer.stop()


def test_legacy_input_kinds_become_clip_players(mixer: VisionMixer) -> None:
    # Saved states and API clients from before the engine: test, file and replay inputs.
    for kind in ("test", "file", "replay"):
        item = LogicalInputCreate(id=f"old-{kind}", label=kind, kind=kind)
        assert item.kind == InputKind.clip_player
    with pytest.raises(ValueError):
        LogicalInputCreate(id="x", label="x", kind="hdmi")


def test_keyers_per_me(mixer: VisionMixer) -> None:
    # Engine keyer numbers follow the list: ME 1 Key 1, Key 2, DSK 1.
    assert [keyer.id for keyer in mixer.keyers] == ["me-1-key-1", "me-1-key-2", "dsk-1"]
    calls = _record_engine(mixer)
    mixer.update_keyer("dsk-1", enabled=True)
    assert calls[-1] == ("POST", "/keyer/3", {"on": True})
    mixer.keyer_auto("dsk-1", duration_ms=500)
    assert calls[-1] == ("POST", "/keyer/3/auto", {"frames": 25})
    assert mixer.get_keyer("dsk-1").enabled is False
    # Auto without a duration takes the keyer's rate.
    mixer.update_keyer("dsk-1", rate_ms=1000)
    mixer.keyer_auto("dsk-1")
    assert calls[-1] == ("POST", "/keyer/3/auto", {"frames": 50})
    assert mixer.get_keyer("dsk-1").rate_ms == 1000 and mixer.get_keyer("dsk-1").enabled is True
    # A luma key of a camera on ME 1's upstream keyer 1.
    mixer.update_keyer("me-1-key-1", mode="luma", source_id="cam-2", clip=0.4, soft=0.05, invert=True)
    # The box stays as it is in the engine: no dme in the settings.
    assert calls[-1] == (
        "POST",
        "/keyer/1/settings",
        {"mode": "luma", "clip": 0.4, "soft": 0.05, "gain": 1.0, "invert": True, "fly": False, "source": "in2"},
    )
    with pytest.raises(MixerError, match="needs a source"):
        mixer.update_keyer("me-1-key-2", mode="luma")
    assert mixer.get_keyer("me-1-key-2").mode.value == "linear"
    with pytest.raises(MixerError, match="unknown keyer"):
        mixer.update_keyer("dsk-9", enabled=True)


def test_pip_and_dme_keyers(mixer: VisionMixer) -> None:
    calls = _record_engine(mixer)
    # A pip of cam-3 in a box with a border: the engine gets the source and the box fields to
    # change (it merges them into its box, which may be moving).
    mixer.update_keyer("me-1-key-1", mode="pip", source_id="cam-3", dme={"x": 0.75, "size": 0.4, "border": {"width": 4, "color": "#ff0000"}})
    method, path, body = calls[-1]
    assert (method, path, body["mode"], body["source"]) == ("POST", "/keyer/1/settings", "pip", "in3")
    assert body["dme"] == {"x": 0.75, "size": 0.4, "border": {"width": 4, "color": "#ff0000"}}
    assert mixer.get_keyer("me-1-key-1").dme.border.model_dump() == {"width": 4.0, "soft": 0.0, "color": "#ff0000", "opacity": 1.0}
    # A partial box keeps the other fields.
    keyer = mixer.update_keyer("me-1-key-1", dme={"y": 0.25, "shadow": {"opacity": 0.5}})
    assert keyer.dme.x == 0.75 and keyer.dme.y == 0.25 and keyer.dme.border.width == 4.0 and keyer.dme.shadow.opacity == 0.5
    # A box on a linear key; ranges; upstream keyers only; a pip needs a source.
    mixer.update_keyer("me-1-key-2", dme={"enabled": True, "crop": {"left": 0.2}})
    assert mixer.get_keyer("me-1-key-2").dme.enabled and calls[-1][2]["mode"] == "linear"
    with pytest.raises(MixerError, match="invalid key"):
        mixer.update_keyer("me-1-key-2", dme={"crop": {"right": 0.9}})
    with pytest.raises(MixerError, match="invalid key"):
        mixer.update_keyer("me-1-key-2", dme={"size": 3})
    with pytest.raises(MixerError, match="upstream"):
        mixer.update_keyer("dsk-1", mode="pip", source_id="cam-1")
    with pytest.raises(MixerError, match="upstream"):
        mixer.update_keyer("dsk-1", dme={"enabled": True})
    with pytest.raises(MixerError, match="needs a source"):
        mixer.update_keyer("me-1-key-2", mode="pip")
    assert mixer.get_keyer("me-1-key-2").dme.crop.right == 0.0 and mixer.get_keyer("dsk-1").mode.value == "linear"
    # A DSK's settings carry no box.
    mixer.update_keyer("dsk-1", gain=0.5)
    assert "dme" not in calls[-1][2]


def test_a_pip_is_always_a_box(mixer: VisionMixer) -> None:
    _record_engine(mixer)
    keyer = mixer.update_keyer("me-1-key-1", mode="pip", source_id="cam-2")
    assert keyer.dme.enabled is True
    with pytest.raises(MixerError, match="always a box"):
        mixer.update_keyer("me-1-key-1", dme={"enabled": False})
    # Leaving pip keeps the box on; it can be turned off then.
    assert mixer.update_keyer("me-1-key-1", mode="luma").dme.enabled is True
    assert mixer.update_keyer("me-1-key-1", dme={"enabled": False}).dme.enabled is False
    # A state saved by P4 M1 (a pip with dme.enabled false) loads as a box.
    saved = Keyer(id="me-1-key-1", label="ME 1 Key 1", mode="pip", source_id="cam-2", dme={"enabled": False, "x": 0.3})
    assert saved.dme.enabled is True and saved.dme.x == 0.3


def test_box_put_replaces_and_engine_reply_is_kept(mixer: VisionMixer) -> None:
    calls = _record_engine(mixer)
    mixer.update_keyer("me-1-key-1", mode="pip", source_id="cam-2", dme={"x": 0.2, "border": {"width": 6}, "shadow": {"opacity": 0.4}})
    # PUT replaces the whole box (defaults for the rest); the engine gets all of it.
    keyer = mixer.replace_keyer_box("me-1-key-1", Dme(x=0.6))
    assert keyer.dme.x == 0.6 and keyer.dme.border.width == 0.0 and keyer.dme.shadow.opacity == 0.0 and keyer.dme.enabled
    assert calls[-1][2]["dme"] == keyer.dme.model_dump()
    with pytest.raises(MixerError, match="upstream"):
        mixer.replace_keyer_box("dsk-1", Dme())
    # The box where the engine has it after a change (its reply), unless a run moves it.
    engine_box = {"x": 0.61, "y": 0.4, "size": 0.5, "soft": 0.0, "crop": {"left": 0.0, "right": 0.0, "top": 0.0, "bottom": 0.0},
                  "border": {"width": 0.0, "soft": 0.0, "opacity": 1.0, "ycbcr": [940, 512, 512]},
                  "shadow": {"dx": 0.0, "dy": 0.0, "blur": 0.0, "opacity": 0.0}}
    _record_engine(mixer, reply={"key": 1, "on": False, "dme": engine_box, "run": {"running": False}})
    assert mixer.update_keyer("me-1-key-1", dme={"y": 0.4}).dme.x == 0.61
    _record_engine(mixer, reply={"key": 1, "on": False, "dme": {**engine_box, "x": 0.9}, "run": {"running": True}})
    assert mixer.update_keyer("me-1-key-1", dme={"border": {"color": "#00ff00"}}).dme.x == 0.61


def test_keyframes_runs_and_fly(mixer: VisionMixer) -> None:
    calls = _record_engine(mixer)
    mixer.update_keyer("me-1-key-1", mode="pip", source_id="cam-3", dme={"y": 0.3, "border": {"width": 2}})
    # Missing box fields take the keyer's box; frames and ease per keyframe.
    keyer = mixer.set_keyframes("me-1-key-1", [{"x": -0.3, "frames": 10}, {"x": 0.5, "size": 0.25, "frames": 20, "ease": "ease-out"}])
    assert [(k.x, k.y, k.size, k.border.width, k.frames, k.ease.value) for k in keyer.keyframes] == [
        (-0.3, 0.3, 0.5, 2.0, 10, "linear"), (0.5, 0.3, 0.25, 2.0, 20, "ease-out")]
    method, path, body = calls[-1]
    assert (method, path, len(body["keyframes"])) == ("PUT", "/keyer/1/keyframes", 2)
    assert body["keyframes"][1] == keyer.keyframes[1].model_dump(mode="json") and "color" not in body["keyframes"][1]["border"]
    for bad, match in (([{}] * 9, "at most 8"), ([{"ease": "bounce"}], "invalid keyframe"), ([{"frames": 0}], "invalid keyframe")):
        with pytest.raises(MixerError, match=match):
            mixer.set_keyframes("me-1-key-1", bad)
    with pytest.raises(MixerError, match="upstream"):
        mixer.set_keyframes("dsk-1", [{}])
    # Runs: to the last keyframe (the box ends there), back to 0, a loop (the box stays), a jump.
    mixer.run_keyer("me-1-key-1")
    assert calls[-1] == ("POST", "/keyer/1/run", {"reverse": False, "loop": "off"}) and mixer.get_keyer("me-1-key-1").dme.x == 0.5
    mixer.run_keyer("me-1-key-1", reverse=True)
    assert calls[-1][2] == {"reverse": True, "loop": "off"} and mixer.get_keyer("me-1-key-1").dme.x == -0.3
    mixer.run_keyer("me-1-key-1", loop=DmeLoop.pingpong)
    assert calls[-1][2] == {"reverse": False, "loop": "pingpong"} and mixer.get_keyer("me-1-key-1").dme.x == -0.3
    mixer.run_keyer("me-1-key-1", to=1, frames=0)
    assert calls[-1][2] == {"reverse": False, "loop": "off", "to": 1, "frames": 0} and mixer.get_keyer("me-1-key-1").dme.size == 0.25
    mixer.stop_keyer("me-1-key-1")
    assert calls[-1] == ("POST", "/keyer/1/stop", {})
    with pytest.raises(MixerError, match="no keyframe 2"):
        mixer.run_keyer("me-1-key-1", to=2)
    with pytest.raises(MixerError, match="no box"):
        mixer.run_keyer("me-1-key-2")
    mixer.set_keyframes("me-1-key-1", [{}])
    with pytest.raises(MixerError, match="loop needs 2"):
        mixer.run_keyer("me-1-key-1", loop=DmeLoop.wrap)
    # Fly: Auto on ends at the last keyframe, Auto off at keyframe 0; cuts jump there too.
    mixer.set_keyframes("me-1-key-1", [{"x": -0.3}, {"x": 0.5, "frames": 20}])
    mixer.update_keyer("me-1-key-1", fly=True)
    assert calls[-1][2]["fly"] is True
    keyer = mixer.keyer_auto("me-1-key-1")
    assert keyer.enabled and keyer.dme.x == 0.5 and calls[-1] == ("POST", "/keyer/1/auto", {"frames": 25})
    keyer = mixer.keyer_auto("me-1-key-1")
    assert not keyer.enabled and keyer.dme.x == -0.3
    assert mixer.update_keyer("me-1-key-1", enabled=True).dme.x == 0.5
    # A stop during a fly off: the engine says the keyer stays on air.
    mixer.keyer_auto("me-1-key-1")
    _record_engine(mixer, reply={"key": 1, "on": True, "dme": {"x": 0.1}, "run": {"running": False}})
    keyer = mixer.stop_keyer("me-1-key-1")
    assert keyer.enabled and keyer.dme.x == 0.1 and keyer.run is not None and not keyer.run.running


def test_dme_presets(mixer: VisionMixer) -> None:
    calls = _record_engine(mixer)
    mixer.update_keyer("me-1-key-1", mode="pip", source_id="cam-2", rate_ms=400)
    assert [p.name.value for p in mixer.dme_presets()] == ["corner-tl", "corner-tr", "corner-bl", "corner-br", "split", "quad", "full"]
    # Off air: the box is there at once (the whole box to the engine); keyframe 0 off screen right.
    (keyer,) = mixer.apply_preset("me-1-key-1", DmePreset.corner_tr)
    mx = (0.05 * 64 + 4) / 64 + 1 / 6
    assert keyer.dme.x == pytest.approx(1 - mx) and keyer.dme.y == pytest.approx((0.05 * 36 + 4) / 36 + 1 / 6)
    assert keyer.dme.size == pytest.approx(1 / 3) and keyer.dme.border.width == 4.0 and keyer.dme.border.color == "#ffffff"
    assert keyer.dme.shadow.opacity == 0.5 and keyer.keyframes[0].x == pytest.approx(1 + 1 / 6 + 0.05)
    assert [(k.frames, k.ease.value) for k in keyer.keyframes] == [(20, "ease"), (20, "ease")]
    assert [c[1] for c in calls[-2:]] == ["/keyer/1/keyframes", "/keyer/1/settings"] and calls[-1][2]["dme"] == keyer.dme.model_dump()
    # On air: the box flies to keyframe 1 from where it is.
    mixer.update_keyer("me-1-key-1", enabled=True)
    (keyer,) = mixer.apply_preset("me-1-key-1", DmePreset.corner_bl)
    assert [c[1] for c in calls[-3:]] == ["/keyer/1/keyframes", "/keyer/1/settings", "/keyer/1/run"]
    assert calls[-2][2]["dme"] == {"enabled": True, "border": {"color": "#ffffff"}} and calls[-1][2] == {"to": 1, "reverse": False, "loop": "off"}
    assert keyer.dme.x == pytest.approx(mx) and keyer.keyframes[0].x == pytest.approx(-1 / 6 - 0.05)
    # Full: keyframe 0 is the box as it was.
    (keyer,) = mixer.apply_preset("me-1-key-1", DmePreset.full)
    assert keyer.keyframes[0].x == pytest.approx(mx) and keyer.keyframes[0].border.width == 4.0
    assert (keyer.dme.x, keyer.dme.y, keyer.dme.size) == (0.5, 0.5, 1.0)
    # Split: keyers 1 and 2 (a linear keyer gets its box on), each cropped to its centre half.
    left, right = mixer.apply_preset("me-1-key-2", DmePreset.split)
    assert (left.id, right.id) == ("me-1-key-1", "me-1-key-2") and right.dme.enabled
    assert (left.dme.x, right.dme.x, left.dme.size, left.dme.crop.left, right.dme.crop.right) == (0.25, 0.75, 1.0, 0.25, 0.25)
    assert left.dme.border.width == 0.0 and left.keyframes[0].x == -0.3 and right.keyframes[0].x == 1.3
    with pytest.raises(MixerError, match="quad needs 4 upstream keyers"):
        mixer.apply_preset("me-1-key-1", DmePreset.quad)
    with pytest.raises(MixerError, match="upstream"):
        mixer.apply_preset("dsk-1", DmePreset.full)
    mixer.apply_workspace(WorkspaceUpdate(keyers_per_me=4))
    keyers = mixer.apply_preset("me-1-key-3", DmePreset.quad)
    assert [(k.id, k.dme.x, k.dme.y, k.dme.size) for k in keyers] == [
        ("me-1-key-1", 0.25, 0.25, 0.5), ("me-1-key-2", 0.75, 0.25, 0.5), ("me-1-key-3", 0.25, 0.75, 0.5), ("me-1-key-4", 0.75, 0.75, 0.5)]


def test_keyers_show_the_engines_run(mixer: VisionMixer) -> None:
    _record_engine(mixer)
    mixer.update_keyer("me-1-key-1", mode="pip", source_id="cam-2", dme={"x": 0.2})
    keyer = mixer.get_keyer("me-1-key-1")
    entry = {"key": 1, "dme": {"x": 0.33, "y": 0.5, "border": {"width": 1.0, "ycbcr": [1, 2, 3]}},
             "run": {"running": True, "fly": True, "loop": "off", "keyframe": 1, "step": 7}}
    live = mixer._live_keyer(keyer, entry)
    assert live.dme.x == 0.33 and live.dme.border.width == 1.0 and live.dme.border.color == "#ffffff"
    assert live.run.model_dump() == {"running": True, "fly": True, "loop": DmeLoop.off, "keyframe": 1, "step": 7}
    # Not running: the stored box; no engine: no run.
    entry["run"]["running"] = False
    assert mixer._live_keyer(keyer, entry).dme.x == 0.2 and mixer._live_keyer(keyer, None).run is None
    assert mixer.get_keyer("me-1-key-1").dme.x == 0.2 and "run" not in mixer.export_state()["keyers"][0]


def test_engine_start_pushes_keyframes_before_on_and_the_box_after(mixer: VisionMixer) -> None:
    calls = _record_engine(mixer)
    mixer.update_keyer("me-1-key-1", mode="pip", source_id="cam-2", fly=True, enabled=True)
    mixer.set_keyframes("me-1-key-1", [{"x": 0.0}, {"x": 0.5}])
    calls.clear()
    mixer._on_engine_ready(None)
    keyer1 = [(method, path) for method, path, _ in calls if path.startswith(("/keyer/1", "/key/1"))]
    assert keyer1 == [("PUT", "/key/1"), ("PUT", "/keyer/1/keyframes"), ("POST", "/keyer/1"), ("POST", "/keyer/1/settings")]
    settings = next(body for _, path, body in calls if path == "/keyer/1/settings")
    assert settings["fly"] is True and settings["dme"] == mixer.get_keyer("me-1-key-1").dme.model_dump()
    assert "dme" not in next(body for _, path, body in calls if path == "/keyer/3/settings")


def test_status_reports_a_program_without_frames(mixer: VisionMixer) -> None:
    # The state stays "running" (on air); the error and a gauge say that no frames come.
    mixer.state = MixerState.running
    mixer.engine = SimpleNamespace(
        ready=True, state="running", status={"output": {"committed": 4}}, summary=lambda: {}, metrics=lambda: "", restarts=0, mosaic_map=None
    )
    mixer._frame_mark = (4, time.monotonic() - 10)
    status = mixer.status()
    assert status.state == MixerState.running
    assert status.error is not None and status.error.startswith("Program renders no frames")
    assert "flowxer_program_stalled 1" in render_prometheus(mixer)
    mixer.engine.status = {"output": {"committed": 5}}
    assert mixer.status().error is None
    assert "flowxer_program_stalled 0" in render_prometheus(mixer)
    mixer.engine = None
    mixer.state = MixerState.idle


def test_program_autostart_tries_again_until_program_runs(
    mixer: VisionMixer, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # Platform vmix after a node reboot: the first start failed and Program stayed off until an
    # operator started it two minutes later.
    import flowxer.engine.mixer as mixer_module

    monkeypatch.setattr(mixer_module, "AUTOSTART_BACKOFF_S", (0.01, 0.02))
    start = mixer._start
    attempts: list[int] = []

    def flaky(request=None):
        attempts.append(1)
        if len(attempts) < 3:
            mixer.state = MixerState.error
            raise MixerError("the media engine did not start: no CUDA device")
        return start(request)

    monkeypatch.setattr(mixer, "_start", flaky)
    caplog.set_level("INFO", logger="flowxer.engine.mixer")
    mixer.autostart()
    deadline = time.monotonic() + 5
    while mixer.state != MixerState.running and time.monotonic() < deadline:
        time.sleep(0.01)
    assert mixer.state == MixerState.running
    assert len(attempts) == 3
    messages = [record.getMessage() for record in caplog.records if "AUTOSTART" in record.getMessage()]
    assert "attempt 1, next in 0 s" in messages[0]
    assert "attempt 2" in messages[1]
    assert messages[2].endswith("(attempt 3)")


def test_a_stop_ends_the_program_autostart_attempts(mixer: VisionMixer, monkeypatch: pytest.MonkeyPatch) -> None:
    import flowxer.engine.mixer as mixer_module

    monkeypatch.setattr(mixer_module, "AUTOSTART_BACKOFF_S", (0.05,))
    attempts: list[int] = []

    def failing(request=None):
        attempts.append(1)
        raise MixerError("the media engine did not start")

    monkeypatch.setattr(mixer, "_start", failing)
    mixer.autostart()
    mixer.stop()
    count = len(attempts)
    time.sleep(0.3)
    assert len(attempts) == count <= 2


def _stinger_ready(mixer: VisionMixer, monkeypatch: pytest.MonkeyPatch, frames: int) -> None:
    """ME 1's next transition is a stinger of `frames` frames whose mezzanine is ready."""
    body = {"path": "/storage/library/st/mezz.mkv", "alpha": "", "audio": "", "channels": 2, "frames": frames}
    monkeypatch.setattr(mixer, "_stinger_media", lambda slot, stinger_id=None: (body, 2))
    mixer.start(MixerStartRequest(program_input_id="cam-1"))
    mixer.set_preview("cam-2")
    mixer.get_panel("me-1").transition.type = TransitionType.stinger


def test_back_to_back_stinger_autos_both_play(mixer: VisionMixer, monkeypatch: pytest.MonkeyPatch) -> None:
    # One stinger player, holding the stinger: it is free 0.1 s after the stinger's end. An Auto
    # sent before that waits for it and plays the stinger too.
    _stinger_ready(mixer, monkeypatch, frames=5)
    mixer._stinger_cued[1] = "/storage/library/st/mezz.mkv"
    mixer.auto("me-1")
    start = time.monotonic()
    mixer.auto("me-1")
    waited = time.monotonic() - start
    assert mixer.transition_counts["stinger"] == 2 and mixer.transition_counts["cut"] == 0
    assert mixer.stinger_fallbacks["busy"] == 0
    assert (mixer.program_input_id, mixer.preview_input_id) == ("cam-1", "cam-2")
    assert waited < 0.5


def test_stinger_auto_with_no_player_free_in_time_is_a_cut(mixer: VisionMixer, monkeypatch: pytest.MonkeyPatch) -> None:
    # A stinger loaded on demand keeps its player for its length + 1 s: a second Auto right after
    # it does not wait (no player is free within 0.5 s) and is a cut, counted as busy.
    _stinger_ready(mixer, monkeypatch, frames=50)
    mixer.auto("me-1")
    start = time.monotonic()
    mixer.auto("me-1")
    assert time.monotonic() - start < 0.1
    assert mixer.transition_counts["stinger"] == 1 and mixer.transition_counts["cut"] == 1
    assert mixer.stinger_fallbacks["busy"] == 1
    assert 'flowxer_stinger_fallbacks_total{reason="busy"} 1' in render_prometheus(mixer)
