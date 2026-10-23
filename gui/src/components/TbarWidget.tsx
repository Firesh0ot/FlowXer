import { api } from "../api";
import { TBar } from "./TBar";
import { useWidget } from "./useWidget";

/** The `tbar` widget: ME `me`'s T-bar lever (a real lever: it stays at the end a transition took
 * it to, src/tbar.ts) and, unless `buttons` is false, Cut and Auto as in the bus widget. */
export function TbarWidget({ me, buttons = true }: { me: number; buttons?: boolean }) {
  const { snapshot, error, setError, command, root } = useWidget();
  const panel = snapshot?.panels[me - 1];
  const format = snapshot?.formats.find((item) => item.id === snapshot.workspace.format_id);
  const [num, den] = String(format?.frame_rate ?? "50/1").split("/").map(Number);
  const fps = num > 0 ? num / (den > 0 ? den : 1) : 50;
  return (
    <div ref={root} className="tbar-widget">
      {!snapshot || !panel ? (
        <div className="empty">{!snapshot ? error ?? "Connecting to the vision mixer…" : `No ME ${me}: the mixer has ${snapshot.panels.length}.`}</div>
      ) : (
        <>
          <div className="bus-title">{panel.label}</div>
          <TBar
            state={snapshot.tbar?.[panel.id] ?? { end: "top", position: 0, lever: 0 }}
            rateFrames={panel.transition.rate_frames}
            fps={fps}
            oneShot={panel.transition.type === "stinger" || panel.transition.background === false}
            onLever={(lever, from) => api.lever(panel.id, lever, from).catch((err: Error) => setError(err.message))}
          />
          {buttons ? (
            <div className="tbar-buttons">
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
          ) : null}
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
