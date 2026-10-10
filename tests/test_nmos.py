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
        state_dir=tmp_path / "config",
        simulate=True,
        width=64,
        height=36,
        frame_rate_num=50,
        frame_rate_den=1,
        stinger_frame_count=8,
        group_hint="FlowXerTest",
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
    # Controllers append "/single/..." to the control href, which ends in "/".
    doubled = client.get(f"/x-nmos/connection/v1.2//single/receivers/{rid}/active")
    assert doubled.status_code == 200
    assert doubled.json() == active
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


def test_refresh_does_not_restart_running_essences(tmp_path: Path) -> None:
    mixer = _live_mixer(tmp_path)
    client = TestClient(create_nmos_app(mixer.nmos))
    rid = ids.receiver_id(SEED, "cam-1", "video")
    cam_dir = mixer.settings.mxl_root / "camera-a"
    cam_dir.mkdir(parents=True)
    (cam_dir / "domain_def.json").write_text(json.dumps({"id": CAM_DOMAIN}), encoding="utf-8")
    _write_flow(cam_dir, VIDEO_FLOW, grains=True)
    patch = client.patch(
        f"/x-nmos/connection/v1.2/single/receivers/{rid}/staged",
        json={
            "master_enable": True,
            "activation": {"mode": "activate_immediate"},
            "transport_params": [{"mxl_domain_id": CAM_DOMAIN, "mxl_flow_id": VIDEO_FLOW}],
        },
    )
    assert patch.status_code == 200
    assert mixer.nmos.input_state("cam-1", "video") == "running"

    applied: list[str] = []
    original = mixer.apply_nmos_receiver

    def counting(input_id: str, role: str, **kwargs) -> None:
        applied.append(role)
        original(input_id, role, **kwargs)

    mixer.apply_nmos_receiver = counting  # type: ignore[method-assign]
    # Re-applying restarts the input's mxlsrc (NULL -> PLAYING); the refresh loop
    # runs every 250 ms, so a running essence must never be applied again.
    for _ in range(3):
        mixer.nmos.refresh_waiting()
    assert applied == []
    assert mixer.nmos.input_state("cam-1", "video") == "running"


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


def _receiver_ids(client) -> set[str]:
    return {item["id"] for item in client.get("/x-nmos/node/v1.3/receivers").json()}


def test_live_inputs_and_the_dsk_have_receivers_clip_players_and_black_none(tmp_path: Path) -> None:
    mixer = VisionMixer(_settings(tmp_path))
    client = TestClient(create_nmos_app(mixer.nmos))
    receivers = client.get("/x-nmos/node/v1.3/receivers").json()
    live = [item for item in mixer.list_inputs() if item.kind == InputKind.mxl_live]
    assert len(receivers) == 2 * len(live) + 1
    labels = {item["label"] for item in receivers}
    assert {"Camera 1 Video", "Camera 1 Audio", "DSK 1 Key"} <= labels
    assert not any(label.startswith(("Clip", "Black")) for label in labels)
    key = next(item for item in receivers if item["label"] == "DSK 1 Key")
    assert key["id"] == ids.receiver_id(SEED, "dsk-1", "key")
    assert key["format"] == "urn:x-nmos:format:video"
    assert key["caps"]["media_types"] == ["video/v210a"]
    # BCP-004-01: constraint sets need the caps version.
    assert all(":" in item["caps"]["version"] for item in receivers)

def test_creating_and_deleting_inputs_adds_receivers(tmp_path: Path) -> None:
    mixer = VisionMixer(_settings(tmp_path))
    client = TestClient(create_nmos_app(mixer.nmos))
    before = _receiver_ids(client)
    mixer.register_input(
        LogicalInputCreate(
            id="ext-1",
            label="Ext 1",
            kind=InputKind.mxl_live,
            group_hint="ext-1",
        )
    )
    assert _receiver_ids(client) - before == {ids.receiver_id(SEED, "ext-1", "video"), ids.receiver_id(SEED, "ext-1", "audio")}
    mixer.delete_input("ext-1")
    assert _receiver_ids(client) == before

def test_workspace_count_does_not_leave_stale_receivers(tmp_path: Path) -> None:
    mixer = _live_mixer(tmp_path)
    mixer.apply_workspace(WorkspaceUpdate(logical_source_count=4))
    # cam-1, cam-2, clip-1 and Black remain; the DSK keeps its key receiver.
    client = TestClient(create_nmos_app(mixer.nmos))
    assert _receiver_ids(client) == {
        ids.receiver_id(SEED, "cam-1", "video"),
        ids.receiver_id(SEED, "cam-1", "audio"),
        ids.receiver_id(SEED, "cam-2", "video"),
        ids.receiver_id(SEED, "cam-2", "audio"),
        ids.receiver_id(SEED, "dsk-1", "key"),
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


def test_connection_id_list_has_trailing_slashes(tmp_path: Path) -> None:
    mixer = VisionMixer(_settings(tmp_path))
    client = TestClient(create_nmos_app(mixer.nmos))
    assert client.get("/").json() == ["x-nmos/"]
    assert "bulk/" in client.get("/x-nmos/connection/v1.2/").json()
    ids_list = client.get("/x-nmos/connection/v1.2/single/senders/").json()
    assert ids_list
    assert all(item.endswith("/") for item in ids_list)
    sid = ids_list[0].rstrip("/")
    constraints = client.get(
        f"/x-nmos/connection/v1.2/single/senders/{sid}/constraints/"
    )
    assert constraints.status_code == 200
    bulk = client.get("/x-nmos/connection/v1.2/bulk/senders")
    assert bulk.status_code == 405
    assert bulk.json()["code"] == 405
    missing = client.get("/x-nmos/connection/v1.2/single/senders/not-a-sender/active")
    assert missing.status_code == 404
    assert missing.json() == {"code": 404, "error": "Not Found", "debug": None}
    unknown = client.get(f"/x-nmos/node/v1.3/{sid}")
    assert unknown.status_code == 404
    assert unknown.json()["code"] == 404
    assert "transporttype/" in client.get(
        f"/x-nmos/connection/v1.2/single/senders/{sid}/"
    ).json()
    transporttype = client.get(
        f"/x-nmos/connection/v1.2/single/senders/{sid}/transporttype/"
    )
    assert transporttype.status_code == 200
    assert transporttype.json() == "urn:x-nmos:transport:mxl"
    transportfile = client.get(
        f"/x-nmos/connection/v1.2/single/senders/{sid}/transportfile/"
    )
    assert transportfile.status_code == 404
    sender = client.get(f"/x-nmos/node/v1.3/senders/{sid}").json()
    assert sender["interface_bindings"] == []
    assert sender["manifest_href"] is None
    node = client.get("/x-nmos/node/v1.3/self").json()
    assert "attached_network_device" not in node["interfaces"][0]
    source = client.get("/x-nmos/node/v1.3/sources/").json()[0]
    assert "caps" in source
    assert "grain_rate" in source
    audio = next(item for item in client.get("/x-nmos/node/v1.3/sources/").json() if item["format"].endswith("audio"))
    assert audio["channels"]
    assert {ch["symbol"] for ch in audio["channels"]} == {"L", "R"}
    flow = next(item for item in client.get("/x-nmos/node/v1.3/flows/").json() if item["format"].endswith("video"))
    assert flow["grain_rate"]["numerator"] == 50
    assert flow["components"]
    assert flow["transfer_characteristic"] == "SDR"


def test_staged_patch_rejects_unknown_fields_and_returns_immediate_activation(
    tmp_path: Path,
) -> None:
    mixer = VisionMixer(_settings(tmp_path))
    client = TestClient(create_nmos_app(mixer.nmos))
    sid = ids.sender_id(SEED, "me-1", "video")
    refused = client.patch(
        f"/x-nmos/connection/v1.2/single/senders/{sid}/staged",
        json={"bad": "data"},
    )
    assert refused.status_code == 400
    assert refused.json()["code"] == 400
    patch = client.patch(
        f"/x-nmos/connection/v1.2/single/senders/{sid}/staged",
        json={"master_enable": True, "activation": {"mode": "activate_immediate"}},
    )
    assert patch.status_code == 200
    assert patch.json()["activation"]["mode"] == "activate_immediate"
    assert patch.json()["activation"]["activation_time"]
    staged = client.get(f"/x-nmos/connection/v1.2/single/senders/{sid}/staged").json()
    assert staged["activation"] == {"mode": None, "requested_time": None, "activation_time": None}
    active = client.get(f"/x-nmos/connection/v1.2/single/senders/{sid}/active").json()
    assert active["activation"]["mode"] == "activate_immediate"


def test_bulk_post_stages_senders(tmp_path: Path) -> None:
    mixer = VisionMixer(_settings(tmp_path))
    client = TestClient(create_nmos_app(mixer.nmos))
    sid = ids.sender_id(SEED, "me-1", "video")
    flow = str(uuid4())
    response = client.post(
        "/x-nmos/connection/v1.2/bulk/senders",
        json=[{"id": sid, "params": {"transport_params": [{"mxl_flow_id": flow}]}}],
    )
    assert response.status_code == 200
    assert response.json() == [{"id": sid, "code": 200}]
    staged = client.get(f"/x-nmos/connection/v1.2/single/senders/{sid}/staged").json()
    assert staged["transport_params"][0]["mxl_flow_id"] == flow
    assert client.get("/x-nmos/connection/v1.2/bulk/senders").status_code == 405


def test_scheduled_relative_activation(tmp_path: Path) -> None:
    mixer = VisionMixer(_settings(tmp_path))
    client = TestClient(create_nmos_app(mixer.nmos))
    sid = ids.sender_id(SEED, "me-1", "video")
    flow = str(uuid4())
    client.patch(
        f"/x-nmos/connection/v1.2/single/senders/{sid}/staged",
        json={"transport_params": [{"mxl_flow_id": flow}]},
    )
    patch = client.patch(
        f"/x-nmos/connection/v1.2/single/senders/{sid}/staged",
        json={"activation": {"mode": "activate_scheduled_relative", "requested_time": "0:50000000"}},
    )
    assert patch.status_code == 202
    assert patch.json()["activation"]["mode"] == "activate_scheduled_relative"
    assert patch.json()["activation"]["requested_time"] == "0:50000000"
    assert patch.json()["activation"]["activation_time"]
    mixer.nmos._scheduled[sid].join(timeout=1.0)
    active = client.get(f"/x-nmos/connection/v1.2/single/senders/{sid}/active").json()
    assert active["transport_params"][0]["mxl_flow_id"] == flow
    assert active["activation"]["mode"] == "activate_scheduled_relative"
    assert active["activation"]["requested_time"] == "0:50000000"
    staged = client.get(f"/x-nmos/connection/v1.2/single/senders/{sid}/staged").json()
    assert staged["activation"] == {"mode": None, "requested_time": None, "activation_time": None}


def test_every_rendering_me_has_pgm_and_pvw_senders_with_its_own_flows(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    settings.panels = 3
    settings.render_mes = 2
    mixer = VisionMixer(settings)
    client = TestClient(create_nmos_app(mixer.nmos))
    senders = client.get("/x-nmos/node/v1.3/senders").json()
    assert sorted(item["label"] for item in senders) == [
        "ME 1 PGM Audio", "ME 1 PGM Video", "ME 1 PVW Video",
        "ME 2 PGM Audio", "ME 2 PGM Video", "ME 2 PVW Video",
    ]
    # PGM keeps its ids from before PVW existed.
    assert ids.sender_id(SEED, "me-1", "video") in {item["id"] for item in senders}
    assert ids.sender_id(SEED, "me-1", "pvw-video") in {item["id"] for item in senders}
    status = mixer.start()
    flows = {item["label"]: item for item in client.get("/x-nmos/node/v1.3/flows").json()}
    me_2 = status.me_outputs[1]
    assert flows["ME 2 PGM Video"]["tags"]["urn:x-nmos:tag:mxl_flow_id"] == [me_2.pgm_video_flow_id]
    assert flows["ME 2 PVW Video"]["tags"]["urn:x-nmos:tag:mxl_flow_id"] == [me_2.pvw_video_flow_id]
    assert flows["ME 1 PGM Video"]["tags"]["urn:x-nmos:tag:mxl_flow_id"] == [status.outputs.video_flow_id]
    for label, flow_id in (("ME 2 PGM Audio", me_2.pgm_audio_flow_id), ("ME 2 PVW Video", me_2.pvw_video_flow_id)):
        sid = next(item["id"] for item in senders if item["label"] == label)
        active = client.get(f"/x-nmos/connection/v1.2/single/senders/{sid}/active").json()
        assert active["transport_params"][0]["mxl_flow_id"] == flow_id
    assert ids.sender_name("me-1", "pvw-video") == "me-1-pvw-video"
    assert ids.sender_name("me-2", "audio") == "me-2-pgm-audio"
    mixer.stop()


def test_no_pvw_senders_without_pvw_outputs(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    settings.pvw_outputs = False
    mixer = VisionMixer(settings)
    labels = {item["label"] for item in mixer.nmos.senders()}
    assert labels == {"ME 1 PGM Video", "ME 1 PGM Audio"}


def test_the_dsk_key_receiver_routes_the_key_input(tmp_path: Path) -> None:
    mixer = VisionMixer(_settings(tmp_path))
    client = TestClient(create_nmos_app(mixer.nmos))
    key_dir = mixer.settings.mxl_root / "browser"
    key_dir.mkdir(parents=True)
    (key_dir / "domain_def.json").write_text(json.dumps({"id": CAM_DOMAIN}), encoding="utf-8")
    _write_flow(key_dir, VIDEO_FLOW, grains=True)
    calls: list = []
    mixer._engine_call = lambda method, path, body, client=None: calls.append((method, path, body))
    rid = ids.receiver_id(SEED, "dsk-1", "key")
    response = client.patch(
        f"/x-nmos/connection/v1.2/single/receivers/{rid}/staged",
        json={
            "sender_id": SENDER_ID,
            "master_enable": True,
            "activation": {"mode": "activate_immediate"},
            "transport_params": [{"mxl_domain_id": CAM_DOMAIN, "mxl_flow_id": VIDEO_FLOW}],
        },
    )
    assert response.status_code == 200
    assert calls[-1] == ("PUT", "/key/1", {"domain": str(key_dir.resolve()), "flow": VIDEO_FLOW})
    receiver = client.get(f"/x-nmos/node/v1.3/receivers/{rid}").json()
    assert receiver["subscription"] == {"sender_id": SENDER_ID, "active": True}
    # The key route belongs to the saved state.
    routes = mixer.export_state()["nmos_receivers"]
    assert {"input_id": "dsk-1", "role": "key", "mxl_flow_id": VIDEO_FLOW}.items() <= next(r for r in routes if r["role"] == "key").items()
