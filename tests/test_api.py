from __future__ import annotations

from fastapi.testclient import TestClient

from flowxer.api.schemas import InputKind, LogicalInputCreate, VideoEssence, AudioEssence
from flowxer.engine.mixer import MixerError, VisionMixer


def test_logical_input_bundles_video_and_audio_essences(mixer: VisionMixer) -> None:
    created = mixer.register_input(
        LogicalInputCreate(
            id="studio-a",
            label="Studio A",
            kind=InputKind.mxl_live,
            video=VideoEssence(flow_id="5fbec3b1-1b0f-417d-9059-8b94a47197ed"),
            audio=AudioEssence(flow_id="b3bb5be7-9fe9-4324-a5bb-4c70e1084449", channels=2),
        )
    )
    assert created.video is not None
    assert created.audio is not None
    assert created.video.media_type == "video/v210"
    assert created.audio.media_type == "audio/float32"
    assert created.slot == len(mixer.inputs) - 1


def test_rejects_compressed_video_media_type() -> None:
    try:
        VideoEssence(media_type="video/h264")
    except Exception as exc:
        assert "video/v210" in str(exc)
    else:
        raise AssertionError("compressed video must be rejected")


def test_file_input_requires_existing_clip(mixer: VisionMixer, tmp_path) -> None:
    try:
        mixer.register_input(
            LogicalInputCreate(
                id="clip-1",
                label="Clip 1",
                kind=InputKind.file,
                file_path="missing.mp4",
            )
        )
    except MixerError as exc:
        assert "clip not found" in str(exc)
    else:
        raise AssertionError("missing clip must be rejected")


def test_openapi_documents_mixer_and_replay(client: TestClient) -> None:
    spec = client.get("/openapi.json").json()
    paths = spec["paths"]
    for path in (
        "/api/v1/health",
        "/api/v1/inputs",
        "/api/v1/mixer/start",
        "/api/v1/mixer/take",
        "/api/v1/overlay",
        "/api/v1/storage/clips",
        "/api/v1/storage/stingers",
        "/api/v1/replay/take",
        "/api/v1/replay/return",
        "/api/v1/stinger/play",
        "/api/v1/stinger-slots/{slot_id}",
        "/api/v1/mixer/cut",
        "/api/v1/mixer/fade",
        "/api/v1/mixer/fade-to-black",
        "/api/v1/mixer/wipe",
        "/api/v1/console",
        "/api/v1/workspace",
        "/api/v1/resources",
        "/api/v1/keyers/{keyer_id}",
    ):
        assert path in paths, path
    tags = {tag["name"] for tag in spec["tags"]}
    assert "stinger" in tags
    assert "gui" in tags
    components = spec["components"]["schemas"]
    assert "StingerPlayRequest" in components
    assert "flip_flop" in components["StingerPlayRequest"]["properties"]
    assert "source_tile_aspect" in components["WorkspaceConfig"]["properties"]
    assert "stinger_slot_id" in components["LogicalInput"]["properties"]
    assert "cut_frame" in components["StingerSlotUpdate"]["properties"]
    assert "ResourceInfo" in components
    assert spec["info"]["title"] == "FlowXer Vision Mixer"
    assert spec["openapi"].startswith("3.")


def test_health_and_config(client: TestClient) -> None:
    health = client.get("/api/v1/health").json()
    assert health["status"] == "ok"
    config = client.get("/api/v1/config").json()
    assert config["video_media_type"] == "video/v210"
    assert config["audio_media_type"] == "audio/float32"


def test_control_surface_and_docs(client: TestClient) -> None:
    home = client.get("/")
    assert home.status_code == 200
    assert "FlowXer" in home.text
    docs = client.get("/docs")
    assert docs.status_code == 200
    graphics = client.get("/graphics/lower-third.html")
    assert graphics.status_code == 200
