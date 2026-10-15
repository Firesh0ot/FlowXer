import { useState } from "react";
import { api, type SnapshotInfo } from "../api";
import { useWidget } from "./useWidget";

/** The `snapshots` widget: a recall button per snapshot of ME `me` and, unless `all` is false,
 * per snapshot of all MEs. A recall uses the snapshot's own options (PGM: with Program/on-air, AUD:
 * with audio). The last recall here is lit; what it kept or skipped shows below. */
export function SnapshotsWidget({ me, all }: { me: number | null; all: boolean }) {
  const { snapshot, error, setError, command, root } = useWidget();
  const [last, setLast] = useState<number | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const panel = me ? snapshot?.panels[me - 1] : undefined;
  const items = (snapshot?.snapshots?.snapshots ?? []).filter(
    (item) => (panel && item.panel_id === panel.id) || (all && item.panel_id === "all"),
  );
  const title = [panel?.label, all ? "all MEs" : null].filter(Boolean).join(" · ");

  const recall = (item: SnapshotInfo) =>
    void command(async () => {
      const result = await api.recallSnapshot(item.number);
      setLast(item.number);
      setNote(result.skipped.length ? `${item.name}: ${result.skipped.join(" · ")}` : null);
    });

  return (
    <div ref={root} className="snapshots-widget">
      {!snapshot || (me && !panel) ? (
        <div className="empty">
          {!snapshot ? error ?? "Connecting to the vision mixer…" : `No ME ${me}: the mixer has ${snapshot.panels.length}.`}
        </div>
      ) : (
        <>
          <div className="bus-title">Snapshots {title}</div>
          {items.length === 0 ? <div className="empty">No snapshots{title ? ` of ${title}` : ""}.</div> : null}
          <div className="snap-buttons">
            {items.map((item) => (
              <button
                key={item.number}
                className={item.number === last ? "on" : ""}
                title={`Recall ${item.number} ${item.name}${item.options.program ? " with Program/on-air" : ""}${item.options.audio ? " with audio" : ""}`}
                onClick={() => recall(item)}
              >
                <span className="snap-number">{item.number}</span>
                <span className="snap-name">{item.name}</span>
                <span className="snap-flags">
                  {item.options.program ? <span className="pgm">PGM</span> : null}
                  {item.options.audio ? <span>AUD</span> : null}
                  {item.panel_id === "all" && panel ? <span>ALL</span> : null}
                </span>
              </button>
            ))}
          </div>
        </>
      )}
      {(error && snapshot) || note ? (
        <div
          className="widget-error"
          title="Dismiss"
          onClick={() => {
            setError(null);
            setNote(null);
          }}
        >
          {error && snapshot ? error : note}
        </div>
      ) : null}
    </div>
  );
}
