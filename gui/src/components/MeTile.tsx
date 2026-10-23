import type { MouseEvent } from "react";
import type { MixerPanel } from "../api";
import { Monitor } from "./Monitor";

/** A higher ME as a source of the active ME (re-entry), next to the source tiles: its Program
 * picture, left half to Preview, right half to Program (the bus widget's ME buttons). */
export function MeTile({
  me,
  panel,
  onPreview,
  onProgram,
  onGrab,
  onAir,
}: {
  /** The higher ME (the source). */
  me: MixerPanel;
  /** The active ME (the bus). */
  panel: MixerPanel;
  onPreview: () => void;
  onProgram: () => void;
  onGrab?: (event: MouseEvent) => void;
  /** Visible on the active ME's Program through any path (a key, a pip, a nested ME). */
  onAir?: boolean;
}) {
  const isPgm = onAir ?? panel.program_input_id === me.id;
  const isPvw = panel.preview_input_id === me.id;
  return (
    <article className={`source-tile me-tile ${isPgm ? "is-pgm" : ""} ${isPvw && !isPgm ? "is-pvw" : ""}`}>
      <header>
        <strong>{me.label}</strong>
        <span className="kind">ME PGM</span>
      </header>
      <div className="source-picture" onContextMenu={onGrab}>
        <Monitor tileId={`panel:${me.id}:pgm`} label="" tally={isPgm ? "pgm" : isPvw ? "pvw" : "off"} />
        <button className="hit left" onClick={onPreview} title={`${me.label} to Preview`}>
          PVW
        </button>
        <button className="hit right" onClick={onProgram} title={`Cut ${me.label} to Program`}>
          PGM
        </button>
      </div>
    </article>
  );
}
