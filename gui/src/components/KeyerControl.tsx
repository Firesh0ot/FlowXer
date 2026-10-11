import { useEffect, useState } from "react";
import { api, type Keyer } from "../api";

/** A keyer (upstream or DSK): cut on or off, Auto (a fade over its rate), the rate in frames, and
 * behind ⚙ its key: linear (its key input, NO KEY without a signal) or luma (source, clip, soft,
 * gain, invert). */
export function KeyerControl({
  keyer,
  keyState,
  sources,
  fps,
  onCommand,
}: {
  keyer: Keyer;
  /** The key input's state in the engine ("running" when its v210a flow comes in). */
  keyState?: string;
  /** The sources a luma key of this ME may take. */
  sources: { id: string; label: string }[];
  fps: number;
  onCommand: (fn: () => Promise<unknown>) => void;
}) {
  const frames = Math.max(1, Math.round((keyer.rate_ms * fps) / 1000));
  const [rate, setRate] = useState(String(frames));
  const [open, setOpen] = useState(false);
  useEffect(() => setRate(String(frames)), [frames]);

  const saveRate = () => {
    const value = Math.round(Number(rate));
    if (!Number.isFinite(value) || value < 1) {
      setRate(String(frames));
      return;
    }
    const rateMs = Math.min(10000, Math.max(1, Math.round((value * 1000) / fps)));
    if (value !== frames) onCommand(() => api.patchKeyer(keyer.id, { rate_ms: rateMs }));
  };
  const patch = (body: Record<string, unknown>) => onCommand(() => api.patchKeyer(keyer.id, body));
  const percent = (name: "clip" | "soft" | "gain") => (
    <label>
      {name} {Math.round(keyer[name] * 100)} %
      <input type="range" min={0} max={100} value={Math.round(keyer[name] * 100)} onChange={(e) => patch({ [name]: Number(e.target.value) / 100 })} />
    </label>
  );

  const noKey = keyer.mode === "linear" && keyState !== undefined && keyState !== "running";
  return (
    <div className={`dsk-control ${keyer.enabled ? "on" : ""}`} title={noKey ? `Key input: ${keyState}` : undefined}>
      <button
        className={keyer.enabled ? "on" : ""}
        title="Cut the keyer on or off"
        onClick={() => patch({ enabled: !keyer.enabled })}
      >
        {keyer.label} {keyer.enabled ? "ON" : "OFF"}
        {noKey ? " · NO KEY" : ""}
      </button>
      <button title="Fade the keyer on or off over its rate" onClick={() => onCommand(() => api.keyerAuto(keyer.id))}>
        AUTO
      </button>
      <label title="Auto rate in frames">
        <input
          type="number"
          min={1}
          max={500}
          value={rate}
          onChange={(e) => setRate(e.target.value)}
          onBlur={saveRate}
          onKeyDown={(e) => {
            if (e.key === "Enter") saveRate();
          }}
        />
        f
      </label>
      <button className="gear" title="Key: linear or luma" onClick={() => setOpen(!open)}>
        ⚙
      </button>
      {open ? (
        <div className="key-settings">
          <select
            value={keyer.mode === "luma" ? keyer.source_id ?? "" : ""}
            onChange={(e) => patch(e.target.value ? { mode: "luma", source_id: e.target.value } : { mode: "linear" })}
          >
            <option value="">Linear (key input)</option>
            {sources.map((source) => (
              <option key={source.id} value={source.id}>
                Luma: {source.label}
              </option>
            ))}
          </select>
          {keyer.mode === "luma" ? (
            <>
              {percent("clip")}
              {percent("soft")}
              <label className="check">
                <input type="checkbox" checked={keyer.invert} onChange={(e) => patch({ invert: e.target.checked })} />
                invert
              </label>
            </>
          ) : null}
          {percent("gain")}
        </div>
      ) : null}
    </div>
  );
}
