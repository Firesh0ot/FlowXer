import { AudioStrip, useAudioMeters } from "./AudioStrip";
import { useWidget } from "./useWidget";

/** The `audio` widget: ME `me`'s strips (meter, MUTE, AFV, and the fader unless `faders` is false)
 * and its master meter with the loudness (docs/engine-p5.md §8). Meters at 10 Hz. */
export function AudioWidget({ me, faders }: { me: number; faders: boolean }) {
  const { snapshot, error, setError, command, root } = useWidget();
  const meters = useAudioMeters(true);
  const panel = snapshot?.panels[me - 1];
  return (
    <div ref={root} className={`audio-widget ${faders ? "" : "no-faders"}`}>
      {!snapshot || !panel ? (
        <div className="empty">
          {!snapshot ? error ?? "Connecting to the vision mixer…" : `No ME ${me}: the mixer has ${snapshot.panels.length}.`}
        </div>
      ) : !snapshot.audio ? (
        <div className="empty">This mixer has no audio settings.</div>
      ) : (
        <>
          <div className="bus-title">{panel.label} audio</div>
          <AudioStrip
            panelId={panel.id}
            audio={snapshot.audio}
            meters={meters}
            faders={faders}
            onChanged={() => void command(async () => undefined)}
            onError={setError}
          />
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
