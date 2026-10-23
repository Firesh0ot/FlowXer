// The T-bar as a real lever (ping-pong), the logic the console and the tbar widget share; checked by
// scripts/check-tbar.ts. The mixer keeps where the lever rests (`end`: top or bottom, flipped by each
// transition the lever, an Auto or a Cut of a held one completes) and takes the lever where it
// physically is (POST /mixer/position {lever}: 0 top .. 1 bottom), so several consoles, the widget
// and a hardware panel agree. A drag:
// - takes its resting end when it starts (the mixer's answers do not fight the drag);
// - starts a transition only past DEADBAND from that end;
// - completes it at the other end (the last few % snap there) and is then latched until the
//   pointer is released: wiggling at the end of travel starts nothing (no flicker);
// - back to its resting end ends the transition without the swap.

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
}

export function startDrag(state: TbarState): Drag {
  return { rest: state.end, active: state.position > 0, latched: false, last: null };
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
  if (v === far) return sendIf(far, { ...drag, active: false, latched: true });
  if (t < DEADBAND) {
    // Back at (or not yet off) the resting end: a held transition ends without the swap.
    if (drag.active) return sendIf(rest, { ...drag, active: false });
    return { send: null, shown: v, drag };
  }
  return sendIf(v, { ...drag, active: true });
}

/** Seconds an Auto moves the lever from `from` to `to` (both 0..1) at the ME's rate. */
export function travelSeconds(from: number, to: number, rateFrames: number, fps: number): number {
  return (Math.abs(to - from) * rateFrames) / Math.max(1, fps);
}
