"""Snapshots (docs/snapshots.md): store, list, rename, delete; a recall with and without
Program/on-air and audio, of one ME and of all MEs; the start-up snapshot; start keeps ME 1's saved
buses; the saved state, export and import (a state without snapshots loads with none)."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from flowxer.api.schemas import (
    AudioStripUpdate,
    MixerStartRequest,
    PanelAudioUpdate,
    PanelTransitionUpdate,
    SnapshotOptionsUpdate,
    SnapshotStore,
    TransitionType,
    WipeSettingsUpdate,
    WorkspaceUpdate,
)
from flowxer.engine.mixer import VisionMixer
from flowxer.settings import Settings


def _record_engine(mixer: VisionMixer) -> list[tuple[str, str, dict]]:
    calls: list[tuple[str, str, dict]] = []
    mixer._engine_call = lambda method, path, body, client=None: calls.append((method, path, body))
    return calls


def _look_a(mixer: VisionMixer) -> None:
    """ME 1: cam-1 on Program, cam-2 on Preview, a wipe, key 1 a luma key of cam-3 on air, the DSK
    off, cam-1's fader at -6 dB."""
    mixer.take("cam-1")
    mixer.set_preview("cam-2")
    mixer.update_transition("me-1", PanelTransitionUpdate(type=TransitionType.wipe, rate_frames=40, wipe=WipeSettingsUpdate(pattern=119)))
    mixer.update_keyer("me-1-key-1", mode="luma", source_id="cam-3", clip=0.3, enabled=True)
    mixer.update_audio_strip("me-1", "cam-1", AudioStripUpdate(fader_db=-6))


def _look_b(mixer: VisionMixer) -> None:
    """Everything different from look A."""
    mixer.take("cam-4")
    mixer.set_preview("cam-5")
    mixer.update_transition("me-1", PanelTransitionUpdate(type=TransitionType.mix, rate_frames=10))
    mixer.update_keyer("me-1-key-1", mode="linear", clip=0.8, enabled=False)
    mixer.update_keyer("dsk-1", enabled=True, gain=0.5)
    mixer.update_audio_strip("me-1", "cam-1", AudioStripUpdate(fader_db=0, mute=True))
    mixer.update_panel_audio("me-1", PanelAudioUpdate(master_db=-3))


def test_store_list_rename_and_delete(client: TestClient, mixer: VisionMixer) -> None:
    mixer.apply_workspace(WorkspaceUpdate(mixer_panel_count=2))
    stored = client.post("/api/v1/snapshots", json={"panel_id": "me-1"}).json()
    assert stored["number"] == 1 and stored["name"] == "ME 1 #1" and list(stored["mes"]) == ["me-1"]
    assert stored["options"] == {"program": False, "audio": False}
    me1 = stored["mes"]["me-1"]
    assert set(me1) == {"program_input_id", "preview_input_id", "ftb", "transition", "keyers", "audio"}
    assert list(me1["keyers"]) == ["me-1-key-1", "me-1-key-2", "dsk-1"]
    assert "label" not in me1["keyers"]["dsk-1"] and "keyframes" in me1["keyers"]["me-1-key-1"]
    everything = client.post("/api/v1/snapshots", json={"panel_id": "all", "name": " Opener ", "options": {"program": True}}).json()
    assert everything["number"] == 2 and everything["name"] == "Opener" and list(everything["mes"]) == ["me-1", "me-2"]
    assert everything["options"] == {"program": True, "audio": False}
    # A chosen number; the list has no contents.
    assert client.post("/api/v1/snapshots", json={"panel_id": "me-2", "number": 10}).json()["name"] == "ME 2 #10"
    listed = client.get("/api/v1/snapshots").json()
    assert listed["startup"] is None and [s["number"] for s in listed["snapshots"]] == [1, 2, 10]
    assert "mes" not in listed["snapshots"][0] and listed["snapshots"][1]["panel_id"] == "all"
    # Storing over a used number keeps its name and options unless given.
    over = client.post("/api/v1/snapshots", json={"panel_id": "me-1", "number": 2, "options": {"audio": True}}).json()
    assert over["name"] == "Opener" and over["options"] == {"program": True, "audio": True} and list(over["mes"]) == ["me-1"]
    # Rename and the recall's defaults.
    renamed = client.patch("/api/v1/snapshots/2", json={"name": "Interview", "options": {"program": False}}).json()
    assert renamed["name"] == "Interview" and renamed["options"] == {"program": False, "audio": True}
    assert client.patch("/api/v1/snapshots/2", json={"name": "  "}).status_code == 409
    assert client.patch("/api/v1/snapshots/3", json={"name": "x"}).status_code == 404
    assert client.get("/api/v1/snapshots/2").json()["name"] == "Interview"
    assert client.get("/api/v1/snapshots/100").status_code == 422
    assert client.post("/api/v1/snapshots", json={"panel_id": "me-9"}).status_code == 404
    assert client.post("/api/v1/snapshots", json={"panel_id": "me-1", "number": 0}).status_code == 422
    assert client.delete("/api/v1/snapshots/10").status_code == 204
    assert client.delete("/api/v1/snapshots/10").status_code == 404
    assert [s["number"] for s in client.get("/api/v1/snapshots").json()["snapshots"]] == [1, 2]


def test_99_numbers_at_most(mixer: VisionMixer) -> None:
    for _ in range(99):
        mixer.store_snapshot(SnapshotStore(panel_id="me-1"))
    with pytest.raises(Exception, match="every snapshot number"):
        mixer.store_snapshot(SnapshotStore(panel_id="me-1"))
    mixer.delete_snapshot(42)
    assert mixer.store_snapshot(SnapshotStore(panel_id="me-1")).number == 42


def test_a_recall_without_program_leaves_what_is_on_air(mixer: VisionMixer) -> None:
    mixer.start(MixerStartRequest(program_input_id="cam-1"))
    _look_a(mixer)
    mixer.store_snapshot(SnapshotStore(panel_id="me-1", number=1))
    _look_b(mixer)
    mixer.fade_to_black("me-1")
    calls = _record_engine(mixer)
    result = mixer.recall_snapshot(1)
    panel = mixer.panels[0]
    key1, dsk = mixer.get_keyer("me-1-key-1"), mixer.get_keyer("dsk-1")
    # Preview, the transition and the settings of keyers off air change.
    assert panel.preview_input_id == "cam-2" and mixer.preview_input_id == "cam-2"
    assert panel.transition.type == TransitionType.wipe and panel.transition.rate_frames == 40 and panel.transition.wipe.pattern == 119
    assert key1.mode.value == "luma" and key1.source_id == "cam-3" and key1.clip == 0.3
    # Program, the keys' on-air state, FTB and audio do not; nor the settings of a keyer on air.
    assert panel.program_input_id == "cam-4" and mixer.program_input_id == "cam-4" and panel.ftb
    assert not key1.enabled and dsk.enabled and dsk.gain == 0.5
    assert result["skipped"] == ["DSK 1: kept, it is on air (recall with Program/on-air to change it)"]
    assert result["options"] == {"program": False, "audio": False}
    assert mixer.audio.panels["me-1"].master_db == -3.0
    paths = [path for _, path, _ in calls]
    assert "/me/1/preview" in paths and "/me/1/transition" in paths and "/keyer/1/settings" in paths
    assert not any(path.endswith(("/take", "/ftb", "/audio")) or path in ("/keyer/1", "/keyer/3") for path in paths), paths


def test_a_recall_with_program_cuts(mixer: VisionMixer) -> None:
    mixer.start(MixerStartRequest(program_input_id="cam-1"))
    _look_a(mixer)
    mixer.store_snapshot(SnapshotStore(panel_id="me-1", number=1, options=SnapshotOptionsUpdate(program=True)))
    _look_b(mixer)
    mixer.fade_to_black("me-1")
    calls = _record_engine(mixer)
    result = mixer.recall_snapshot(1)
    panel = mixer.panels[0]
    key1, dsk = mixer.get_keyer("me-1-key-1"), mixer.get_keyer("dsk-1")
    assert result["skipped"] == [] and result["options"] == {"program": True, "audio": False}
    assert panel.program_input_id == "cam-1" and mixer.program_input_id == "cam-1" and panel.preview_input_id == "cam-2"
    assert key1.enabled and key1.mode.value == "luma" and not dsk.enabled and dsk.gain == 1.0 and not panel.ftb
    # Audio stays (its own option).
    assert mixer.audio.panels["me-1"].master_db == -3.0
    # Cuts, in this order: the DSK off, Program, Preview, the transition, the settings, key 1 on,
    # FTB off last.
    assert [(path, body) for _, path, body in calls if path in ("/keyer/1", "/keyer/3", "/me/1/ftb", "/me/1/take")] == [
        ("/keyer/3", {"on": False}),
        ("/me/1/take", {"source": "in1", "frames": 0}),
        ("/keyer/1", {"on": True}),
        ("/me/1/ftb", {"on": False}),
    ]
    paths = [path for _, path, _ in calls]
    assert paths.index("/me/1/take") < paths.index("/me/1/preview") < paths.index("/keyer/1/settings") < paths.index("/keyer/1")
    assert "/me/1/ftb/auto" not in paths
    # A snapshot with FTB on goes to black first.
    mixer.fade_to_black("me-1")
    mixer.store_snapshot(SnapshotStore(panel_id="me-1", number=2))
    mixer.fade_to_black("me-1")
    mixer.take("cam-6")
    calls.clear()
    mixer.recall_snapshot(2, SnapshotOptionsUpdate(program=True))
    assert calls[0][1:] == ("/me/1/ftb", {"on": True}) and panel.ftb and panel.program_input_id == "cam-1"


def test_a_recall_with_audio(mixer: VisionMixer) -> None:
    mixer.start(MixerStartRequest(program_input_id="cam-1"))
    _look_a(mixer)
    mixer.store_snapshot(SnapshotStore(panel_id="me-1", number=1, options=SnapshotOptionsUpdate(audio=True)))
    _look_b(mixer)
    mixer.update_audio_strip("me-1", "clip-1", AudioStripUpdate(afv=False))
    calls = _record_engine(mixer)
    result = mixer.recall_snapshot(1)
    assert result["options"] == {"program": False, "audio": True}
    # The snapshot's strips; a strip it does not name goes to its defaults.
    assert mixer.audio_state()["panels"]["me-1"]["master_db"] == 0.0
    strips = mixer.audio_state()["panels"]["me-1"]["strips"]
    assert strips["cam-1"]["fader_db"] == -6.0 and not strips["cam-1"]["mute"] and strips["clip-1"]["afv"] is True
    audio = [body for _, path, body in calls if path == "/me/1/audio"]
    assert len(audio) == 1 and audio[0]["master_db"] == 0.0 and audio[0]["strips"]["in1"] == {"fader_db": -6.0, "mute": False, "afv": True}
    # Overridden per recall: without audio this time.
    mixer.update_panel_audio("me-1", PanelAudioUpdate(master_db=-10))
    mixer.recall_snapshot(1, SnapshotOptionsUpdate(audio=False))
    assert mixer.audio.panels["me-1"].master_db == -10.0


def test_an_all_mes_snapshot(client: TestClient, mixer: VisionMixer) -> None:
    mixer.apply_workspace(WorkspaceUpdate(mixer_panel_count=2))
    mixer.start(MixerStartRequest(program_input_id="cam-1"))
    mixer.take("me-2")
    mixer.take("cam-3", panel_id="me-2")
    mixer.set_preview("cam-4", panel_id="me-2")
    mixer.update_keyer("me-2-key-1", enabled=True)
    client.post("/api/v1/snapshots", json={"panel_id": "all", "number": 5, "options": {"program": True}})
    mixer.take("cam-2")
    mixer.take("cam-5", panel_id="me-2")
    mixer.set_preview("cam-6", panel_id="me-2")
    mixer.update_keyer("me-2-key-1", enabled=False)
    reply = client.post("/api/v1/snapshots/5/recall")
    assert reply.status_code == 200 and reply.json()["skipped"] == []
    me1, me2 = mixer.panels
    assert me1.program_input_id == "me-2" and me2.program_input_id == "cam-3" and me2.preview_input_id == "cam-4"
    assert mixer.get_keyer("me-2-key-1").enabled
    # An ME snapshot of an ME the console no longer has: 409; an all-MEs one skips it.
    client.post("/api/v1/snapshots", json={"panel_id": "me-2", "number": 6})
    mixer.stop()
    mixer.apply_workspace(WorkspaceUpdate(mixer_panel_count=1))
    mixer.panels[0].program_input_id = "cam-1"
    assert client.post("/api/v1/snapshots/6/recall").status_code == 409
    reply = client.post("/api/v1/snapshots/5/recall").json()
    assert "me-2: not on the console" in reply["skipped"]
    assert any(item.startswith("ME 1 Program") for item in reply["skipped"]), reply["skipped"]


def test_what_is_gone_is_skipped(mixer: VisionMixer) -> None:
    mixer.update_transition("me-1", PanelTransitionUpdate(type=TransitionType.stinger, stinger_slot_id=mixer.stinger_slots[0].id))
    mixer.update_transition("me-1", PanelTransitionUpdate(ties=["me-1-key-2"], background=False))
    mixer.update_keyer("me-1-key-2", mode="luma", source_id="cam-6")
    mixer.panels[0].preview_input_id = "cam-6"
    mixer.store_snapshot(SnapshotStore(panel_id="me-1", number=1))
    mixer.apply_workspace(WorkspaceUpdate(logical_source_count=4, keyers_per_me=1))
    result = mixer.recall_snapshot(1)
    assert "ME 1: keyer me-1-key-2 is not on the console" in result["skipped"]
    assert any(item.startswith("ME 1 Preview: unknown input") for item in result["skipped"]), result["skipped"]
    # The tie to a keyer that is gone is dropped (BKGD comes back on); the rest of the transition is recalled.
    transition = mixer.panels[0].transition
    assert transition.type == TransitionType.stinger and transition.ties == [] and transition.background


def test_an_off_air_recall_and_the_start_up_snapshot(client: TestClient, mixer: VisionMixer) -> None:
    mixer.start(MixerStartRequest(program_input_id="cam-1"))
    _look_a(mixer)
    mixer.store_snapshot(SnapshotStore(panel_id="me-1", number=7, options=SnapshotOptionsUpdate(program=True)))
    _look_b(mixer)
    mixer.stop()
    # Off-air: only the state.
    assert client.post("/api/v1/snapshots/7/recall", json={"program": False}).json()["skipped"] == ["DSK 1: kept, it is on air (recall with Program/on-air to change it)"]
    assert mixer.panels[0].program_input_id == "cam-4" and mixer.state.value == "idle"
    assert client.put("/api/v1/snapshots/startup", json={"number": 8}).status_code == 404
    assert client.put("/api/v1/snapshots/startup", json={"number": 7}).json()["startup"] == 7
    # The console carries the list (for the snapshot panel and the widget).
    assert client.get("/api/v1/console").json()["snapshots"]["startup"] == 7
    # The GUI's start ({}): the start-up snapshot (with Program/on-air) sets the start look.
    status = client.post("/api/v1/mixer/start", json={}).json()["mixer"]
    assert status["program_input_id"] == "cam-1" and status["preview_input_id"] == "cam-2"
    assert mixer.get_keyer("me-1-key-1").enabled and not mixer.get_keyer("dsk-1").enabled
    # Without Program/on-air it sets Preview only (Program stays the saved one).
    client.post("/api/v1/mixer/stop")
    mixer.take("cam-6")
    mixer.stop()
    client.patch("/api/v1/snapshots/7", json={"options": {"program": False}})
    status = client.post("/api/v1/mixer/start", json={}).json()["mixer"]
    assert status["program_input_id"] == "cam-6" and status["preview_input_id"] == "cam-2"
    # Deleting it clears the start-up snapshot.
    assert client.delete("/api/v1/snapshots/7").status_code == 204
    assert client.get("/api/v1/snapshots").json()["startup"] is None


def test_start_keeps_the_saved_buses(client: TestClient, mixer: VisionMixer, settings: Settings) -> None:
    mixer.apply_workspace(WorkspaceUpdate(mixer_panel_count=2))
    mixer.start(MixerStartRequest(program_input_id="cam-3", preview_input_id="cam-4"))
    mixer.take("cam-5", panel_id="me-2")
    mixer.stop()
    mixer.persist()
    # A restart of the control plane, then the GUI's start ({}): both MEs keep their buses.
    again = VisionMixer(settings)
    status = again.start(MixerStartRequest())
    assert (status.program_input_id, status.preview_input_id) == ("cam-3", "cam-4")
    assert again.panels[1].program_input_id == "cam-5" and again.last_live_input_id == "cam-3"
    again.stop()
    # Preview off-air starts with the saved Program.
    again.set_preview("cam-6")
    assert (again.program_input_id, again.preview_input_id) == ("cam-3", "cam-6")
    again.stop()
    # A request's bus wins over the saved one; a saved source that is gone falls back to the first input.
    assert again.start(MixerStartRequest(program_input_id="cam-2")).preview_input_id == "cam-6"
    again.stop()
    again.panels[0].program_input_id = "nope"
    again.panels[0].preview_input_id = None
    status = again.start(MixerStartRequest())
    assert (status.program_input_id, status.preview_input_id) == ("cam-1", "cam-1")


def test_snapshots_in_the_saved_state_export_and_import(client: TestClient, mixer: VisionMixer, settings: Settings) -> None:
    client.post("/api/v1/snapshots", json={"panel_id": "me-1", "number": 3, "name": "Wide"})
    client.put("/api/v1/snapshots/startup", json={"number": 3})
    saved = json.loads((settings.state_dir / "state.json").read_text(encoding="utf-8"))
    assert saved["snapshots"]["startup"] == 3 and saved["snapshots"]["items"][0]["name"] == "Wide"
    again = VisionMixer(settings)
    assert again.snapshots_view()["startup"] == 3 and again.get_snapshot(3).mes["me-1"].transition.type == TransitionType.mix
    exported = client.get("/api/v1/config/export").json()
    assert exported["snapshots"]["items"][0]["number"] == 3
    # An older state (no snapshots) imports with none.
    old = {key: value for key, value in exported.items() if key != "snapshots"}
    assert client.post("/api/v1/config/import", json=old).status_code == 200
    assert client.get("/api/v1/snapshots").json() == {"startup": None, "snapshots": []}
    assert client.post("/api/v1/config/import", json=exported).status_code == 200
    assert client.get("/api/v1/snapshots").json()["startup"] == 3
    # Broken snapshots are refused.
    for broken in (
        {"startup": 4, "items": exported["snapshots"]["items"]},
        {"items": exported["snapshots"]["items"] * 2},
        {"items": [dict(exported["snapshots"]["items"][0], number=100)]},
    ):
        assert client.post("/api/v1/config/import", json=dict(exported, snapshots=broken)).status_code == 422, broken


def test_the_engine_gets_a_recall(tmp_path, settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    """With the fake engine: a recall with Program/on-air sends the cut to the engine; the engine's
    buses follow."""
    import socket
    import sys
    from pathlib import Path

    log = tmp_path / "engine-commands.jsonl"
    monkeypatch.setenv("FAKE_ENGINE_LOG", str(log))
    settings.simulate = False
    fake = Path(__file__).with_name("fake_engine.py")
    settings.engine_bin = f'"{Path(sys.executable).as_posix()}" "{fake.as_posix()}"'
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        settings.engine_port = sock.getsockname()[1]
    mixer = VisionMixer(settings)
    try:
        mixer.start(MixerStartRequest(program_input_id="cam-2", preview_input_id="cam-3"))
        mixer.store_snapshot(SnapshotStore(panel_id="me-1", number=1))
        mixer.take("cam-4")
        before = len(log.read_text(encoding="utf-8").splitlines())
        mixer.recall_snapshot(1, SnapshotOptionsUpdate(program=True))
        commands = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()[before:]]
        assert {"path": "/me/1/take", "body": {"source": "in2", "frames": 0}} in [{"path": c["path"], "body": c["body"]} for c in commands]
        assert mixer.panels[0].program_input_id == "cam-2" and mixer.panels[0].preview_input_id == "cam-3"
    finally:
        mixer.stop()
