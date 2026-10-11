import { useCallback, useEffect, useRef, useState } from "react";
import { api, type ConsoleState } from "../api";

function post(message: Record<string, unknown>) {
  if (window.parent !== window) window.parent.postMessage(message, "*");
}

/** The `bus` widget: Program and Preview rows (the inputs and the higher MEs' Programs) and
 * Cut/Auto of ME `me`. */
export function BusWidget({ me }: { me: number }) {
  const [snapshot, setSnapshot] = useState<ConsoleState | null>(null);
  const [error, setError] = useState<string | null>(null);
  const root = useRef<HTMLDivElement>(null);
  const ready = useRef(false);

  const refresh = useCallback(async () => {
    try {
      setSnapshot(await api.console());
    } catch (err) {
      setError(err instanceof Error ? err.message : "mixer unreachable");
    }
  }, []);

  const command = async (fn: () => Promise<unknown>) => {
    try {
      await fn();
      setError(null);
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "command failed");
    }
  };

  const postSize = () => {
    const rect = root.current?.getBoundingClientRect();
    if (rect) post({ type: "widget-size", w: Math.round(rect.width), h: Math.round(rect.height) });
  };

  useEffect(() => {
    void refresh();
    const timer = window.setInterval(() => void refresh(), 500);
    return () => window.clearInterval(timer);
  }, [refresh]);

  // Ready once the first state is shown; the size whenever it changes.
  useEffect(() => {
    if (!snapshot || ready.current) return;
    ready.current = true;
    post({ type: "widget-ready" });
    postSize();
  }, [snapshot]);
  useEffect(() => {
    const element = root.current;
    if (!element) return;
    const observer = new ResizeObserver(() => {
      if (ready.current) postSize();
    });
    observer.observe(element);
    return () => observer.disconnect();
  }, []);

  const panel = snapshot?.panels[me - 1];
  const sources = snapshot
    ? [
        ...snapshot.inputs.map((input) => ({ id: input.id, label: input.label })),
        // Re-entry: the Program of a higher ME.
        ...snapshot.panels.slice(me).map((item) => ({ id: item.id, label: item.label })),
      ]
    : [];

  return (
    <div ref={root} className="bus-widget">
      {!snapshot || !panel ? (
        <div className="empty">
          {!snapshot ? error ?? "Connecting to the vision mixer…" : `No ME ${me}: the mixer has ${snapshot.panels.length}.`}
        </div>
      ) : (
        <>
          <div className="bus-rows">
            <div className="bus-title">{panel.label}</div>
            <div className="bus-row pgm">
              <span className="row-label">PGM</span>
              <div className="row-buttons">
                {sources.map((source) => (
                  <button
                    key={source.id}
                    className={panel.program_input_id === source.id ? "on" : ""}
                    title={`Cut ${source.label} to Program`}
                    onClick={() => void command(() => api.take(source.id, panel.id))}
                  >
                    {source.label}
                  </button>
                ))}
              </div>
            </div>
            <div className="bus-row pvw">
              <span className="row-label">PVW</span>
              <div className="row-buttons">
                {sources.map((source) => (
                  <button
                    key={source.id}
                    className={panel.preview_input_id === source.id ? "on" : ""}
                    title={`${source.label} to Preview`}
                    onClick={() => void command(() => api.preview(source.id, panel.id))}
                  >
                    {source.label}
                  </button>
                ))}
              </div>
            </div>
          </div>
          <div className="bus-actions">
            <button className="cut" title="Cut Preview to Program" onClick={() => void command(() => api.cut(panel.id))}>
              Cut
            </button>
            <button className="auto" title="Fade Preview to Program" onClick={() => void command(() => api.fade(panel.id))}>
              Auto
            </button>
          </div>
        </>
      )}
      {error && snapshot ? (
        <div className="widget-error" title="Dismiss" onClick={() => setError(null)}>
          {error}
        </div>
      ) : null}
    </div>
  );
}
