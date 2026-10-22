// The GUI's box geometry (src/dme.ts) against the engine's makeDmeBox (engine/tests/dme-vectors.json),
// and the corner handle: dragged to where the corner is, the box stays. Run:
// node --experimental-strip-types scripts/check-dme.ts
import { readFileSync } from "node:fs";
import { boxRect, boxResized, type Corner } from "../src/dme.ts";

const doc = JSON.parse(readFileSync(new URL("../../engine/tests/dme-vectors.json", import.meta.url), "utf8"));
let worst = 0;
let handle = 0;
for (const [x, y, size, left, right, top, bottom, L, R, T, B] of doc.vectors as number[][]) {
  // The vectors' crops are the engine's fractions; the GUI's are source pixels.
  const box = { x, y, size, crop: { left: left * doc.width, right: right * doc.width, top: top * doc.height, bottom: bottom * doc.height } };
  const r = boxRect(box, doc.width, doc.height);
  worst = Math.max(worst, Math.abs(r.left - L), Math.abs(r.right - R), Math.abs(r.top - T), Math.abs(r.bottom - B));
  if (size < 0.02) continue;
  for (const corner of ["tl", "tr", "bl", "br"] as Corner[]) {
    const px = corner.endsWith("l") ? r.left : r.right;
    const py = corner.startsWith("t") ? r.top : r.bottom;
    const moved = boxResized(box, corner, px, py, doc.width, doc.height);
    handle = Math.max(handle, Math.abs(moved.x - x) * doc.width, Math.abs(moved.y - y) * doc.height, Math.abs(moved.size - size) * doc.width);
  }
}
console.log(`box geometry: ${doc.vectors.length} vectors, max error ${worst.toExponential(2)} px; corner handles in place: ${handle.toExponential(2)} px`);
if (worst > 1e-3 || handle > 1e-3) process.exit(1);
