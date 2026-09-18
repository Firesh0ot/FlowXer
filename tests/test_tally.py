from __future__ import annotations

import socket
import threading

from fastapi.testclient import TestClient

from flowxer.api.schemas import MixerStartRequest, TallyKind, TallyReceiver
from flowxer.engine.mixer import VisionMixer
from flowxer.engine.tsl import (
    TALLY_GREEN,
    TALLY_OFF,
    TALLY_RED,
    decode_packet,
    encode_packet,
    wrap_tcp,
    DisplayMessage,
)


def test_tsl5_packet_roundtrip() -> None:
    packet = encode_packet(
        0,
        [
            DisplayMessage(index=0, text="Camera 1", rh=TALLY_RED, txt=TALLY_RED, lh=TALLY_OFF),
            DisplayMessage(index=1, text="Camera 2", rh=TALLY_OFF, txt=TALLY_GREEN, lh=TALLY_GREEN),
        ],
    )
    decoded = decode_packet(packet)
    assert decoded["ver"] == 0
    assert decoded["flags"] == 0
    assert decoded["screen"] == 0
    assert decoded["messages"][0]["text"] == "Camera 1"
    assert decoded["messages"][0]["rh"] == TALLY_RED
    assert decoded["messages"][1]["lh"] == TALLY_GREEN
    wrapped = wrap_tcp(b"\xfe\x01")
    assert wrapped[:2] == b"\xfe\x02"
    assert wrapped[2:4] == b"\xfe\xfe"


def test_tally_udp_pgm_red_pvw_green(mixer: VisionMixer) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    sock.settimeout(1.0)
    port = sock.getsockname()[1]
    mixer.replace_tally_receivers(
        [
            TallyReceiver(
                id="companion-1",
                kind=TallyKind.companion,
                label="Bitfocus Companion",
                host="127.0.0.1",
                port=port,
            )
        ]
    )
    sock.recvfrom(2048)
    mixer.start(MixerStartRequest(program_input_id="cam-1", preview_input_id="cam-2"))
    packet, _addr = sock.recvfrom(2048)
    messages = {item["index"]: item for item in decode_packet(packet)["messages"]}
    cam1 = mixer.get_input("cam-1")
    cam2 = mixer.get_input("cam-2")
    assert messages[cam1.slot]["text"] == cam1.label
    assert messages[cam1.slot]["rh"] == TALLY_RED
    assert messages[cam2.slot]["lh"] == TALLY_GREEN
    mixer.set_preview("cam-3")
    packet, _addr = sock.recvfrom(2048)
    messages = {item["index"]: item for item in decode_packet(packet)["messages"]}
    cam3 = mixer.get_input("cam-3")
    assert messages[cam3.slot]["lh"] == TALLY_GREEN
    assert messages[cam2.slot]["lh"] == TALLY_OFF
    mixer.stop()
    packet, _addr = sock.recvfrom(2048)
    messages = {item["index"]: item for item in decode_packet(packet)["messages"]}
    assert all(item["rh"] == TALLY_OFF and item["lh"] == TALLY_OFF for item in messages.values())
    sock.close()


def test_tally_tcp_dle_stx(mixer: VisionMixer) -> None:
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]
    received: list[bytes] = []

    def accept() -> None:
        conn, _addr = server.accept()
        conn.settimeout(1.0)
        received.append(conn.recv(4096))
        conn.close()

    thread = threading.Thread(target=accept, daemon=True)
    thread.start()
    mixer.replace_tally_receivers(
        [
            TallyReceiver(
                id="hi-1",
                kind=TallyKind.hi,
                label="Riedel HI",
                host="127.0.0.1",
                port=port,
                transport="tcp",
            )
        ]
    )
    mixer.start(MixerStartRequest(program_input_id="cam-1"))
    thread.join(timeout=2)
    server.close()
    assert received
    assert received[0][:2] == b"\xfe\x02"
    raw = received[0][2:]
    # Unstuff DLE/DLE → DLE for decode of the inner packet.
    unstuffed = bytearray()
    i = 0
    while i < len(raw):
        unstuffed.append(raw[i])
        if raw[i] == 0xFE and i + 1 < len(raw) and raw[i + 1] == 0xFE:
            i += 1
        i += 1
    decoded = decode_packet(bytes(unstuffed))
    assert decoded["messages"]
    assert any(item["text"] for item in decoded["messages"])


def test_tally_api_crud_and_presets(client: TestClient) -> None:
    listed = client.get("/api/v1/tally").json()
    kinds = {item["kind"] for item in listed["presets"]}
    assert kinds == {"companion", "vsm", "bfe", "hi", "custom"}
    assert any(item["label"] == "Riedel HI" for item in listed["presets"])
    saved = client.put(
        "/api/v1/tally/receivers",
        json={
            "receivers": [
                {
                    "id": "vsm-1",
                    "kind": "vsm",
                    "label": "Lawo VSM",
                    "host": "10.0.0.8",
                    "port": 8900,
                    "transport": "udp",
                    "enabled": True,
                    "screen": 1,
                    "index_offset": 0,
                }
            ]
        },
    )
    assert saved.status_code == 200
    body = saved.json()
    assert body["receivers"][0]["kind"] == "vsm"
    console = client.get("/api/v1/console").json()
    assert console["tally"]["receivers"][0]["id"] == "vsm-1"
    refresh = client.post("/api/v1/tally/refresh")
    assert refresh.status_code == 200


def test_tally_label_change_updates_umd(mixer: VisionMixer) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    sock.settimeout(1.0)
    port = sock.getsockname()[1]
    mixer.replace_tally_receivers(
        [
            TallyReceiver(
                id="bfe-1",
                kind=TallyKind.bfe,
                label="BFE Commander",
                host="127.0.0.1",
                port=port,
            )
        ]
    )
    sock.recvfrom(2048)
    from flowxer.api.schemas import LogicalInputUpdate

    mixer.update_input("cam-1", LogicalInputUpdate(label="Studio A"))
    packet, _ = sock.recvfrom(2048)
    sock.close()
    texts = [item["text"] for item in decode_packet(packet)["messages"]]
    assert "Studio A" in texts
