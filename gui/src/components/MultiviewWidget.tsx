import { useEffect } from "react";
import { wallGrid, wallTiles } from "../multiview";
import { previewSession } from "../preview";
import { Monitor } from "./Monitor";
import { useWidget } from "./useWidget";

/** The `multiview` widget: a control-room wall of the mixer's pictures over WebRTC (the preview
 * mosaic's tiles and each ME's Program stream) with labels and tally borders; the layout follows
 * the mixer's sources and MEs. */
export function MultiviewWidget({ me = 1, pvw = false }: { me?: number; pvw?: boolean }) {
  const { snapshot, error, root } = useWidget();

  // The widget's sessions follow the mixer's preview map (the mosaic and the Program streams).
  useEffect(() => {
    if (snapshot) previewSession.setMap(snapshot.preview);
  }, [snapshot]);

  const tiles = snapshot ? wallTiles(snapshot, me, pvw) : [];
  const { cols, rows } = wallGrid(tiles.length, window.innerWidth, window.innerHeight);

  return (
    <div ref={root} className="multiview-widget">
      {!snapshot ? (
        <div className="empty">{error ?? "Connecting to the vision mixer…"}</div>
      ) : (
        <div
          className="multiview-grid"
          style={{ gridTemplateColumns: `repeat(${cols}, minmax(0, 1fr))`, gridTemplateRows: `repeat(${rows}, minmax(0, 1fr))` }}
        >
          {tiles.map((tile) => (
            <div key={tile.id} className="multiview-cell">
              <Monitor tileId={tile.id} label={tile.label} tally={tile.tally} />
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
