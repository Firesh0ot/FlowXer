"""Stinger slots keep their media (TGA sequences, videos, library items) for P3, where the engine
plays them on the GPU. In this version a stinger take is a cut (tests in test_console.py)."""

from fastapi.testclient import TestClient

from flowxer.engine.mixer import VisionMixer


def test_the_default_stinger_is_generated_and_listed(client: TestClient, mixer: VisionMixer) -> None:
    stingers = client.get("/api/v1/storage/stingers").json()
    assert stingers[0]["id"] == "replay-wipe"
    assert stingers[0]["has_alpha"] is True
    assert stingers[0]["cut_frame"] > 0
    assert mixer.get_stinger("replay-wipe").frame_count == mixer.settings.stinger_frame_count


def test_storage_clips_lists_what_a_clip_player_can_play(client: TestClient) -> None:
    clips = client.get("/api/v1/storage/clips").json()
    # A new library holds the colour bars still.
    assert [c["name"] for c in clips] == ["Colour bars"]


def test_replay_and_stinger_tick_routes_are_gone(client: TestClient) -> None:
    for path in ("/api/v1/replay/load", "/api/v1/replay/take", "/api/v1/replay/return", "/api/v1/stinger/tick"):
        assert client.post(path, json={}).status_code in {404, 405}, path
