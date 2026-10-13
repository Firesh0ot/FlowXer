// The audio strip's fader law (src/fader.ts) against vectors: dB → position, position → dB, the
// round trip to 0.1 dB, the ends, and the meter scale. Run:
// node --experimental-strip-types scripts/check-fader.ts
import { dbToPosition, formatDb, meterPosition, positionToDb } from "../src/fader.ts";

const vectors: [number, number][] = [
  [-120, 0],
  [-90, 0],
  [-75, 0.025],
  [-60, 0.05],
  [-50, 0.1],
  [-40, 0.15],
  [-30, 0.25],
  [-20, 0.4],
  [-15, 0.475],
  [-10, 0.55],
  [-6, 0.63],
  [-3, 0.69],
  [0, 0.75],
  [5, 0.875],
  [10, 1],
  [20, 1],
];
let failures = 0;
const near = (a: number, b: number, tolerance: number) => Math.abs(a - b) <= tolerance;
for (const [db, position] of vectors) {
  if (!near(dbToPosition(db), position, 1e-9)) {
    console.log(`dbToPosition(${db}) = ${dbToPosition(db)}, expected ${position}`);
    failures++;
  }
  const back = Math.max(-90, Math.min(10, db));
  if (!near(positionToDb(position), back, 0.05)) {
    console.log(`positionToDb(${position}) = ${positionToDb(position)}, expected ${back}`);
    failures++;
  }
}
// Every 0.1 dB step survives the round trip.
for (let tenth = -900; tenth <= 100; tenth++) {
  const db = tenth / 10;
  if (positionToDb(dbToPosition(db)) !== db) {
    console.log(`round trip ${db} -> ${positionToDb(dbToPosition(db))}`);
    failures++;
    break;
  }
}
for (const [db, text] of [[-90, "−∞"], [-6.02, "−6.0"], [0, "0.0"], [4.5, "+4.5"]] as [number, string][]) {
  if (formatDb(db) !== text) {
    console.log(`formatDb(${db}) = ${formatDb(db)}, expected ${text}`);
    failures++;
  }
}
for (const [db, height] of [[-120, 0], [-60, 0], [-40, 0.2], [-20, 0.5], [-10, 0.7], [0, 1], [3, 1]] as [number, number][]) {
  if (!near(meterPosition(db), height, 1e-9)) {
    console.log(`meterPosition(${db}) = ${meterPosition(db)}, expected ${height}`);
    failures++;
  }
}
console.log(`fader law: ${vectors.length} vectors, 1001 round trips, ${failures} failures`);
if (failures > 0) process.exit(1);
