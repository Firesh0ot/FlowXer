// The preview mosaic: the engine encodes every source and every ME's Program and Preview once,
// into one stream. One WebRTC (WHEP) session per page plays it; each picture is a <video> on that
// one MediaStream inside a box with its tile's aspect ratio and overflow hidden, and a CSS
// transform scales and moves the video so only the tile shows. That works in every browser
// (object-view-box is Chromium only); mxl-replay 1.4.0 and mxl-multiviewer 1.4.1 do the same.
// The session follows the console's map, stays for the page and connects again when it drops:
// at once when no video packet came for a second (WebRTC itself notices only after 5 s or more).
// No audio.
import { useEffect, useState, type CSSProperties } from "react";

export interface PreviewTile {
  /** `source:<input id>` or `panel:<panel id>:pgm|pvw`. */
  id: string;
  x: number;
  y: number;
  w: number;
  h: number;
  label?: string;
}

export interface PreviewMap {
  available: boolean;
  reason?: string;
  width?: number;
  height?: number;
  path?: string;
  /** Shared MediaMTX (the preview contract): absolute. */
  whep_url?: string | null;
  /** The engine's own MediaMTX: on the page's host at this port. */
  whep_port?: number | null;
  tiles: PreviewTile[];
}

const RETRY_MS = 500;
// No new video packet for this long: the stream stalled (e.g. its MediaMTX restarted). Before the
// first packet (ICE, DTLS, the next key frame) the session gets longer.
const STALL_MS = 1000;
const FIRST_PACKET_MS = 4000;

/** Where the page plays the mosaic from; "" when there is none. */
export function whepUrl(map: PreviewMap | null): string {
  if (!map?.available) return "";
  if (map.whep_url) return map.whep_url;
  if (!map.whep_port || !map.path) return "";
  return `${location.protocol}//${location.hostname}:${map.whep_port}/${map.path}/whep`;
}

/**
 * How tile `id` is shown: `box` for its container (the tile's aspect ratio) and `video` for the
 * <video> filling it: the whole mosaic stretched to the box, moved so the tile is at the top left
 * and scaled up so the tile fills the box. Null when the map has no such tile.
 */
export function tileCrop(map: PreviewMap | null, id: string): { box: CSSProperties; video: CSSProperties } | null {
  const tile = map?.available ? map.tiles.find((t) => t.id === id) : undefined;
  if (!map?.width || !map.height || !tile || !tile.w || !tile.h) return null;
  const { width: W, height: H } = map;
  return {
    box: { "--ar": `${tile.w} / ${tile.h}` } as CSSProperties,
    video: {
      objectFit: "fill",
      transformOrigin: "0 0",
      transform: `scale(${W / tile.w}, ${H / tile.h}) translate(${(-100 * tile.x) / W}%, ${(-100 * tile.y) / H}%)`,
    },
  };
}

class PreviewSession {
  map: PreviewMap | null = null;
  stream: MediaStream | null = null;
  connected = false;
  private url = "";
  private pc: RTCPeerConnection | null = null;
  private timer: number | undefined;
  private listeners = new Set<() => void>();

  subscribe(listener: () => void): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  /** The console's map (every refresh): a new stream URL connects again. */
  setMap(map: PreviewMap) {
    this.map = map;
    const url = whepUrl(map);
    if (url !== this.url) {
      this.url = url;
      this.close();
      if (url) void this.connect(url);
    }
    this.notify();
  }

  private notify() {
    for (const listener of this.listeners) listener();
  }

  private close() {
    window.clearTimeout(this.timer);
    this.pc?.close();
    this.pc = null;
    this.stream = null;
    this.connected = false;
  }

  /** Connects again when the video stops arriving. */
  private watch(pc: RTCPeerConnection) {
    let packets = 0;
    let changed = Date.now();
    let busy = false;
    const timer = window.setInterval(async () => {
      if (this.pc !== pc) {
        window.clearInterval(timer);
        return;
      }
      if (busy) return;
      busy = true;
      let received = packets;
      try {
        (await pc.getStats()).forEach((report) => {
          if (report.type === "inbound-rtp" && report.kind === "video") received = report.packetsReceived ?? received;
        });
      } catch {
        // closed meanwhile
      }
      busy = false;
      if (received !== packets) {
        packets = received;
        changed = Date.now();
      } else if (Date.now() - changed > (packets ? STALL_MS : FIRST_PACKET_MS)) {
        window.clearInterval(timer);
        this.retry(pc);
      }
    }, 250);
  }

  private retry(pc: RTCPeerConnection) {
    if (this.pc !== pc) return;
    const url = this.url;
    this.close();
    this.notify();
    this.timer = window.setTimeout(() => {
      if (url && url === this.url) void this.connect(url);
    }, RETRY_MS);
  }

  private async connect(url: string) {
    const pc = new RTCPeerConnection();
    this.pc = pc;
    pc.addTransceiver("video", { direction: "recvonly" });
    const stream = new MediaStream();
    pc.ontrack = (event) => stream.addTrack(event.track);
    try {
      await pc.setLocalDescription(await pc.createOffer());
      // The offer goes with all candidates (no trickle).
      await new Promise<void>((resolve) => {
        if (pc.iceGatheringState === "complete") return resolve();
        const timer = window.setTimeout(resolve, 1000);
        pc.addEventListener("icegatheringstatechange", () => {
          if (pc.iceGatheringState === "complete") {
            window.clearTimeout(timer);
            resolve();
          }
        });
      });
      const response = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/sdp" },
        body: pc.localDescription?.sdp ?? "",
      });
      if (!response.ok) throw new Error(`WHEP ${response.status}`);
      await pc.setRemoteDescription({ type: "answer", sdp: await response.text() });
      if (this.pc !== pc) return;
      pc.onconnectionstatechange = () => {
        if (this.pc !== pc) return;
        this.connected = pc.connectionState === "connected";
        if (["failed", "disconnected", "closed"].includes(pc.connectionState)) this.retry(pc);
        else this.notify();
      };
      this.stream = stream;
      this.watch(pc);
      this.notify();
    } catch {
      this.retry(pc);
    }
  }
}

/** The page's one session. */
export const previewSession = new PreviewSession();

/** Re-renders the caller when the map or the stream changes. */
export function usePreview(): PreviewSession {
  const [, setTick] = useState(0);
  useEffect(() => previewSession.subscribe(() => setTick((n) => n + 1)), []);
  return previewSession;
}
