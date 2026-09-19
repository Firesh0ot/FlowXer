from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from flowxer.api.schemas import MixerStartRequest, StingerSlotUpdate, TallyKind, TallyReceiver
from flowxer.app import create_app
from flowxer.engine.mixer import MixerError, VisionMixer
from flowxer.settings import Settings

ROOT = Path(__file__).resolve().parents[1]


def test_apache_license_and_notice_are_complete() -> None:
    license_text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert "Apache License" in license_text
    assert "Copyright 2026 FlowXer contributors" in license_text
    notice = (ROOT / "NOTICE").read_text(encoding="utf-8")
    assert "Apache-2.0" in notice
    assert "GStreamer" in notice
    assert "FFmpeg" in notice
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'license = { text = "Apache-2.0" }' in pyproject
    gui_pkg = (ROOT / "gui" / "package.json").read_text(encoding="utf-8")
    assert '"license": "Apache-2.0"' in gui_pkg


def test_compose_keeps_mixer_api_on_loopback() -> None:
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "127.0.0.1:9610:9610" in compose
    assert "FLOWXER_API_TOKEN" in compose


def test_api_token_protects_control_plane(settings: Settings, mixer: VisionMixer) -> None:
    locked = settings.model_copy(update={"api_token": "staging-token-1"})
    client = TestClient(create_app(locked, mixer))
    assert client.get("/api/v1/health").status_code == 200
    assert client.get("/graphics/lower-third.html").status_code == 200
    assert client.get("/docs").status_code == 401
    assert client.get("/openapi.json").status_code == 401
    denied = client.post("/api/v1/mixer/start", json={"program_input_id": "cam-1"})
    assert denied.status_code == 401
    assert denied.json()["detail"] == "Not authenticated"
    headers = {"Authorization": "Bearer staging-token-1"}
    console = client.get("/api/v1/console", headers=headers)
    assert console.status_code == 200
    wrong = client.get("/api/v1/console", headers={"X-FlowXer-Token": "nope-token"})
    assert wrong.status_code == 401
    started = client.post(
        "/api/v1/mixer/start",
        json={"program_input_id": "cam-1"},
        headers=headers,
    )
    assert started.status_code == 200


def test_clip_paths_cannot_escape_storage(mixer: VisionMixer) -> None:
    with pytest.raises(MixerError, match="storage"):
        mixer._resolve_clip("/etc/passwd")
    with pytest.raises(MixerError, match="storage"):
        mixer._resolve_clip("../../etc/passwd")


def test_replay_load_rejects_traversal(client: TestClient) -> None:
    response = client.post("/api/v1/replay/load", json={"file_path": "../secret.mp4"})
    assert response.status_code == 409
    assert "storage" in response.json()["detail"]


def test_stinger_id_and_media_stay_in_storage(mixer: VisionMixer) -> None:
    with pytest.raises(MixerError, match="invalid stinger"):
        mixer.get_stinger("../etc")
    with pytest.raises(MixerError, match="storage"):
        mixer.configure_stinger_slot(
            "shared-1",
            StingerSlotUpdate(kind="video", media_path="/etc/passwd"),
        )


def test_overlay_rejects_file_and_metadata_urls(client: TestClient) -> None:
    file_url = client.post("/api/v1/overlay", json={"url": "file:///etc/passwd"})
    assert file_url.status_code == 409
    meta = client.post("/api/v1/overlay", json={"url": "http://169.254.169.254/latest/meta-data"})
    assert meta.status_code == 409
    ok = client.post(
        "/api/v1/overlay",
        json={"url": "http://127.0.0.1:9610/graphics/lower-third.html"},
    )
    assert ok.status_code == 200


def test_tally_rejects_cloud_metadata_but_allows_lan(mixer: VisionMixer) -> None:
    with pytest.raises(MixerError, match="not allowed"):
        mixer.replace_tally_receivers(
            [
                TallyReceiver(
                    id="evil",
                    kind=TallyKind.custom,
                    label="Metadata",
                    host="169.254.169.254",
                    port=80,
                )
            ]
        )
    status = mixer.replace_tally_receivers(
        [
            TallyReceiver(
                id="lan-vsm",
                kind=TallyKind.vsm,
                label="Lawo VSM",
                host="10.0.0.8",
                port=8900,
            )
        ]
    )
    assert status[0].host == "10.0.0.8"


def test_start_rejects_domain_override(mixer: VisionMixer, tmp_path: Path) -> None:
    with pytest.raises(MixerError, match="domain"):
        mixer.start(MixerStartRequest(domain=str(tmp_path / "other-domain"), program_input_id="cam-1"))
    mixer.start(
        MixerStartRequest(
            domain=str(mixer.settings.mxl_domain.resolve()),
            program_input_id="cam-1",
        )
    )
    mixer.stop()
