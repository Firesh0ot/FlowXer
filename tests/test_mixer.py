import time
from types import SimpleNamespace

import pytest

from flowxer.api.metrics import render_prometheus
from flowxer.api.schemas import InputKind, LogicalInputCreate, MixerStartRequest, MixerState, TransitionType, WorkspaceUpdate
from flowxer.domain import nmos
from flowxer.engine.mixer import MixerError, VisionMixer


def _record_engine(mixer: VisionMixer) -> list[tuple[str, str, dict]]:
    """The commands the mixer would send to a running engine."""
    calls: list[tuple[str, str, dict]] = []
    mixer._engine_call = lambda method, path, body, client=None: calls.append((method, path, body))
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


def test_one_downstream_keyer(mixer: VisionMixer) -> None:
    assert [keyer.id for keyer in mixer.keyers] == ["dsk-1"]
    with pytest.raises(MixerError, match="at most 1"):
        mixer.apply_workspace(WorkspaceUpdate(downstream_keyer_count=2))
    calls = _record_engine(mixer)
    mixer.update_keyer("dsk-1", enabled=True)
    assert calls[-1] == ("PUT", "/dsk/1", {"on": True})
    mixer.keyer_auto("dsk-1", duration_ms=500)
    assert calls[-1] == ("POST", "/dsk/1/auto", {"frames": 25})
    assert mixer.keyers[0].enabled is False
    # Auto without a duration takes the keyer's rate.
    mixer.update_keyer("dsk-1", rate_ms=1000)
    mixer.keyer_auto("dsk-1")
    assert calls[-1] == ("POST", "/dsk/1/auto", {"frames": 50})
    assert mixer.keyers[0].rate_ms == 1000 and mixer.keyers[0].enabled is True


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
