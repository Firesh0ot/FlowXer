// The box editor's geometry (docs/engine-p4.md §7): where a keyer's box is on the raster, as the
// engine computes it (engine/src/core/dme.cpp makeDmeBox: the crop on whole 4:2:2 samples, a
// sub-pixel position), and the box after a drag or a corner handle. Checked against the engine's
// values (engine/tests/dme-vectors.json) by scripts/check-dme.ts.

/** The fields of a box the geometry needs (a Dme or a keyframe). */
export interface BoxGeometry {
  x: number;
  y: number;
  size: number;
  /** Source pixels. */
  crop: { left: number; right: number; top: number; bottom: number };
}

/** A rectangle in raster pixels. */
export interface Rect {
  left: number;
  right: number;
  top: number;
  bottom: number;
}

export type Corner = "tl" | "tr" | "bl" | "br";

/** The crop (source pixels) in source samples: columns [cl, cr) (even: left and right on 4:2:2
 * pairs, an odd value to the nearest even one), rows [ct, cb). */
function cropSamples(box: BoxGeometry, width: number, height: number) {
  const cl = Math.min(2 * Math.floor(box.crop.left * 0.5 + 0.5), width - 2);
  const cr = Math.max(width - 2 * Math.floor(box.crop.right * 0.5 + 0.5), cl + 2);
  const ct = Math.min(Math.floor(box.crop.top + 0.5), height - 1);
  const cb = Math.max(height - Math.floor(box.crop.bottom + 0.5), ct + 1);
  return { cl, cr, ct, cb };
}

/** The picture's rectangle on a width × height raster. */
export function boxRect(box: BoxGeometry, width: number, height: number): Rect {
  const { cl, cr, ct, cb } = cropSamples(box, width, height);
  const dx = box.x * width - 0.5 * width * box.size;
  const dy = box.y * height - 0.5 * height * box.size;
  return { left: dx + cl * box.size, right: dx + cr * box.size, top: dy + ct * box.size, bottom: dy + cb * box.size };
}

const clamp = (v: number, lo: number, hi: number) => Math.min(hi, Math.max(lo, v));

/** The box's centre moved by (dx, dy) raster pixels (within the engine's −1..2). */
export function boxMoved(box: BoxGeometry, dx: number, dy: number, width: number, height: number) {
  return { x: clamp(box.x + dx / width, -1, 2), y: clamp(box.y + dy / height, -1, 2) };
}

/** The box after its `corner` handle is dragged to (px, py) raster pixels: the opposite corner
 * stays, the aspect is kept (the scale whose corner is nearest the pointer along the diagonal). */
export function boxResized(box: BoxGeometry, corner: Corner, px: number, py: number, width: number, height: number) {
  const r = boxRect(box, width, height);
  const { cl, cr, ct, cb } = cropSamples(box, width, height);
  const left = corner === "tl" || corner === "bl";
  const top = corner === "tl" || corner === "tr";
  const fx = left ? r.right : r.left;
  const fy = top ? r.bottom : r.top;
  // The picture's width and height at scale 1.
  const w1 = cr - cl;
  const h1 = cb - ct;
  const size = clamp((Math.abs(px - fx) * w1 + Math.abs(py - fy) * h1) / (w1 * w1 + h1 * h1), 0.02, 2);
  const l = left ? fx - w1 * size : fx;
  const t = top ? fy - h1 * size : fy;
  return {
    x: clamp((l + width * size * 0.5 - cl * size) / width, -1, 2),
    y: clamp((t + height * size * 0.5 - ct * size) / height, -1, 2),
    size,
  };
}

/** The DME presets as buttons (GET /api/v1/dme/presets describes them). */
export const PRESET_BUTTONS: { name: "corner-tl" | "corner-tr" | "corner-bl" | "corner-br" | "split" | "quad" | "full"; label: string; title: string }[] = [
  { name: "corner-tl", label: "TL", title: "Corner PiP top left (1/3 size)" },
  { name: "corner-tr", label: "TR", title: "Corner PiP top right (1/3 size)" },
  { name: "corner-bl", label: "BL", title: "Corner PiP bottom left (1/3 size)" },
  { name: "corner-br", label: "BR", title: "Corner PiP bottom right (1/3 size)" },
  { name: "split", label: "SPLIT", title: "Split: keyers 1 and 2 side by side" },
  { name: "quad", label: "QUAD", title: "Quad: keyers 1 to 4 at 1/2 size (needs 4 keyers)" },
  { name: "full", label: "FULL", title: "Full: the box flies to and from full screen" },
];
