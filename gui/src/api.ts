export type InputKind = "mxl_live" | "file" | "replay" | "test" | "black";

export interface LogicalInput {
  id: string;
  label: string;
  kind: InputKind;
  slot: number;
  file_path?: string | null;
  library_item_id?: string | null;
  group_hint?: string | null;
  stinger_slot_id?: string | null;
  video?: { flow_id?: string | null; media_type: string } | null;
  audio?: { flow_id?: string | null; media_type: string; channels: number } | null;
}

export interface MixerPanel {
  id: string;
  label: string;
  program_input_id: string | null;
  preview_input_id: string | null;
  wipe_armed?: boolean;
  last_transition?: string;
}

export interface DownstreamKeyer {
  id: string;
  label: string;
  enabled: boolean;
  url: string;
  title: string;
  subtitle: string;
}

export interface StingerSlot {
  id: string;
  role: string;
  label: string;
  stinger_id: string;
  library_item_id?: string | null;
  kind?: "sequence" | "video";
  media_path?: string | null;
  cut_ms?: number | null;
  cut_frame?: number | null;
  ready?: boolean;
}

export interface LibraryItem {
  id: string;
  kind: "clip" | "stinger";
  name: string;
  tags: string[];
  status: string;
  ready: boolean;
  playback: string;
  has_alpha: boolean;
  cut_frame?: number | null;
  cut_ms?: number | null;
  frame_count: number;
  duration_s: number;
  thumb_url?: string | null;
  error?: string | null;
  source: string;
  legacy_path?: string | null;
  in_use: boolean;
}

export interface ConvertJob {
  id: string;
  item_id: string;
  format_id: string;
  state: string;
  progress: number;
  error?: string | null;
}

export interface StingerInfo {
  id: string;
  path: string;
  kind?: "sequence" | "video";
  media_path?: string;
  frame_count: number;
  cut_frame: number;
  cut_ms?: number;
  duration_ms?: number;
  fps?: number;
}

export interface WorkspaceConfig {
  format_id: string;
  logical_source_count: number;
  mixer_panel_count: number;
  stinger_mode: string;
  stinger_count: number;
  downstream_keyer_count: number;
  source_tile_aspect?: "16:9" | "9:16";
}

export interface MixerStatus {
  state: string;
  backend: string;
  program_input_id: string | null;
  preview_input_id: string | null;
  program_bus: string;
  raster: string;
  frame_rate: string;
  video_format: string;
  stinger: { phase: string; id?: string | null };
  webrtc_enabled: boolean;
  wipe_armed?: boolean;
  last_transition?: string;
  error?: string | null;
  nmos?: NmosStatus;
}

export interface NmosReceiverStatus {
  input_id: string;
  role: string;
  receiver_id: string;
  state: string;
  master_enable: boolean;
  sender_id?: string | null;
  mxl_domain_id?: string | null;
  mxl_flow_id?: string | null;
}

export interface NmosStatus {
  enabled: boolean;
  registry_url: string;
  registry_up: boolean;
  node_id: string;
  device_id: string;
  href: string;
  host_ip: string;
  port: number;
  dns_sd: boolean;
  receivers: NmosReceiverStatus[];
}

export interface ResourceInfo {
  cpu_percent: number;
  cpu_count: number;
  memory_bytes: number;
  memory_limit_bytes: number;
  memory_percent: number;
  status: string;
  issues: { level: string; message: string }[];
  load?: { m1: number; m5: number; m15: number };
  uptime_s?: number;
  pid?: number;
}

export interface TallyReceiver {
  id: string;
  kind: "companion" | "vsm" | "bfe" | "hi" | "custom";
  label: string;
  host: string;
  port: number;
  transport: "udp" | "tcp";
  enabled: boolean;
  screen: number;
  index_offset: number;
  dle_stx?: boolean | null;
  last_error?: string | null;
  last_sent_at?: number | null;
}

export interface TallyPreset {
  kind: TallyReceiver["kind"];
  label: string;
  port: number;
  transport: "udp" | "tcp";
  hint: string;
}

export interface TallyConfig {
  protocol: string;
  receivers: TallyReceiver[];
  presets: TallyPreset[];
}

export interface ConsoleState {
  workspace: WorkspaceConfig;
  formats: { id: string; label: string; width: number; height: number; frame_rate: string }[];
  inputs: LogicalInput[];
  panels: MixerPanel[];
  keyers: DownstreamKeyer[];
  stinger_slots: StingerSlot[];
  mixer: MixerStatus;
  resources: ResourceInfo;
  webrtc: { enabled: boolean; protocol: string };
  clips: { name: string; path: string; library_item_id?: string | null; ready?: boolean }[];
  stingers: StingerInfo[];
  library?: LibraryItem[];
  jobs?: ConvertJob[];
  tally?: TallyConfig;
  nmos?: NmosStatus;
}

const jsonHeaders = { "Content-Type": "application/json" };

async function parse<T>(response: Response): Promise<T> {
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(typeof body.detail === "string" ? body.detail : response.statusText);
  }
  return body as T;
}

export const api = {
  console: () => fetch("/api/v1/console").then((r) => parse<ConsoleState>(r)),
  start: () =>
    fetch("/api/v1/mixer/start", { method: "POST", headers: jsonHeaders, body: "{}" }).then((r) =>
      parse(r),
    ),
  stop: () => fetch("/api/v1/mixer/stop", { method: "POST" }).then((r) => parse(r)),
  preview: (input_id: string, panel_id: string) =>
    fetch("/api/v1/mixer/preview", {
      method: "POST",
      headers: jsonHeaders,
      body: JSON.stringify({ input_id, panel_id }),
    }).then((r) => parse(r)),
  take: (input_id: string, panel_id: string) =>
    fetch("/api/v1/mixer/take", {
      method: "POST",
      headers: jsonHeaders,
      body: JSON.stringify({ input_id, panel_id, transition: "cut" }),
    }).then((r) => parse(r)),
  cut: (panel_id: string) =>
    fetch("/api/v1/mixer/cut", {
      method: "POST",
      headers: jsonHeaders,
      body: JSON.stringify({ panel_id }),
    }).then((r) => parse(r)),
  fade: (panel_id: string) =>
    fetch("/api/v1/mixer/fade", {
      method: "POST",
      headers: jsonHeaders,
      body: JSON.stringify({ panel_id }),
    }).then((r) => parse(r)),
  fadeToBlack: (panel_id: string) =>
    fetch("/api/v1/mixer/fade-to-black", {
      method: "POST",
      headers: jsonHeaders,
      body: JSON.stringify({ panel_id }),
    }).then((r) => parse(r)),
  wipe: (panel_id: string) =>
    fetch("/api/v1/mixer/wipe", {
      method: "POST",
      headers: jsonHeaders,
      body: JSON.stringify({ panel_id }),
    }).then((r) => parse(r)),
  workspace: (payload: Partial<WorkspaceConfig>) =>
    fetch("/api/v1/workspace", {
      method: "PUT",
      headers: jsonHeaders,
      body: JSON.stringify(payload),
    }).then((r) => parse<WorkspaceConfig>(r)),
  patchInput: (id: string, payload: Record<string, unknown>) =>
    fetch(`/api/v1/inputs/${id}`, {
      method: "PATCH",
      headers: jsonHeaders,
      body: JSON.stringify(payload),
    }).then((r) => parse<LogicalInput>(r)),
  patchKeyer: (id: string, payload: Record<string, unknown>) =>
    fetch(`/api/v1/keyers/${id}`, {
      method: "PATCH",
      headers: jsonHeaders,
      body: JSON.stringify(payload),
    }).then((r) => parse<DownstreamKeyer>(r)),
  patchStingerSlot: (id: string, payload: Record<string, unknown>) =>
    fetch(`/api/v1/stinger-slots/${id}`, {
      method: "PATCH",
      headers: jsonHeaders,
      body: JSON.stringify(payload),
    }).then((r) => parse<StingerSlot>(r)),
  replayLoad: (file_path: string, input_id: string) =>
    fetch("/api/v1/replay/load", {
      method: "POST",
      headers: jsonHeaders,
      body: JSON.stringify({ file_path, input_id }),
    }).then((r) => parse(r)),
  stingerPlay: (
    stinger_id: string,
    target_input_id: string,
    direction: string,
    extra?: { flip_flop?: boolean; panel_id?: string },
  ) =>
    fetch("/api/v1/stinger/play", {
      method: "POST",
      headers: jsonHeaders,
      body: JSON.stringify({ stinger_id, target_input_id, direction, ...extra }),
    }).then((r) => parse(r)),
  tally: () => fetch("/api/v1/tally").then((r) => parse<TallyConfig>(r)),
  tallyReceivers: (receivers: TallyReceiver[]) =>
    fetch("/api/v1/tally/receivers", {
      method: "PUT",
      headers: jsonHeaders,
      body: JSON.stringify({ receivers }),
    }).then((r) => parse<TallyConfig>(r)),
  tallyRefresh: () =>
    fetch("/api/v1/tally/refresh", { method: "POST" }).then((r) => parse<TallyConfig>(r)),
  library: (kind?: "clip" | "stinger", q = "") => {
    const params = new URLSearchParams();
    if (kind) params.set("kind", kind);
    if (q) params.set("q", q);
    const qs = params.toString();
    return fetch(`/api/v1/library${qs ? `?${qs}` : ""}`).then((r) => parse<LibraryItem[]>(r));
  },
  libraryDelete: (id: string) =>
    fetch(`/api/v1/library/${id}`, { method: "DELETE" }).then(async (r) => {
      if (!r.ok) {
        const body = await r.json().catch(() => ({}));
        throw new Error(typeof body.detail === "string" ? body.detail : r.statusText);
      }
    }),
  libraryReconvert: (id: string) =>
    fetch(`/api/v1/library/${id}/reconvert`, { method: "POST", headers: jsonHeaders, body: "{}" }).then((r) =>
      parse<ConvertJob>(r),
    ),
  jobs: () => fetch("/api/v1/jobs").then((r) => parse<ConvertJob[]>(r)),
  cancelJob: (id: string) =>
    fetch(`/api/v1/jobs/${id}/cancel`, { method: "POST" }).then((r) => parse<ConvertJob>(r)),
  uploadInit: (payload: {
    name: string;
    size: number;
    kind: "clip" | "stinger";
    mode: "video" | "image_sequence" | "zip";
    options?: Record<string, unknown>;
  }) =>
    fetch("/api/v1/uploads", {
      method: "POST",
      headers: jsonHeaders,
      body: JSON.stringify(payload),
    }).then((r) => parse<{ id: string; chunk_size: number; received: number[] }>(r)),
  uploadChunk: async (uploadId: string, index: number, chunk: Blob) => {
    const response = await fetch(`/api/v1/uploads/${uploadId}/chunks/${index}`, {
      method: "PUT",
      body: chunk,
    });
    return parse<{ id: string; chunk_size: number; received: number[] }>(response);
  },
  uploadComplete: (uploadId: string) =>
    fetch(`/api/v1/uploads/${uploadId}/complete`, { method: "POST" }).then((r) => parse<LibraryItem>(r)),
  uploadSequence: async (
    name: string,
    files: File[],
    options?: { sequence_fps?: number; cut_frame?: number; fit?: string },
  ) => {
    const body = new FormData();
    body.set("name", name);
    if (options?.sequence_fps != null) body.set("sequence_fps", String(options.sequence_fps));
    if (options?.cut_frame != null) body.set("cut_frame", String(options.cut_frame));
    if (options?.fit) body.set("fit", options.fit);
    for (const file of files) body.append("files", file, file.name);
    const response = await fetch("/api/v1/uploads/sequence", { method: "POST", body });
    return parse<LibraryItem>(response);
  },
};

export async function uploadFileChunked(
  file: File,
  kind: "clip" | "stinger",
  mode: "video" | "zip",
  options: Record<string, unknown> = {},
  onProgress?: (ratio: number) => void,
): Promise<LibraryItem> {
  const session = await api.uploadInit({
    name: file.name,
    size: file.size,
    kind,
    mode,
    options,
  });
  const chunkSize = session.chunk_size;
  const total = Math.max(1, Math.ceil(file.size / chunkSize));
  for (let index = 0; index < total; index += 1) {
    const start = index * chunkSize;
    const end = Math.min(file.size, start + chunkSize);
    await api.uploadChunk(session.id, index, file.slice(start, end));
    onProgress?.((index + 1) / total);
  }
  return api.uploadComplete(session.id);
}
