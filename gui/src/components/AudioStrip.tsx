import { useEffect, useRef, useState } from "react";
import { api, type AudioMeters, type AudioState, type AudioStripSettings, type SourceMeter } from "../api";
import { FADER_MARKS, dbToPosition, formatDb, meterPosition, positionToDb } from "../fader";
import "../audio.css";

/** The meters of the last 100 ms block, polled at 10 Hz while `active` (docs/engine-p5.md §8). */
export function useAudioMeters(active: boolean): AudioMeters | null {
  const [meters, setMeters] = useState<AudioMeters | null>(null);
  useEffect(() => {
    if (!active) return;
    let stopped = false;
    let busy = false;
    const tick = async () => {
      if (busy) return;
      busy = true;
      try {
        const next = await api.audioMeters();
        if (!stopped) setMeters(next);
      } catch {
        // The next poll tries again.
      } finally {
        busy = false;
      }
    };
    void tick();
    const timer = window.setInterval(() => void tick(), 100);
    return () => {
      stopped = true;
      window.clearInterval(timer);
    };
  }, [active]);
  return meters;
}

/** Sends the latest value of a control that moves: one request at a time, the last value always. */
function useLatest<T>(send: (value: T) => Promise<unknown>, onError: (message: string) => void) {
  const sender = useRef(send);
  sender.current = send;
  const pending = useRef<{ value: T } | null>(null);
  const busy = useRef(false);
  return (value: T) => {
    pending.current = { value };
    if (busy.current) return;
    busy.current = true;
    void (async () => {
      while (pending.current) {
        const { value: next } = pending.current;
        pending.current = null;
        try {
          await sender.current(next);
        } catch (err) {
          onError(err instanceof Error ? err.message : "audio change failed");
        }
      }
      busy.current = false;
    })();
  };
}

/** Up to two bars: the channels, or with more than two the largest of the odd and of the even ones. */
function bars(values: number[] | undefined): number[] {
  if (!values || values.length === 0) return [-120];
  if (values.length <= 2) return values;
  const odd = Math.max(...values.filter((_, k) => k % 2 === 0));
  const even = Math.max(...values.filter((_, k) => k % 2 === 1));
  return [odd, even];
}

/** A meter: one bar per (displayed) channel, a 1 s peak hold and an over mark. `marks`: a second
 * value per bar drawn as a line (the true peak). */
function MeterBars({ peaks, marks, title }: { peaks: number[]; marks?: number[]; title: string }) {
  const held = useRef<{ db: number; at: number }[]>([]);
  const now = Date.now();
  const holds = peaks.map((db, k) => {
    const old = held.current[k];
    const keep = old && now - old.at < 1000 && old.db >= db;
    const next = keep ? old : { db, at: now };
    held.current[k] = next;
    return next.db;
  });
  return (
    <div className="meter" title={title}>
      {peaks.map((db, k) => (
        <div key={k} className={`meter-bar ${holds[k] >= 0 ? "over" : ""}`}>
          <div className="meter-fill" style={{ height: `${meterPosition(db) * 100}%` }} />
          <div className="meter-hold" style={{ bottom: `${meterPosition(holds[k]) * 100}%` }} />
          {marks ? <div className="meter-mark" style={{ bottom: `${meterPosition(marks[k] ?? -120) * 100}%` }} /> : null}
        </div>
      ))}
    </div>
  );
}

/** A vertical fader (+10 … −∞ dB; a double click sets 0 dB). It shows the value it sends while it
 * moves, and the mixer's once that has caught up. */
function Fader({ db, onChange, label }: { db: number; onChange: (db: number) => void; label: string }) {
  const [local, setLocal] = useState<number | null>(null);
  const release = useRef<number | undefined>(undefined);
  useEffect(() => {
    if (local !== null && Math.abs(local - db) < 0.05) setLocal(null);
  }, [db, local]);
  const shown = local ?? db;
  const set = (value: number) => {
    setLocal(value);
    onChange(value);
    window.clearTimeout(release.current);
    release.current = window.setTimeout(() => setLocal(null), 1500);
  };
  return (
    <div className="fader-box">
      <div className="fader-scale">
        {FADER_MARKS.map((mark) => (
          <span key={mark} style={{ bottom: `${dbToPosition(mark) * 100}%` }}>
            {mark > 0 ? `+${mark}` : mark}
          </span>
        ))}
      </div>
      <input
        type="range"
        className="fader"
        aria-label={`${label} fader`}
        min={0}
        max={1000}
        step={1}
        value={Math.round(dbToPosition(shown) * 1000)}
        onChange={(event) => set(positionToDb(Number(event.target.value) / 1000))}
        onDoubleClick={() => set(0)}
      />
      <div className="fader-value">{formatDb(shown)}</div>
    </div>
  );
}

function Column({
  id,
  strip,
  meter,
  heard,
  delayMs,
  faders,
  onStrip,
  onError,
}: {
  id: string;
  strip: AudioStripSettings;
  meter?: SourceMeter;
  heard: boolean;
  delayMs?: number;
  faders: boolean;
  onStrip: (payload: { fader_db?: number; mute?: boolean; afv?: boolean }) => Promise<unknown>;
  onError: (message: string) => void;
}) {
  const fader = useLatest((db: number) => onStrip({ fader_db: db }), onError);
  return (
    <div className={`audio-column ${heard ? "heard" : ""}`} data-strip={id}>
      <div className="audio-label" title={strip.label}>
        <span className={`on-air-dot ${heard ? "on" : ""}`} title={heard ? "Heard in PGM" : "Not heard"} />
        {strip.label}
      </div>
      {delayMs ? <div className="audio-delay" title="Audio delay">+{delayMs} ms</div> : <div className="audio-delay" />}
      <div className="audio-body">
        <MeterBars peaks={bars(meter?.peak_dbfs)} title={`${strip.label}: peak ${bars(meter?.peak_dbfs).map((v) => v.toFixed(1)).join(" / ")} dBFS`} />
        {faders ? <Fader db={strip.fader_db} label={strip.label} onChange={fader} /> : null}
      </div>
      {!faders ? <div className="fader-value">{formatDb(strip.fader_db)}</div> : null}
      <div className="audio-buttons">
        <button className={strip.mute ? "mute on" : "mute"} title="Mute (the fader stays)" onClick={() => void onStrip({ mute: !strip.mute }).catch((err: Error) => onError(err.message))}>
          MUTE
        </button>
        {strip.afv !== null ? (
          <button
            className={strip.afv ? "afv on" : "afv"}
            title={strip.afv ? "Audio follows video: heard while on Program" : "Always in the mix (AFV off)"}
            onClick={() => void onStrip({ afv: !strip.afv }).catch((err: Error) => onError(err.message))}
          >
            AFV
          </button>
        ) : (
          <span className="afv-none" />
        )}
      </div>
    </div>
  );
}

/** The AUDIO strip of one ME (docs/engine-p5.md §8): a column per strip (inputs, clip players,
 * re-entered MEs, the stinger) with its meter, fader, MUTE, AFV and an ON AIR dot while it is heard,
 * then the master: the PGM meter (peak, with the true peak as a line), the loudness of pair 1–2
 * (M, S, I) with RESET, and the master fader. No audio preview. `faders` false hides the faders
 * (the widget). */
export function AudioStrip({
  panelId,
  audio,
  meters,
  faders = true,
  onChanged,
  onError,
}: {
  panelId: string;
  audio: AudioState;
  meters: AudioMeters | null;
  faders?: boolean;
  onChanged: () => void;
  onError: (message: string) => void;
}) {
  const panel = audio.panels[panelId];
  const live = meters?.panels[panelId];
  const master = useLatest((db: number) => api.patchAudioPanel(panelId, { master_db: db }), onError);
  if (!panel) {
    return <div className="audio-strip empty">This ME does not render: no audio.</div>;
  }
  const meterOf = (id: string, kind: AudioStripSettings["kind"]): SourceMeter | undefined => {
    if (kind === "panel") return meters?.panels[id];
    if (kind === "stinger") {
      // The stinger players: the loudest one.
      const players = Object.entries(meters?.sources ?? {}).filter(([key]) => key.startsWith("stinger-"));
      return players.length ? players.map(([, m]) => m).reduce((a, b) => (Math.max(...a.peak_dbfs) >= Math.max(...b.peak_dbfs) ? a : b)) : undefined;
    }
    return meters?.sources[id];
  };
  const loudness = live?.loudness[0];
  const lufs = (v: number | undefined) => (v === undefined || v <= -120 ? "–" : v.toFixed(1));
  return (
    <div className="audio-strip">
      {Object.entries(panel.strips).map(([id, strip]) => (
        <Column
          key={id}
          id={id}
          strip={strip}
          meter={meterOf(id, strip.kind)}
          heard={Boolean(live?.strips[id]?.heard)}
          delayMs={strip.kind === "input" ? audio.inputs[id]?.delay_ms : undefined}
          faders={faders}
          onError={onError}
          onStrip={async (payload) => {
            await api.patchAudioStrip(panelId, id, payload);
            if (payload.fader_db === undefined) onChanged();
          }}
        />
      ))}
      <div className="audio-column master">
        <div className="audio-label">{panel.label} PGM</div>
        <div className="audio-delay">{live && live.overs > 0 ? <span className="overs" title="Samples at or above 0 dBFS">OVER {live.overs}</span> : null}</div>
        <div className="audio-body">
          <MeterBars
            peaks={bars(live?.peak_dbfs)}
            marks={bars(live?.true_peak_dbtp)}
            title={`PGM peak ${bars(live?.peak_dbfs).map((v) => v.toFixed(1)).join(" / ")} dBFS, true peak ${bars(live?.true_peak_dbtp)
              .map((v) => v.toFixed(1))
              .join(" / ")} dBTP`}
          />
          {faders ? <Fader db={panel.master_db} label={`${panel.label} master`} onChange={master} /> : null}
        </div>
        {!faders ? <div className="fader-value">{formatDb(panel.master_db)}</div> : null}
        <div className="loudness" title="EBU R 128 loudness of channels 1–2 (LUFS)">
          <span>M {lufs(loudness?.momentary)}</span>
          <span>S {lufs(loudness?.short_term)}</span>
          <span>I {lufs(loudness?.integrated)}</span>
        </div>
        <div className="audio-buttons">
          <button
            title="Integrated loudness from now on"
            onClick={() => void api.resetLoudness(panelId).catch((err: Error) => onError(err.message))}
          >
            RESET
          </button>
        </div>
      </div>
    </div>
  );
}
