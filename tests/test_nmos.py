from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from flowxer.api.schemas import (
    AudioEssence,
    InputKind,
    LogicalInputCreate,
    LogicalInputUpdate,
    MixerStartRequest,
    VideoEssence,
    WorkspaceUpdate,
)
from flowxer.app import create_app
from flowxer.engine.mixer import VisionMixer
from flowxer.nmos import ids
from flowxer.nmos.http import create_nmos_app
from flowxer.nmos.ids import nmos_uuid
from flowxer.settings import Settings

SEED = "test-flowxer"
CAM_DOMAIN = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa1"
VIDEO_FLOW = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbb1"
AUDIO_FLOW = "cccccccc-cccc-cccc-cccc-ccccccccccc1"
SENDER_ID = "dddddddd-dddd-dddd-dddd-ddddddddddd1"


def _settings(tmp_path: Path) -> Settings:
    root = tmp_path / "mxl"
    return Settings(
        mxl_root=root,
        mxl_output_domain_dir=root / "flowxer-test",
        mxl_output_domain_id="flowxer-test",
        nmos_seed=SEED,
        nmos_enable=True,
        nmos_bind=False,
        storage_root=tmp_path / "storage",
        simulate=True,
        gst_mode="simulate",
        width=64,
        height=36,
        frame_rate_num=50,
        frame_rate_den=1,
        stinger_frame_count=8,
        overlay_url="http://127.0.0.1:9610/graphics/lower-third.html",
        group_hint="FlowXerTest",
        stinger_auto_tick=False,
    )


def _write_flow(domain_dir: Path, flow_id: str, *, grains: bool) -> None:
    flow = domain_dir / f"{flow_id}.mxl-flow"
    flow.mkdir(parents=True, exist_ok=True)
    (flow / "flow_def.json").write_text("{}", encoding="utf-8")
    extra = flow / "grain.bin"
    if grains:
        extra.write_bytes(b"grain")
    elif extra.exists():
        extra.unlink()


def _live_mixer(tmp_path: Path) -> VisionMixer:
    mixer = VisionMixer(_settings(tmp_path))
    mixer.update_input(
        "cam-1",
        LogicalInputUpdate(
            kind=InputKind.mxl_live,
            video=VideoEssence(),
            audio=AudioEssence(),
            group_hint="cam-1",
        ),
    )
    return mixer


def test_ids_are_stable_uuidv5() -> None:
    assert ids.node_id(SEED) == nmos_uuid(SEED, "node")
    assert UUID(ids.node_id(SEED)).version == 5
    assert ids.node_id(SEED) == ids.node_id(SEED)
    assert ids.node_id(SEED) != ids.node_id("other-seed")
    assert ids.receiver_id(SEED, "cam-1", "video") != ids.receiver_id(SEED, "cam-1", "audio")
    assert ids.sender_id(SEED, "me-1", "video") == ids.sender_id(SEED, "me-1", "video")


def test_rest_defaults_domain_id_and_syncs_is05(tmp_path: Path) -> None:
    mixer = VisionMixer(_settings(tmp_path))
    created = mixer.register_input(
        LogicalInputCreate(
            id="studio-a",
            label="Studio A",
            kind=InputKind.mxl_live,
            video=VideoEssence(flow_id=VIDEO_FLOW),
            audio=AudioEssence(flow_id=AUDIO_FLOW),
        )
    )
    assert created.video is not None and created.video.domain_id == "flowxer-test"
    assert created.audio is not None and created.audio.domain_id == "flowxer-test"
    client = TestClient(create_nmos_app(mixer.nmos))
    rid = ids.receiver_id(SEED, "studio-a", "video")
    active = client.get(f"/x-nmos/connection/v1.2/single/receivers/{rid}/active").json()
    assert active["master_enable"] is True
    assert active["transport_params"][0]["mxl_flow_id"] == VIDEO_FLOW
    assert active["transport_params"][0]["mxl_domain_id"] == "flowxer-test"
    receiver = client.get(f"/x-nmos/node/v1.3/receivers/{rid}").json()
    assert receiver["subscription"]["active"] is True
    assert receiver["transport"] == "urn:x-nmos:transport:mxl"


def test_is05_activation_state_machine(tmp_path: Path) -> None:
    mixer = _live_mixer(tmp_path)
    client = TestClient(create_nmos_app(mixer.nmos))
    rid = ids.receiver_id(SEED, "cam-1", "video")
    assert mixer.nmos.input_state("cam-1", "video") == "not_routed"

    missing = str(uuid4())
    patch = client.patch(
        f"/x-nmos/connection/v1.2/single/receivers/{rid}/staged",
        json={
            "sender_id": SENDER_ID,
            "master_enable": True,
            "activation": {"mode": "activate_immediate"},
            "transport_params": [{"mxl_domain_id": missing, "mxl_flow_id": VIDEO_FLOW}],
        },
    )
    assert patch.status_code == 200, patch.text
    assert mixer.nmos.input_state("cam-1", "video") == "waiting"
    receiver = client.get(f"/x-nmos/node/v1.3/receivers/{rid}").json()
    assert receiver["subscription"]["sender_id"] == SENDER_ID
    assert receiver["subscription"]["active"] is True

    cam_dir = mixer.settings.mxl_root / "camera-a"
    cam_dir.mkdir(parents=True)
    (cam_dir / "domain_def.json").write_text(
        json.dumps({"id": CAM_DOMAIN, "label": "Camera A"}),
        encoding="utf-8",
    )
    patch = client.patch(
        f"/x-nmos/connection/v1.2/single/receivers/{rid}/staged",
        json={
            "master_enable": True,
            "activation": {"mode": "activate_immediate"},
            "transport_params": [{"mxl_domain_id": CAM_DOMAIN, "mxl_flow_id": VIDEO_FLOW}],
        },
    )
    assert patch.status_code == 200
    assert mixer.nmos.input_state("cam-1", "video") == "waiting"

    _write_flow(cam_dir, VIDEO_FLOW, grains=False)
    mixer.nmos.refresh_waiting()
    assert mixer.nmos.input_state("cam-1", "video") == "no_signal"

    _write_flow(cam_dir, VIDEO_FLOW, grains=True)
    mixer.nmos.refresh_waiting()
    assert mixer.nmos.input_state("cam-1", "video") == "running"
    assert mixer.get_input("cam-1").video is not None
    assert str(mixer.get_input("cam-1").video.flow_id) == VIDEO_FLOW
    assert mixer.get_input("cam-1").video.domain_id == CAM_DOMAIN

    (cam_dir / f"{VIDEO_FLOW}.mxl-flow").rename(cam_dir / "gone.mxl-flow")
    mixer.nmos.refresh_waiting()
    assert mixer.nmos.input_state("cam-1", "video") == "waiting"

    disable = client.patch(
        f"/x-nmos/connection/v1.2/single/receivers/{rid}/staged",
        json={"master_enable": False, "activation": {"mode": "activate_immediate"}},
    )
    assert disable.status_code == 200
    assert mixer.nmos.input_state("cam-1", "video") == "not_routed"


def test_malformed_params_are_rejected(tmp_path: Path) -> None:
    mixer = _live_mixer(tmp_path)
    client = TestClient(create_nmos_app(mixer.nmos))
    rid = ids.receiver_id(SEED, "cam-1", "video")
    bad = client.patch(
        f"/x-nmos/connection/v1.2/single/receivers/{rid}/staged",
        json={
            "master_enable": True,
            "activation": {"mode": "activate_immediate"},
            "transport_params": [{"mxl_domain_id": "not-a-uuid", "mxl_flow_id": VIDEO_FLOW}],
        },
    )
    assert bad.status_code == 400
    assert bad.json()["code"] == 400
    auto = client.patch(
        f"/x-nmos/connection/v1.2/single/receivers/{rid}/staged",
        json={
            "master_enable": True,
            "activation": {"mode": "activate_immediate"},
            "transport_params": [{"mxl_flow_id": "auto"}],
        },
    )
    assert auto.status_code == 400


def test_test_black_file_have_no_receivers(tmp_path: Path) -> None:
    mixer = VisionMixer(_settings(tmp_path))
    client = TestClient(create_nmos_app(mixer.nmos))
    receivers = client.get("/x-nmos/node/v1.3/receivers").json()
    assert receivers == []
    mixer.update_input("cam-1", LogicalInputUpdate(kind=InputKind.mxl_live, group_hint="cam-1"))
    receivers = client.get("/x-nmos/node/v1.3/receivers").json()
    assert len(receivers) == 2
    labels = {item["label"] for item in receivers}
    assert "Camera 1 Video" in labels
    assert "Camera 1 Audio" in labels


def test_creating_and_deleting_inputs_adds_receivers(tmp_path: Path) -> None:
    mixer = VisionMixer(_settings(tmp_path))
    mixer.register_input(
        LogicalInputCreate(
            id="ext-1",
            label="Ext 1",
            kind=InputKind.mxl_live,
            group_hint="ext-1",
        )
    )
    client = TestClient(create_nmos_app(mixer.nmos))
    assert len(client.get("/x-nmos/node/v1.3/receivers").json()) == 2
    mixer.delete_input("ext-1")
    assert client.get("/x-nmos/node/v1.3/receivers").json() == []


def test_workspace_count_does_not_leave_stale_receivers(tmp_path: Path) -> None:
    mixer = _live_mixer(tmp_path)
    mixer.apply_workspace(WorkspaceUpdate(logical_source_count=4))
    # cam-1 is still mxl_live among the remaining four.
    client = TestClient(create_nmos_app(mixer.nmos))
    receivers = client.get("/x-nmos/node/v1.3/receivers").json()
    assert {item["id"] for item in receivers} == {
        ids.receiver_id(SEED, "cam-1", "video"),
        ids.receiver_id(SEED, "cam-1", "audio"),
    }


def test_on_air_activation_keeps_program_running(tmp_path: Path) -> None:
    mixer = _live_mixer(tmp_path)
    mixer.start(MixerStartRequest(program_input_id="cam-1"))
    client = TestClient(create_nmos_app(mixer.nmos))
    rid = ids.receiver_id(SEED, "cam-1", "video")
    cam_dir = mixer.settings.mxl_root / "camera-a"
    cam_dir.mkdir(parents=True)
    (cam_dir / "domain_def.json").write_text(
        json.dumps({"id": CAM_DOMAIN, "label": "Camera A"}),
        encoding="utf-8",
    )
    _write_flow(cam_dir, VIDEO_FLOW, grains=True)
    response = client.patch(
        f"/x-nmos/connection/v1.2/single/receivers/{rid}/staged",
        json={
            "master_enable": True,
            "activation": {"mode": "activate_immediate"},
            "transport_params": [{"mxl_domain_id": CAM_DOMAIN, "mxl_flow_id": VIDEO_FLOW}],
        },
    )
    assert response.status_code == 200
    assert mixer.state.value == "running"
    assert mixer.program_input_id == "cam-1"
    mixer.stop()


def test_pgm_sender_active_params_follow_output_flows(tmp_path: Path) -> None:
    mixer = VisionMixer(_settings(tmp_path))
    first = mixer.start(MixerStartRequest(program_input_id="cam-1"))
    client = TestClient(create_nmos_app(mixer.nmos))
    sid = ids.sender_id(SEED, "me-1", "video")
    active = client.get(f"/x-nmos/connection/v1.2/single/senders/{sid}/active").json()
    assert active["master_enable"] is True
    assert active["transport_params"][0]["mxl_domain_id"] == "flowxer-test"
    assert active["transport_params"][0]["mxl_flow_id"] == first.outputs.video_flow_id
    node = client.get("/x-nmos/node/v1.3/self").json()
    assert node["id"] == ids.node_id(SEED)
    assert node["api"]["endpoints"][0]["host"] != "0.0.0.0"
    mixer.stop()
    mixer.apply_workspace(WorkspaceUpdate(format_id="720p50"))
    second = mixer.start(MixerStartRequest(program_input_id="cam-1"))
    assert second.outputs.video_flow_id != first.outputs.video_flow_id
    active = client.get(f"/x-nmos/connection/v1.2/single/senders/{sid}/active").json()
    assert active["transport_params"][0]["mxl_flow_id"] == second.outputs.video_flow_id
    mixer.stop()


def test_separate_video_and_audio_senders(tmp_path: Path) -> None:
    mixer = _live_mixer(tmp_path)
    client = TestClient(create_nmos_app(mixer.nmos))
    video = ids.receiver_id(SEED, "cam-1", "video")
    audio = ids.receiver_id(SEED, "cam-1", "audio")
    other_domain = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa2"
    client.patch(
        f"/x-nmos/connection/v1.2/single/receivers/{video}/staged",
        json={
            "master_enable": True,
            "activation": {"mode": "activate_immediate"},
            "transport_params": [{"mxl_domain_id": CAM_DOMAIN, "mxl_flow_id": VIDEO_FLOW}],
        },
    )
    client.patch(
        f"/x-nmos/connection/v1.2/single/receivers/{audio}/staged",
        json={
            "master_enable": True,
            "activation": {"mode": "activate_immediate"},
            "transport_params": [{"mxl_domain_id": other_domain, "mxl_flow_id": AUDIO_FLOW}],
        },
    )
    assert mixer.get_input("cam-1").video.domain_id == CAM_DOMAIN
    assert mixer.get_input("cam-1").audio.domain_id == other_domain


def test_console_and_mixer_status_include_nmos(tmp_path: Path) -> None:
    mixer = _live_mixer(tmp_path)
    app = create_app(_settings(tmp_path), mixer)
    client = TestClient(app)
    console = client.get("/api/v1/console").json()
    assert console["nmos"]["enabled"] is True
    assert console["nmos"]["node_id"] == ids.node_id(SEED)
    assert console["mixer"]["nmos"]["node_id"] == ids.node_id(SEED)
    assert any(item["input_id"] == "cam-1" for item in console["nmos"]["receivers"])
    status = client.get("/api/v1/mixer").json()
    assert status["nmos"]["enabled"] is True
