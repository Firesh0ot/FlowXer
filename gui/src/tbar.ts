// The T-bar as a real lever (ping-pong), the logic the console and the tbar widget share; checked by
// scripts/check-tbar.ts. The mixer keeps where the lever rests (`end`: top or bottom, flipped by each
// transition the lever, an Auto or a Cut of a held one completes) and takes the lever where it
// physically is (POST /mixer/position {lever}: 0 top .. 1 bottom), so several consoles, the widget
// and a hardware panel agree. A drag:
// - takes its resting end when it starts (the mixer's answers do not fight the drag);
// - starts a transition only past DEADBAND from that end;
// - completes it at the other end (the last few % snap there) and is then latched until the
//   pointer is released: wiggling at the end of travel starts nothing (no flicker);
// - back to its resting end ends the transition without the swap;
// - with a stinger or BKGD off (an Auto from the lever, no scrubbing) it is latched after its first
//   send: one drag, one Auto;
// - when the mixer's resting end changes under it (another console's Auto or Cut) it is latched
//   (endChanged), and each send says the end it began at (`from`), which the mixer checks too.

export interface TbarState {
  /** Where the lever rests. */
  end: "top" | "bottom";
  /** The progress of a transition it holds (0: none). */
  position: number;
  /** Where it is: 0 top .. 1 bottom. */
  lever: number;
}

export const DEADBAND = 0.02;
/** Within this much of an end the lever is at the end. */
export const SNAP = 0.01;

export interface Drag {
  /** The end the lever rested at when the drag began. */
  rest: TbarState["end"];
  /** This drag holds a transition (sent a position off its resting end). */
  active: boolean;
  /** It completed a transition: nothing more until the pointer is released. */
  latched: boolean;
  /** The last lever value sent. */
  last: number | null;
  /** A stinger or BKGD off: the first send fires an Auto, then the drag is latched. */
  oneShot: boolean;
  /** It reached the other end (the handle stays there). */
  completed: boolean;
}

export function startDrag(state: TbarState, oneShot = false): Drag {
  return { rest: state.end, active: state.position > 0, latched: false, last: null, oneShot, completed: false };
}

/** The mixer's state while a drag runs: a resting end that changed under it (another console's
 * Auto or Cut) latches the drag, so it cannot take the shot back. */
export function endChanged(drag: Drag, state: TbarState): Drag {
  return !drag.latched && state.end !== drag.rest && state.position === 0 ? { ...drag, latched: true } : drag;
}

const restLever = (end: TbarState["end"]) => (end === "top" ? 0 : 1);

/** One pointer position of a drag (0 top .. 1 bottom): what to send (null: nothing), where to draw
 * the handle and the drag after it. */
export function dragStep(drag: Drag, pointer: number): { send: number | null; shown: number; drag: Drag } {
  const rest = restLever(drag.rest);
  const far = 1 - rest;
  let v = Math.min(1, Math.max(0, pointer));
  if (v <= SNAP) v = 0;
  if (v >= 1 - SNAP) v = 1;
  v = Math.round(v * 1000) / 1000;
  if (drag.latched) return { send: null, shown: far, drag };
  const t = Math.abs(v - rest);
  // A value already sent is not sent again.
  const sendIf = (value: number, next: Drag) => ({ send: drag.last === value ? null : value, shown: value, drag: { ...next, last: value } });
  if (v === far) return sendIf(far, { ...drag, active: false, latched: true, completed: true });
  // (The deadband's edge counts, as in the mixer.)
  if (t <= DEADBAND + 1e-9) {
    // Back at (or not yet off) the resting end: a held transition ends without the swap.
    if (drag.active) return sendIf(rest, { ...drag, active: false });
    return { send: null, shown: v, drag };
  }
  return sendIf(v, { ...drag, active: true, latched: drag.oneShot });
}

/** Sends one value at a time, in order: a value given while one is in flight waits, and a newer
 * one replaces it (newest wins), so a completion is never overtaken by an older position. */
export function createSender<T>(send: (value: T) => Promise<unknown>): (value: T) => void {
  let inFlight = false;
  let waiting: T | null = null;
  const next = (value: T) => {
    if (inFlight) {
      waiting = value;
      return;
    }
    inFlight = true;
    void send(value)
      .catch(() => undefined)
      .finally(() => {
        inFlight = false;
        const queued = waiting;
        waiting = null;
        if (queued !== null) next(queued);
      });
  };
  return next;
}

/** Seconds an Auto moves the lever from `from` to `to` (both 0..1) at the ME's rate. */
export function travelSeconds(from: number, to: number, rateFrames: number, fps: number): number {
  return (Math.abs(to - from) * rateFrames) / Math.max(1, fps);
}
