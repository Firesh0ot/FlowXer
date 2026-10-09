"""Raw tally export for the platform's tally calculator (FLOWXER_TALLY_TSL)."""

from __future__ import annotations

import hashlib
import logging
import socket
import time

import pytest
from pydantic import ValidationError

from flowxer.api.metrics import render_prometheus
from flowxer.api.schemas import InputKind, LogicalInputCreate, MixerStartRequest, TransitionType, WorkspaceUpdate
from flowxer.engine import tally_export
from flowxer.engine.mixer import VisionMixer
from flowxer.engine.tally_export import MeTally, TallyExport, TallySnapshot, assign_indexes, me_messages
from flowxer.engine.tsl import (
    DLE,
    ETX,
    FLAG_UTF16,
    STX,
    TALLY_GREEN,
    TALLY_OFF,
    TALLY_RED,
    DisplayMessage,
    decode_packet,
    encode_packets,
    wrap_tcp,
)
from flowxer.settings import Settings


def lit(mixer: VisionMixer, screen: int = 1, now: float | None = None) -> tuple[set[str], set[str]]:
    """Inputs with LH red and inputs with RH green on one SCREEN, as the export computes them."""
    snapshot = mixer._tally_snapshot()
    indexes: dict[str, int] = {}
    assign_indexes(indexes, snapshot)
    ids = {index: input_id for input_id, index in indexes.items()}
    messages = me_messages(snapshot, snapshot.mes[screen - 1], indexes, time.monotonic() if now is None else now)
    assert [message.text for message in messages] == [item.label for item in mixer.list_inputs()]
    assert all(message.txt == TALLY_OFF and message.brightness == 3 for message in messages)
    assert {message.lh for message in messages} <= {TALLY_OFF, TALLY_RED}
    assert {message.rh for message in messages} <= {TALLY_OFF, TALLY_GREEN}
    red = {ids[message.index] for message in messages if message.lh == TALLY_RED}
    green = {ids[message.index] for message in messages if message.rh == TALLY_GREEN}
    return red, green


# Tally computation.


def test_program_lh_red_preview_rh_green(mixer: VisionMixer) -> None:
    assert lit(mixer) == (set(), set())
    mixer.start(MixerStartRequest(program_input_id="cam-1", preview_input_id="cam-2"))
    assert lit(mixer) == ({"cam-1"}, {"cam-2"})
    mixer.set_preview("cam-3")
    assert lit(mixer) == ({"cam-1"}, {"cam-3"})
    mixer.cut()
    assert lit(mixer) == ({"cam-3"}, {"cam-1"})
    mixer.take("cam-4")
    assert lit(mixer) == ({"cam-4"}, {"cam-1"})
    mixer.stop()
    assert lit(mixer) == (set(), set())


def test_keyer_and_stinger_media_light_nothing(mixer: VisionMixer) -> None:
    # The downstream keyers are HTML (CEF) graphics; no key takes its fill from an input.
    mixer.start(MixerStartRequest(program_input_id="cam-1", preview_input_id="cam-2"))
    mixer.update_keyer("dsk-1", enabled=True)
    mixer.set_overlay(enabled=True)
    assert lit(mixer) == ({"cam-1"}, {"cam-2"})
    assert len(mixer._tally_snapshot().inputs) == len(mixer.inputs)


def test_mix_lights_both_sources_until_it_ends(mixer: VisionMixer) -> None:
    mixer.start(MixerStartRequest(program_input_id="cam-1", preview_input_id="cam-2"))
    mixer.fade(duration_ms=400)
    assert lit(mixer) == ({"cam-1", "cam-2"}, {"cam-1"})
    assert lit(mixer, now=time.monotonic() + 0.5) == ({"cam-2"}, {"cam-1"})
    mixer.take("cam-3", TransitionType.mix, duration_ms=200)
    assert lit(mixer)[0] == {"cam-2", "cam-3"}
    mixer.cut()
    assert lit(mixer) == ({"cam-1"}, {"cam-3"})
    mixer.fade_to_black(duration_ms=600)
    assert lit(mixer)[0] == {"cam-1", "black"}
    assert lit(mixer, now=time.monotonic() + 0.7)[0] == {"black"}


def test_stinger_lights_both_sources_until_it_ends(mixer: VisionMixer) -> None:
    mixer.start(MixerStartRequest(program_input_id="cam-1", preview_input_id="cam-2"))
    mixer.set_wipe(armed=True)
    mixer.cut()
    frames = 0
    while mixer.stinger_player is not None:
        assert lit(mixer)[0] == {"cam-1", "cam-2"}
        mixer.advance_stinger(1)
        frames += 1
    assert frames > 1
    assert lit(mixer) == ({"cam-2"}, {"cam-1"})


def test_each_me_is_its_own_screen(mixer: VisionMixer) -> None:
    mixer.apply_workspace(WorkspaceUpdate(mixer_panel_count=2))
    mixer.start(MixerStartRequest(program_input_id="cam-1", preview_input_id="cam-2"))
    assert len(mixer._tally_snapshot().mes) == 2
    assert lit(mixer, screen=2) == (set(), set())
    mixer.set_preview("cam-4", panel_id="me-2")
    mixer.take("cam-3", panel_id="me-2")
    assert lit(mixer, screen=1) == ({"cam-1"}, {"cam-2"})
    assert lit(mixer, screen=2) == ({"cam-3"}, {"cam-4"})
    mixer.fade(panel_id="me-2", duration_ms=400)
    assert lit(mixer, screen=1) == ({"cam-1"}, {"cam-2"})
    assert lit(mixer, screen=2) == ({"cam-3", "cam-4"}, {"cam-3"})


def test_indexes_stay_when_an_input_is_removed(mixer: VisionMixer) -> None:
    indexes: dict[str, int] = {}
    assign_indexes(indexes, mixer._tally_snapshot())
    assert indexes == {item.id: item.slot for item in mixer.list_inputs()}
    assert indexes["cam-4"] == 3
    mixer.delete_input("cam-3")
    mixer.register_input(LogicalInputCreate(id="clip-1", label="Clip 1", kind=InputKind.test))
    snapshot = mixer._tally_snapshot()
    assign_indexes(indexes, snapshot)
    assert mixer.get_input("cam-4").slot == 2
    assert indexes["cam-4"] == 3
    assert indexes["clip-1"] == 8
    messages = me_messages(snapshot, snapshot.mes[0], indexes, time.monotonic())
    assert [message.index for message in messages] == [0, 1, 3, 4, 5, 6, 7, 8]


# Encoder: byte vectors from the platform's reference codec (mxl-poc-platform, tally
# calculator tsl5.py at a1883cb: encode() and wrap()).


def test_encoder_matches_the_reference_utf16() -> None:
    packets = encode_packets(
        2,
        [
            DisplayMessage(index=0, text="Kamera Süd", lh=TALLY_RED),
            DisplayMessage(index=1, text="Cam 2", rh=TALLY_GREEN),
            DisplayMessage(index=5, text="Black"),
        ],
        utf16=True,
    )
    assert [packet.hex() for packet in packets] == [
        "3e00000102000000d00014004b0061006d0065007200610020005300fc0064000100c2000a00"
        "430061006d00200032000500c0000a0042006c00610063006b00"
    ]


def test_encoder_matches_the_reference_ascii() -> None:
    packets = encode_packets(
        1,
        [
            DisplayMessage(index=0, text="Cam 1", lh=TALLY_RED, rh=TALLY_GREEN),
            DisplayMessage(index=3, text="Replay"),
        ],
    )
    assert [packet.hex() for packet in packets] == ["1b00000001000000d200050043616d20310300c00006005265706c6179"]


def test_encoder_splits_at_2048_bytes_like_the_reference() -> None:
    # The first ten messages fill a packet to exactly 2048 bytes, the eleventh starts the next.
    labels = ["x" * 99] * 9 + ["y" * 100, "z"]
    packets = encode_packets(4, [DisplayMessage(index=n, text=label) for n, label in enumerate(labels)], utf16=True)
    assert [(len(packet), hashlib.sha256(packet).hexdigest()) for packet in packets] == [
        (2048, "d5fbf039033dd94e48b718e78d261afc18843d99e87a18d7123bf11b28229663"),
        (14, "d2296134cbdde85d80886557dc5cec5d44344e503cda45e0a962902385bdedc8"),
    ]


def test_tcp_framing_stuffs_dle_like_the_reference() -> None:
    # Index 254 and "þ" (UTF-16LE fe 00) put DLE bytes into the packet.
    (packet,) = encode_packets(3, [DisplayMessage(index=254, text="þ", lh=TALLY_RED)], utf16=True)
    assert packet.hex() == "0c0000010300fe00d0000200fe00"
    assert wrap_tcp(packet, etx=True).hex() == "fe020c0000010300fefe00d0000200fefe00fe03"


# Setting.


@pytest.mark.parametrize(
    ("value", "target"),
    [
        (
            "udp://mxl-tally.mxl-platform.svc.cluster.local:8910",
            ("udp", "mxl-tally.mxl-platform.svc.cluster.local", 8910),
        ),
        ("tcp://10.0.0.5:8911", ("tcp", "10.0.0.5", 8911)),
        ("", None),
    ],
)
def test_tally_tsl_setting(monkeypatch: pytest.MonkeyPatch, value: str, target) -> None:
    monkeypatch.setenv("FLOWXER_TALLY_TSL", value)
    assert Settings(_env_file=None).tally_tsl_target == target


@pytest.mark.parametrize(
    "value",
    ["udp://host", "udp://host:0", "udp://host:99999", "http://host:8910", "udp://:8910", "host:8910", "udp://host:8910/x"],
)
def test_invalid_tally_tsl_setting(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("FLOWXER_TALLY_TSL", value)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


# Sending.


def _receive(sock: socket.socket, screen: int, match=lambda lamps: True, timeout: float = 3.0):
    """The next packet for `screen` whose {text: (LH, RH)} matches: (arrival, decoded packet)."""
    deadline = time.monotonic() + timeout
    while True:
        sock.settimeout(max(deadline - time.monotonic(), 0.01))
        decoded = decode_packet(sock.recv(2048))
        lamps = {item["text"]: (item["lh"], item["rh"]) for item in decoded["messages"]}
        if decoded["screen"] == screen and match(lamps):
            return time.monotonic(), decoded


def test_export_over_udp(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tally_export, "REFRESH_S", 0.5)
    receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    receiver.bind(("127.0.0.1", 0))
    for name, value in {"live_inputs": 2, "input_labels": "Kamera Süd,Cam 2", "panels": 2}.items():
        setattr(settings, name, value)
    settings.tally_tsl = f"udp://127.0.0.1:{receiver.getsockname()[1]}"
    mixer = VisionMixer(settings)
    try:
        # Every ME, every input (designer order), all off before Program starts.
        for screen in (1, 2):
            _, decoded = _receive(receiver, screen)
            assert decoded["flags"] == FLAG_UTF16
            assert [(item["index"], item["text"], item["lh"], item["rh"]) for item in decoded["messages"]] == [
                (0, "Kamera Süd", TALLY_OFF, TALLY_OFF),
                (1, "Cam 2", TALLY_OFF, TALLY_OFF),
                (2, "Black", TALLY_OFF, TALLY_OFF),
                (3, "Replay", TALLY_OFF, TALLY_OFF),
            ]
        mixer.start(MixerStartRequest(program_input_id="cam-1", preview_input_id="cam-2"))
        _receive(receiver, 1, lambda lamps: lamps["Kamera Süd"] == (TALLY_RED, TALLY_OFF) and lamps["Cam 2"] == (TALLY_OFF, TALLY_GREEN))

        # Transition start and end: both sources red, then only the new one (before the refresh).
        started = time.monotonic()
        mixer.fade(duration_ms=100)
        _receive(receiver, 1, lambda lamps: lamps["Kamera Süd"][0] == lamps["Cam 2"][0] == TALLY_RED)
        ended, _ = _receive(receiver, 1, lambda lamps: lamps["Kamera Süd"] == (TALLY_OFF, TALLY_GREEN))
        assert ended - started < 0.4

        # Nothing changes: the same state again after the refresh interval.
        refreshed, decoded = _receive(receiver, 1)
        assert 0.3 < refreshed - ended < 1.0
        assert {item["text"]: item["lh"] for item in decoded["messages"]}["Cam 2"] == TALLY_RED
        metrics = render_prometheus(mixer)
        assert "flowxer_tally_export_send_errors_total 0\n" in metrics
        assert mixer.tally_export.packets_sent >= 5
        assert time.time() - mixer.tally_export.last_success < 1.0
    finally:
        mixer.shutdown()
    try:
        # The last state: all off once Program stopped.
        _receive(receiver, 1, lambda lamps: set(lamps.values()) == {(TALLY_OFF, TALLY_OFF)})
    finally:
        receiver.close()


def test_export_resolves_the_name_again_and_logs_once(monkeypatch: pytest.MonkeyPatch, caplog) -> None:
    monkeypatch.setattr(tally_export, "REFRESH_S", 0.05)
    monkeypatch.setattr(tally_export, "RETRY_BACKOFF_S", (0.02,))
    receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    receiver.bind(("127.0.0.1", 0))
    resolve = socket.getaddrinfo
    names: list[str] = []

    def not_ready_yet(host, *args, **kwargs):
        # The Service name exists only from the fourth lookup on.
        names.append(host)
        if len(names) < 4:
            raise socket.gaierror("Name or service not known")
        return resolve("127.0.0.1", *args, **kwargs)

    monkeypatch.setattr(tally_export.socket, "getaddrinfo", not_ready_yet)
    snapshot = TallySnapshot(inputs=(("cam-1", "Cam 1"),), mes=(MeTally(program="cam-1"),))
    with caplog.at_level(logging.WARNING, logger=tally_export.__name__):
        export = TallyExport("udp", "mxl-tally.example", receiver.getsockname()[1], lambda: snapshot)
        try:
            _, decoded = _receive(receiver, 1)
        finally:
            export.close()
            receiver.close()
    assert decoded["messages"][0]["lh"] == TALLY_RED
    assert names[:4] == ["mxl-tally.example"] * 4
    assert len(caplog.records) == 1
    assert export.send_errors == 3


def test_export_metrics_only_when_configured(mixer: VisionMixer) -> None:
    assert "flowxer_tally_export" not in render_prometheus(mixer)


def _unwrap(stream: bytes) -> list[bytes]:
    """Packets of a DLE/STX ... DLE/ETX stream, DLE unstuffed."""
    packets: list[bytes] = []
    packet = bytearray()
    index = 0
    while index < len(stream):
        if stream[index] == DLE:
            code = stream[index + 1]
            index += 2
            if code == STX:
                packet = bytearray()
            elif code == ETX:
                packets.append(bytes(packet))
            else:
                packet.append(DLE)
            continue
        packet.append(stream[index])
        index += 1
    return packets


def test_export_over_tcp_frames_and_reconnects(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tally_export, "REFRESH_S", 0.05)
    monkeypatch.setattr(tally_export, "RETRY_BACKOFF_S", (0.02,))
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(2)
    server.settimeout(3.0)
    snapshot = TallySnapshot(inputs=(("cam-1", "Cam 1"), ("cam-2", "Cam 2")), mes=(MeTally("cam-1", "cam-2"),))
    export = TallyExport("tcp", "127.0.0.1", server.getsockname()[1], lambda: snapshot)
    try:
        conn, _ = server.accept()
        conn.settimeout(3.0)
        stream = b""
        while not stream.endswith(bytes((DLE, ETX))):
            stream += conn.recv(4096)
        assert stream[:2] == bytes((DLE, STX))
        decoded = decode_packet(_unwrap(stream)[0])
        assert [(item["index"], item["lh"], item["rh"]) for item in decoded["messages"]] == [
            (0, TALLY_RED, TALLY_OFF),
            (1, TALLY_OFF, TALLY_GREEN),
        ]
        # The listener goes away: the export connects again.
        conn.close()
        again, _ = server.accept()
        again.settimeout(3.0)
        assert again.recv(4096)[:2] == bytes((DLE, STX))
        again.close()
    finally:
        export.close()
        server.close()
