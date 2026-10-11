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
    assert "FFmpeg" in notice
    assert "MediaMTX" in notice
    assert "GStreamer" not in notice and "gstcefsrc" not in notice
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    # The media engine is built in the same image (CUDA multi-stage).
    assert "AS engine-build" in dockerfile
    assert "flowxer-engine" in dockerfile
    assert "ARG MXL_REF=218ddaa" in dockerfile
    assert "USER 1000:1000" in dockerfile
    assert "io.dmf.mxl.revision" in dockerfile
    assert "FLOWXER_MXL_ROOT=/Volumes/mxl" in dockerfile
    assert "NVIDIA_DRIVER_CAPABILITIES=compute,video,utility" in dockerfile
    assert "gstreamer" not in dockerfile.lower() and "cef" not in dockerfile.lower()
    assert "pre-built" not in dockerfile
    assert "cp /app/configs/domain_def.json /mxl-domain/domain_def.json" not in dockerfile
    entrypoint = (ROOT / "docker" / "entrypoint.sh").read_text(encoding="utf-8")
    assert "domain_def.json" not in entrypoint
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'license = { text = "Apache-2.0" }' in pyproject
    gui_pkg = (ROOT / "gui" / "package.json").read_text(encoding="utf-8")
    assert '"license": "Apache-2.0"' in gui_pkg
    gui_df = (ROOT / "gui" / "Dockerfile").read_text(encoding="utf-8")
    assert "USER 1000:1000" in gui_df
    assert "FLOWXER_MIXER_URL" in gui_df
    nginx = (ROOT / "gui" / "nginx.conf").read_text(encoding="utf-8")
    assert "${FLOWXER_MIXER_URL}" in nginx
    assert "${FLOWXER_GUI_PORT}" in nginx
    assert "vision-mixer:9610" not in nginx
    workflow = (ROOT / ".github" / "workflows" / "main.yml").read_text(encoding="utf-8")
    assert "git-${{ github.sha }}" in workflow


def test_compose_keeps_mixer_api_on_loopback() -> None:
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "127.0.0.1:9610:9610" in compose
    assert "FLOWXER_HOST" in compose
    assert "FLOWXER_MIXER_URL" in compose
    assert "FLOWXER_API_TOKEN" in compose
    assert "ghcr.io/firesh0ot/flowxer-vision-mixer" in compose
    assert "ghcr.io/firesh0ot/flowxer-gui" in compose
    assert "dockerfile:" not in compose
    assert "build:" not in compose


def test_api_token_protects_control_plane(settings: Settings, mixer: VisionMixer) -> None:
    locked = settings.model_copy(update={"api_token": "staging-token-1"})
    client = TestClient(create_app(locked, mixer))
    assert client.get("/api/v1/health").status_code == 200
    assert client.get("/static/panel.js").status_code == 200
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


def test_library_ids_cannot_escape_the_library(mixer: VisionMixer) -> None:
    with pytest.raises(MixerError, match="unknown library"):
        mixer._check_player_media("../../etc/passwd")

def test_player_media_is_a_library_item(client: TestClient) -> None:
    response = client.patch("/api/v1/inputs/clip-1", json={"library_item_id": "../secret"})
    assert response.status_code in {404, 409}
    assert "library" in response.json()["detail"]

def test_stinger_id_and_media_stay_in_storage(mixer: VisionMixer) -> None:
    with pytest.raises(MixerError, match="invalid stinger"):
        mixer.get_stinger("../etc")
    with pytest.raises(MixerError, match="storage"):
        mixer.configure_stinger_slot(
            "shared-1",
            StingerSlotUpdate(kind="video", media_path="/etc/passwd"),
        )


def test_the_html_overlay_is_gone(client: TestClient) -> None:
    # The DSK keys a v210a input (e.g. mxl-browser-source); the mixer renders no URLs any more.
    assert client.post("/api/v1/overlay", json={"url": "file:///etc/passwd"}).status_code in {404, 405}
    assert client.patch("/api/v1/keyers/dsk-1", json={"url": "file:///etc/passwd"}).json().get("url") is None

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
            domain=str(mixer.settings.output_domain.resolve()),
            program_input_id="cam-1",
        )
    )
    mixer.stop()
