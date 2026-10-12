import { useEffect, useState } from "react";
import { api, type Dme, type DmeKeyframe, type DmeLoop, type Ease, type Keyer } from "../api";
import { PRESET_BUTTONS } from "../dme";

const EASES: Ease[] = ["linear", "ease", "ease-in", "ease-out", "hold"];

/** A box's animated fields: what a keyframe stores. */
function boxState(dme: Dme): Omit<DmeKeyframe, "frames" | "ease"> {
  return {
    x: dme.x,
    y: dme.y,
    size: dme.size,
    soft: dme.soft,
    crop: { ...dme.crop },
    border: { width: dme.border.width, soft: dme.border.soft, opacity: dme.border.opacity },
    shadow: { ...dme.shadow },
  };
}

/** A keyer's keyframes (up to 8): STORE adds the box as it is; per keyframe its frames (the time
 * from the one before) and ease, GO (jump there), SET (the box as it is), move up and down,
 * delete. FLY: the keyer's Auto flies the box. RUN ◀ ▶ (with LOOP), STOP; the presets. */
function DmeKeyframes({ keyer, fps, onCommand }: { keyer: Keyer; fps: number; onCommand: (fn: () => Promise<unknown>) => void }) {
  const [loop, setLoop] = useState<DmeLoop>("off");
  const keyframes = keyer.keyframes;
  const save = (list: DmeKeyframe[]) => onCommand(() => api.keyframes(keyer.id, list));
  const change = (j: number, patch: Partial<DmeKeyframe>) => save(keyframes.map((k, i) => (i === j ? { ...k, ...patch } : k)));
  const swap = (j: number) => {
    const list = [...keyframes];
    [list[j], list[j + 1]] = [list[j + 1], list[j]];
    save(list);
  };
  const rate = Math.max(1, Math.round((keyer.rate_ms * fps) / 1000));
  const run = keyer.run;
  const status = run?.running
    ? `${run.loop === "off" ? "RUN" : run.loop.toUpperCase()} → ${run.keyframe ?? ""}`
    : run?.keyframe != null
      ? `AT ${run.keyframe}`
      : "";
  return (
    <div className="dme-keyframes">
      <label className="check" title="Auto flies the box from keyframe 0 (off) to the last (on) instead of fading">
        <input type="checkbox" checked={keyer.fly} onChange={(e) => onCommand(() => api.patchKeyer(keyer.id, { fly: e.target.checked }))} />
        FLY with Auto
      </label>
      {keyframes.length > 0 ? (
        <table className="kf-list">
          <tbody>
            {keyframes.map((k, j) => (
              <tr key={j} data-keyframe={j}>
                <td className="kf-index">{j}</td>
                <td>
                  <NumberField
                    label=""
                    value={k.frames}
                    min={1}
                    max={1000}
                    onCommit={(v) => change(j, { frames: Math.round(v) })}
                  />
                </td>
                <td>
                  <select value={k.ease} title="Ease from the keyframe before" onChange={(e) => change(j, { ease: e.target.value as Ease })}>
                    {EASES.map((ease) => (
                      <option key={ease} value={ease}>
                        {ease}
                      </option>
                    ))}
                  </select>
                </td>
                <td className="kf-actions">
                  <button title="Jump the box to this keyframe" onClick={() => onCommand(() => api.keyerRun(keyer.id, { to: j, frames: 0 }))}>
                    GO
                  </button>
                  <button title="Store the box as it is in this keyframe" onClick={() => change(j, boxState(keyer.dme))}>
                    SET
                  </button>
                  <button title="Move up" disabled={j === 0} onClick={() => swap(j - 1)}>
                    ▲
                  </button>
                  <button title="Move down" disabled={j === keyframes.length - 1} onClick={() => swap(j)}>
                    ▼
                  </button>
                  <button title="Delete" onClick={() => save(keyframes.filter((_, i) => i !== j))}>
                    ✕
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : null}
      <div className="dme-row">
        <button
          className="kf-store"
          title="Add the box as it is as the next keyframe (its time: the keyer's rate)"
          disabled={keyframes.length >= 8}
          onClick={() => save([...keyframes, { ...boxState(keyer.dme), frames: rate, ease: "linear" }])}
        >
          STORE
        </button>
        <button
          className="kf-run back"
          title="Run back to keyframe 0"
          disabled={keyframes.length === 0}
          onClick={() => onCommand(() => api.keyerRun(keyer.id, loop === "off" ? { reverse: true } : { reverse: true, loop }))}
        >
          ◀ RUN
        </button>
        <button className="kf-stop" title="Stop the run: the box holds" onClick={() => onCommand(() => api.keyerStop(keyer.id))}>
          STOP
        </button>
        <button
          className="kf-run"
          title="Run to the last keyframe"
          disabled={keyframes.length === 0}
          onClick={() => onCommand(() => api.keyerRun(keyer.id, loop === "off" ? {} : { loop }))}
        >
          RUN ▶
        </button>
        <select className="kf-loop" value={loop} title="Loop of RUN" onChange={(e) => setLoop(e.target.value as DmeLoop)}>
          <option value="off">once</option>
          <option value="wrap">wrap</option>
          <option value="pingpong">pingpong</option>
        </select>
        {status ? <span className="kf-status">{status}</span> : null}
      </div>
      <div className="dme-row presets">
        {PRESET_BUTTONS.map((preset) => (
          <button key={preset.name} title={preset.title} onClick={() => onCommand(() => api.keyerPreset(keyer.id, preset.name))}>
            {preset.label}
          </button>
        ))}
      </div>
    </div>
  );
}

/** A number that is sent when the field is left or Enter is pressed (`scale` 100: a percentage of
 * a 0..1 value). */
function NumberField({
  label,
  value,
  min,
  max,
  step = 1,
  scale = 1,
  onCommit,
}: {
  label: string;
  value: number;
  min: number;
  max: number;
  step?: number;
  scale?: number;
  onCommit: (value: number) => void;
}) {
  const shown = Math.round(value * scale * 1000) / 1000;
  const [text, setText] = useState(String(shown));
  useEffect(() => setText(String(shown)), [shown]);
  const commit = () => {
    const v = Number(text);
    if (!Number.isFinite(v) || v < min || v > max) {
      setText(String(shown));
      return;
    }
    if (v !== shown) onCommit(v / scale);
  };
  return (
    <label className="num">
      {label}
      <input
        type="number"
        min={min}
        max={max}
        step={step}
        value={text}
        onChange={(e) => setText(e.target.value)}
        onBlur={commit}
        onKeyDown={(e) => {
          if (e.key === "Enter") commit();
        }}
      />
    </label>
  );
}

/** A keyer (upstream or DSK): cut on or off, Auto (a fade over its rate, or a fly), the rate in
 * frames, and behind ⚙ its key: linear (its key input, NO KEY without a signal), luma (source,
 * clip, soft, gain, invert) or, upstream, pip (a source in a box); an upstream keyer's box
 * (position, size, crop, border, shadow; changes go to Program at once while it is on air), its
 * keyframes, runs and presets. */
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

  const dme = keyer.dme;
  const box = (body: Record<string, unknown>) => patch({ dme: body });
  const upstream = keyer.stage === "upstream";
  const boxOn = upstream && (keyer.mode === "pip" || dme.enabled);
  const modeValue = keyer.mode === "linear" ? "" : `${keyer.mode}:${keyer.source_id ?? ""}`;
  const noKey = keyer.mode === "linear" && keyState !== undefined && keyState !== "running";
  return (
    <div className={`dsk-control ${keyer.enabled ? "on" : ""}`} title={noKey ? `Key input: ${keyState}` : undefined}>
      <button
        className={`${keyer.enabled ? "on" : ""} ${noKey ? "nokey" : ""}`}
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
      <button className="gear" title="Key: linear, luma or pip, and its box" onClick={() => setOpen(!open)}>
        ⚙
      </button>
      {open ? (
        <div className="key-settings">
          <select
            value={modeValue}
            onChange={(e) => {
              const [mode, sourceId] = e.target.value.split(":");
              patch(mode ? { mode, source_id: sourceId } : { mode: "linear" });
            }}
          >
            <option value="">Linear (key input)</option>
            {sources.map((source) => (
              <option key={`luma:${source.id}`} value={`luma:${source.id}`}>
                Luma: {source.label}
              </option>
            ))}
            {upstream
              ? sources.map((source) => (
                  <option key={`pip:${source.id}`} value={`pip:${source.id}`}>
                    PiP: {source.label}
                  </option>
                ))
              : null}
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
          {upstream ? (
            <div className="dme-fields">
              <label className="check">
                <input
                  type="checkbox"
                  checked={boxOn}
                  disabled={keyer.mode === "pip"}
                  onChange={(e) => box({ enabled: e.target.checked })}
                />
                Box (DME){boxOn && keyer.enabled ? <span className="on-air">ON AIR</span> : null}
              </label>
              {boxOn ? (
                <>
                  <div className="dme-row">
                    <NumberField label="x %" value={dme.x} min={-100} max={200} step={0.1} scale={100} onCommit={(v) => box({ x: v })} />
                    <NumberField label="y %" value={dme.y} min={-100} max={200} step={0.1} scale={100} onCommit={(v) => box({ y: v })} />
                    <NumberField label="size %" value={dme.size} min={0} max={200} step={0.1} scale={100} onCommit={(v) => box({ size: v })} />
                  </div>
                  <div className="dme-row">
                    {(["left", "right", "top", "bottom"] as const).map((side) => (
                      <NumberField
                        key={side}
                        label={`crop ${side[0].toUpperCase()} %`}
                        value={dme.crop[side]}
                        min={0}
                        max={99}
                        step={0.1}
                        scale={100}
                        onCommit={(v) => box({ crop: { [side]: v } })}
                      />
                    ))}
                  </div>
                  <div className="dme-row">
                    <NumberField label="border px" value={dme.border.width} min={0} max={64} step={0.5} onCommit={(v) => box({ border: { width: v } })} />
                    <NumberField label="soft px" value={dme.border.soft} min={0} max={64} step={0.5} onCommit={(v) => box({ border: { soft: v } })} />
                    <NumberField label="opacity %" value={dme.border.opacity} min={0} max={100} scale={100} onCommit={(v) => box({ border: { opacity: v } })} />
                    <label className="num">
                      colour
                      <input type="color" value={dme.border.color} onChange={(e) => box({ border: { color: e.target.value } })} />
                    </label>
                  </div>
                  <div className="dme-row">
                    <NumberField label="shadow x" value={dme.shadow.dx} min={-128} max={128} onCommit={(v) => box({ shadow: { dx: v } })} />
                    <NumberField label="shadow y" value={dme.shadow.dy} min={-128} max={128} onCommit={(v) => box({ shadow: { dy: v } })} />
                    <NumberField label="blur px" value={dme.shadow.blur} min={0} max={128} onCommit={(v) => box({ shadow: { blur: v } })} />
                    <NumberField label="opacity %" value={dme.shadow.opacity} min={0} max={100} scale={100} onCommit={(v) => box({ shadow: { opacity: v } })} />
                  </div>
                  <div className="dme-row">
                    <NumberField label="edge soft px" value={dme.soft} min={0} max={64} step={0.5} onCommit={(v) => box({ soft: v })} />
                  </div>
                  <DmeKeyframes keyer={keyer} fps={fps} onCommand={onCommand} />
                </>
              ) : null}
            </div>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
