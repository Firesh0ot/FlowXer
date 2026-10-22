"""Tally through every path (engine/visibility.py): what is visible on an ME's Program, keys and
pips and nested MEs included, hidden boxes and covered layers excluded."""

from __future__ import annotations

import json
import socket
import time
from pathlib import Path

from flowxer.api.schemas import Dme, MixerStartRequest, TallyKind, TallyReceiver, WorkspaceUpdate
from flowxer.engine.mixer import VisionMixer
from flowxer.engine.tsl import TALLY_RED, decode_packet
from flowxer.engine.visibility import KeyLayer, MeLayers, box_covers, box_shows, key_visibility, visible_on_program


def pip(source: str, x: float = 0.75, y: float = 0.25, size: float = 0.25, **extra) -> KeyLayer:
    fields = dict(
        source_id=source, keys_source=True, pip=True, upstream=True, on_air=True, full=True, leaving=False, gain=1.0,
        dme=Dme(enabled=True, x=x, y=y, size=size),
    )
    fields.update(extra)
    return KeyLayer(**fields)


def luma(source: str, upstream: bool = True, **extra) -> KeyLayer:
    fields = dict(source_id=source, keys_source=True, pip=False, upstream=upstream, on_air=True, full=True, leaving=False, gain=1.0, dme=None)
    fields.update(extra)
    return KeyLayer(**fields)


def test_boxes_that_show_nothing() -> None:
    assert box_shows(Dme(x=0.5, y=0.5, size=0.5))
    assert not box_shows(Dme(size=0.0))
    assert not box_shows(Dme(x=-0.3, size=0.5))  # off screen left
    assert not box_shows(Dme(y=1.3, size=0.5))  # off screen below
    assert box_shows(Dme(x=-0.2, size=0.5))  # half on screen
    assert not box_shows(Dme(size=0.5, crop={"left": 0.6, "right": 0.39999}))
    assert box_covers(Dme(size=1.0)) and not box_covers(Dme(size=0.99))
    assert not box_covers(Dme(size=1.0, soft=2.0)) and not box_covers(Dme(size=1.0, crop={"top": 0.1}))


def test_hidden_and_covered_keys() -> None:
    assert key_visibility([pip("cam-3"), luma("clip-1")]) == (["cam-3", "clip-1"], False)
    assert key_visibility([pip("cam-3", on_air=False)]) == ([], False)
    assert key_visibility([pip("cam-3", gain=0.0)]) == ([], False)
    assert key_visibility([pip("cam-3", size=0.0)]) == ([], False)
    assert key_visibility([pip("cam-3", x=1.5)]) == ([], False)
    # Flying off: lit until the fly ends, wherever the box is.
    assert key_visibility([pip("cam-3", x=-0.5, on_air=True, full=False, leaving=True)]) == (["cam-3"], False)
    # A full-raster pip on top covers the background and the layers under it ...
    assert key_visibility([pip("cam-3"), pip("cam-5", x=0.5, y=0.5, size=1.0)]) == (["cam-5"], True)
    # ... not while it fades, nor under it (key order is bottom to top).
    assert key_visibility([pip("cam-3"), pip("cam-5", x=0.5, y=0.5, size=1.0, full=False)]) == (["cam-3", "cam-5"], False)
    assert key_visibility([pip("cam-5", x=0.5, y=0.5, size=1.0), pip("cam-3")]) == (["cam-5", "cam-3"], True)
    # A DSK is over the upstream keys.
    assert key_visibility([luma("cam-6", upstream=False), pip("cam-5", x=0.5, y=0.5, size=1.0)]) == (["cam-5", "cam-6"], True)


def test_nested_mes_recursively() -> None:
    # ME 1: Program cam-1, a pip of ME 2. ME 2: Program cam-2, a luma key of ME 3. ME 3: Program
    # clip-1, a pip of cam-3. ME 4: Program cam-4 (not shown anywhere).
    mes = {
        "me-1": MeLayers(("cam-1",), (pip("me-2"),)),
        "me-2": MeLayers(("cam-2",), (luma("me-3"),)),
        "me-3": MeLayers(("clip-1",), (pip("cam-3"),)),
        "me-4": MeLayers(("cam-4",), ()),
    }
    assert visible_on_program("me-1", mes) == ["cam-1", "me-2", "cam-2", "me-3", "clip-1", "cam-3"]
    assert visible_on_program("me-3", mes) == ["clip-1", "cam-3"]
    # ME 3's pip off screen: cam-3 leaves every ME above it.
    mes["me-3"] = MeLayers(("clip-1",), (pip("cam-3", x=2.0),))
    assert "cam-3" not in visible_on_program("me-1", mes)
    # ME 2 covered by a full-raster pip of cam-5 on ME 1: nothing of ME 2 is on ME 1's Program, nor
    # ME 1's background.
    mes["me-1"] = MeLayers(("cam-1",), (pip("me-2"), pip("cam-5", x=0.5, y=0.5, size=1.0)))
    assert visible_on_program("me-1", mes) == ["cam-5"]
    # A re-entered ME as Program (background), with a mix running to ME 4.
    mes["me-1"] = MeLayers(("me-2", "me-4"), ())
    assert set(visible_on_program("me-1", mes)) == {"me-2", "me-4", "cam-2", "me-3", "clip-1", "cam-4"}


def test_receivers_and_console_follow_every_path(mixer: VisionMixer, client) -> None:
    mixer.apply_workspace(WorkspaceUpdate(mixer_panel_count=3))
    mixer.start(MixerStartRequest(program_input_id="cam-1", preview_input_id="cam-2"))
    mixer.take("cam-2", panel_id="me-2")
    mixer.take("clip-1", panel_id="me-3")
    # ME 3: a pip of cam-3. ME 2: a pip of ME 3. ME 1: a pip of ME 2.
    mixer.update_keyer("me-3-key-1", mode="pip", source_id="cam-3", enabled=True)
    mixer.update_keyer("me-2-key-1", mode="pip", source_id="me-3", enabled=True)
    mixer.update_keyer("me-1-key-1", mode="pip", source_id="me-2", enabled=True)
    on_air = client.get("/api/v1/console").json()["on_air"]
    assert on_air["me-1"] == ["cam-1", "me-2", "cam-2", "me-3", "clip-1", "cam-3"]
    assert on_air["me-2"] == ["cam-2", "me-3", "clip-1", "cam-3"]
    # ME 3's box sized to 0: cam-3 is hidden on every ME.
    mixer.update_keyer("me-3-key-1", dme={"size": 0.0})
    assert all("cam-3" not in shown for shown in mixer.program_visibility().values())
    # ME 1 key 2: a full-raster pip of cam-4 covers ME 1's background and its pip of ME 2.
    mixer.update_keyer("me-1-key-2", mode="pip", source_id="cam-4", dme={"x": 0.5, "y": 0.5, "size": 1.0}, enabled=True)
    assert mixer.program_visibility()["me-1"] == ["cam-4"]

    # The TSL receivers: red for what is visible on some ME's Program.
    mixer.update_keyer("me-1-key-2", enabled=False)
    mixer.update_keyer("me-3-key-1", dme={"size": 0.25})
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    sock.settimeout(1.0)
    try:
        mixer.replace_tally_receivers(
            [TallyReceiver(id="r", kind=TallyKind.custom, label="r", host="127.0.0.1", port=sock.getsockname()[1])]
        )
        packet, _ = sock.recvfrom(4096)
    finally:
        sock.close()
    red = {m["text"] for m in decode_packet(packet)["messages"] if m["rh"] == TALLY_RED}
    labels = {item.id: item.label for item in mixer.list_inputs()}
    assert red == {labels[i] for i in ("cam-1", "cam-2", "cam-3", "clip-1")}


def test_a_flying_off_pip_stays_visible_until_it_ends(mixer: VisionMixer) -> None:
    mixer.start(MixerStartRequest(program_input_id="cam-1", preview_input_id="cam-2"))
    mixer.update_keyer("me-1-key-1", mode="pip", source_id="cam-4", fly=True)
    mixer.set_keyframes("me-1-key-1", [{"x": -0.3}, {"x": 0.5, "frames": 50}])
    mixer.keyer_auto("me-1-key-1")
    assert "cam-4" in mixer.program_visibility()["me-1"]
    mixer.keyer_auto("me-1-key-1")  # flies off (its box is set to keyframe 0, off screen)
    assert "cam-4" in mixer.program_visibility()["me-1"]
    mixer._key_until["me-1-key-1"] = time.monotonic() - 0.01
    assert "cam-4" not in mixer.program_visibility()["me-1"]


def test_simulated_meters_hear_a_visible_pip(mixer: VisionMixer) -> None:
    mixer.start(MixerStartRequest(program_input_id="cam-1", preview_input_id="cam-2"))
    strips = mixer.audio_meters()["panels"]["me-1"]["strips"]
    assert strips["cam-1"]["heard"] and not strips["cam-3"]["heard"]
    mixer.update_keyer("me-1-key-1", mode="pip", source_id="cam-3", enabled=True)
    with mixer._meters_lock:
        mixer._meters_at = 0.0
    strips = mixer.audio_meters()["panels"]["me-1"]["strips"]
    assert strips["cam-1"]["heard"] and strips["cam-3"]["heard"]


VECTORS = Path(__file__).parents[1] / "engine" / "tests" / "visibility-vectors.json"


def test_the_shared_vectors_agree_with_the_engine() -> None:
    # engine/tests/test_visibility.cpp runs the same cases against core/visibility.cpp.
    cases = json.loads(VECTORS.read_text(encoding="utf-8"))["cases"]
    assert len(cases) >= 10
    for case in cases:
        layers = []
        for key in case["keys"]:
            box = key["box"]
            layers.append(
                KeyLayer(
                    source_id=key["source"] or None, keys_source=key["mode"] in ("pip", "luma"), pip=key["mode"] == "pip",
                    upstream=key["upstream"], on_air=key["fade"] > 0, full=key["fade"] >= 1, leaving=False, gain=key["gain"],
                    dme=None if box is None else Dme(enabled=True, x=box["x"], y=box["y"], size=box["size"], soft=box["soft"], crop=box["crop"]),
                )
            )
        assert key_visibility(layers) == (case["keyed"], case["covered"]), case["name"]


def _receiver(mixer: VisionMixer) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    sock.settimeout(2.0)
    mixer.replace_tally_receivers([TallyReceiver(id="r", kind=TallyKind.custom, label="r", host="127.0.0.1", port=sock.getsockname()[1])])
    return sock


def _red(packet: bytes) -> set[str]:
    return {m["text"] for m in decode_packet(packet)["messages"] if m["rh"] == TALLY_RED}


def _last_red(sock: socket.socket, wait: float) -> set[str]:
    """The red labels of the last packet that comes within `wait` seconds."""
    red: set[str] | None = None
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        sock.settimeout(max(0.01, deadline - time.monotonic()))
        try:
            red = _red(sock.recvfrom(4096)[0])
        except TimeoutError:
            break
    assert red is not None
    return red


def test_receivers_get_the_end_of_a_mix_and_of_a_key_fade(mixer: VisionMixer) -> None:
    mixer.start(MixerStartRequest(program_input_id="cam-1", preview_input_id="cam-2"))
    labels = {item.id: item.label for item in mixer.list_inputs()}
    sock = _receiver(mixer)
    try:
        sock.recvfrom(4096)  # the receivers' first packet
        mixer.auto("me-1", frames=5)
        assert _red(sock.recvfrom(4096)[0]) == {labels["cam-1"], labels["cam-2"]}
        # The mix ends (0.1 s) without an operator action: a packet with only cam-2 red follows.
        assert _last_red(sock, 1.0) == {labels["cam-2"]}
        assert mixer.program_visibility()["me-1"] == ["cam-2"]
        # A pip fading off: red until the fade ends, then a packet without it.
        mixer.update_keyer("me-1-key-1", mode="pip", source_id="cam-4", enabled=True, rate_ms=100)
        mixer.keyer_auto("me-1-key-1")
        assert labels["cam-4"] in _red(sock.recvfrom(4096)[0])
        assert _last_red(sock, 1.0) == {labels["cam-2"]}
    finally:
        sock.close()


def test_a_covered_program_is_not_red_on_the_receivers(mixer: VisionMixer) -> None:
    mixer.start(MixerStartRequest(program_input_id="cam-1", preview_input_id="cam-2"))
    labels = {item.id: item.label for item in mixer.list_inputs()}
    sock = _receiver(mixer)
    try:
        sock.recvfrom(4096)
        mixer.update_keyer("me-1-key-1", mode="pip", source_id="cam-5", dme={"x": 0.5, "y": 0.5, "size": 1.0}, enabled=True)
        assert _last_red(sock, 0.3) == {labels["cam-5"]}
    finally:
        sock.close()
