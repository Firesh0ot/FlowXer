from __future__ import annotations

from pathlib import Path

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
    # Live inputs come before the clip players and Black (slot = TSL INDEX).
    live = [item for item in mixer.list_inputs() if item.kind == InputKind.mxl_live]
    assert created.slot == len(live) - 1
    assert mixer.get_input("black").slot == len(mixer.inputs) - 1


def test_rejects_compressed_video_media_type() -> None:
    try:
        VideoEssence(media_type="video/h264")
    except Exception as exc:
        assert "video/v210" in str(exc)
    else:
        raise AssertionError("compressed video must be rejected")


def test_clip_player_requires_a_library_item(mixer: VisionMixer, tmp_path) -> None:
    try:
        mixer.register_input(
            LogicalInputCreate(
                id="clip-9",
                label="Clip 9",
                kind=InputKind.clip_player,
                library_item_id="missing",
            )
        )
    except MixerError as exc:
        assert "unknown library clip or still" in str(exc)
    else:
        raise AssertionError("a missing library item must be rejected")
    # An empty clip player is fine (it shows black).
    empty = mixer.register_input(LogicalInputCreate(id="clip-9", label="Clip 9", kind=InputKind.clip_player))
    assert empty.library_item_id is None


def test_openapi_documents_the_mixer(client: TestClient) -> None:
    spec = client.get("/openapi.json").json()
    paths = spec["paths"]
    for path in (
        "/api/v1/health",
        "/api/v1/inputs",
        "/api/v1/mixer/start",
        "/api/v1/mixer/take",
        "/api/v1/storage/clips",
        "/api/v1/storage/stingers",
        "/api/v1/stinger/play",
        "/api/v1/players/{input_id}/{action}",
        "/api/v1/keyers/{keyer_id}/auto",
        "/api/v1/preview",
        "/api/v1/widgets",
        "/api/v1/engine",
        "/api/v1/stinger-slots/{slot_id}",
        "/api/v1/mixer/cut",
        "/api/v1/mixer/fade",
        "/api/v1/mixer/fade-to-black",
        "/api/v1/mixer/wipe",
        "/api/v1/console",
        "/api/v1/workspace",
        "/api/v1/resources",
        "/api/v1/keyers/{keyer_id}",
        "/api/v1/tally",
        "/api/v1/tally/receivers",
        "/api/v1/tally/refresh",
    ):
        assert path in paths, path
    for removed in ("/api/v1/overlay", "/api/v1/replay/take", "/api/v1/webrtc/whep/{stream_id}", "/api/v1/preview/jpeg/{stream_id}"):
        assert removed not in paths, removed
    tags = {tag["name"] for tag in spec["tags"]}
    assert "stinger" in tags
    assert "gui" in tags
    assert "tally" in tags
    components = spec["components"]["schemas"]
    assert "StingerPlayRequest" in components
    assert "flip_flop" in components["StingerPlayRequest"]["properties"]
    assert "source_tile_aspect" in components["WorkspaceConfig"]["properties"]
    assert "stinger_slot_id" in components["LogicalInput"]["properties"]
    assert {"loop", "play_on_cut", "library_item_id"} <= set(components["LogicalInput"]["properties"])
    assert "me_outputs" in components["MixerStatus"]["properties"]
    assert "cut_frame" in components["StingerSlotUpdate"]["properties"]
    assert "ResourceInfo" in components
    assert "TallyReceiver" in components
    assert spec["info"]["title"] == "FlowXer Vision Mixer"
    assert spec["openapi"].startswith("3.")


def test_widgets_list_the_bus_widget(client: TestClient, mixer: VisionMixer) -> None:
    widgets = client.get("/api/v1/widgets").json()
    assert [w["id"] for w in widgets] == ["bus", "keys", "dme", "audio", "snapshots", "tbar", "multiview"]
    tbar = widgets[5]
    assert tbar["params"]["required"] == [] and tbar["min_size"] == {"w": 120, "h": 240}
    assert tbar["params"]["properties"]["buttons"]["default"] is True and tbar["params"]["properties"]["me"]["default"] == 1
    multiview = widgets[6]
    assert multiview["params"]["required"] == [] and multiview["min_size"] == {"w": 480, "h": 270}
    assert multiview["params"]["properties"]["pvw"]["default"] is False and multiview["params"]["properties"]["me"]["default"] == 1
    snapshots = widgets[4]
    assert snapshots["params"]["required"] == [] and snapshots["params"]["properties"]["all"]["default"] is True
    assert snapshots["min_size"] == {"w": 240, "h": 120}
    audio = widgets[3]
    assert audio["params"]["required"] == ["me"] and audio["params"]["properties"]["faders"]["default"] is True
    assert audio["min_size"] == {"w": 480, "h": 240}
    assert widgets[1]["params"]["required"] == ["me"] and widgets[1]["min_size"] == {"w": 240, "h": 150}
    bus = widgets[0]
    assert bus["params"]["required"] == ["me"]
    assert bus["params"]["properties"]["me"]["maximum"] == len(mixer.panels)
    assert bus["params"]["properties"]["transition"] == {"type": "boolean", "default": True, "title": "Transition type"}
    assert bus["min_size"] == {"w": 480, "h": 150}
    dme = widgets[2]
    assert dme["params"]["required"] == ["me", "keyer"] and dme["min_size"] == {"w": 320, "h": 120}
    assert dme["params"]["properties"]["keyer"] == {"type": "integer", "minimum": 1, "maximum": mixer.workspace.keyers_per_me, "title": "Upstream keyer"}


def test_every_widget_has_a_framed_page_in_nginx(client: TestClient) -> None:
    # flowxer-gui serves each widget of the list at /widget/<id> with the CSP frame-ancestors.
    conf = (Path(__file__).parents[1] / "gui" / "nginx.conf").read_text(encoding="utf-8")
    for widget in client.get("/api/v1/widgets").json():
        start = conf.index(f"location = /widget/{widget['id']} {{")
        block = conf[start : conf.index("\n    }", start)]
        assert "flowxer-widgets-csp.inc" in block and "try_files /widget.html" in block
        for param in widget["params"]["required"] or widget["params"]["properties"]:
            assert f"$arg_{param}" in block


def test_keyer_rate_is_kept_and_validated(client: TestClient, mixer: VisionMixer) -> None:
    keyer = client.patch("/api/v1/keyers/dsk-1", json={"rate_ms": 800}).json()
    assert keyer["rate_ms"] == 800
    assert client.patch("/api/v1/keyers/dsk-1", json={"rate_ms": 0}).status_code == 422
    assert mixer.get_keyer("dsk-1").rate_ms == 800


def test_keyer_pip_and_box(client: TestClient) -> None:
    keyer = client.patch("/api/v1/keyers/me-1-key-1", json={"mode": "pip", "source_id": "cam-2", "dme": {"x": 0.7, "size": 0.33}}).json()
    assert keyer["mode"] == "pip" and keyer["dme"]["x"] == 0.7 and keyer["dme"]["size"] == 0.33 and keyer["dme"]["crop"]["left"] == 0.0
    assert client.patch("/api/v1/keyers/me-1-key-1", json={"dme": {"size": 5}}).status_code == 409
    assert client.patch("/api/v1/keyers/dsk-1", json={"mode": "pip", "source_id": "cam-2"}).status_code == 409
    assert client.patch("/api/v1/keyers/me-1-key-1", json={"mode": "chroma"}).status_code == 422
    assert client.get("/api/v1/keyers").json()[0]["dme"]["size"] == 0.33


def test_keyer_keyframes_runs_and_presets(client: TestClient) -> None:
    base = "/api/v1/keyers/me-1-key-1"
    client.patch(base, json={"mode": "pip", "source_id": "cam-2"})
    keyer = client.put(f"{base}/keyframes", json=[{"x": -0.3}, {"x": 0.5, "frames": 10, "ease": "ease"}]).json()
    assert [(k["x"], k["frames"], k["ease"]) for k in keyer["keyframes"]] == [(-0.3, 25, "linear"), (0.5, 10, "ease")]
    assert client.put(f"{base}/keyframes", json=[{"ease": "bounce"}]).status_code == 409
    assert client.put(f"{base}/keyframes", json={"x": 1}).status_code == 422
    assert client.put("/api/v1/keyers/dsk-1/keyframes", json=[]).status_code == 409
    # Runs (simulate mode: the box is where the run ends at once).
    assert client.post(f"{base}/run", json={"to": 0}).json()["dme"]["x"] == -0.3
    assert client.post(f"{base}/run").json()["dme"]["x"] == 0.5
    assert client.post(f"{base}/run", json={"reverse": True, "loop": "pingpong"}).status_code == 200
    assert client.post(f"{base}/run", json={"to": 1, "loop": "wrap"}).status_code == 422
    assert client.post(f"{base}/run", json={"frames": 3}).status_code == 422
    assert client.post(f"{base}/run", json={"to": 5}).status_code == 409
    assert client.post(f"{base}/stop").status_code == 200
    assert client.post("/api/v1/keyers/nope/stop").status_code == 404
    # Fly, presets, the box replaced.
    assert client.patch(base, json={"fly": True}).json()["fly"] is True
    presets = client.get("/api/v1/dme/presets").json()
    assert [p["name"] for p in presets] == ["corner-tl", "corner-tr", "corner-bl", "corner-br", "split", "quad", "full"]
    assert [p["keyers"] for p in presets] == [1, 1, 1, 1, 2, 4, 1]
    written = client.post(f"{base}/preset", json={"name": "split"}).json()
    assert [k["id"] for k in written] == ["me-1-key-1", "me-1-key-2"] and written[1]["dme"]["crop"]["left"] == 16  # a quarter of the 64 px test raster
    assert client.post(f"{base}/preset", json={"name": "zoom"}).status_code == 422
    assert client.post(f"{base}/preset", json={"name": "quad"}).status_code == 409
    keyer = client.put(f"{base}/dme", json={"x": 0.3}).json()
    assert keyer["dme"]["x"] == 0.3 and keyer["dme"]["crop"]["left"] == 0.0 and keyer["dme"]["enabled"] is True
    listed = client.get("/api/v1/keyers").json()[0]
    assert listed["fly"] is True and len(listed["keyframes"]) == 2 and listed["run"] is None


def test_health_and_config(client: TestClient) -> None:
    health = client.get("/api/v1/health").json()
    assert health["status"] == "ok"
    assert health["mxl_output_domain_id"] == "flowxer-test"
    assert "mxl_revision" in health
    config = client.get("/api/v1/config").json()
    assert config["video_media_type"] == "video/v210"
    assert config["audio_media_type"] == "audio/float32"
    assert config["mxl_output_domain_id"] == "flowxer-test"
    domains = client.get("/api/v1/domains").json()
    assert any(item["id"] == "flowxer-test" for item in domains)


def test_control_surface_and_docs(client: TestClient) -> None:
    home = client.get("/")
    assert home.status_code == 200
    assert "FlowXer" in home.text
    docs = client.get("/docs")
    assert docs.status_code == 200
    # The CEF overlay's sample page is gone with the overlay.
    assert client.get("/graphics/lower-third.html").status_code == 404


def test_invalid_input_patch_is_422_not_500(client: TestClient) -> None:
    # mxl_live without essences or group hint is invalid once merged into the input.
    response = client.patch("/api/v1/inputs/black", json={"kind": "mxl_live"})
    assert response.status_code == 422
    assert "group_hint" in response.text
