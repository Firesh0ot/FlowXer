import type { MouseEvent } from "react";
import { api, type LibraryItem, type LogicalInput, type MixerPanel, type SourceRuntime } from "../api";
import { Monitor } from "./Monitor";
import { PlayerBar, PlayerPosition, PlayerSelect } from "./PlayerBar";

const KIND_LABEL: Record<string, string> = { mxl_live: "live", clip_player: "clip player", black: "black" };

export function SourceTile({
  input,
  panel,
  runtime,
  library,
  fps,
  onPreview,
  onProgram,
  onSettings,
  onPlayer,
  onGrab,
}: {
  input: LogicalInput;
  panel: MixerPanel;
  runtime?: SourceRuntime;
  /** Ready clips and stills, for a clip player. */
  library: LibraryItem[];
  fps: number;
  onPreview: () => void;
  onProgram: () => void;
  onSettings: () => void;
  onPlayer: (command: () => Promise<unknown>) => void;
  /** Its picture's context menu (GRAB); none for Black. */
  onGrab?: (event: MouseEvent) => void;
}) {
  const isPgm = panel.program_input_id === input.id;
  const isPvw = panel.preview_input_id === input.id;
  const tally = isPgm ? "pgm" : isPvw ? "pvw" : "off";
  const player = input.kind === "clip_player";
  return (
    <article className={`source-tile ${isPgm ? "is-pgm" : ""} ${isPvw && !isPgm ? "is-pvw" : ""}`}>
      <header>
        <strong>{input.label}</strong>
        {player ? (
          <PlayerSelect
            input={input}
            items={library}
            onLoad={(id) => onPlayer(() => api.patchInput(input.id, { library_item_id: id }))}
          />
        ) : (
          <span className="kind">{KIND_LABEL[input.kind] ?? input.kind}</span>
        )}
        <button className="gear" title="Source settings" onClick={onSettings}>
          ⚙
        </button>
      </header>
      {/* The picture's context menu (GRAB) over the tile's PVW and PGM hit areas too. */}
      <div className="source-picture" onContextMenu={input.kind === "black" ? undefined : onGrab}>
        <Monitor
          tileId={`source:${input.id}`}
          label=""
          tally={tally}
          placeholder={input.kind === "black" ? "BLACK" : undefined}
        />
        <button className="hit left" onClick={onPreview} title="Cut to Preview">
          PVW
        </button>
        <button className="hit right" onClick={onProgram} title="Cut to Program">
          PGM
        </button>
        {player ? <PlayerPosition input={input} runtime={runtime} items={library} fps={fps} /> : null}
      </div>
      {player ? (
        <PlayerBar
          input={input}
          runtime={runtime}
          items={library}
          onTransport={(action) => onPlayer(() => api.player(input.id, action))}
          onToggle={(field, value) => onPlayer(() => api.patchInput(input.id, { [field]: value }))}
        />
      ) : null}
    </article>
  );
}
