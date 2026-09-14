from pathlib import Path

from fastapi.testclient import TestClient

from flowxer.api.schemas import MixerStartRequest
from flowxer.engine.mixer import VisionMixer


def test_stinger_cuts_to_replay_at_opaque_frame(mixer: VisionMixer) -> None:
    clip = mixer.settings.clips_dir / "replay.mp4"
    clip.write_bytes(b"clip")
    mixer.load_clip("replay", "replay.mp4")
    mixer.start(MixerStartRequest(program_input_id="cam-1"))
    assert mixer.program_input_id == "cam-1"

    mixer.take_replay("replay-wipe")
    stinger = mixer.get_stinger("replay-wipe")
    assert mixer.stinger_player is not None
    assert mixer.stinger_player.frame == 1
    assert mixer.program_input_id == "cam-1"

    remaining_to_cut = stinger.cut_frame - mixer.stinger_player.frame
    mixer.advance_stinger(remaining_to_cut)
    assert mixer.program_input_id == "replay"
    assert mixer.program_bus.value == "replay"
    assert mixer.stinger_player.cut_fired

    mixer.advance_stinger(stinger.frame_count)
    assert mixer.stinger_player is None
    assert mixer.program_input_id == "replay"


def test_stinger_returns_to_live(mixer: VisionMixer) -> None:
    clip = mixer.settings.clips_dir / "replay.mp4"
    clip.write_bytes(b"clip")
    mixer.load_clip("replay", "replay.mp4")
    mixer.start(MixerStartRequest(program_input_id="cam-1"))
    mixer.take_replay("replay-wipe")
    mixer.advance_stinger(64)
    assert mixer.program_input_id == "replay"

    mixer.return_live("replay-wipe")
    mixer.advance_stinger(64)
    assert mixer.program_input_id == "cam-1"
    assert mixer.program_bus.value == "live"


def test_replay_api_roundtrip(client: TestClient, mixer: VisionMixer) -> None:
    clip = mixer.settings.clips_dir / "highlight.mp4"
    clip.write_bytes(b"clip")
    assert client.post("/api/v1/mixer/start", json={"program_input_id": "cam-1"}).status_code == 200
    loaded = client.post("/api/v1/replay/load", json={"file_path": "highlight.mp4"})
    assert loaded.status_code == 200
    take = client.post("/api/v1/replay/take", json={"stinger_id": "replay-wipe"})
    assert take.status_code == 200
    client.post("/api/v1/stinger/tick?frames=64")
    status = client.get("/api/v1/mixer").json()
    assert status["program_input_id"] == "replay"
    back = client.post("/api/v1/replay/return", json={"stinger_id": "replay-wipe"})
    assert back.status_code == 200
    client.post("/api/v1/stinger/tick?frames=64")
    status = client.get("/api/v1/mixer").json()
    assert status["program_input_id"] == "cam-1"


def test_storage_lists_clips_and_stingers(client: TestClient, mixer: VisionMixer) -> None:
    (mixer.settings.clips_dir / "sizzle.ts").write_bytes(b"ts")
    clips = client.get("/api/v1/storage/clips").json()
    assert any(item["name"] == "sizzle.ts" for item in clips)
    stingers = client.get("/api/v1/storage/stingers").json()
    assert stingers[0]["id"] == "replay-wipe"
    assert stingers[0]["has_alpha"] is True
    assert stingers[0]["cut_frame"] > 0
