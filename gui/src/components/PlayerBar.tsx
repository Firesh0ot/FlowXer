import type { LibraryItem, LogicalInput, SourceRuntime } from "../api";

/** mm:ss.ff of a frame count. */
function timecode(frames: number, fps: number): string {
  const whole = Math.max(0, Math.round(frames));
  const perSecond = Math.max(1, Math.round(fps));
  const seconds = Math.floor(whole / perSecond);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${pad(Math.floor(seconds / 60))}:${pad(seconds % 60)}.${pad(whole % perSecond)}`;
}

/** A clip player's controls under its picture: the library item it plays, the transport, loop and
 * play on cut, and the position. A still is a frame memory: no transport. */
export function PlayerBar({
  input,
  runtime,
  items,
  fps,
  onLoad,
  onTransport,
  onToggle,
}: {
  input: LogicalInput;
  runtime?: SourceRuntime;
  /** Ready clips and stills of the library. */
  items: LibraryItem[];
  fps: number;
  onLoad: (libraryItemId: string | null) => void;
  onTransport: (action: "cue" | "play" | "pause" | "stop") => void;
  onToggle: (field: "loop" | "play_on_cut", value: boolean) => void;
}) {
  const item = items.find((entry) => entry.id === input.library_item_id);
  const isStill = item?.kind === "still" || runtime?.kind === "still";
  const frame = runtime?.frame ?? 0;
  const frames = runtime?.frames ?? item?.frame_count ?? 0;
  const state = runtime?.state ?? (input.library_item_id ? "loading" : "empty");
  return (
    <div className={`player-bar state-${state}`}>
      <select
        value={input.library_item_id ?? ""}
        title="Clip or still from the library"
        onChange={(e) => onLoad(e.target.value || null)}
      >
        <option value="">Empty (black)</option>
        {input.library_item_id && !item ? <option value={input.library_item_id}>Converting…</option> : null}
        {items.map((entry) => (
          <option key={entry.id} value={entry.id}>
            {entry.kind === "still" ? "▣ " : "▶ "}
            {entry.name}
          </option>
        ))}
      </select>
      {isStill ? (
        <span className="frame-memory" title="A still is held as a frame memory">
          FRAME MEM
        </span>
      ) : input.library_item_id ? (
        <>
          <div className="transport">
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
            <button
              className={input.loop ? "on" : ""}
              title="Loop: go on over the end"
              onClick={() => onToggle("loop", !input.loop)}
            >
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
          <span className="position" title={`${state}: frame ${frame} of ${frames}`}>
            {timecode(frame, fps)} <em>−{timecode(Math.max(0, frames - 1 - frame), fps)}</em>
          </span>
        </>
      ) : null}
    </div>
  );
}
