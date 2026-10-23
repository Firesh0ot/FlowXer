import { useEffect, useRef, useState } from "react";
import { createSender, dragStep, endChanged, startDrag, travelSeconds, type Drag, type TbarState } from "../tbar";

/** The T-bar lever (src/tbar.ts): top 0, bottom 1, resting at the end the mixer says. A drag
 * holds the ME's transition; the other end completes it and the lever stays there (the next
 * transition goes back). While the operator drags, the mixer's state does not move the handle;
 * otherwise the handle follows it, and runs to the other end over the ME's rate when an Auto (or
 * another console) moved it there. */
export function TBar({
  state,
  rateFrames,
  fps,
  oneShot = false,
  onLever,
}: {
  state: TbarState;
  rateFrames: number;
  fps: number;
  /** A stinger or BKGD off: the lever fires an Auto (no scrubbing), once per drag. */
  oneShot?: boolean;
  /** Sends the lever and the end its drag began at to the mixer; the component sends one at a
   * time, in order (newest wins), so a completion is never overtaken by an older position. */
  onLever: (lever: number, from: TbarState["end"]) => Promise<unknown>;
}) {
  const track = useRef<HTMLDivElement>(null);
  const drag = useRef<Drag | null>(null);
  const animation = useRef(0);
  const [shown, setShown] = useState(state.lever);
  const shownRef = useRef(shown);
  shownRef.current = shown;
  const stateRef = useRef(state);
  stateRef.current = state;

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

  // One request at a time, newest wins (src/tbar.ts createSender).
  const onLeverRef = useRef(onLever);
  onLeverRef.current = onLever;
  const sender = useRef<((value: number) => void) | null>(null);
  const from = useRef<TbarState["end"]>(state.end);
  if (!sender.current) sender.current = createSender((value) => onLeverRef.current(value, from.current));
  const send = (lever: number) => sender.current?.(lever);
  // The resting end changed under a drag (another console's Auto or Cut): the drag stops.
  useEffect(() => {
    if (drag.current) drag.current = endChanged(drag.current, state);
  }, [state.end, state.position]);

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
  // The drag ends (released, cancelled or its capture lost): the handle goes to where the mixer
  // has the lever (unchanged after a completion or a held position; corrected after a latched drag
  // or a failed request).
  const resync = useRef(0);
  const end = () => {
    const ended = drag.current;
    if (!ended) return;
    drag.current = null;
    if (ended.completed) return;
    // Once the mixer's state has caught up (the console polls every second).
    window.clearTimeout(resync.current);
    resync.current = window.setTimeout(() => {
      if (!drag.current) setShown(stateRef.current.lever);
    }, 1200);
  };
  useEffect(() => () => window.clearTimeout(resync.current), []);
  const release = (event: React.PointerEvent) => {
    if (event.currentTarget.hasPointerCapture?.(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId);
    end();
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
        drag.current = startDrag(state, oneShot);
        from.current = state.end;
        grab.current = at(event) - shownRef.current;
      }}
      onPointerMove={(event) => {
        if (event.buttons) move(event);
      }}
      onPointerUp={release}
      onPointerCancel={release}
      onLostPointerCapture={end}
    >
      <div className="tbar-slot" />
      <div className="tbar-handle" style={{ top: `${shown * 100}%` }} />
    </div>
  );
}
