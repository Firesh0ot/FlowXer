import pytest
from fastapi.testclient import TestClient

from flowxer.api.schemas import MixerStartRequest, TransitionType, WorkspaceUpdate
from flowxer.engine.mixer import MixerError, VisionMixer


def test_default_console_has_sources_panel_and_dsk(mixer: VisionMixer) -> None:
    assert mixer.workspace.source_tile_aspect == "16:9"
    assert mixer.workspace.logical_source_count == 8
    assert len(mixer.list_inputs()) == 8
    assert mixer.panels[0].id == "me-1"
    assert mixer.keyers[0].id == "dsk-1"
    assert mixer.stinger_slots[0].role == "shared"
    # Live inputs, a clip player, Black (no test sources or replay input any more).
    assert [item.kind.value for item in mixer.list_inputs()] == ["mxl_live"] * 6 + ["clip_player", "black"]
    assert mixer.get_input("cam-1").kind.value == "mxl_live"
    assert mixer.get_input("clip-1").kind.value == "clip_player"
    assert mixer.get_input("black").kind.value == "black"


def test_workspace_settings_resize_console(mixer: VisionMixer) -> None:
    mixer.apply_workspace(
        WorkspaceUpdate(
            format_id="720p50",
            logical_source_count=4,
            mixer_panel_count=2,
            stinger_mode="separate",
            stinger_count=1,
            downstream_keyer_count=0,
        )
    )
    assert mixer.settings.width == 1280
    assert mixer.settings.height == 720
    assert len(mixer.list_inputs()) == 4
    assert [item.kind.value for item in mixer.list_inputs()] == ["mxl_live", "mxl_live", "clip_player", "black"]
    assert len(mixer.panels) == 2
    assert [slot.role for slot in mixer.stinger_slots] == ["in", "out"]
    assert mixer.keyers == []
    with pytest.raises(MixerError, match="at most 1"):
        mixer.apply_workspace(WorkspaceUpdate(downstream_keyer_count=2))


def test_source_tile_aspect_while_running(mixer: VisionMixer) -> None:
    mixer.start(MixerStartRequest(program_input_id="cam-1"))
    mixer.apply_workspace(WorkspaceUpdate(source_tile_aspect="9:16"))
    assert mixer.workspace.source_tile_aspect == "9:16"
    with pytest.raises(MixerError, match="stop the mixer"):
        mixer.apply_workspace(WorkspaceUpdate(format_id="720p50"))
    with pytest.raises(MixerError, match="16:9 or 9:16"):
        mixer.apply_workspace(WorkspaceUpdate(source_tile_aspect="4:3"))


def test_left_preview_right_program_on_panel(mixer: VisionMixer) -> None:
    mixer.start(MixerStartRequest(program_input_id="cam-1"))
    mixer.set_preview("cam-2", panel_id="me-1")
    mixer.take("black", panel_id="me-1")
    assert mixer.preview_input_id == "cam-2"
    assert mixer.program_input_id == "black"
    assert mixer.panels[0].preview_input_id == "cam-2"
    assert mixer.panels[0].program_input_id == "black"


def test_console_and_resources_api(client: TestClient) -> None:
    console = client.get("/api/v1/console").json()
    assert console["workspace"]["format_id"] == "1080p50"
    assert console["workspace"]["source_tile_aspect"] == "16:9"
    assert console["nmos"]["enabled"] is False
    assert len(console["formats"]) >= 4
    assert console["resources"]["cpu_count"] >= 1
    assert console["preview"]["available"] is False
    assert console["sources"] == {}
    assert client.get("/api/v1/resources").status_code == 200

def test_keyer_and_stinger_slot_api(client: TestClient) -> None:
    updated = client.patch(
        "/api/v1/keyers/dsk-1",
        json={"enabled": True, "title": "LOWER THIRD"},
    )
    assert updated.status_code == 200
    assert updated.json()["enabled"] is True
    slot = client.patch(
        "/api/v1/stinger-slots/shared-1",
        json={"stinger_id": "replay-wipe"},
    )
    assert slot.status_code == 200


def test_stinger_slot_cut_time_and_video(mixer: VisionMixer) -> None:
    from flowxer.api.schemas import StingerSlotUpdate

    info = mixer.get_stinger("replay-wipe")
    assert info.kind == "sequence"
    assert info.cut_ms > 0
    mixer.configure_stinger_slot(
        "shared-1",
        StingerSlotUpdate(stinger_id="replay-wipe", cut_frame=5),
    )
    slot = mixer.stinger_slots[0]
    assert slot.cut_frame == 5
    assert mixer.get_stinger("replay-wipe").cut_frame == 5

    mixer.configure_stinger_slot(
        "shared-1",
        StingerSlotUpdate(stinger_id="replay-wipe", cut_ms=40),
    )
    slot = mixer.stinger_slots[0]
    assert slot.cut_ms == 40
    assert slot.cut_frame == mixer.stinger_slots[0].cut_frame
    updated = mixer.get_stinger("replay-wipe")
    assert updated.cut_ms == 40

    clip = mixer.settings.clips_dir / "sting.webm"
    clip.write_bytes(b"fake-video")
    video_slot = mixer.configure_stinger_slot(
        "shared-1",
        StingerSlotUpdate(kind="video", media_path="sting.webm", cut_ms=200, duration_ms=800),
    )
    assert video_slot.kind == "video"
    assert video_slot.stinger_id == "sting"
    bound = mixer.get_stinger("sting")
    assert bound.kind == "video"
    assert bound.cut_ms == 200
    assert "sting.webm" in bound.media_path


def test_an_armed_wipe_does_not_change_the_cut(mixer: VisionMixer) -> None:
    # Wipes and stingers come in P3: the next Cut is a cut and disarms the Wipe.
    mixer.start(MixerStartRequest(program_input_id="cam-1"))
    mixer.set_preview("cam-2")
    mixer.set_wipe()
    assert mixer.panels[0].wipe_armed is True
    mixer.cut()
    assert mixer.program_input_id == "cam-2" and mixer.preview_input_id == "cam-1"
    assert mixer.panels[0].wipe_armed is False
    assert mixer.last_transition == "cut"

def test_cut_flip_flops_preview_and_program(mixer: VisionMixer) -> None:
    mixer.start(MixerStartRequest(program_input_id="cam-1"))
    mixer.set_preview("cam-2")
    mixer.cut()
    assert mixer.program_input_id == "cam-2"
    assert mixer.preview_input_id == "cam-1"
    assert mixer.panels[0].last_transition == "cut"
    assert mixer.last_transition == "cut"


def test_fade_mixes_preview_onto_program(mixer: VisionMixer) -> None:
    mixer.start(MixerStartRequest(program_input_id="cam-1"))
    mixer.set_preview("cam-3")
    mixer.fade(duration_ms=400)
    assert mixer.program_input_id == "cam-3"
    assert mixer.preview_input_id == "cam-1"
    assert mixer.last_transition == "mix"


def test_fade_to_black_and_restore(mixer: VisionMixer) -> None:
    mixer.start(MixerStartRequest(program_input_id="cam-1"))
    mixer.set_preview("cam-2")
    mixer.fade_to_black()
    assert mixer.program_input_id == "black"
    assert mixer.preview_input_id == "cam-2"
    mixer.fade_to_black()
    assert mixer.program_input_id == "cam-2"


def test_wipe_stays_armed_over_a_take(mixer: VisionMixer) -> None:
    mixer.start(MixerStartRequest(program_input_id="cam-1"))
    mixer.set_preview("cam-2")
    mixer.set_wipe()
    mixer.take("cam-3")
    assert mixer.program_input_id == "cam-3"
    assert mixer.panels[0].wipe_armed is True

def test_set_wipe_toggles(mixer: VisionMixer) -> None:
    mixer.start(MixerStartRequest(program_input_id="cam-1"))
    mixer.set_wipe()
    mixer.set_wipe()
    assert mixer.panels[0].wipe_armed is False
    mixer.set_wipe(armed=True)
    assert mixer.panels[0].wipe_armed is True

def test_transition_bank_api(client: TestClient) -> None:
    client.post("/api/v1/mixer/start", json={"program_input_id": "cam-1"})
    client.post("/api/v1/mixer/preview", json={"input_id": "cam-2", "panel_id": "me-1"})
    wipe = client.post("/api/v1/mixer/wipe", json={"panel_id": "me-1"})
    assert wipe.status_code == 200
    assert wipe.json()["mixer"]["wipe_armed"] is True
    fade = client.post("/api/v1/mixer/fade", json={"panel_id": "me-1"})
    assert fade.status_code == 200
    assert fade.json()["mixer"]["program_input_id"] == "cam-2"
    assert fade.json()["mixer"]["last_transition"] == "mix"
    ftb = client.post("/api/v1/mixer/fade-to-black", json={"panel_id": "me-1"})
    assert ftb.status_code == 200
    assert ftb.json()["mixer"]["program_input_id"] == "black"
    cut = client.post("/api/v1/mixer/cut", json={"panel_id": "me-1"})
    assert cut.status_code == 200


def test_stinger_play_flip_flops_preview_to_program(mixer: VisionMixer) -> None:
    # A stinger is a cut in this version (stingers come in P3).
    mixer.start(MixerStartRequest(program_input_id="cam-1"))
    mixer.set_preview("cam-2")
    mixer.play_stinger("replay-wipe", "cam-2", direction="to_live", flip_flop=True, panel_id="me-1")
    assert mixer.program_input_id == "cam-2"
    assert mixer.preview_input_id == "cam-1"
    assert mixer.status().stinger["phase"] == "idle"

def test_every_me_switches_on_its_own(mixer: VisionMixer) -> None:
    mixer.apply_workspace(WorkspaceUpdate(mixer_panel_count=2))
    mixer.start(MixerStartRequest(program_input_id="cam-1"))
    mixer.set_preview("cam-2")
    mixer.set_preview("cam-4", panel_id="me-2")
    mixer.take("cam-3", panel_id="me-2")
    mixer.cut(panel_id="me-2")
    me_2 = mixer.get_panel("me-2")
    assert (me_2.program_input_id, me_2.preview_input_id) == ("cam-4", "cam-3")
    mixer.take("cam-1", TransitionType.stinger, panel_id="me-2")
    assert mixer.get_panel("me-2").program_input_id == "cam-1"
    # ME 1 is untouched.
    assert (mixer.program_input_id, mixer.preview_input_id) == ("cam-1", "cam-2")

def test_source_auto_stinger_is_a_cut(mixer: VisionMixer) -> None:
    from flowxer.api.schemas import LogicalInputUpdate

    mixer.update_input("cam-2", LogicalInputUpdate(stinger_slot_id="shared-1"))
    assert mixer.get_input("cam-2").stinger_slot_id == "shared-1"
    mixer.start(MixerStartRequest(program_input_id="cam-1"))
    mixer.take("cam-2")
    assert mixer.program_input_id == "cam-2"
    mixer.set_preview("cam-3")
    mixer.cut()
    assert mixer.program_input_id == "cam-3"
    assert mixer.preview_input_id == "cam-2"
