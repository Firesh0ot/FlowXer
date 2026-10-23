// The previews' geometry (src/preview.ts) and the multiview wall (src/multiview.ts): a mosaic tile
// cropped 5 px inside its edges, a Program that is a stream of its own, the WHEP URLs, the wall's
// pictures, tally and grid. Run: node --experimental-strip-types scripts/check-preview.ts
import { insetTile, rasterView, streamView, tileCrop, whepUrl, TILE_INSET, type PreviewMap } from "../src/preview.ts";
import { wallGrid, wallTiles } from "../src/multiview.ts";
import type { ConsoleState } from "../src/api.ts";

let failed = 0;
const check = (ok: boolean, what: string) => {
  if (!ok) {
    failed += 1;
    console.error(`FAILED ${what}`);
  }
};
(globalThis as { location?: unknown }).location = { protocol: "http:", hostname: "mixer" };

const map: PreviewMap = {
  available: true,
  width: 1920,
  height: 1080,
  path: "flowxer/mosaic",
  whep_port: 8989,
  tiles: [{ id: "source:cam-1", x: 480, y: 270, w: 480, h: 270 }],
  streams: [{ id: "panel:me-1:pgm", path: "flowxer/me1-pgm", width: 960, height: 540 }],
};

// 5 px inside each edge of the tile.
check(TILE_INSET === 5, "inset is 5 px");
const inner = insetTile(map.tiles[0]);
check(inner.x === 485 && inner.y === 275 && inner.w === 470 && inner.h === 260, "tile inset");
check(insetTile({ x: 0, y: 0, w: 16, h: 16 }).w === 16, "a tiny tile stays");
const crop = tileCrop(map, "source:cam-1");
check(crop !== null && String(crop.video.transform).includes(`scale(${1920 / 470}, ${1080 / 260})`), "crop scale");
check(String(crop?.video.transform).includes(`translate(${(-100 * 485) / 1920}%, ${(-100 * 275) / 1080}%)`), "crop offset");
check((crop?.box as Record<string, string>)["--ar"] === "470 / 260", "crop aspect");
// A Program stream: the whole stream, no crop; not a mosaic tile.
check(tileCrop(map, "panel:me-1:pgm") === null, "pgm is no tile");
check((streamView(map, "panel:me-1:pgm")?.box as Record<string, string>)["--ar"] === "960 / 540", "pgm stream aspect");
check(streamView(map, "source:cam-1") === null, "a source is no stream");
// What part of the raster a picture shows (the box editor draws over it): a Program stream all of
// it; a mosaic tile 5 mosaic px less at each edge (480-px tile of 1920: 20 raster px).
check(JSON.stringify(rasterView(map, "panel:me-1:pgm", 1920, 1080)) === JSON.stringify({ left: 0, top: 0, right: 1920, bottom: 1080 }), "stream view");
check(JSON.stringify(rasterView(map, "source:cam-1", 1920, 1080)) === JSON.stringify({ left: 20, top: 20, right: 1900, bottom: 1060 }), `tile view ${JSON.stringify(rasterView(map, "source:cam-1", 1920, 1080))}`);
check(rasterView(null, "x", 1920, 1080).right === 1920, "no map: all");
// WHEP URLs: the own MediaMTX on the page's host, or the shared one's absolute URL.
check(whepUrl(map) === "http://mixer:8989/flowxer/mosaic/whep", "mosaic url");
check(whepUrl(map, map.streams![0]) === "http://mixer:8989/flowxer/me1-pgm/whep", "pgm url");
check(whepUrl(map, { ...map.streams![0], whep_url: "https://mtx/x/whep" }) === "https://mtx/x/whep", "shared pgm url");
check(whepUrl({ ...map, available: false }) === "", "no url off-air");

// The wall: each ME's Program, then the sources (no Black); red on air on ME 1 through any path.
const snapshot = {
  panels: [
    { id: "me-1", label: "ME 1", program_input_id: "cam-1", preview_input_id: "cam-2" },
    { id: "me-2", label: "ME 2", program_input_id: "cam-3", preview_input_id: null },
  ],
  inputs: [
    { id: "cam-1", label: "Cam 1", kind: "mxl_live" },
    { id: "cam-2", label: "Cam 2", kind: "mxl_live" },
    { id: "cam-3", label: "Cam 3", kind: "mxl_live" },
    { id: "clip-1", label: "Clip 1", kind: "clip_player" },
    { id: "black", label: "Black", kind: "black" },
  ],
  on_air: { "me-1": ["cam-1", "me-2", "cam-3"] },
} as unknown as ConsoleState;
const wall = wallTiles(snapshot, 1, false);
check(wall.map((t) => t.id).join() === "panel:me-1:pgm,panel:me-2:pgm,source:cam-1,source:cam-2,source:cam-3,source:clip-1", "wall order");
check(wall.map((t) => t.tally).join() === "pgm,pgm,pgm,pvw,pgm,off", "wall tally (ME 2 in a pip of ME 1)");
check(wallTiles({ ...snapshot, on_air: undefined } as ConsoleState, 1, false).map((t) => t.tally).join() === "pgm,off,pgm,pvw,off,off", "tally without on_air");
check(wallTiles(snapshot, 1, true).length === 8, "with previews");
// Green for every ME's Preview: ME 2 previews Clip 1, ME 1 previews ME 3; every PVW picture green.
const three = {
  ...snapshot,
  panels: [
    { id: "me-1", label: "ME 1", program_input_id: "cam-1", preview_input_id: "me-3" },
    { id: "me-2", label: "ME 2", program_input_id: "cam-3", preview_input_id: "clip-1" },
    { id: "me-3", label: "ME 3", program_input_id: "cam-2", preview_input_id: null },
  ],
  on_air: { "me-1": ["cam-1"] },
} as unknown as ConsoleState;
check(wallTiles(three, 1, false).map((t) => t.tally).join() === "pgm,off,pvw,pgm,off,off,pvw", "green for every ME's Preview");
check(wallTiles(three, 1, true).filter((t) => t.id.endsWith(":pvw")).every((t) => t.tally === "pvw"), "every PVW picture green");
// ME 2 does not render (FLOWXER_RENDER_MES=1): the map has no picture of it, the wall no tile.
const oneMe = { ...snapshot, preview: { ...map, tiles: [...map.tiles, { id: "panel:me-1:pvw", x: 0, y: 0, w: 480, h: 270 }] } } as unknown as ConsoleState;
check(wallTiles(oneMe, 1, true).map((t) => t.id).join() === "panel:me-1:pgm,panel:me-1:pvw,source:cam-1,source:cam-2,source:cam-3,source:clip-1", "rendering MEs only");
// The grid: as large 16:9 pictures as fit.
const g6 = wallGrid(6, 1920, 1080);
check(g6.cols === 3 && g6.rows === 2, `6 on 1920x1080: ${JSON.stringify(g6)}`);
const g6p = wallGrid(6, 1080, 1920);
check(g6p.cols === 1 && g6p.rows === 6, `6 on a portrait screen: ${JSON.stringify(g6p)}`);
check(JSON.stringify(wallGrid(1, 800, 450)) === JSON.stringify({ cols: 1, rows: 1 }), "1");

console.log(`previews and multiview: ${failed ? `${failed} failed` : "ok"}`);
if (failed) process.exit(1);
