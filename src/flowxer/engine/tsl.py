"""TSL UMD Protocol 5.0 — tally lamps and under-monitor display labels.

Packets are little-endian. UDP carries the packet as-is. TCP (and other
byte streams) wrap it with DLE/STX and DLE stuffing (DLE=0xFE, STX=0x02).

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


def _text_bytes(text: str) -> bytes:
    cleaned = "".join(ch if 32 <= ord(ch) < 127 else " " for ch in text)[:64]
    return cleaned.encode("ascii")


def encode_packet(screen: int, messages: list[DisplayMessage], *, utf16: bool = False) -> bytes:
    if utf16:
        raise ValueError("UTF-16 TSL text is not used; send ASCII")
    body = bytearray()
    body += struct.pack("<BBH", VERSION_5_00, 0, screen & 0xFFFF)
    for message in messages:
        text = _text_bytes(message.text)
        control = control_word(message.rh, message.txt, message.lh, message.brightness)
        body += struct.pack("<HHH", message.index & 0xFFFF, control, len(text))
        body += text
    packet = struct.pack("<H", len(body)) + bytes(body)
    if len(packet) > MAX_UDP_PACKET:
        raise ValueError(f"TSL packet {len(packet)} bytes exceeds {MAX_UDP_PACKET}")
    return packet


def wrap_tcp(packet: bytes) -> bytes:
    stuffed = bytearray((DLE, STX))
    for byte in packet:
        stuffed.append(byte)
        if byte == DLE:
            stuffed.append(DLE)
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
        text = body[offset : offset + length].decode("ascii", "replace")
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
