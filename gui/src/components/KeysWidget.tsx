import { api } from "../api";
import { useWidget } from "./useWidget";

/** The `keys` widget: ME `me`'s upstream keyers and DSKs with ON/OFF and AUTO. */
export function KeysWidget({ me }: { me: number }) {
  const { snapshot, error, setError, command, root } = useWidget();
  const panel = snapshot?.panels[me - 1];
  const keyers = snapshot && panel ? snapshot.keyers.filter((keyer) => keyer.panel_id === panel.id) : [];

  return (
    <div ref={root} className="keys-widget">
      {!snapshot || !panel ? (
        <div className="empty">
          {!snapshot ? error ?? "Connecting to the vision mixer…" : `No ME ${me}: the mixer has ${snapshot.panels.length}.`}
        </div>
      ) : (
        <>
          <div className="bus-title">{panel.label} keys</div>
          {keyers.length === 0 ? <div className="empty">No keyers on {panel.label}.</div> : null}
          {keyers.map((keyer) => (
            <div key={keyer.id} className={`key-row ${keyer.enabled ? "on" : ""}`}>
              <span className="key-label">{keyer.label}</span>
              <button
                className={keyer.enabled ? "on" : ""}
                title="Cut the keyer on or off"
                onClick={() => void command(() => api.patchKeyer(keyer.id, { enabled: !keyer.enabled }))}
              >
                {keyer.enabled ? "On" : "Off"}
              </button>
              <button title="Fade the keyer on or off over its rate" onClick={() => void command(() => api.keyerAuto(keyer.id))}>
                Auto
              </button>
            </div>
          ))}
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
