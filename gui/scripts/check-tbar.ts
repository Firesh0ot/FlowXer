// The T-bar lever (src/tbar.ts): a drag that completes a transition and stays at the end, the
// reverse drag, no flicker at the end of travel, the deadband, back to the start without the swap.
// Run: node --experimental-strip-types scripts/check-tbar.ts
import { createSender, dragStep, endChanged, startDrag, travelSeconds, type Drag, type TbarState } from "../src/tbar.ts";

let failed = 0;
const check = (ok: boolean, what: string) => {
  if (!ok) {
    failed += 1;
    console.error(`FAILED ${what}`);
  }
};

/** A drag through `pointer` positions: what it sent and where it drew the handle last. */
function drag(state: TbarState, pointer: number[]): { sent: number[]; shown: number; drag: Drag } {
  let d = startDrag(state);
  const sent: number[] = [];
  let shown = state.lever;
  for (const p of pointer) {
    const step = dragStep(d, p);
    d = step.drag;
    shown = step.shown;
    if (step.send !== null) sent.push(step.send);
  }
  return { sent, shown, drag: d };
}

const top: TbarState = { end: "top", position: 0, lever: 0 };
const bottom: TbarState = { end: "bottom", position: 0, lever: 1 };

// A drag down completes at the bottom (the last 1 % snaps), then nothing until release: the handle
// stays at the bottom while the pointer wiggles there (the old console jumped back to the top).
const down = drag(top, [0.0, 0.01, 0.3, 0.6, 0.995, 0.97, 1.0, 0.98]);
check(JSON.stringify(down.sent) === JSON.stringify([0.3, 0.6, 1]), `drag down sends ${JSON.stringify(down.sent)}`);
check(down.shown === 1 && down.drag.latched, "the handle stays at the bottom, latched");

// The reverse drag from the bottom: up to the top completes the next transition.
const up = drag(bottom, [1.0, 0.99, 0.985, 0.7, 0.2, 0.004]);
check(JSON.stringify(up.sent) === JSON.stringify([0.7, 0.2, 0]), `drag up sends ${JSON.stringify(up.sent)}`);
check(up.shown === 0 && up.drag.latched, "the handle stays at the top");

// Within the deadband of the resting end nothing starts (no flicker at the end of travel).
check(drag(bottom, [0.995, 0.985, 0.99]).sent.length === 0, "deadband at the bottom");
check(drag(top, [0.005, 0.015, 0.0]).sent.length === 0, "deadband at the top");

// Back to the resting end ends the held transition without the swap (once).
const back = drag(top, [0.4, 0.1, 0.0, 0.005, 0.0]);
check(JSON.stringify(back.sent) === JSON.stringify([0.4, 0.1, 0]) && !back.drag.latched, `back to the top: ${JSON.stringify(back.sent)}`);

// Picking up a transition another console holds: the drag continues it (active from the start).
const held = drag({ end: "top", position: 0.5, lever: 0.5 }, [0.5, 0.01]);
check(JSON.stringify(held.sent) === JSON.stringify([0.5, 0]), `held elsewhere: ${JSON.stringify(held.sent)}`);

// The same value is not sent twice; an Auto's lever travel takes the ME's rate.
check(drag(top, [0.3, 0.3, 0.3]).sent.length === 1, "no repeats");
check(travelSeconds(0, 1, 25, 50) === 0.5 && travelSeconds(0.6, 1, 25, 50) === 0.2, "travel at the rate");

// A stinger or BKGD off: the first send fires the Auto, then the drag is latched (one Auto per drag).
{
  let d = startDrag(top, true);
  const sent: number[] = [];
  for (const p of [0.01, 0.05, 0.3, 0.6, 1.0]) {
    const step = dragStep(d, p);
    d = step.drag;
    if (step.send !== null) sent.push(step.send);
  }
  check(JSON.stringify(sent) === JSON.stringify([0.05]) && d.latched, `one shot: ${JSON.stringify(sent)}`);
}

// Another console's Auto flips the resting end under a drag: the drag is latched (no take back).
{
  let d = startDrag(top);
  d = dragStep(d, 0.4).drag;
  d = endChanged(d, { end: "bottom", position: 0, lever: 1 });
  check(d.latched && dragStep(d, 0.5).send === null, "latched after the end changed under the drag");
  // Its own held transition (end unchanged) does not latch it.
  check(!endChanged(startDrag(top), { end: "top", position: 0.4, lever: 0.4 }).latched, "own drag not latched");
}

// The send queue: one request at a time, a value waiting while one is in flight is replaced by a
// newer one (newest wins), the order is kept, and a failed request does not stop it.
{
  const calls: number[] = [];
  const pending: (() => void)[] = [];
  const send = createSender((value) => {
    calls.push(value);
    return new Promise<void>((resolve, reject) => pending.push(value === 0.2 ? () => reject(new Error("x")) : resolve));
  });
  send(0.2);
  send(0.4);
  send(0.6);
  send(1);
  check(JSON.stringify(calls) === JSON.stringify([0.2]), "one at a time");
  pending.shift()!();
  await new Promise((r) => setTimeout(r, 0));
  check(JSON.stringify(calls) === JSON.stringify([0.2, 1]), `newest wins after a failure: ${JSON.stringify(calls)}`);
  pending.shift()!();
  await new Promise((r) => setTimeout(r, 0));
  send(0.5);
  check(JSON.stringify(calls) === JSON.stringify([0.2, 1, 0.5]), "idle again: sent at once");
}

// A queued value keeps the end its own drag began at (not the end of the time it is sent).
{
  const calls: string[] = [];
  const pending: (() => void)[] = [];
  const send = createSender((value: { lever: number; from: string }) => {
    calls.push(`${value.lever}@${value.from}`);
    return new Promise<void>((resolve) => pending.push(resolve));
  });
  send({ lever: 0.5, from: "top" });
  send({ lever: 1, from: "top" });
  pending.shift()!();
  await new Promise((r) => setTimeout(r, 0));
  check(JSON.stringify(calls) === JSON.stringify(["0.5@top", "1@top"]), `from per value: ${JSON.stringify(calls)}`);
}

console.log(`t-bar: ${failed ? `${failed} failed` : "ok"}`);
if (failed) process.exit(1);
