"""Raw tally for the platform's tally calculator (FLOWXER_TALLY_TSL), TSL UMD 5.0.

Each ME is one SCREEN (ME 1 = 1 .. ME 4 = 4), each input one display INDEX. LH tally is
red while the input is on that ME's Program, and for both sources of a mix or stinger
while it runs; RH tally is green while the input is on the ME's Preview. Text tally is
off, TEXT is the input's label (UTF-16LE). Every send carries all inputs of every ME, on
each change, when a mix ends and at least once a second. The calculator works out what
is on air further down (path-aware tally).
"""

from __future__ import annotations

import logging
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from flowxer.engine.tsl import TALLY_GREEN, TALLY_OFF, TALLY_RED, DisplayMessage, encode_packets, wrap_tcp

log = logging.getLogger(__name__)

# The full state goes out at least this often (s).
REFRESH_S = 1.0
# After a failed send (name not resolvable, connection refused) the export waits this long (s)
# before it resolves the name and connects again; the last pause repeats.
RETRY_BACKOFF_S = (1.0, 2.0, 5.0, 10.0)
# A connect or send that blocks gives up after this long (s).
SEND_TIMEOUT_S = 1.0
# Failures are logged at most this often (s).
LOG_EVERY_S = 60.0


@dataclass(frozen=True)
class MeTally:
    program: str | None = None
    preview: str | None = None
    # A mix or stinger that runs: outgoing input, incoming input, monotonic end (None: until
    # the mixer reports its end).
    transition: tuple[str | None, str, float | None] | None = None


@dataclass(frozen=True)
class TallySnapshot:
    # (input id, label) in slot order.
    inputs: tuple[tuple[str, str], ...]
    # ME 1..n in panel order.
    mes: tuple[MeTally, ...]


def assign_indexes(indexes: dict[str, int], snapshot: TallySnapshot) -> None:
    """An input seen for the first time gets the next number (in slot order). Numbers are
    never changed or reused, so removing an input does not move the others."""
    for input_id, _label in snapshot.inputs:
        indexes.setdefault(input_id, len(indexes))


def me_messages(snapshot: TallySnapshot, me: MeTally, indexes: dict[str, int], now: float) -> list[DisplayMessage]:
    """One display message per input for one ME."""
    on_air = {me.program}
    if me.transition is not None and (me.transition[2] is None or now < me.transition[2]):
        on_air.update(me.transition[:2])
    return [
        DisplayMessage(
            index=indexes[input_id],
            text=label,
            lh=TALLY_RED if input_id in on_air else TALLY_OFF,
            rh=TALLY_GREEN if input_id == me.preview else TALLY_OFF,
        )
        for input_id, label in snapshot.inputs
    ]


class TallyExport:
    """Sends the raw tally to one TSL 5.0 listener from its own thread. The mixer only
    calls changed(); the thread takes the snapshot, encodes and sends."""

    def __init__(self, transport: str, host: str, port: int, snapshot: Callable[[], TallySnapshot]) -> None:
        self.target = f"{transport}://{host}:{port}"
        self._transport = transport
        self._host = host
        self._port = port
        self._snapshot = snapshot
        self._indexes: dict[str, int] = {}
        self._sock: socket.socket | None = None
        self._failures = 0
        self._retry_at = 0.0
        self._logged_at = -LOG_EVERY_S
        self._closed = False
        # For /metrics: packets sent, failed sends, wall-clock time of the last good send.
        self.packets_sent = 0
        self.send_errors = 0
        self.last_success = 0.0
        self._wake = threading.Event()
        log.info("FLOWXER_TALLY_TSL: raw tally to %s", self.target)
        self._thread = threading.Thread(target=self._run, name="tally-tsl", daemon=True)
        self._thread.start()

    def changed(self) -> None:
        """The mixer state changed: send it now (from the export thread; never blocks)."""
        self._wake.set()

    def close(self) -> None:
        """Send the last state (all off once Program stopped) and end the thread."""
        self._closed = True
        self._wake.set()
        self._thread.join(timeout=2 * SEND_TIMEOUT_S)

    def _run(self) -> None:
        while True:
            self._wake.clear()
            closing = self._closed
            now = time.monotonic()
            next_send = now + REFRESH_S
            if now >= self._retry_at:
                try:
                    snapshot = self._snapshot()
                    packets = self._packets(snapshot, now)
                    self._send(packets)
                    self._failures = 0
                    self.packets_sent += len(packets)
                    self.last_success = time.time()
                    # A mix that ran at `now` is sent again when it ends.
                    ends = [me.transition[2] for me in snapshot.mes if me.transition and me.transition[2]]
                    next_send = min([next_send] + [end for end in ends if end > now])
                except Exception as exc:
                    self._fail(exc)
            if closing:
                break
            self._wake.wait(max(next_send - time.monotonic(), 0.0))
        self._disconnect()

    def _packets(self, snapshot: TallySnapshot, now: float) -> list[bytes]:
        assign_indexes(self._indexes, snapshot)
        return [
            packet
            for screen, me in enumerate(snapshot.mes, 1)
            for packet in encode_packets(screen, me_messages(snapshot, me, self._indexes, now), utf16=True)
        ]

    def _send(self, packets: list[bytes]) -> None:
        if self._sock is None:
            # Resolved at every connect: a name that does not exist yet is found later.
            kind = socket.SOCK_STREAM if self._transport == "tcp" else socket.SOCK_DGRAM
            family, kind, proto, _name, address = socket.getaddrinfo(self._host, self._port, type=kind)[0]
            sock = socket.socket(family, kind, proto)
            sock.settimeout(SEND_TIMEOUT_S)
            try:
                sock.connect(address)
            except OSError:
                sock.close()
                raise
            self._sock = sock
        if self._transport == "tcp":
            self._sock.sendall(b"".join(wrap_tcp(packet, etx=True) for packet in packets))
        else:
            for packet in packets:
                self._sock.send(packet)

    def _fail(self, exc: Exception) -> None:
        self._disconnect()
        self._failures += 1
        self.send_errors += 1
        pause = RETRY_BACKOFF_S[min(self._failures, len(RETRY_BACKOFF_S)) - 1]
        now = time.monotonic()
        self._retry_at = now + pause
        if now - self._logged_at >= LOG_EVERY_S:
            self._logged_at = now
            log.warning(
                "FLOWXER_TALLY_TSL: sending to %s failed (%d in a row, next try in %.0f s): %s",
                self.target,
                self._failures,
                pause,
                exc,
            )

    def _disconnect(self) -> None:
        if self._sock is not None:
            self._sock.close()
            self._sock = None
