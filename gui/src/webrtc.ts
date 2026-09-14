export function jpegUrl(streamId: string): string {
  return `/api/v1/preview/jpeg/${streamId}?t=${Date.now()}`;
}

async function waitIce(pc: RTCPeerConnection): Promise<void> {
  if (pc.iceGatheringState === "complete") return;
  await new Promise<void>((resolve) => {
    const timer = window.setTimeout(resolve, 2500);
    pc.onicegatheringstatechange = () => {
      if (pc.iceGatheringState === "complete") {
        window.clearTimeout(timer);
        resolve();
      }
    };
  });
}

export async function connectWhep(video: HTMLVideoElement, streamId: string): Promise<() => void> {
  const pc = new RTCPeerConnection({ iceServers: [] });
  pc.addTransceiver("video", { direction: "recvonly" });
  pc.ontrack = (event) => {
    const [stream] = event.streams;
    if (stream) video.srcObject = stream;
  };
  const offer = await pc.createOffer();
  await pc.setLocalDescription(offer);
  await waitIce(pc);
  const response = await fetch(`/api/v1/webrtc/whep/${encodeURIComponent(streamId)}`, {
    method: "POST",
    headers: { "Content-Type": "application/sdp" },
    body: pc.localDescription?.sdp ?? "",
  });
  if (!response.ok) {
    pc.close();
    throw new Error(`WHEP ${response.status}`);
  }
  const answer = await response.text();
  await pc.setRemoteDescription({ type: "answer", sdp: answer });
  return () => pc.close();
}
