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
