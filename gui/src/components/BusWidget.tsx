import { api, type TransitionKind } from "../api";
import { useWidget } from "./useWidget";

const TYPES: { type: TransitionKind; label: string }[] = [
  { type: "mix", label: "Mix" },
  { type: "wipe", label: "Wipe" },
  { type: "stinger", label: "Sting" },
];

/** The `bus` widget: Program and Preview rows (the inputs and the higher MEs' Programs), Cut/Auto
 * of ME `me` and, unless `transition` is false, its transition type and keyer ties (BKGD, K1 ..). */
export function BusWidget({ me, transition = true }: { me: number; transition?: boolean }) {
  const { snapshot, error, setError, command, root } = useWidget();

  const panel = snapshot?.panels[me - 1];
  const keyers = snapshot && panel ? snapshot.keyers.filter((keyer) => keyer.panel_id === panel.id && keyer.stage === "upstream") : [];
  const ties = panel?.transition.ties ?? [];
  const background = panel?.transition.background ?? true;
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
          <div className={`bus-actions${transition ? (keyers.length > 0 ? " with-type with-ties" : " with-type") : ""}`}>
            {transition ? (
              <div className="type-select" role="radiogroup" aria-label="Transition type">
                {TYPES.map((item) => (
                  <button
                    key={item.type}
                    role="radio"
                    aria-checked={panel.transition.type === item.type}
                    className={panel.transition.type === item.type ? "on" : ""}
                    title={`Auto runs a ${item.label.toLowerCase()}`}
                    onClick={() => void command(() => api.transition(panel.id, { type: item.type }))}
                  >
                    {item.label}
                  </button>
                ))}
              </div>
            ) : null}
            {transition && keyers.length > 0 ? (
              <div className="tie-select" role="group" aria-label="Next transition">
                <button
                  className={background ? "on" : ""}
                  aria-pressed={background}
                  disabled={background && ties.length === 0}
                  title="BKGD: the background goes with Cut and Auto (off: key only)"
                  onClick={() => void command(() => api.transition(panel.id, { background: !background }))}
                >
                  BG
                </button>
                {keyers.map((keyer, index) => {
                  const tied = ties.includes(keyer.id);
                  return (
                    <button
                      key={keyer.id}
                      className={tied ? "on" : ""}
                      aria-pressed={tied}
                      disabled={tied && !background && ties.length === 1}
                      title={`${keyer.label}: tied to Cut and Auto`}
                      onClick={() =>
                        void command(() =>
                          api.transition(panel.id, { ties: tied ? ties.filter((tie) => tie !== keyer.id) : [...ties, keyer.id] }),
                        )
                      }
                    >
                      K{index + 1}
                    </button>
                  );
                })}
              </div>
            ) : null}
            <button className="cut" title="Cut Preview to Program" onClick={() => void command(() => api.cut(panel.id))}>
              Cut
            </button>
            <button
              className="auto"
              title={`${panel.transition.type} Preview to Program over ${panel.transition.rate_frames} frames`}
              onClick={() => void command(() => api.auto(panel.id))}
            >
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
