// The previews: the engine encodes every source and every ME's Preview once, into one mosaic
// stream, and (by default) each ME's Program as a sharper stream of its own (`streams` of the
// map; an older engine has the Programs as mosaic tiles). One WebRTC (WHEP) session per stream
// and page: the mosaic's pictures are a <video> on that one MediaStream inside a box with its
// tile's aspect ratio and overflow hidden, and a CSS transform scales and moves the video so only
// the tile shows (5 px inside its edges, so the browser's scaling never shows a neighbour). That
// works in every browser (object-view-box is Chromium only); mxl-replay 1.4.0 and mxl-multiviewer
// 1.4.1 do the same. A Program stream is a <video> of the whole stream. The sessions follow the
// console's map, stay for the page and connect again when they drop: at once when no video packet
// came for a second (WebRTC itself notices only after 5 s or more). No audio.
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

/** A picture that is a stream of its own (an ME's Program). */
export interface PreviewStream {
  /** `panel:<panel id>:pgm`. */
  id: string;
  path: string;
  width?: number;
  height?: number;
  label?: string;
  panel_id?: string;
  /** Shared MediaMTX: absolute. */
  whep_url?: string | null;
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
  /** Pictures that are streams of their own (absent: none, an older engine). */
  streams?: PreviewStream[];
}

/** Pixels of the mosaic left out at each edge of a tile: the browser scales the whole mosaic, so
 * a crop exactly on the tile would show a sliver of the neighbouring tile or the border. */
export const TILE_INSET = 5;

const RETRY_MS = 500;
// No new video packet for this long: the stream stalled (e.g. its MediaMTX restarted). Before the
// first packet (ICE, DTLS, the next key frame) the session gets longer.
const STALL_MS = 1000;
const FIRST_PACKET_MS = 4000;

/** Where the page plays the mosaic (or, with `stream`, that stream) from; "" when there is none. */
export function whepUrl(map: PreviewMap | null, stream?: PreviewStream): string {
  if (!map?.available) return "";
  const absolute = stream ? stream.whep_url : map.whep_url;
  if (absolute) return absolute;
  const path = stream ? stream.path : map.path;
  if (!map.whep_port || !path) return "";
  return `${location.protocol}//${location.hostname}:${map.whep_port}/${path}/whep`;
}

/**
 * How tile `id` is shown: `box` for its container (the tile's aspect ratio) and `video` for the
 * <video> filling it: the whole mosaic stretched to the box, moved so the tile is at the top left
 * and scaled up so the tile fills the box. Null when the map has no such tile.
 */
export function tileCrop(map: PreviewMap | null, id: string): { box: CSSProperties; video: CSSProperties } | null {
  const found = map?.available ? map.tiles.find((t) => t.id === id) : undefined;
  if (!map?.width || !map.height || !found || !found.w || !found.h) return null;
  const tile = insetTile(found);
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

/** The tile less TILE_INSET at each edge (a tile too small for it stays as it is). */
export function insetTile<T extends { x: number; y: number; w: number; h: number }>(tile: T): T {
  if (tile.w <= 4 * TILE_INSET || tile.h <= 4 * TILE_INSET) return tile;
  return { ...tile, x: tile.x + TILE_INSET, y: tile.y + TILE_INSET, w: tile.w - 2 * TILE_INSET, h: tile.h - 2 * TILE_INSET };
}

/** A picture that is a stream of its own: the whole stream in a box of its aspect ratio. */
export function streamView(map: PreviewMap | null, id: string): { box: CSSProperties; video: CSSProperties } | null {
  const stream = map?.available ? map.streams?.find((s) => s.id === id) : undefined;
  if (!stream) return null;
  const w = stream.width || 16;
  const h = stream.height || 9;
  return { box: { "--ar": `${w} / ${h}` } as CSSProperties, video: { objectFit: "fill" } };
}

/** One WHEP session (a stream of the map), connected again when it drops. */
class WhepSession {
  stream: MediaStream | null = null;
  connected = false;
  private url = "";
  private pc: RTCPeerConnection | null = null;
  private timer: number | undefined;

  private notify: () => void;

  constructor(notify: () => void) {
    this.notify = notify;
  }

  /** A new URL connects again; "" closes it. */
  setUrl(url: string) {
    if (url === this.url) return;
    this.url = url;
    this.close();
    if (url) void this.connect(url);
  }

  close() {
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

/** The page's sessions: the mosaic and each stream of the map, following the console's map. */
class PreviewSession {
  map: PreviewMap | null = null;
  private mosaic: WhepSession;
  private streams = new Map<string, WhepSession>();
  private listeners = new Set<() => void>();

  constructor() {
    this.mosaic = new WhepSession(() => this.notify());
  }

  /** The mosaic's MediaStream. */
  get stream(): MediaStream | null {
    return this.mosaic.stream;
  }

  get connected(): boolean {
    return this.mosaic.connected;
  }

  /** The MediaStream of stream `id` (`panel:<id>:pgm`), or null. */
  streamOf(id: string): MediaStream | null {
    return this.streams.get(id)?.stream ?? null;
  }

  subscribe(listener: () => void): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  /** The console's map (every refresh): a new stream URL connects again, a stream that is gone closes. */
  setMap(map: PreviewMap) {
    this.map = map;
    this.mosaic.setUrl(whepUrl(map));
    const wanted = new Map((map.available ? map.streams ?? [] : []).map((stream) => [stream.id, whepUrl(map, stream)]));
    for (const [id, session] of this.streams) {
      if (!wanted.get(id)) {
        session.close();
        this.streams.delete(id);
      }
    }
    for (const [id, url] of wanted) {
      if (!url) continue;
      let session = this.streams.get(id);
      if (!session) {
        session = new WhepSession(() => this.notify());
        this.streams.set(id, session);
      }
      session.setUrl(url);
    }
    this.notify();
  }

  private notify() {
    for (const listener of this.listeners) listener();
  }
}

/** The page's sessions. */
export const previewSession = new PreviewSession();

/** Re-renders the caller when the map or the stream changes. */
export function usePreview(): PreviewSession {
  const [, setTick] = useState(0);
  useEffect(() => previewSession.subscribe(() => setTick((n) => n + 1)), []);
  return previewSession;
}
