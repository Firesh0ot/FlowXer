// The wipe patterns of the engine (engine/src/core/wipe.hpp) for the picker's icons: s(u, v) in
// [0, 1] is the moment the edge reaches a point (0 first, 1 last). engine/tests/wipe-vectors.json
// holds values of the C++ reference (checked by the engine's unit tests); scripts/check-wipe.ts
// checks this port against them.

export const WIPE_PATTERNS: { code: number; label: string }[] = [
  { code: 1, label: "Bar, left to right" },
  { code: 2, label: "Bar, top to bottom" },
  { code: 3, label: "Box from the top-left corner" },
  { code: 4, label: "Box from the top-right corner" },
  { code: 5, label: "Box from the bottom-right corner" },
  { code: 6, label: "Box from the bottom-left corner" },
  { code: 21, label: "Barn door, vertical edges" },
  { code: 22, label: "Barn door, horizontal edges" },
  { code: 23, label: "Box from the top centre" },
  { code: 24, label: "Box from the right centre" },
  { code: 25, label: "Box from the bottom centre" },
  { code: 26, label: "Box from the left centre" },
  { code: 41, label: "Diagonal from the top-left corner" },
  { code: 42, label: "Diagonal from the top-right corner" },
  { code: 45, label: "Diagonal barn door (bottom-left to top-right)" },
  { code: 46, label: "Diagonal barn door (top-left to bottom-right)" },
  { code: 101, label: "Box from the centre" },
  { code: 102, label: "Diamond" },
  { code: 119, label: "Circle (iris)" },
  { code: 201, label: "Clock from 12 o'clock" },
  { code: 202, label: "Clock from 3 o'clock" },
  { code: 203, label: "Clock from 6 o'clock" },
  { code: 204, label: "Clock from 9 o'clock" },
];

/** Clockwise angle from 12 o'clock in turns [0, 1); dx to the right, dy down. */
function clockTurns(dx: number, dy: number): number {
  if (dx === 0 && dy === 0) return 0;
  const turns = Math.atan2(dx, -dy) / (2 * Math.PI);
  return turns < 0 ? turns + 1 : turns;
}

/** s(u, v) of a wipe code on a picture of the given aspect ratio (width / height). */
export function wipePattern(code: number, u: number, v: number, aspect = 16 / 9, reverse = false): number {
  const bu = Math.abs(2 * u - 1);
  const bv = Math.abs(2 * v - 1);
  let s = u;
  switch (code) {
    case 2: s = v; break;
    case 3: s = Math.max(u, v); break;
    case 4: s = Math.max(1 - u, v); break;
    case 5: s = Math.max(1 - u, 1 - v); break;
    case 6: s = Math.max(u, 1 - v); break;
    case 21: s = bu; break;
    case 22: s = bv; break;
    case 23: s = Math.max(bu, v); break;
    case 24: s = Math.max(1 - u, bv); break;
    case 25: s = Math.max(bu, 1 - v); break;
    case 26: s = Math.max(u, bv); break;
    case 41: s = 0.5 * (u + v); break;
    case 42: s = 0.5 * (1 - u + v); break;
    case 45: s = Math.abs(u + v - 1); break;
    case 46: s = Math.abs(u - v); break;
    case 101: s = Math.max(bu, bv); break;
    case 102: s = 0.5 * (bu + bv); break;
    case 119:
      s = Math.hypot((u - 0.5) * aspect, v - 0.5) / (0.5 * Math.hypot(aspect, 1));
      break;
    case 201: case 202: case 203: case 204: {
      const f = clockTurns((u - 0.5) * aspect, v - 0.5) - 0.25 * (code - 201);
      s = f < 0 ? f + 1 : f;
      break;
    }
  }
  return reverse ? 1 - s : s;
}
