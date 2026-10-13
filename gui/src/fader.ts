// The audio strip's fader law and meter scale (docs/engine-p5.md §8): piecewise linear in dB, with
// more travel near 0 dB, as on a desk. A fader runs from −∞ (−90 dB, off) at the bottom to +10 dB
// at the top, 0 dB at three quarters. Checked against vectors by scripts/check-fader.ts in CI.

export const FADER_MIN_DB = -90;
export const FADER_MAX_DB = 10;

/** (position 0..1, dB) from the bottom to the top. */
export const FADER_POINTS: readonly [number, number][] = [
  [0, -90],
  [0.05, -60],
  [0.15, -40],
  [0.25, -30],
  [0.4, -20],
  [0.55, -10],
  [0.75, 0],
  [1, 10],
];

/** The scale marks of a fader (dB). */
export const FADER_MARKS = [10, 5, 0, -5, -10, -20, -30, -40, -60];

function interpolate(points: readonly [number, number][], from: 0 | 1, to: 0 | 1, value: number): number {
  if (value <= points[0][from]) return points[0][to];
  for (let k = 1; k < points.length; k++) {
    const [a, b] = [points[k - 1], points[k]];
    if (value <= b[from]) {
      const u = (value - a[from]) / (b[from] - a[from]);
      return a[to] + u * (b[to] - a[to]);
    }
  }
  return points[points.length - 1][to];
}

/** A fader position (0..1) for a level in dB (−90 and below: 0). */
export function dbToPosition(db: number): number {
  return interpolate(FADER_POINTS, 1, 0, Math.min(FADER_MAX_DB, Math.max(FADER_MIN_DB, db)));
}

/** The level (dB, to 0.1 dB; −90 is off) at a fader position (0..1). */
export function positionToDb(position: number): number {
  const db = interpolate(FADER_POINTS, 0, 1, Math.min(1, Math.max(0, position)));
  return Math.round(db * 10) / 10;
}

/** A level as a fader shows it: "−∞" for off, else one decimal and a sign. */
export function formatDb(db: number): string {
  if (db <= FADER_MIN_DB) return "−∞";
  const text = Math.abs(db).toFixed(1);
  return db > 0 ? `+${text}` : db < 0 ? `−${text}` : text;
}

/** A meter's scale: −60 dBFS (and below) at the bottom, 0 dBFS at the top. */
export const METER_POINTS: readonly [number, number][] = [
  [0, -60],
  [0.2, -40],
  [0.5, -20],
  [0.7, -10],
  [1, 0],
];

/** A meter bar's height (0..1) for a level in dBFS. */
export function meterPosition(db: number): number {
  return interpolate(METER_POINTS, 1, 0, Math.min(0, db));
}
