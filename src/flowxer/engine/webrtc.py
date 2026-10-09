from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from typing import Any

log = logging.getLogger(__name__)

_PEERS: set[Any] = set()


def webrtc_available() -> bool:
    try:
        import aiortc  # noqa: F401
        from av import VideoFrame  # noqa: F401

        return True
    except Exception:
        return False


def peer_count() -> int:
    return len(_PEERS)


def rewrite_ice_host(sdp: str, public_ip: str) -> str:
    """Advertise FLOWXER_WEBRTC_PUBLIC_IP on host ICE candidates.

    aioice gathers the kernel's local address; lab clients need the management IP.
    """
    ip = (public_ip or "").strip()
    if not ip:
        return sdp
    out: list[str] = []
    for line in sdp.splitlines(keepends=True):
        ending = "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""
        stripped = line.rstrip("\r\n")
        if stripped.startswith("c=IN IP4 "):
            out.append(f"c=IN IP4 {ip}{ending}")
            continue
        if stripped.startswith("a=candidate:") and " typ host" in stripped:
            parts = stripped.split()
            if len(parts) >= 6:
                parts[4] = ip
                out.append(" ".join(parts) + ending)
                continue
        out.append(line)
    return "".join(out)


def resolved_webrtc_ip(settings) -> str:
    return (
        (getattr(settings, "webrtc_public_ip", "") or "").strip()
        or (getattr(settings, "nmos_host_ip", "") or "").strip()
    )


@asynccontextmanager
async def ice_udp_port_range(port_min: int, port_max: int):
    """Bind aioice host sockets in [min, max]. aioice 0.10 always uses port 0.

    Documented in docs/platform-integration-plan.md: wrap create_datagram_endpoint
    rather than a non-existent RTCConfiguration field.
    """
    if port_min <= 0 or port_max <= 0 or port_max < port_min:
        yield
        return
    loop = asyncio.get_running_loop()
    original = loop.create_datagram_endpoint

    async def _bound(protocol_factory, local_addr=None, **kwargs):
        if (
            local_addr
            and isinstance(local_addr, tuple)
            and len(local_addr) >= 2
            and local_addr[1] == 0
        ):
            last_error: OSError | None = None
            host = local_addr[0]
            extra = local_addr[2:]
            for port in range(port_min, port_max + 1):
                try:
                    return await original(
                        protocol_factory, local_addr=(host, port, *extra), **kwargs
                    )
                except OSError as exc:
                    last_error = exc
            log.warning(
                "WebRTC UDP ports %s-%s busy (%s); falling back to an ephemeral port",
                port_min,
                port_max,
                last_error,
            )
        return await original(protocol_factory, local_addr=local_addr, **kwargs)

    loop.create_datagram_endpoint = _bound  # type: ignore[method-assign]
    try:
        yield
    finally:
        loop.create_datagram_endpoint = original  # type: ignore[method-assign]


async def create_whep_answer(mixer, stream_id: str, offer_sdp: str) -> str:
    from aiortc import RTCPeerConnection, RTCSessionDescription
    from aiortc.mediastreams import VIDEO_CLOCK_RATE, VIDEO_TIME_BASE, MediaStreamError
    from aiortc.rtcrtpsender import RTCRtpSender
    from av import VideoFrame

    from flowxer.engine.preview import render_monitor

    max_peers = int(getattr(mixer.settings, "max_webrtc_peers", 16) or 16)
    stale = {
        peer
        for peer in list(_PEERS)
        if getattr(peer, "connectionState", "") in {"failed", "closed", "disconnected"}
    }
    for peer in stale:
        _PEERS.discard(peer)
    if len(_PEERS) >= max_peers:
        raise RuntimeError("too many WebRTC preview peers")

    try:
        from aiortc import VideoStreamTrack
    except ImportError:  # pragma: no cover
        from aiortc.mediastreams import VideoStreamTrack

    # One frame per monitor picture: the pictures change FLOWXER_MONITOR_FPS times a second, and at
    # aiortc's 30 frames/s every peer encoded each one about three times.
    fps = int(getattr(mixer.settings, "monitor_fps", 0) or 0) or 10

    class MixerTrack(VideoStreamTrack):
        kind = "video"

        def __init__(self) -> None:
            super().__init__()
            self._stream_id = stream_id

        async def next_timestamp(self):
            # VideoStreamTrack.next_timestamp (aiortc 1.9) at `fps` instead of 30 frames/s.
            if self.readyState != "live":
                raise MediaStreamError
            if hasattr(self, "_timestamp"):
                self._timestamp += VIDEO_CLOCK_RATE // fps
                await asyncio.sleep(self._start + self._timestamp / VIDEO_CLOCK_RATE - time.time())
            else:
                self._start = time.time()
                self._timestamp = 0
            return self._timestamp, VIDEO_TIME_BASE

        async def recv(self):
            pts, time_base = await self.next_timestamp()
            image = render_monitor(mixer, self._stream_id, width=640, height=360)
            frame = VideoFrame.from_ndarray(_as_ndarray(image), format="rgb24")
            frame.pts = pts
            frame.time_base = time_base
            return frame

    pc = RTCPeerConnection()
    _PEERS.add(pc)

    @pc.on("connectionstatechange")
    async def _on_state() -> None:
        if pc.connectionState in {"failed", "closed", "disconnected"}:
            await pc.close()
            _PEERS.discard(pc)

    sender = pc.addTrack(MixerTrack())
    _prefer_h264(sender, RTCRtpSender)
    await pc.setRemoteDescription(RTCSessionDescription(sdp=offer_sdp, type="offer"))
    answer = await pc.createAnswer()
    settings = mixer.settings
    async with ice_udp_port_range(
        int(getattr(settings, "webrtc_udp_port_min", 0) or 0),
        int(getattr(settings, "webrtc_udp_port_max", 0) or 0),
    ):
        await pc.setLocalDescription(answer)
        await _wait_ice(pc)
    sdp = pc.localDescription.sdp
    return rewrite_ice_host(sdp, resolved_webrtc_ip(settings))


def _as_ndarray(image):
    try:
        import numpy as np

        return np.asarray(image)
    except Exception:  # pragma: no cover
        import array

        return image


def _prefer_h264(sender, RTCRtpSender) -> None:
    try:
        caps = RTCRtpSender.getCapabilities("video")
        prefs = [c for c in caps.codecs if c.mimeType.lower() in {"video/h264", "video/vp8"}]
        if prefs:
            sender.setCodecPreferences(prefs)
    except Exception:
        return


async def _wait_ice(pc, timeout: float = 4.0) -> None:
    if pc.iceGatheringState == "complete":
        return
    done = asyncio.Event()

    @pc.on("icegatheringstatechange")
    def _() -> None:
        if pc.iceGatheringState == "complete":
            done.set()

    try:
        await asyncio.wait_for(done.wait(), timeout=timeout)
    except TimeoutError:
        log.warning("ICE gathering timed out; returning partial SDP")
