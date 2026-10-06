"""Platform contract (mxl-poc-platform G1-G10): settings, saved state, registry, shutdown."""

from __future__ import annotations

import json
import socket
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

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
