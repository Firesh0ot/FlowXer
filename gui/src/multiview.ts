// The multiview widget's layout (components/MultiviewWidget.tsx), checked by
// scripts/check-preview.ts: which pictures the wall shows, their tally, and the grid.
import type { ConsoleState } from "./api";

/** One picture of the wall: its preview id, label and tally on ME `me`. */
export interface WallTile {
  id: string;
  label: string;
  tally: "pgm" | "pvw" | "off";
}

/** The wall's pictures as the mixer has them: each ME's Program (its own stream), with `pvw` each
 * ME's Preview, then the live inputs and clip players (mosaic tiles; Black has none). Red: on air
 * on ME `me` through any path (the console's `on_air`, else its Program); green: on the Preview of
 * any ME (and every ME's Preview picture). Red wins. */
export function wallTiles(snapshot: ConsoleState, me: number, pvw: boolean): WallTile[] {
  const panel = snapshot.panels[me - 1] ?? snapshot.panels[0];
  const onAir = new Set(snapshot.on_air?.[panel?.id ?? ""] ?? (panel?.program_input_id ? [panel.program_input_id] : []));
  const previews = new Set(snapshot.panels.map((item) => item.preview_input_id).filter((id): id is string => !!id));
  const tally = (id: string): WallTile["tally"] => (onAir.has(id) ? "pgm" : previews.has(id) ? "pvw" : "off");
  const tiles: WallTile[] = [];
  // Only the MEs that render (FLOWXER_RENDER_MES): those the previews have a picture of.
  const map = snapshot.preview;
  const shown = (id: string) => !map?.available || map.tiles.some((t) => t.id === id) || (map.streams ?? []).some((s) => s.id === id);
  for (const item of snapshot.panels) {
    if (!shown(`panel:${item.id}:pgm`) && !shown(`panel:${item.id}:pvw`)) continue;
    // An ME's Program is on air on ME `me` when that ME shows it (or it is ME `me` itself).
    tiles.push({ id: `panel:${item.id}:pgm`, label: `${item.label} PGM`, tally: item.id === panel?.id ? "pgm" : tally(item.id) });
    if (pvw) tiles.push({ id: `panel:${item.id}:pvw`, label: `${item.label} PVW`, tally: "pvw" });
  }
  for (const input of snapshot.inputs) {
    if (input.kind === "black") continue;
    tiles.push({ id: `source:${input.id}`, label: input.label, tally: tally(input.id) });
  }
  return tiles;
}

/** Columns and rows of a 16:9 grid that shows `count` pictures as large as they fit in w × h. */
export function wallGrid(count: number, w: number, h: number): { cols: number; rows: number } {
  let best = { cols: 1, rows: Math.max(1, count), size: 0 };
  for (let cols = 1; cols <= Math.max(1, count); cols += 1) {
    const rows = Math.ceil(count / cols);
    const cell = Math.min(w / cols / 16, h / rows / 9);
    // A tie goes to more columns (fewer, wider rows).
    if (cell >= best.size) best = { cols, rows, size: cell };
  }
  return { cols: best.cols, rows: best.rows };
}

