"""A clip player's end action (docs/clip-end.md): the setting (API, saved state, export), when
each action starts for a clip's last grain, the take held by the engine for that grain, the
conditions (on Program, not looping, another Preview, no transition running), several MEs, a
stinger, and what cancels it."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from flowxer.api.schemas import (
    ClipEnd,
    LogicalInputUpdate,
    MixerStartRequest,
    PanelTransitionUpdate,
    TransitionType,
    WorkspaceUpdate,
)
from flowxer.engine.mixer import CLIP_END_LEAD, VisionMixer


class _Engine:
    """Stands in for the engine process: ready, no status; commands are recorded by the mixer's
    _engine_call (replaced) or here (player commands)."""

    ready = True
    status = None
    state = "running"
    last_error = None

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []

    def call(self, method: str, path: str, body=None):
        self.calls.append((method, path, body or {}))
        return {}

    def summary(self) -> dict:
        return {"state": "running"}


def _on_air(mixer: VisionMixer, on_end: ClipEnd, **fields) -> list[tuple[str, str, dict]]:
    """ME 1: clip-1 (with `on_end`) on Program, cam-2 on Preview; the engine calls recorded."""
    mixer.update_input("clip-1", LogicalInputUpdate(on_end=on_end, **fields))
    mixer.start(MixerStartRequest(program_input_id="clip-1", preview_input_id="cam-2"))
    mixer.engine = _Engine()
    calls: list[tuple[str, str, dict]] = []
    mixer._engine_call = lambda method, path, body, client=None: calls.append((method, path, body))
    return calls


def _players(index: int, end: int | None, state: str = "playing") -> dict:
    return {"index": index, "players": [{"player": 1, "state": state, "end_index": end, "loop": False}]}


def _takes(calls) -> list[tuple[str, dict]]:
    return [(path, body) for _, path, body in calls if path.endswith("/take")]


def _wait_for(condition, timeout: float = 1.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if condition():
            return True
        time.sleep(0.01)
    return condition()


@pytest.fixture(autouse=True)
def _stop(mixer: VisionMixer):
    yield
    mixer._cancel_clip_end()
    mixer.engine = None


def test_the_setting(client: TestClient, mixer: VisionMixer) -> None:
    assert client.get("/api/v1/inputs/clip-1").json()["on_end"] == "off"
    assert client.patch("/api/v1/inputs/clip-1", json={"on_end": "auto"}).json()["on_end"] == "auto"
    assert client.patch("/api/v1/inputs/clip-1", json={"on_end": "fade"}).status_code == 422
    exported = client.get("/api/v1/config/export").json()
    assert next(i for i in exported["inputs"] if i["id"] == "clip-1")["on_end"] == "auto"
    # A state from before: off.
    for item in exported["inputs"]:
        item.pop("on_end", None)
    assert client.post("/api/v1/config/import", json=exported).status_code == 200
    assert mixer.get_input("clip-1").on_end == ClipEnd.off


def test_a_cut_on_the_grain_after_the_last_frame(mixer: VisionMixer) -> None:
    calls = _on_air(mixer, ClipEnd.cut)
    panel = mixer.panels[0]
    assert mixer._clip_end_start(panel, 1005) == 1006
    mixer._clip_end_tick(_players(1000, 1005), time.monotonic())
    # Sent CLIP_END_LEAD grains early (60 ms at 50p), held by the engine for grain 1006.
    assert _wait_for(lambda: _takes(calls))
    path, body = _takes(calls)[0]
    assert path == "/me/1/take" and body["at"] == 1006 and body["frames"] == 0 and body["source"] == "in2" and body["preview"] == "cp1"
    assert panel.program_input_id == "cam-2" and panel.preview_input_id == "clip-1" and mixer.program_input_id == "cam-2"
    assert mixer.transition_counts["cut"] >= 1
    # Done once for that end.
    mixer.take("clip-1")
    calls.clear()
    mixer._clip_end_tick(_players(1006, 1005, "ended"), time.monotonic())
    time.sleep(0.15)
    assert not _takes(calls)


def test_an_auto_ends_on_the_last_frame(mixer: VisionMixer) -> None:
    calls = _on_air(mixer, ClipEnd.auto)
    mixer.update_transition("me-1", PanelTransitionUpdate(type=TransitionType.wipe, rate_frames=10, ties=["me-1-key-1"]))
    panel = mixer.panels[0]
    # Its last grain (the target in full) is the clip's last frame: 1011 .. 1020.
    assert mixer._clip_end_start(panel, 1020) == 1011
    # Beyond the horizon: nothing yet.
    mixer._clip_end_tick(_players(990, 1020), time.monotonic())
    assert not mixer._clip_end_timers
    mixer._clip_end_tick(_players(1000, 1020), time.monotonic())
    assert mixer._clip_end_timers["me-1"][:2] == ("clip-1", 1011)
    assert _wait_for(lambda: _takes(calls))
    path, body = _takes(calls)[0]
    assert body == {"source": "in2", "frames": 10, "type": "wipe", "preview": "cp1", "ties": True, "at": 1011}
    # The tied keyer goes with it, the tally holds both sources.
    assert mixer.get_keyer("me-1-key-1").enabled and mixer._mixes["me-1"][:2] == ("clip-1", "cam-2")


def test_a_clip_shorter_than_the_transition_starts_it_at_once(mixer: VisionMixer) -> None:
    calls = _on_air(mixer, ClipEnd.auto)
    mixer._clip_end_tick(_players(1000, 1005), time.monotonic())
    assert _wait_for(lambda: _takes(calls))
    body = _takes(calls)[0][1]
    assert "at" not in body and body["frames"] == 25


def test_a_stinger_with_its_cut_point_on_the_last_frame(mixer: VisionMixer, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _on_air(mixer, ClipEnd.stinger)
    media = {"path": "/stingers/s.mkv", "alpha": "", "audio": "", "channels": 2, "frames": 20}
    monkeypatch.setattr(mixer, "_stinger_media", lambda slot, stinger_id=None: (media, 8))
    panel = mixer.panels[0]
    assert mixer._clip_end_start(panel, 1010) == 1002
    mixer._clip_end_tick(_players(995, 1010), time.monotonic())
    # Seen: its stinger is cued at once (on stinger player 1).
    assert ("POST", "/stinger/1/load", media) in calls
    assert _wait_for(lambda: _takes(calls))
    body = _takes(calls)[0][1]
    assert body["type"] == "stinger" and body["stinger"] == 1 and body["cut"] == 8 and body["at"] == 1002
    # Without a stinger it cuts on the grain after the last frame.
    monkeypatch.setattr(mixer, "_stinger_media", lambda slot, stinger_id=None: None)
    assert mixer._clip_end_start(panel, 1010) == 1011


def test_only_on_program_without_loop_and_another_preview(mixer: VisionMixer) -> None:
    calls = _on_air(mixer, ClipEnd.cut)
    panel = mixer.panels[0]
    for change in (
        lambda: mixer.update_input("clip-1", LogicalInputUpdate(loop=True)),
        lambda: mixer.set_preview("clip-1"),
        lambda: mixer.update_input("clip-1", LogicalInputUpdate(on_end=ClipEnd.off)),
    ):
        mixer.update_input("clip-1", LogicalInputUpdate(loop=False, on_end=ClipEnd.cut))
        mixer.set_preview("cam-2")
        change()
        assert mixer._clip_end_action(panel) == ClipEnd.off
        mixer._clip_end_tick(_players(1000, 1002), time.monotonic())
        assert not mixer._clip_end_timers
    # An end long past (more than CLIP_END_LATE grains) does nothing, nor a clip that had ended
    # before it was seen playing on this ME's Program.
    mixer.update_input("clip-1", LogicalInputUpdate(on_end=ClipEnd.cut))
    mixer.set_preview("cam-2")
    mixer._clip_end_tick(_players(1100, 1002, "ended"), time.monotonic())
    mixer._clip_end_tick(_players(1010, 1005, "ended"), time.monotonic())
    assert not mixer._clip_end_timers and not _takes(calls)


def test_a_manual_take_or_a_stop_cancels_it(mixer: VisionMixer) -> None:
    calls = _on_air(mixer, ClipEnd.cut)
    mixer._clip_end_tick(_players(1000, 1010), time.monotonic())
    assert "me-1" in mixer._clip_end_timers
    mixer.take("cam-3")
    assert not mixer._clip_end_timers
    time.sleep(0.25)
    assert [body["source"] for _, body in _takes(calls)] == ["in3"]
    # A stop of the player before its end.
    mixer.take("clip-1")
    mixer.set_preview("cam-2")
    mixer._clip_end_tick(_players(1000, 1010), time.monotonic())
    assert "me-1" in mixer._clip_end_timers
    mixer.player_command("clip-1", "stop")
    assert not mixer._clip_end_timers and mixer.engine.calls[-1][1] == "/player/1/stop"


def test_not_while_a_transition_runs(mixer: VisionMixer) -> None:
    calls = _on_air(mixer, ClipEnd.cut)
    mixer._mixes["me-1"] = ("cam-1", "clip-1", time.monotonic() + 5)
    mixer._clip_end_tick(_players(1000, 1005), time.monotonic())
    time.sleep(0.2)
    assert not _takes(calls) and "me-1" not in mixer._clip_end_done
    # Once it has ended, the next read acts (at once: late).
    del mixer._mixes["me-1"]
    mixer._clip_end_tick(_players(1010, 1005, "ended"), time.monotonic())
    assert _wait_for(lambda: _takes(calls)) and "at" not in _takes(calls)[0][1]


def test_each_me_with_it_on_program(mixer: VisionMixer) -> None:
    mixer.apply_workspace(WorkspaceUpdate(mixer_panel_count=2))
    calls = _on_air(mixer, ClipEnd.cut)
    mixer.take("clip-1", panel_id="me-2")
    mixer.set_preview("cam-4", panel_id="me-2")
    calls.clear()
    mixer._clip_end_tick(_players(1000, 1005), time.monotonic())
    assert _wait_for(lambda: len(_takes(calls)) == 2)
    assert sorted((path, body["source"], body["at"]) for path, body in _takes(calls)) == [("/me/1/take", "in2", 1006), ("/me/2/take", "in4", 1006)]


def test_the_lead(mixer: VisionMixer) -> None:
    """The take is sent CLIP_END_LEAD grains before its grain (a cut 12 grains ahead: ~180 ms)."""
    calls = _on_air(mixer, ClipEnd.cut)
    seen = time.monotonic()
    mixer._clip_end_tick(_players(1000, 1011), seen)
    assert _wait_for(lambda: _takes(calls), timeout=2.0)
    elapsed = time.monotonic() - seen
    expected = (1012 - CLIP_END_LEAD - 1000) / mixer.settings.fps
    assert expected - 0.01 <= elapsed <= expected + 0.25
