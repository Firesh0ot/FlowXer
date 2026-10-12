import { api } from "../api";
import { PRESET_BUTTONS } from "../dme";
import { useWidget } from "./useWidget";

/** The `dme` widget: one upstream keyer's box (upstream keyer `keyer` of ME `me`): ON/OFF (a cut),
 * AUTO (a fade, or a fly with FLY), ◀ RUN and RUN ▶ along its keyframes, STOP, and the presets.
 * Editing the box stays in the console. */
export function DmeWidget({ me, keyer: number }: { me: number; keyer: number }) {
  const { snapshot, error, setError, command, root } = useWidget();
  const panel = snapshot?.panels[me - 1];
  const upstream = snapshot && panel ? snapshot.keyers.filter((k) => k.panel_id === panel.id && k.stage === "upstream") : [];
  const keyer = upstream[number - 1];
  const run = keyer?.run;

  let empty: string | null = null;
  if (!snapshot) empty = error ?? "Connecting to the vision mixer…";
  else if (!panel) empty = `No ME ${me}: the mixer has ${snapshot.panels.length}.`;
  else if (!keyer) empty = `No upstream keyer ${number} on ${panel.label} (it has ${upstream.length}).`;

  return (
    <div ref={root} className="dme-widget">
      {empty || !keyer ? (
        <div className="empty">{empty}</div>
      ) : (
        <>
          <div className="dme-head">
            <span className="key-label">
              {keyer.label}
              {keyer.fly ? <span className="dme-flag">FLY</span> : null}
              {run?.running ? <span className="dme-flag running">{run.loop === "off" ? "RUN" : run.loop.toUpperCase()}</span> : null}
            </span>
            <button
              className={keyer.enabled ? "on" : ""}
              title="Cut the keyer on or off"
              onClick={() => void command(() => api.patchKeyer(keyer.id, { enabled: !keyer.enabled }))}
            >
              {keyer.enabled ? "On" : "Off"}
            </button>
            <button
              title={keyer.fly ? "Fly the box on or off along its keyframes" : "Fade the keyer on or off over its rate"}
              onClick={() => void command(() => api.keyerAuto(keyer.id))}
            >
              Auto
            </button>
          </div>
          <div className="dme-run">
            <button title="Run the box back to keyframe 0" onClick={() => void command(() => api.keyerRun(keyer.id, { reverse: true }))}>
              ◀ Run
            </button>
            <button title="Stop the run: the box holds" onClick={() => void command(() => api.keyerStop(keyer.id))}>
              Stop
            </button>
            <button title="Run the box to its last keyframe" onClick={() => void command(() => api.keyerRun(keyer.id))}>
              Run ▶
            </button>
          </div>
          <div className="dme-presets">
            {PRESET_BUTTONS.map((preset) => (
              <button key={preset.name} title={preset.title} onClick={() => void command(() => api.keyerPreset(keyer.id, preset.name))}>
                {preset.label}
              </button>
            ))}
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
