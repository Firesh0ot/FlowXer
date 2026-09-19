"""Push Program/Preview tally and UMD labels to TSL 5.0 receivers."""

from __future__ import annotations

import logging
import socket
import time
from typing import TYPE_CHECKING

from flowxer.api.schemas import MixerState, TallyKind, TallyPreset, TallyReceiver, TallyReceiverStatus
from flowxer.engine.security import SecurityError, assert_egress_host_allowed
from flowxer.engine.tsl import DisplayMessage, encode_packet, lamps_for, wrap_tcp

if TYPE_CHECKING:
    from flowxer.engine.mixer import VisionMixer

log = logging.getLogger(__name__)

TALLY_PRESETS: list[TallyPreset] = [
    TallyPreset(
        kind=TallyKind.companion,
        label="Bitfocus Companion",
        port=8900,
        transport="udp",
        hint="Companion TSL Listener typically on UDP 8900.",
    ),
    TallyPreset(
        kind=TallyKind.vsm,
        label="Lawo VSM",
        port=8900,
        transport="udp",
        hint="Virtual Studio Manager TSL UMD 5.0 input.",
    ),
    TallyPreset(
        kind=TallyKind.bfe,
        label="BFE Commander",
        port=8900,
        transport="udp",
        hint="BFE KSC/Commander TSL UMD 5.0 listener.",
    ),
    TallyPreset(
        kind=TallyKind.hi,
        label="Riedel HI",
        port=8900,
        transport="udp",
        hint="Riedel HI (human interface) Broadcast Controller TSL 5.0 tally/UMD.",
    ),
    TallyPreset(
        kind=TallyKind.custom,
        label="Custom TSL 5.0",
        port=8900,
        transport="udp",
        hint="Any TSL UMD Protocol 5.0 receiver.",
    ),
]


class TallyService:
    def __init__(self) -> None:
        self.receivers: list[TallyReceiver] = []
        self._last_error: dict[str, str | None] = {}
        self._last_sent_at: dict[str, float] = {}
        self._tcp: dict[tuple[str, int], socket.socket] = {}

    def list(self) -> list[TallyReceiver]:
        return list(self.receivers)

    def status(self) -> list[TallyReceiverStatus]:
        return [
            TallyReceiverStatus(
                **item.model_dump(),
                last_error=self._last_error.get(item.id),
                last_sent_at=self._last_sent_at.get(item.id),
            )
            for item in self.receivers
        ]

    def replace(self, receivers: list[TallyReceiver]) -> list[TallyReceiverStatus]:
        seen: set[str] = set()
        cleaned: list[TallyReceiver] = []
        for item in receivers:
            if item.id in seen:
                raise ValueError(f"duplicate tally receiver id {item.id}")
            seen.add(item.id)
            try:
                assert_egress_host_allowed(item.host, what="tally host")
            except SecurityError as exc:
                raise ValueError(str(exc)) from exc
            cleaned.append(item)
        self.receivers = cleaned
        keep = {item.id for item in cleaned}
        self._last_error = {key: value for key, value in self._last_error.items() if key in keep}
        self._last_sent_at = {key: value for key, value in self._last_sent_at.items() if key in keep}
        return self.status()

    def close(self) -> None:
        for sock in self._tcp.values():
            try:
                sock.close()
            except OSError:
                pass
        self._tcp.clear()

    def publish(self, mixer: VisionMixer) -> list[TallyReceiverStatus]:
        messages_by_offset: dict[tuple[int, int], list[DisplayMessage]] = {}
        for receiver in self.receivers:
            if not receiver.enabled:
                continue
            key = (receiver.screen, receiver.index_offset)
            if key not in messages_by_offset:
                messages_by_offset[key] = self._messages(mixer, index_offset=receiver.index_offset)
        for receiver in self.receivers:
            if not receiver.enabled:
                continue
            packet = encode_packet(
                receiver.screen,
                messages_by_offset[(receiver.screen, receiver.index_offset)],
            )
            try:
                self._send(receiver, packet)
                self._last_error[receiver.id] = None
                self._last_sent_at[receiver.id] = time.time()
            except OSError as exc:
                self._last_error[receiver.id] = str(exc)
                log.warning("tally send to %s (%s:%s) failed: %s", receiver.id, receiver.host, receiver.port, exc)
        return self.status()

    def _messages(self, mixer: VisionMixer, *, index_offset: int) -> list[DisplayMessage]:
        running = mixer.state == MixerState.running
        program_ids = {panel.program_input_id for panel in mixer.panels if panel.program_input_id}
        preview_ids = {panel.preview_input_id for panel in mixer.panels if panel.preview_input_id}
        if running:
            if mixer.program_input_id:
                program_ids.add(mixer.program_input_id)
            if mixer.preview_input_id:
                preview_ids.add(mixer.preview_input_id)
        messages: list[DisplayMessage] = []
        for source in mixer.list_inputs():
            program = running and source.id in program_ids
            preview = running and source.id in preview_ids
            rh, txt, lh = lamps_for(program=program, preview=preview)
            messages.append(
                DisplayMessage(
                    index=source.slot + index_offset,
                    text=source.label,
                    rh=rh,
                    txt=txt,
                    lh=lh,
                )
            )
        return messages

    def _send(self, receiver: TallyReceiver, packet: bytes) -> None:
        assert_egress_host_allowed(receiver.host, what="tally host")
        use_tcp = receiver.transport == "tcp"
        framed = wrap_tcp(packet) if (receiver.dle_stx if receiver.dle_stx is not None else use_tcp) else packet
        if use_tcp:
            self._send_tcp(receiver.host, receiver.port, framed)
            return
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.settimeout(0.4)
            sock.sendto(framed, (receiver.host, receiver.port))
        finally:
            sock.close()

    def _send_tcp(self, host: str, port: int, payload: bytes) -> None:
        key = (host, port)
        sock = self._tcp.get(key)
        try:
            if sock is None:
                sock = socket.create_connection((host, port), timeout=0.8)
                sock.settimeout(0.8)
                self._tcp[key] = sock
            sock.sendall(payload)
        except OSError:
            old = self._tcp.pop(key, None)
            if old is not None:
                try:
                    old.close()
                except OSError:
                    pass
            sock = socket.create_connection((host, port), timeout=0.8)
            sock.settimeout(0.8)
            sock.sendall(payload)
            self._tcp[key] = sock
