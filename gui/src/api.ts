export type InputKind = "mxl_live" | "file" | "replay" | "test" | "black";

export interface LogicalInput {
  id: string;
  label: string;
  kind: InputKind;
  slot: number;
  file_path?: string | null;
  group_hint?: string | null;
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
  kind?: "sequence" | "video";
  media_path?: string | null;
  cut_ms?: number | null;
  cut_frame?: number | null;
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
}

export interface ResourceInfo {
  cpu_percent: number;
  cpu_count: number;
  memory_bytes: number;
  memory_limit_bytes: number;
  memory_percent: number;
  status: string;
  issues: { level: string; message: string }[];
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
  clips: { name: string; path: string }[];
  stingers: StingerInfo[];
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
  stingerPlay: (stinger_id: string, target_input_id: string, direction: string) =>
    fetch("/api/v1/stinger/play", {
      method: "POST",
      headers: jsonHeaders,
      body: JSON.stringify({ stinger_id, target_input_id, direction }),
    }).then((r) => parse(r)),
};
