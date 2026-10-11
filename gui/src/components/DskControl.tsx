import { useEffect, useState } from "react";
import { api, type DownstreamKeyer } from "../api";

/** A downstream keyer: cut on or off, Auto (a fade over its rate) and the rate in frames. */
export function DskControl({
  keyer,
  keyState,
  fps,
  onCommand,
}: {
  keyer: DownstreamKeyer;
  /** The key input's state in the engine ("running" when its v210a flow comes in). */
  keyState?: string;
  fps: number;
  onCommand: (fn: () => Promise<unknown>) => void;
}) {
  const frames = Math.max(1, Math.round((keyer.rate_ms * fps) / 1000));
  const [rate, setRate] = useState(String(frames));
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

  const noKey = keyState !== undefined && keyState !== "running";
  return (
    <div className={`dsk-control ${keyer.enabled ? "on" : ""}`} title={noKey ? `Key input: ${keyState}` : undefined}>
      <button
        className={keyer.enabled ? "on" : ""}
        title="Cut the keyer on or off"
        onClick={() => onCommand(() => api.patchKeyer(keyer.id, { enabled: !keyer.enabled }))}
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
    </div>
  );
}
