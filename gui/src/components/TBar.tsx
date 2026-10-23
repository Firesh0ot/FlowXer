import { useEffect, useRef, useState } from "react";
import { dragStep, startDrag, travelSeconds, type Drag, type TbarState } from "../tbar";

/** The T-bar lever (src/tbar.ts): top 0, bottom 1, resting at the end the mixer says. A drag
 * holds the ME's transition; the other end completes it and the lever stays there (the next
 * transition goes back). While the operator drags, the mixer's state does not move the handle;
 * otherwise the handle follows it, and runs to the other end over the ME's rate when an Auto (or
 * another console) moved it there. */
export function TBar({
  state,
  rateFrames,
  fps,
  onLever,
}: {
  state: TbarState;
  rateFrames: number;
  fps: number;
  /** Sends the lever to the mixer; the component sends one at a time, in order (the last value
   * waits for the one in flight), so a completion is never overtaken by an older position. */
  onLever: (lever: number) => Promise<unknown>;
}) {
  const track = useRef<HTMLDivElement>(null);
  const drag = useRef<Drag | null>(null);
  const animation = useRef(0);
  const [shown, setShown] = useState(state.lever);
  const shownRef = useRef(shown);
  shownRef.current = shown;

  // Follow the mixer when not dragging: a jump of the resting end (an Auto) runs at the ME's rate.
  useEffect(() => {
    if (drag.current) return;
    window.cancelAnimationFrame(animation.current);
    const from = shownRef.current;
    const to = state.lever;
    const seconds = state.position === 0 ? travelSeconds(from, to, rateFrames, fps) : 0;
    if (seconds <= 0.02) {
      setShown(to);
      return;
    }
    const start = performance.now();
    const step = (now: number) => {
      const k = Math.min(1, (now - start) / 1000 / seconds);
      setShown(from + (to - from) * k);
      if (k < 1) animation.current = window.requestAnimationFrame(step);
    };
    animation.current = window.requestAnimationFrame(step);
  }, [state.end, state.lever, state.position, rateFrames, fps]);
  useEffect(() => () => window.cancelAnimationFrame(animation.current), []);

  // One request at a time: positions that wait are replaced by newer ones, never reordered.
  const inFlight = useRef(false);
  const waiting = useRef<number | null>(null);
  const send = (lever: number) => {
    if (inFlight.current) {
      waiting.current = lever;
      return;
    }
    inFlight.current = true;
    void onLever(lever).finally(() => {
      inFlight.current = false;
      const next = waiting.current;
      waiting.current = null;
      if (next !== null) send(next);
    });
  };

  const at = (event: React.PointerEvent) => {
    const rect = track.current?.getBoundingClientRect();
    return rect ? (event.clientY - rect.top) / rect.height : 0;
  };
  // The handle moves with the pointer from where it was grabbed (a click on the track does not make
  // it jump to the far end and complete a transition).
  const grab = useRef(0);
  const move = (event: React.PointerEvent) => {
    if (!drag.current) return;
    const step = dragStep(drag.current, at(event) - grab.current);
    drag.current = step.drag;
    setShown(step.shown);
    if (step.send !== null) send(step.send);
  };
  const release = (event: React.PointerEvent) => {
    if (!drag.current) return;
    drag.current = null;
    event.currentTarget.releasePointerCapture?.(event.pointerId);
  };
  return (
    <div
      className="tbar"
      ref={track}
      role="slider"
      aria-label="T-bar"
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={Math.round(shown * 100)}
      title={`T-bar: drag the handle ${state.end === "top" ? "down" : "up"} to make the transition (it stays at the end)`}
      onDragStart={(event) => event.preventDefault()}
      onPointerDown={(event) => {
        // No text selection or native drag: the browser would cancel the pointer mid-drag.
        event.preventDefault();
        window.cancelAnimationFrame(animation.current);
        event.currentTarget.setPointerCapture(event.pointerId);
        drag.current = startDrag(state);
        grab.current = at(event) - shownRef.current;
      }}
      onPointerMove={(event) => {
        if (event.buttons) move(event);
      }}
      onPointerUp={release}
      onPointerCancel={release}
      onLostPointerCapture={() => {
        drag.current = null;
      }}
    >
      <div className="tbar-slot" />
      <div className="tbar-handle" style={{ top: `${shown * 100}%` }} />
    </div>
  );
}
