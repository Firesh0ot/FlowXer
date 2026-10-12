// The GUI's wipe patterns (src/wipe.ts) against the engine's CPU reference values
// (engine/tests/wipe-vectors.json). Run: node --experimental-strip-types scripts/check-wipe.ts
import { readFileSync } from "node:fs";
import { wipePattern } from "../src/wipe.ts";

const doc = JSON.parse(readFileSync(new URL("../../engine/tests/wipe-vectors.json", import.meta.url), "utf8"));
let worst = 0;
for (const [code, x, y, s] of doc.vectors as number[][]) {
  const got = wipePattern(code, (x + 0.5) / doc.width, (y + 0.5) / doc.height, doc.width / doc.height);
  worst = Math.max(worst, Math.abs(got - s));
}
console.log(`wipe patterns: ${doc.vectors.length} vectors, max error ${worst.toExponential(2)}`);
if (worst > 1e-4) process.exit(1);
