from __future__ import annotations

import asyncio
import logging
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


async def create_whep_answer(mixer, stream_id: str, offer_sdp: str) -> str:
    from aiortc import RTCPeerConnection, RTCSessionDescription
    from aiortc.rtcrtpsender import RTCRtpSender
    from av import VideoFrame

    from flowxer.engine.preview import render_monitor

    try:
        from aiortc import VideoStreamTrack
    except ImportError:  # pragma: no cover
        from aiortc.mediastreams import VideoStreamTrack

    class MixerTrack(VideoStreamTrack):
        kind = "video"

        def __init__(self) -> None:
            super().__init__()
            self._stream_id = stream_id

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
    await pc.setLocalDescription(answer)
    await _wait_ice(pc)
    return pc.localDescription.sdp


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
