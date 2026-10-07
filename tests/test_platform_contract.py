"""Platform contract (mxl-poc-platform G1-G10): settings, saved state, registry, shutdown."""

from __future__ import annotations

import json
import socket
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from flowxer import app as app_module
from flowxer.api.metrics import ready_payload
from flowxer.api.schemas import (
    AudioEssence,
    InputKind,
    LogicalInputUpdate,
    TallyReceiver,
    VideoEssence,
    WorkspaceUpdate,
)
from flowxer.domain.mxl_domain import remove_output_domain
from flowxer.engine.mixer import VisionMixer
from flowxer.nmos import ids
from flowxer.nmos import service as nmos_service
from flowxer.nmos.http import create_nmos_app
from flowxer.settings import Settings, get_settings

VIDEO_FLOW = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbb1"
SENDER_ID = "dddddddd-dddd-dddd-dddd-ddddddddddd1"


def test_platform_env_names_win_over_flowxer_names(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MXL_DOMAIN_SCAN_PATH", str(tmp_path / "mxl"))
    monkeypatch.setenv("FLOWXER_MXL_ROOT", str(tmp_path / "other"))
    monkeypatch.setenv("MXL_OUTPUT_DOMAIN_ID", "domain-1")
    monkeypatch.setenv("MXL_HISTORY_DURATION", "400000000")
    monkeypatch.setenv("MXL_CLEANUP_ON_EXIT", "true")
    monkeypatch.setenv("NMOS_SEED", "prod-flowxer")
    monkeypatch.setenv("NMOS_LABEL", "Mixer 1")
    monkeypatch.setenv("NMOS_TAGS", '{"urn:x-srf:production":["sport-sa"]}')
    monkeypatch.setenv("NMOS_REGISTRY_ADDRESS", "10.0.0.5")
    monkeypatch.setenv("NMOS_REGISTRY_PORT", "8010")
    monkeypatch.setenv("NMOS_HOST_ADDRESS", "10.1.2.3")
    monkeypatch.setenv("NMOS_PORT", "3300")
    monkeypatch.setenv("SHUTDOWN_TIMEOUT_S", "20")
    settings = Settings(_env_file=None)
    assert settings.mxl_root == tmp_path / "mxl"
    assert settings.resolved_output_domain_id == "domain-1"
    assert settings.mxl_history_duration_ns == 400_000_000
    assert settings.mxl_cleanup_on_exit is True
    assert settings.resolved_nmos_seed == "prod-flowxer"
    assert settings.node_label == "Mixer 1"
    assert settings.nmos_tags == {"urn:x-srf:production": ["sport-sa"]}
    assert settings.resolved_registry_url == "http://10.0.0.5:8010"
    assert settings.nmos_host_ip == "10.1.2.3"
    assert settings.nmos_port == 3300
    assert settings.shutdown_timeout_s == 20


def test_nmos_label_and_tags_on_node_and_device(settings: Settings) -> None:
    settings.nmos_label = "Mixer 1"
    settings.nmos_tags = {"urn:x-srf:function": ["mixer1"]}
    mixer = VisionMixer(settings)
    for resource in (mixer.nmos.self_resource(), mixer.nmos.device_resource()):
        assert resource["label"] == "Mixer 1"
        assert resource["tags"] == {"urn:x-srf:function": ["mixer1"]}


def _run_exit_code(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, **env: str) -> int:
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.chdir(tmp_path)  # no .env file
    get_settings.cache_clear()
    try:
        with pytest.raises(SystemExit) as exc:
            app_module.run()
    finally:
        get_settings.cache_clear()
    return int(exc.value.code)


def test_invalid_setting_exits_78(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    assert _run_exit_code(monkeypatch, tmp_path, FLOWXER_PORT="not-a-port") == 78


def test_taken_port_exits_75(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    taken = socket.socket()
    taken.bind(("127.0.0.1", 0))
    taken.listen(1)
    try:
        port = str(taken.getsockname()[1])
        assert _run_exit_code(monkeypatch, tmp_path, FLOWXER_HOST="127.0.0.1", FLOWXER_PORT=port) == 75
    finally:
        taken.close()


def test_state_survives_a_restart(settings: Settings) -> None:
    mixer = VisionMixer(settings)
    mixer.apply_workspace(WorkspaceUpdate(logical_source_count=4, source_tile_aspect="9:16"))
    mixer.update_input("cam-1", LogicalInputUpdate(label="Studio A"))
    mixer.replace_tally_receivers([TallyReceiver(id="vsm", label="VSM", host="127.0.0.1")])
    mixer.update_keyer("dsk-1", title="Breaking")
    mixer.persist()
    assert (settings.state_dir / "state.json").is_file()

    again = VisionMixer(settings)
    assert again.workspace.logical_source_count == 4
    assert again.workspace.source_tile_aspect == "9:16"
    assert len(again.inputs) == 4
    assert again.get_input("cam-1").label == "Studio A"
    assert [item.id for item in again.tally.receivers] == ["vsm"]
    assert again.keyers[0].title == "Breaking"
    assert again.overlay.title == "Breaking"


def test_broken_state_file_starts_with_defaults(settings: Settings) -> None:
    settings.state_dir.mkdir(parents=True)
    (settings.state_dir / "state.json").write_text("{not json", encoding="utf-8")
    mixer = VisionMixer(settings)
    assert mixer.workspace.logical_source_count == 8


def test_api_changes_are_saved(client: TestClient, settings: Settings) -> None:
    response = client.put("/api/v1/workspace", json={"logical_source_count": 3})
    assert response.status_code == 200
    saved = json.loads((settings.state_dir / "state.json").read_text(encoding="utf-8"))
    assert saved["workspace"]["logical_source_count"] == 3
    assert len(saved["inputs"]) == 3


def test_config_export_and_import(client: TestClient, mixer: VisionMixer) -> None:
    client.put("/api/v1/workspace", json={"logical_source_count": 5})
    exported = client.get("/api/v1/config/export").json()
    assert exported["format"] == "flowxer-config/1"
    assert "api_token" not in json.dumps(exported)

    client.put("/api/v1/workspace", json={"logical_source_count": 3})
    response = client.post("/api/v1/config/import", json=exported)
    assert response.status_code == 200, response.text
    assert mixer.workspace.logical_source_count == 5
    assert len(mixer.inputs) == 5

    assert client.post("/api/v1/config/import", json={"format": "other"}).status_code == 422
    escape = json.loads(json.dumps(exported))
    escape["inputs"][0].update(kind="file", file_path="../../../../etc/passwd")
    assert client.post("/api/v1/config/import", json=escape).status_code == 422
    assert mixer.workspace.logical_source_count == 5

    mixer.start()
    assert client.post("/api/v1/config/import", json=exported).status_code == 409
    mixer.stop()


def _routed_settings(settings: Settings) -> Settings:
    settings.nmos_enable = True
    return settings


def test_receiver_routes_survive_a_restart(settings: Settings) -> None:
    mixer = VisionMixer(_routed_settings(settings))
    mixer.update_input(
        "cam-1",
        LogicalInputUpdate(kind=InputKind.mxl_live, video=VideoEssence(), audio=AudioEssence()),
    )
    rid = ids.receiver_id(settings.resolved_nmos_seed, "cam-1", "video")
    client = TestClient(create_nmos_app(mixer.nmos))
    response = client.patch(
        f"/x-nmos/connection/v1.2/single/receivers/{rid}/staged",
        json={
            "sender_id": SENDER_ID,
            "master_enable": False,
            "activation": {"mode": "activate_immediate"},
            "transport_params": [{"mxl_flow_id": VIDEO_FLOW}],
        },
    )
    assert response.status_code == 200, response.text

    again = VisionMixer(settings)
    active = again.nmos.active(rid, "receivers")
    assert active["sender_id"] == SENDER_ID
    assert active["master_enable"] is False
    assert active["transport_params"][0]["mxl_flow_id"] == VIDEO_FLOW
    assert again.nmos.input_state("cam-1", "video") == "not_routed"


class _FakeRegistry:
    timeout_s = 0.1
    calls: list[tuple[str, str]] = []
    fail = False

    def __init__(self, base_url: str) -> None:
        self.base = base_url

    def register(self, resource_type: str, data: dict) -> None:
        self.calls.append(("register", resource_type))

    def heartbeat(self, node_id: str) -> None:
        if self.fail:
            raise OSError("registry down")
        self.calls.append(("heartbeat", node_id))

    def delete(self, resource_type: str, resource_id: str) -> None:
        self.calls.append(("delete", resource_type))

    def delete_node(self, node_id: str) -> None:
        self.calls.append(("delete", "node"))


def _wait_for(predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.01)


@pytest.fixture
def registry(monkeypatch: pytest.MonkeyPatch, settings: Settings):
    _FakeRegistry.calls = []
    _FakeRegistry.fail = False
    monkeypatch.setattr(nmos_service, "RegistryClient", _FakeRegistry)
    monkeypatch.setattr(nmos_service, "HEARTBEAT_S", 0.01)
    settings.nmos_enable = True
    settings.nmos_registry_url = "http://registry.test:8010"
    return _FakeRegistry


def test_registers_once_then_heartbeats_and_deregisters(registry, settings: Settings) -> None:
    mixer = VisionMixer(settings)
    mixer.nmos.boot()
    try:
        _wait_for(lambda: registry.calls.count(("heartbeat", mixer.nmos.node_uuid)) >= 3)
        assert registry.calls.count(("register", "node")) == 1
        assert mixer.nmos.registered()
        assert ready_payload(mixer)[0] == 200

        mixer.nmos.mark_changed()
        _wait_for(lambda: registry.calls.count(("register", "node")) == 2)
    finally:
        mixer.shutdown()
    assert registry.calls[-1] == ("delete", "node")
    assert not mixer.nmos.registered()


def test_not_ready_until_registered(registry, settings: Settings) -> None:
    registry.fail = True
    mixer = VisionMixer(settings)
    mixer.nmos.boot()
    try:
        _wait_for(lambda: ("register", "node") in registry.calls)
        code, body = ready_payload(mixer)
        assert code == 503
        assert any("not registered" in reason for reason in body["reasons"])
    finally:
        mixer.shutdown()


def test_shutdown_removes_only_the_own_output_domain(settings: Settings) -> None:
    mixer = VisionMixer(settings)
    output = settings.output_domain
    assert (output / "domain_def.json").is_file()
    mixer.shutdown()
    assert output.is_dir()

    settings.mxl_cleanup_on_exit = True
    mixer = VisionMixer(settings)
    mixer.shutdown()
    assert not output.exists()


def test_remove_output_domain_checks_id_and_root(tmp_path: Path) -> None:
    root = tmp_path / "mxl"
    other = root / "other"
    other.mkdir(parents=True)
    (other / "domain_def.json").write_text('{"id": "someone-else"}', encoding="utf-8")
    assert remove_output_domain(other, domain_id="mine", root=root) is False
    assert other.is_dir()
    (root / "domain_def.json").write_text('{"id": "mine"}', encoding="utf-8")
    assert remove_output_domain(root, domain_id="mine", root=root) is False
    assert root.is_dir()


def test_program_flow_ids_follow_the_seed(settings: Settings) -> None:
    mixers = [
        VisionMixer(settings),
        VisionMixer(settings),
        VisionMixer(settings.model_copy(update={"nmos_seed": "other-seed"})),
    ]
    for mixer in mixers:
        mixer.start()
    try:
        first, again, other = (mixer.outputs for mixer in mixers)
        assert first.video_flow_id == again.video_flow_id
        assert first.video_flow_id != other.video_flow_id
        assert first.audio_flow_id != other.audio_flow_id
    finally:
        for mixer in mixers:
            mixer.stop()


# Production structure from the environment (the platform's designer, plan §3.10).


def test_structure_settings_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FLOWXER_FORMAT", "720p50")
    monkeypatch.setenv("FLOWXER_LIVE_INPUTS", "3")
    monkeypatch.setenv("FLOWXER_INPUT_LABELS", '["Cam A", "Cam, B"]')
    monkeypatch.setenv("FLOWXER_TEST_SOURCES", "1")
    monkeypatch.setenv("FLOWXER_PANELS", "2")
    monkeypatch.setenv("FLOWXER_PROGRAM_AUTOSTART", "true")
    settings = Settings(_env_file=None)
    assert settings.live_input_labels == ["Cam A", "Cam, B", "Camera 3"]
    assert (settings.width, settings.height, settings.frame_rate) == (1280, 720, "50/1")
    assert settings.program_autostart is True
    assert settings.pinned_workspace == {
        "format_id": ("720p50", "FLOWXER_FORMAT"),
        "logical_source_count": (
            6,
            "FLOWXER_LIVE_INPUTS, FLOWXER_INPUT_LABELS, FLOWXER_TEST_SOURCES",
        ),
        "mixer_panel_count": (2, "FLOWXER_PANELS"),
    }

    monkeypatch.setenv("FLOWXER_INPUT_LABELS", " Cam A , Cam B ")
    assert Settings(_env_file=None).live_input_labels == ["Cam A", "Cam B", "Camera 3"]


def test_structure_settings_unset_or_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("FORMAT", "LIVE_INPUTS", "INPUT_LABELS", "TEST_SOURCES", "PANELS"):
        monkeypatch.setenv(f"FLOWXER_{name}", "")
    settings = Settings(_env_file=None)
    assert settings.pinned_workspace == {}
    assert settings.program_autostart is False


@pytest.mark.parametrize(
    "env",
    [
        {"FLOWXER_FORMAT": "1080i50"},
        {"FLOWXER_PANELS": "5"},
        {"FLOWXER_PANELS": "0"},
        {"FLOWXER_LIVE_INPUTS": "23"},
        {"FLOWXER_LIVE_INPUTS": "20", "FLOWXER_TEST_SOURCES": "3"},
        {"FLOWXER_LIVE_INPUTS": "1", "FLOWXER_INPUT_LABELS": "A,B"},
        {"FLOWXER_INPUT_LABELS": "A"},
        {"FLOWXER_LIVE_INPUTS": "2", "FLOWXER_INPUT_LABELS": "A,A"},
        {"FLOWXER_LIVE_INPUTS": "3", "FLOWXER_INPUT_LABELS": "A,,B"},
        {"FLOWXER_LIVE_INPUTS": "2", "FLOWXER_INPUT_LABELS": "[1, 2]"},
        {"FLOWXER_LIVE_INPUTS": "2", "FLOWXER_INPUT_LABELS": '["A", "B"'},
    ],
)
def test_invalid_structure_settings(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> None:
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_invalid_format_exits_78(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    assert _run_exit_code(monkeypatch, tmp_path, FLOWXER_FORMAT="1080i50") == 78


def _pin(settings: Settings, **values) -> Settings:
    # Attributes instead of the environment: the fixture's small raster stays.
    for name, value in values.items():
        setattr(settings, name, value)
    return settings


def test_env_structure_seeds_inputs_panels_and_nmos_labels(settings: Settings) -> None:
    _pin(settings, live_inputs=2, input_labels="Cam A,Cam B", test_sources=1, panels=2)
    mixer = VisionMixer(settings)
    inputs = mixer.list_inputs()
    assert [(item.id, item.kind, item.label) for item in inputs] == [
        ("cam-1", InputKind.mxl_live, "Cam A"),
        ("cam-2", InputKind.mxl_live, "Cam B"),
        ("test-1", InputKind.test, "Test 1"),
        ("black", InputKind.black, "Black"),
        ("replay", InputKind.replay, "Replay"),
    ]
    assert [item.slot for item in inputs] == [0, 1, 2, 3, 4]
    assert mixer.workspace.logical_source_count == 5
    assert [panel.label for panel in mixer.panels] == ["ME 1", "ME 2"]
    assert [item["label"] for item in mixer.nmos.receivers()] == [
        "Cam A Video",
        "Cam A Audio",
        "Cam B Video",
        "Cam B Audio",
    ]
    assert [item["label"] for item in mixer.nmos.senders()] == [
        "ME 1 PGM Video",
        "ME 1 PGM Audio",
        "ME 2 PGM Video",
        "ME 2 PGM Audio",
    ]


def test_env_structure_wins_over_saved_state_but_keeps_routes(settings: Settings) -> None:
    mixer = VisionMixer(_routed_settings(settings))
    mixer.apply_workspace(
        WorkspaceUpdate(format_id="1080p25", mixer_panel_count=3, stinger_count=2)
    )
    mixer.update_input(
        "cam-1",
        LogicalInputUpdate(
            kind=InputKind.mxl_live, label="Old", video=VideoEssence(), audio=AudioEssence()
        ),
    )
    mixer.update_input("cam-2", LogicalInputUpdate(stinger_slot_id="shared-2"))
    mixer.replace_tally_receivers([TallyReceiver(id="vsm", label="VSM", host="127.0.0.1")])
    mixer.update_keyer("dsk-1", title="Breaking")
    rid = ids.receiver_id(settings.resolved_nmos_seed, "cam-1", "video")
    response = TestClient(create_nmos_app(mixer.nmos)).patch(
        f"/x-nmos/connection/v1.2/single/receivers/{rid}/staged",
        json={
            "sender_id": SENDER_ID,
            "master_enable": True,
            "activation": {"mode": "activate_immediate"},
            "transport_params": [{"mxl_flow_id": VIDEO_FLOW}],
        },
    )
    assert response.status_code == 200, response.text
    mixer.set_preview("cam-5")  # starts the mixer
    mixer.stop()
    mixer.persist()

    _pin(settings, format="720p50", live_inputs=2, input_labels="Cam A", panels=1)
    again = VisionMixer(settings)
    assert [(item.id, item.kind, item.label) for item in again.list_inputs()] == [
        ("cam-1", InputKind.mxl_live, "Cam A"),
        ("cam-2", InputKind.mxl_live, "Camera 2"),
        ("black", InputKind.black, "Black"),
        ("replay", InputKind.replay, "Replay"),
    ]
    assert again.workspace.format_id == "720p50"
    assert (settings.width, settings.height) == (1280, 720)
    assert again.workspace.logical_source_count == 4
    assert len(again.panels) == again.workspace.mixer_panel_count == 1
    # cam-5 is gone: Preview starts empty.
    assert again.preview_input_id is None
    assert again.panels[0].preview_input_id is None
    # Routes, auto-stingers, stingers, keyers and tally stay from the saved state.
    active = again.nmos.active(rid, "receivers")
    assert active["sender_id"] == SENDER_ID
    assert active["transport_params"][0]["mxl_flow_id"] == VIDEO_FLOW
    assert str(again.get_input("cam-1").video.flow_id) == VIDEO_FLOW
    assert again.get_input("cam-2").stinger_slot_id == "shared-2"
    assert again.workspace.stinger_count == 2
    assert again.keyers[0].title == "Breaking"
    assert [item.id for item in again.tally.receivers] == ["vsm"]


def test_env_pinned_structure_is_refused_by_the_api(settings: Settings) -> None:
    _pin(settings, format="720p50", live_inputs=2, input_labels="Cam A", panels=2)
    mixer = VisionMixer(settings)
    client = TestClient(app_module.create_app(settings, mixer))

    for patch, variables in (
        ({"format_id": "1080p25"}, "FLOWXER_FORMAT"),
        ({"logical_source_count": 8}, "FLOWXER_LIVE_INPUTS, FLOWXER_INPUT_LABELS"),
        ({"mixer_panel_count": 3}, "FLOWXER_PANELS"),
    ):
        response = client.put("/api/v1/workspace", json=patch)
        assert response.status_code == 409
        assert variables in response.json()["detail"]
    response = client.put("/api/v1/workspace", json={"mixer_panel_count": 2, "stinger_count": 2})
    assert response.status_code == 200, response.text

    created = client.post("/api/v1/inputs", json={"id": "cam-9", "label": "X", "kind": "test"})
    assert created.status_code == 409
    assert client.delete("/api/v1/inputs/cam-2").status_code == 409
    assert client.patch("/api/v1/inputs/cam-1", json={"label": "Other"}).status_code == 409
    assert client.patch("/api/v1/inputs/cam-1", json={"kind": "test"}).status_code == 409
    # The GUI sends label and kind with every save: unchanged values pass.
    response = client.patch(
        "/api/v1/inputs/cam-1",
        json={"label": "Cam A", "kind": "mxl_live", "stinger_slot_id": "shared-1"},
    )
    assert response.status_code == 200, response.text

    # An import keeps the environment's structure, like a start.
    exported = client.get("/api/v1/config/export").json()
    exported["workspace"].update(format_id="1080p25", logical_source_count=8, mixer_panel_count=4)
    exported["inputs"][0]["label"] = "Imported"
    assert client.post("/api/v1/config/import", json=exported).status_code == 200
    assert mixer.workspace.format_id == "720p50"
    assert mixer.workspace.logical_source_count == 4
    assert mixer.workspace.mixer_panel_count == 2
    assert mixer.get_input("cam-1").label == "Cam A"
    assert mixer.get_input("cam-1").stinger_slot_id == "shared-1"

    assert client.get("/api/v1/console").json()["pinned"] == {
        "format_id": "FLOWXER_FORMAT",
        "logical_source_count": "FLOWXER_LIVE_INPUTS, FLOWXER_INPUT_LABELS",
        "mixer_panel_count": "FLOWXER_PANELS",
    }


def test_program_autostart(settings: Settings) -> None:
    mixer = VisionMixer(_pin(settings, live_inputs=2, program_autostart=True))
    with TestClient(app_module.create_app(settings, mixer)):
        assert mixer.state.value == "running"
        assert (mixer.program_input_id, mixer.preview_input_id) == ("cam-1", "cam-2")
        panel = mixer.panels[0]
        assert (panel.program_input_id, panel.preview_input_id) == ("cam-1", "cam-2")
    assert mixer.state.value == "idle"


def test_program_autostart_without_live_inputs(settings: Settings) -> None:
    mixer = VisionMixer(_pin(settings, program_autostart=True))
    with TestClient(app_module.create_app(settings, mixer)):
        assert (mixer.program_input_id, mixer.preview_input_id) == ("cam-1", "cam-2")
        assert mixer.get_input("cam-1").kind == InputKind.test
