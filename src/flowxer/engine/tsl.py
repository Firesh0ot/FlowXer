"""TSL UMD Protocol 5.0 — tally lamps and under-monitor display labels.

Packets are little-endian, at most 2048 bytes; more display messages go in
more packets. UDP carries the packet as-is. TCP (and other byte streams) wrap
it with DLE/STX and DLE stuffing (DLE=0xFE, STX=0x02), optionally ended by
DLE/ETX (ETX=0x03). Text is ASCII, or UTF-16LE with FLAGS bit 0.

CONTROL word (per display):
  bits 0-1  RH tally   0=off 1=red 2=green 3=amber
  bits 2-3  text tally
  bits 4-5  LH tally
  bits 6-7  brightness 0-3
  bit 15    1 = control data (unused here)
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

DLE = 0xFE
STX = 0x02
ETX = 0x03
VERSION_5_00 = 0
FLAG_UTF16 = 0x01
FLAG_SCONTROL = 0x02
BROADCAST_INDEX = 0xFFFF
MAX_UDP_PACKET = 2048

TALLY_OFF = 0
TALLY_RED = 1
TALLY_GREEN = 2
TALLY_AMBER = 3


@dataclass(frozen=True)
class DisplayMessage:
    index: int
    text: str
    rh: int = TALLY_OFF
    txt: int = TALLY_OFF
    lh: int = TALLY_OFF
    brightness: int = 3


def control_word(rh: int, txt: int, lh: int, brightness: int = 3) -> int:
    return (rh & 3) | ((txt & 3) << 2) | ((lh & 3) << 4) | ((brightness & 3) << 6)


def lamps_for(*, program: bool, preview: bool) -> tuple[int, int, int]:
    """RH = Program (red), LH = Preview (green), text follows the stronger bus."""
    rh = TALLY_RED if program else TALLY_OFF
    lh = TALLY_GREEN if preview else TALLY_OFF
    if program and preview:
        txt = TALLY_AMBER
    elif program:
        txt = TALLY_RED
    elif preview:
        txt = TALLY_GREEN
    else:
        txt = TALLY_OFF
    return rh, txt, lh


def _text_bytes(text: str, utf16: bool) -> bytes:
    if utf16:
        return text.encode("utf-16-le")
    cleaned = "".join(ch if 32 <= ord(ch) < 127 else " " for ch in text)[:64]
    return cleaned.encode("ascii")


def _message_bytes(message: DisplayMessage, utf16: bool) -> bytes:
    text = _text_bytes(message.text, utf16)
    control = control_word(message.rh, message.txt, message.lh, message.brightness)
    return struct.pack("<HHH", message.index & 0xFFFF, control, len(text)) + text


def encode_packets(screen: int, messages: list[DisplayMessage], *, utf16: bool = False) -> list[bytes]:
    """The messages in as many packets as needed, each at most MAX_UDP_PACKET bytes."""
    bodies = [b""]
    for message in messages:
        data = _message_bytes(message, utf16)
        # 6 header bytes: PBC, VER, FLAGS, SCREEN.
        if bodies[-1] and 6 + len(bodies[-1]) + len(data) > MAX_UDP_PACKET:
            bodies.append(b"")
        bodies[-1] += data
    flags = FLAG_UTF16 if utf16 else 0
    return [struct.pack("<HBBH", 4 + len(body), VERSION_5_00, flags, screen & 0xFFFF) + body for body in bodies]


def encode_packet(screen: int, messages: list[DisplayMessage], *, utf16: bool = False) -> bytes:
    packets = encode_packets(screen, messages, utf16=utf16)
    if len(packets) > 1:
        raise ValueError(f"TSL messages exceed one {MAX_UDP_PACKET}-byte packet")
    return packets[0]


def wrap_tcp(packet: bytes, *, etx: bool = False) -> bytes:
    stuffed = bytearray((DLE, STX))
    for byte in packet:
        stuffed.append(byte)
        if byte == DLE:
            stuffed.append(DLE)
    if etx:
        stuffed += bytes((DLE, ETX))
    return bytes(stuffed)


def decode_packet(packet: bytes) -> dict:
    if len(packet) < 6:
        raise ValueError("truncated TSL packet")
    pbc = struct.unpack_from("<H", packet, 0)[0]
    body = packet[2 : 2 + pbc]
    if len(body) < 4:
        raise ValueError("truncated TSL header")
    ver, flags, screen = struct.unpack_from("<BBH", body, 0)
    offset = 4
    messages: list[dict] = []
    while offset + 6 <= len(body):
        index, control, length = struct.unpack_from("<HHH", body, offset)
        offset += 6
        text = body[offset : offset + length].decode("utf-16-le" if flags & FLAG_UTF16 else "ascii", "replace")
        offset += length
        messages.append(
            {
                "index": index,
                "rh": control & 3,
                "txt": (control >> 2) & 3,
                "lh": (control >> 4) & 3,
                "brightness": (control >> 6) & 3,
                "text": text,
            }
        )
    return {"ver": ver, "flags": flags, "screen": screen, "messages": messages}
