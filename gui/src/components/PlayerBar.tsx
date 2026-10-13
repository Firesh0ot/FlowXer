import type { LibraryItem, LogicalInput, SourceRuntime } from "../api";

/** mm:ss.ff of a frame count. */
function timecode(frames: number, fps: number): string {
  const whole = Math.max(0, Math.round(frames));
  const perSecond = Math.max(1, Math.round(fps));
  const seconds = Math.floor(whole / perSecond);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${pad(Math.floor(seconds / 60))}:${pad(seconds % 60)}.${pad(whole % perSecond)}`;
}

function isStill(input: LogicalInput, items: LibraryItem[], runtime?: SourceRuntime): boolean {
  return items.find((entry) => entry.id === input.library_item_id)?.kind === "still" || runtime?.kind === "still";
}

/** The library clip or still a clip player plays (in its tile's header); empty is black. */
export function PlayerSelect({
  input,
  items,
  onLoad,
}: {
  input: LogicalInput;
  /** Ready clips and stills of the library. */
  items: LibraryItem[];
  onLoad: (libraryItemId: string | null) => void;
}) {
  const known = items.some((entry) => entry.id === input.library_item_id);
  return (
    <select
      className="player-select"
      value={input.library_item_id ?? ""}
      title="Clip or still from the library"
      onChange={(e) => onLoad(e.target.value || null)}
    >
      <option value="">Empty (black)</option>
      {input.library_item_id && !known ? <option value={input.library_item_id}>Converting…</option> : null}
      {items.map((entry) => (
        <option key={entry.id} value={entry.id}>
          {entry.kind === "still" ? "▣ " : "▶ "}
          {entry.name}
        </option>
      ))}
    </select>
  );
}

/** A clip player's position and remaining time, over its picture; FRAME MEM for a still. */
export function PlayerPosition({
  input,
  runtime,
  items,
  fps,
}: {
  input: LogicalInput;
  runtime?: SourceRuntime;
  items: LibraryItem[];
  fps: number;
}) {
  if (!input.library_item_id) return null;
  if (isStill(input, items, runtime)) {
    return (
      <div className="player-position frame-memory" title="A still is held as a frame memory">
        FRAME MEM
      </div>
    );
  }
  const frame = runtime?.frame ?? 0;
  const frames = runtime?.frames ?? 0;
  return (
    <div className="player-position" title={`${runtime?.state ?? "loading"}: frame ${frame} of ${frames}`}>
      {timecode(frame, fps)} <em>−{timecode(Math.max(0, frames - 1 - frame), fps)}</em>
    </div>
  );
}

/** A clip player's transport under its picture: cue, play, pause, stop, loop, play on cut. */
export function PlayerBar({
  input,
  runtime,
  items,
  onTransport,
  onToggle,
}: {
  input: LogicalInput;
  runtime?: SourceRuntime;
  items: LibraryItem[];
  onTransport: (action: "cue" | "play" | "pause" | "stop") => void;
  onToggle: (field: "loop" | "play_on_cut", value: boolean) => void;
}) {
  if (!input.library_item_id || isStill(input, items, runtime)) return null;
  const state = runtime?.state ?? "loading";
  return (
    <div className={`player-bar state-${state}`}>
      <button title="Cue the first frame" onClick={() => onTransport("cue")}>
        ⏮
      </button>
      <button className={state === "playing" ? "on" : ""} title="Play" onClick={() => onTransport("play")}>
        ▶
      </button>
      <button className={state === "paused" ? "on" : ""} title="Pause" onClick={() => onTransport("pause")}>
        ⏸
      </button>
      <button title="Stop: back to the first frame" onClick={() => onTransport("stop")}>
        ■
      </button>
      <button className={input.loop ? "on" : ""} title="Loop: go on over the end" onClick={() => onToggle("loop", !input.loop)}>
        LOOP
      </button>
      <button
        className={input.play_on_cut ? "on" : ""}
        title="Play on cut: start when taken to Program"
        onClick={() => onToggle("play_on_cut", !input.play_on_cut)}
      >
        AUTO
      </button>
    </div>
  );
}
