from fastapi.testclient import TestClient

from flowxer.api.schemas import MixerStartRequest, WorkspaceUpdate
from flowxer.engine.mixer import VisionMixer


def test_default_console_has_sources_panel_and_dsk(mixer: VisionMixer) -> None:
    assert mixer.workspace.logical_source_count == 8
    assert len(mixer.list_inputs()) == 8
    assert mixer.panels[0].id == "me-1"
    assert mixer.keyers[0].id == "dsk-1"
    assert mixer.stinger_slots[0].role == "shared"
    assert mixer.get_input("cam-1").kind.value == "test"
    assert mixer.get_input("replay").kind.value == "replay"


def test_workspace_settings_resize_console(mixer: VisionMixer) -> None:
    mixer.apply_workspace(
        WorkspaceUpdate(
            format_id="720p50",
            logical_source_count=4,
            mixer_panel_count=2,
            stinger_mode="separate",
            stinger_count=1,
            downstream_keyer_count=2,
        )
    )
    assert mixer.settings.width == 1280
    assert mixer.settings.height == 720
    assert len(mixer.list_inputs()) == 4
    assert len(mixer.panels) == 2
    assert [slot.role for slot in mixer.stinger_slots] == ["in", "out"]
    assert len(mixer.keyers) == 2


def test_left_preview_right_program_on_panel(mixer: VisionMixer) -> None:
    mixer.start(MixerStartRequest(program_input_id="cam-1"))
    mixer.set_preview("cam-2", panel_id="me-1")
    mixer.take("black", panel_id="me-1")
    assert mixer.preview_input_id == "cam-2"
    assert mixer.program_input_id == "black"
    assert mixer.panels[0].preview_input_id == "cam-2"
    assert mixer.panels[0].program_input_id == "black"


def test_console_and_jpeg_and_resources_api(client: TestClient) -> None:
    console = client.get("/api/v1/console").json()
    assert console["workspace"]["format_id"] == "1080p50"
    assert len(console["formats"]) >= 4
    assert console["resources"]["cpu_count"] >= 1
    jpeg = client.get("/api/v1/preview/jpeg/source:cam-1")
    assert jpeg.status_code == 200
    assert jpeg.headers["content-type"] == "image/jpeg"
    assert jpeg.content[:2] == b"\xff\xd8"
    pgm = client.get("/api/v1/preview/jpeg/panel:me-1:pgm")
    assert pgm.status_code == 200


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


def test_wipe_arms_stinger_for_next_cut(mixer: VisionMixer) -> None:
    mixer.start(MixerStartRequest(program_input_id="cam-1"))
    mixer.set_preview("cam-2")
    mixer.set_wipe()
    assert mixer.panels[0].wipe_armed is True
    mixer.take("cam-3")
    assert mixer.program_input_id == "cam-3"
    assert mixer.panels[0].wipe_armed is True
    mixer.cut()
    assert mixer.stinger_player is not None
    assert mixer.panels[0].wipe_armed is False
    assert mixer.program_input_id == "cam-3"
    stinger = mixer.get_stinger("replay-wipe")
    remaining = stinger.cut_frame - mixer.stinger_player.frame
    mixer.advance_stinger(remaining)
    assert mixer.program_input_id == "cam-2"
    assert mixer.preview_input_id == "cam-3"


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
