"""The operator previews are tiles of the engine's one mosaic stream (GET /api/v1/preview);
the engine-backed map is in test_control_plane.py."""

from __future__ import annotations

from flowxer.api.schemas import MixerStartRequest
from flowxer.engine.mixer import VisionMixer
from flowxer.engine.preview import preview_info


def test_no_preview_off_air_or_simulated(mixer: VisionMixer) -> None:
    off = preview_info(mixer)
    assert off == {"available": False, "reason": "the mixer is off-air", "tiles": [], "streams": []}
    mixer.start(MixerStartRequest(program_input_id="cam-1"))
    simulated = preview_info(mixer)
    assert simulated["available"] is False and "simulated" in simulated["reason"]
    mixer.stop()


def test_preview_api_and_the_removed_monitor_routes(client) -> None:
    response = client.get("/api/v1/preview")
    assert response.status_code == 200 and response.json()["available"] is False
    # Per-source JPEG and WebRTC monitors are gone: the console uses the mosaic.
    assert client.get("/api/v1/preview/jpeg/source:cam-1").status_code == 404
    assert client.post("/api/v1/webrtc/whep/source:cam-1", content="v=0").status_code in {404, 405}
    console = client.get("/api/v1/console").json()
    assert console["preview"]["available"] is False
    assert "webrtc" not in console
