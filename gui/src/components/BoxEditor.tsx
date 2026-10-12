import { useRef, useState, type PointerEvent as ReactPointerEvent } from "react";
import { api, type Dme, type Keyer } from "../api";
import { boxMoved, boxRect, boxResized, type Corner } from "../dme";

type Drag = { id: string; corner: Corner | null; startX: number; startY: number; start: Dme; frame: DOMRect };

/** The box editor over an ME's Program picture (docs/engine-p4.md §7): the outline of each box of
 * the ME's upstream keyers, drawn by the browser (no engine cost). Drag a box to move it, a corner
 * handle to scale it (the opposite corner stays, the aspect is kept). Changes go to the API while
 * dragging (one request at a time, the latest wins), so a box on air moves on Program at once: it is
 * marked ON AIR. A box off air has a dashed outline (boxes never show on Preview). The handles'
 * pointer events bubble to their box, which handles the drag. */
export function BoxEditor({
  keyers,
  width,
  height,
  onDone,
  onError,
}: {
  /** The ME's upstream keyers with a box. */
  keyers: Keyer[];
  /** The production raster. */
  width: number;
  height: number;
  /** After a drag: refresh the console. */
  onDone: () => void;
  onError: (message: string) => void;
}) {
  const frame = useRef<HTMLDivElement>(null);
  const [drag, setDrag] = useState<Drag | null>(null);
  // The box being dragged as the editor shows it (the console catches up after the drag).
  const [shown, setShown] = useState<{ id: string; dme: Dme } | null>(null);
  const sending = useRef(false);
  const pending = useRef<{ id: string; body: Partial<Dme> } | null>(null);

  const send = (id: string, body: Partial<Dme>) => {
    pending.current = { id, body };
    if (sending.current) return;
    sending.current = true;
    const next = async () => {
      while (pending.current) {
        const { id: keyerId, body: patch } = pending.current;
        pending.current = null;
        try {
          await api.patchKeyer(keyerId, { dme: patch });
        } catch (err) {
          onError(err instanceof Error ? err.message : "box change failed");
        }
      }
      sending.current = false;
    };
    void next();
  };

  const start = (event: ReactPointerEvent, keyer: Keyer, corner: Corner | null) => {
    const rect = frame.current?.getBoundingClientRect();
    if (!rect || event.button !== 0) return;
    event.preventDefault();
    event.stopPropagation();
    (event.target as Element).setPointerCapture(event.pointerId);
    setDrag({ id: keyer.id, corner, startX: event.clientX, startY: event.clientY, start: keyer.dme, frame: rect });
    setShown({ id: keyer.id, dme: keyer.dme });
  };

  const move = (event: ReactPointerEvent) => {
    if (!drag) return;
    const sx = width / drag.frame.width;
    const sy = height / drag.frame.height;
    const change = drag.corner
      ? boxResized(drag.start, drag.corner, (event.clientX - drag.frame.left) * sx, (event.clientY - drag.frame.top) * sy, width, height)
      : boxMoved(drag.start, (event.clientX - drag.startX) * sx, (event.clientY - drag.startY) * sy, width, height);
    setShown({ id: drag.id, dme: { ...drag.start, ...change } });
    send(drag.id, change);
  };

  const end = () => {
    if (!drag) return;
    setDrag(null);
    // The console refreshes once the last change is in.
    const settle = () => {
      if (sending.current) {
        window.setTimeout(settle, 30);
        return;
      }
      setShown(null);
      onDone();
    };
    settle();
  };

  return (
    <div
      className="box-frame"
      ref={frame}
      style={{ aspectRatio: `${width} / ${height}`, width: `min(100cqw, calc(100cqh * ${width} / ${height}))` }}
    >
      {keyers.map((keyer) => {
        const dme = shown?.id === keyer.id ? shown.dme : keyer.dme;
        const r = boxRect(dme, width, height);
        const style = {
          left: `${(100 * r.left) / width}%`,
          top: `${(100 * r.top) / height}%`,
          width: `${(100 * (r.right - r.left)) / width}%`,
          height: `${(100 * (r.bottom - r.top)) / height}%`,
        };
        return (
          <div
            key={keyer.id}
            className={`dme-box ${keyer.enabled ? "on-air" : "off-air"}${drag?.id === keyer.id ? " dragging" : ""}`}
            data-keyer={keyer.id}
            style={style}
            title={`${keyer.label}: drag to move, corners to scale`}
            onPointerDown={(e) => start(e, keyer, null)}
            onPointerMove={move}
            onPointerUp={end}
            onPointerCancel={end}
          >
            <span className="dme-box-label">
              {keyer.label}
              {keyer.enabled ? <span className="on-air-mark">ON AIR</span> : null}
            </span>
            {(["tl", "tr", "bl", "br"] as Corner[]).map((corner) => (
              <span
                key={corner}
                className={`dme-handle ${corner}`}
                data-corner={corner}
                onPointerDown={(e) => start(e, keyer, corner)}
              />
            ))}
          </div>
        );
      })}
    </div>
  );
}
