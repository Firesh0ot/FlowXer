import type { LogicalInput, MixerPanel } from "../api";
import { Monitor } from "./Monitor";

export function SourceTile({
  input,
  panel,
  webrtc,
  onPreview,
  onProgram,
  onSettings,
}: {
  input: LogicalInput;
  panel: MixerPanel;
  webrtc: boolean;
  onPreview: () => void;
  onProgram: () => void;
  onSettings: () => void;
}) {
  const isPgm = panel.program_input_id === input.id;
  const isPvw = panel.preview_input_id === input.id;
  const tally = isPgm ? "pgm" : isPvw ? "pvw" : "off";
  return (
    <article className={`source-tile ${isPgm ? "is-pgm" : ""} ${isPvw && !isPgm ? "is-pvw" : ""}`}>
      <header>
        <strong>{input.label}</strong>
        <span className="kind">{input.kind}</span>
        <button className="gear" title="Source settings" onClick={onSettings}>
          ⚙
        </button>
      </header>
      <div className="source-picture">
        <Monitor
          streamId={`source:${input.id}`}
          webrtc={webrtc}
          label=""
          tally={tally}
        />
        <button className="hit left" onClick={onPreview} title="Cut to Preview">
          PVW
        </button>
        <button className="hit right" onClick={onProgram} title="Cut to Program">
          PGM
        </button>
      </div>
    </article>
  );
}
