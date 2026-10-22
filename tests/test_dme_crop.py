"""DME crop in source pixels (2026-10-22): the API, the engine's fractions, saved states and exports
with the old fractions converted, a format change, the presets."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from flowxer.api.schemas import WorkspaceUpdate
from flowxer.engine.mixer import MixerError, VisionMixer, scale_crops
from flowxer.settings import Settings


def _record_engine(mixer: VisionMixer, reply=None) -> list:
    calls: list = []

    def call(method, path, body=None, client=None):
        calls.append((method, path, body))
        return reply

    mixer._engine_call = call  # type: ignore[method-assign]
    return calls


def test_the_api_takes_pixels_and_refuses_fractions(client: TestClient) -> None:
    base = "/api/v1/keyers/me-1-key-1"
    keyer = client.patch(base, json={"mode": "pip", "source_id": "cam-2", "dme": {"crop": {"left": 8, "bottom": 4}}}).json()
    assert keyer["dme"]["crop"] == {"left": 8, "right": 0, "top": 0, "bottom": 4}
    # Fractions (the old unit) are refused with a clear message; so is a crop that leaves nothing
    # of the 64 × 36 test raster.
    response = client.patch(base, json={"dme": {"crop": {"left": 0.25}}})
    assert response.status_code in (409, 422) and "integer" in response.text
    assert client.patch(base, json={"dme": {"crop": {"left": 32, "right": 32}}}).status_code == 409
    assert client.patch(base, json={"dme": {"crop": {"top": 36}}}).status_code == 409
    assert client.put(f"{base}/dme", json={"crop": {"left": 70}}).status_code == 409
    assert client.put(f"{base}/keyframes", json=[{"crop": {"top": 40}}]).status_code == 409
    assert client.put(f"{base}/keyframes", json=[{"crop": {"top": 10}}, {"crop": {"top": 0}}]).status_code == 200


def test_the_engine_gets_fractions_and_its_boxes_come_back_in_pixels(mixer: VisionMixer) -> None:
    calls = _record_engine(mixer)
    mixer.update_keyer("me-1-key-1", mode="pip", source_id="cam-2", dme={"crop": {"left": 7, "right": 8, "top": 9}})
    body = calls[-1][2]["dme"]
    # Left and right on whole 4:2:2 pairs (7 → 8), over the 64 px width; top over 36 lines.
    assert body["crop"] == {"left": 8 / 64, "right": 8 / 64, "top": 9 / 36}
    mixer.set_keyframes("me-1-key-1", [{"crop": {"left": 16}}, {"crop": {"left": 0}}])
    assert [frame["crop"]["left"] for frame in calls[-1][2]["keyframes"]] == [0.25, 0.0]
    # The engine's box (fractions; between keyframes not whole) in pixels.
    engine_box = {"x": 0.5, "y": 0.5, "size": 0.5, "soft": 0.0, "crop": {"left": 0.12, "right": 0.0, "top": 0.5, "bottom": 0.0},
                  "border": {"width": 0.0, "soft": 0.0, "opacity": 1.0}, "shadow": {"dx": 0.0, "dy": 0.0, "blur": 0.0, "opacity": 0.0}}
    _record_engine(mixer, reply={"key": 1, "on": False, "dme": engine_box, "run": {"running": False}})
    keyer = mixer.update_keyer("me-1-key-1", dme={"x": 0.5})
    assert (keyer.dme.crop.left, keyer.dme.crop.top) == (8, 18)  # round(0.12 · 64), 0.5 · 36


def test_a_saved_state_with_fractions_loads_in_pixels(mixer: VisionMixer, settings: Settings, client: TestClient) -> None:
    mixer.update_keyer("me-1-key-1", mode="pip", source_id="cam-2")
    mixer.set_keyframes("me-1-key-1", [{"x": -0.3}, {"x": 0.5}])
    client.post("/api/v1/snapshots", json={"panel_id": "me-1", "number": 2, "name": "Box"})
    mixer.persist()
    path = settings.state_dir / "state.json"
    saved = json.loads(path.read_text(encoding="utf-8"))
    # As a state written before 2026-10-22: crops in fractions of the source.
    legacy = {"left": 0.25, "right": 0.125, "top": 0.5, "bottom": 0.0}
    saved["keyers"][0]["dme"]["crop"] = dict(legacy)
    saved["keyers"][0]["keyframes"][0]["crop"] = dict(legacy)
    snapshot_keyers = saved["snapshots"]["items"][0]["mes"]["me-1"]["keyers"]
    snapshot_keyer = snapshot_keyers[0] if isinstance(snapshot_keyers, list) else next(iter(snapshot_keyers.values()))
    snapshot_keyer["dme"]["crop"] = dict(legacy)
    path.write_text(json.dumps(saved), encoding="utf-8")

    again = VisionMixer(settings)
    # In pixels of the state's format (1080p50; loading it sets the raster).
    pixels = {"left": 480, "right": 240, "top": 540, "bottom": 0}
    keyer = again.get_keyer("me-1-key-1")
    assert keyer.dme.crop.model_dump() == pixels and keyer.keyframes[0].crop.model_dump() == pixels
    stored = json.dumps(again.export_state()["snapshots"])
    assert '"left": 480' in stored and "0.25" not in stored
    # The same document through POST /config/import.
    importing = TestClient(client.app)
    assert importing.post("/api/v1/config/import", json=saved).status_code == 200
    assert mixer.get_keyer("me-1-key-1").dme.crop.model_dump() == pixels


def test_scale_crops_walks_every_crop() -> None:
    doc = {"keyers": [{"dme": {"crop": {"left": 480, "right": 0, "top": 270, "bottom": 0}}, "keyframes": [{"crop": {"left": 960}}]}],
           "snapshots": {"items": [{"mes": {"me-1": {"keyers": [{"dme": {"crop": {"left": 0.25}}}]}}}]}}
    assert scale_crops(doc, 1280 / 1920, 720 / 1080, legacy_only=False) == 3
    assert doc["keyers"][0]["dme"]["crop"] == {"left": 320, "right": 0, "top": 180, "bottom": 0}
    assert doc["keyers"][0]["keyframes"][0]["crop"] == {"left": 640}
    # Only the old fractions with legacy_only.
    legacy = {"a": {"crop": {"left": 0.25, "top": 0.5}}, "b": {"crop": {"left": 16, "top": 0}}, "c": {"crop": {"left": 0.0}}}
    assert scale_crops(legacy, 1920, 1080, legacy_only=True) == 1
    assert legacy == {"a": {"crop": {"left": 480, "top": 540}}, "b": {"crop": {"left": 16, "top": 0}}, "c": {"crop": {"left": 0.0}}}


def test_format_change_rescales_keyers_keyframes_and_snapshots(settings: Settings) -> None:
    settings.width, settings.height = 1920, 1080
    mixer = VisionMixer(settings)
    mixer.apply_workspace(WorkspaceUpdate(format_id="1080p50"))
    _record_engine(mixer)
    mixer.update_keyer("me-1-key-1", mode="pip", source_id="cam-2", dme={"crop": {"left": 480, "top": 270}})
    mixer.set_keyframes("me-1-key-1", [{"crop": {"right": 960}}, {}])
    with pytest.raises(MixerError, match="must leave a picture"):
        mixer.update_keyer("me-1-key-1", dme={"crop": {"left": 1000, "right": 920}})
    mixer.apply_workspace(WorkspaceUpdate(format_id="720p50"))
    keyer = mixer.get_keyer("me-1-key-1")
    assert (keyer.dme.crop.left, keyer.dme.crop.top) == (320, 180)
    assert keyer.keyframes[0].crop.right == 640 and keyer.keyframes[1].crop.left == 320
