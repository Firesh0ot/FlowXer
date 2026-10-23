import { useEffect, useRef } from "react";
import type { Keyer, MixerPanel, PanelTransition, StingerSlot, TransitionKind } from "../api";
import type { TbarState } from "../tbar";
import { WIPE_PATTERNS, wipePattern } from "../wipe";
import { TBar } from "./TBar";

const TYPES: { type: TransitionKind; label: string }[] = [
  { type: "mix", label: "Mix" },
  { type: "wipe", label: "Wipe" },
  { type: "stinger", label: "Sting" },
];

/** A pattern icon: the wipe half-way (B where s < 0.5), drawn from the engine's s(u, v). */
function WipeIcon({ code, reverse }: { code: number; reverse: boolean }) {
  const canvas = useRef<HTMLCanvasElement>(null);
  useEffect(() => {
    const context = canvas.current?.getContext("2d");
    if (!context) return;
    const w = 32;
    const h = 18;
    const image = context.createImageData(w, h);
    for (let y = 0; y < h; y += 1) {
      for (let x = 0; x < w; x += 1) {
        const s = wipePattern(code, (x + 0.5) / w, (y + 0.5) / h, w / h, reverse);
        const b = s < 0.5;
        const i = (y * w + x) * 4;
        image.data.set(b ? [226, 232, 240, 255] : [40, 52, 72, 255], i);
      }
    }
    context.putImageData(image, 0, 0);
  }, [code, reverse]);
  return <canvas ref={canvas} width={32} height={18} />;
}


/** The next transition's elements: BKGD and the ME's upstream keyers (KEY 1 .. K), each lit when
 * it goes with Cut, Auto and the T-bar. At least one stays lit. */
function TieRow({
  transition,
  keyers,
  onChange,
  onGrab,
}: {
  transition: PanelTransition;
  keyers: Keyer[];
  onChange: (patch: Record<string, unknown>) => void;
  onGrab: () => void;
}) {
  const ties = transition.ties ?? [];
  const background = transition.background ?? true;
  const toggle = (id: string) =>
    onChange({ ties: ties.includes(id) ? ties.filter((tie) => tie !== id) : [...ties, id] });
  return (
    <div className="tie-row" role="group" aria-label="Next transition">
      <button
        className={background ? "on" : ""}
        aria-pressed={background}
        disabled={background && ties.length === 0}
        title="BKGD: the background goes with the transition (off: key only)"
        onClick={() => onChange({ background: !background })}
      >
        BKGD
      </button>
      {keyers.map((keyer, index) => {
        const tied = ties.includes(keyer.id);
        return (
          <button
            key={keyer.id}
            className={`${tied ? "on" : ""}${keyer.enabled ? " on-air" : ""}`}
            aria-pressed={tied}
            disabled={tied && !background && ties.length === 1}
            title={`${keyer.label}: tied to the transition (Cut, Auto and the T-bar turn it ${keyer.enabled ? "off" : "on"})`}
            onClick={() => toggle(keyer.id)}
          >
            KEY {index + 1}
          </button>
        );
      })}
      <button className="grab" title="Grab this ME's Program as a still in the library" onClick={onGrab}>
        GRAB
      </button>
    </div>
  );
}

/** One ME's transition section: the next transition (type, rate, wipe pattern and edge, stinger
 * slot, keyer ties) and Cut, Auto and Fade to Black; GRAB grabs its Program. */
export function TransitionSection({
  panel,
  keyers,
  stingerSlots,
  fps,
  onChange,
  onCut,
  onAuto,
  onLever,
  tbar,
  onFadeToBlack,
  onGrab,
}: {
  panel: MixerPanel;
  /** The ME's upstream keyers (the tie buttons). */
  keyers: Keyer[];
  stingerSlots: StingerSlot[];
  fps: number;
  onChange: (patch: Record<string, unknown>) => void;
  onCut: () => void;
  onAuto: () => void;
  /** The T-bar lever where the operator holds it (0 top .. 1 bottom). */
  onLever: (lever: number, from: TbarState["end"]) => Promise<unknown>;
  /** The ME's T-bar as the mixer has it (GET /console → tbar). */
  tbar?: TbarState;
  onFadeToBlack: () => void;
  onGrab: () => void;
}) {
  const transition: PanelTransition = panel.transition;
  const wipe = transition.wipe;
  const setWipe = (patch: Record<string, unknown>) => onChange({ wipe: patch });
  return (
    <aside className="transition-section" aria-label="Transition">
      <div className="type-row" role="radiogroup" aria-label="Transition type">
        {TYPES.map((item) => (
          <button
            key={item.type}
            role="radio"
            aria-checked={transition.type === item.type}
            className={transition.type === item.type ? "on" : ""}
            onClick={() => onChange({ type: item.type })}
          >
            {item.label}
          </button>
        ))}
        <label className="rate" title="Rate in frames">
          <input
            type="number"
            min={1}
            max={1000}
            value={transition.rate_frames}
            onChange={(event) => {
              const frames = Number(event.target.value);
              if (frames >= 1 && frames <= 1000) onChange({ rate_frames: frames });
            }}
          />
          fr
        </label>
      </div>
      <TieRow transition={transition} keyers={keyers} onChange={onChange} onGrab={onGrab} />
      {transition.type === "wipe" ? (
        <div className="wipe-settings">
          <div className="patterns">
            {WIPE_PATTERNS.map((item) => (
              <button
                key={item.code}
                className={wipe.pattern === item.code ? "on" : ""}
                title={`${item.label} (SMPTE ${item.code})`}
                onClick={() => setWipe({ pattern: item.code })}
              >
                <WipeIcon code={item.code} reverse={wipe.reverse} />
              </button>
            ))}
          </div>
          <div className="wipe-edge">
            <label className="check">
              <input type="checkbox" checked={wipe.reverse} onChange={(event) => setWipe({ reverse: event.target.checked })} />
              Reverse
            </label>
            <label className="check" title="After each wipe, reverse toggles: the next wipe goes back">
              <input type="checkbox" checked={wipe.alternate} onChange={(event) => setWipe({ alternate: event.target.checked })} />
              Flip-flop
            </label>
            <label>
              Soft {Math.round(wipe.soft * 100)} %
              <input
                type="range"
                min={0}
                max={100}
                value={Math.round(wipe.soft * 100)}
                onChange={(event) => setWipe({ soft: Number(event.target.value) / 100 })}
              />
            </label>
            <label>
              Border {Math.round(wipe.border * 100)} %
              <input
                type="range"
                min={0}
                max={100}
                value={Math.round(wipe.border * 100)}
                onChange={(event) => setWipe({ border: Number(event.target.value) / 100 })}
              />
            </label>
            <label className="colour" title="Border colour">
              <input type="color" value={wipe.border_color} onChange={(event) => setWipe({ border_color: event.target.value })} />
            </label>
          </div>
        </div>
      ) : null}
      {transition.type === "stinger" ? (
        <label className="sting-slot">
          Stinger slot
          <select
            value={transition.stinger_slot_id ?? ""}
            onChange={(event) => onChange({ stinger_slot_id: event.target.value || null })}
          >
            <option value="">By direction</option>
            {stingerSlots.map((slot) => (
              <option key={slot.id} value={slot.id}>
                {slot.label}
                {slot.cut_frame != null ? ` · cut ${slot.cut_frame}` : ""}
                {slot.ready === false ? " · converting" : ""}
              </option>
            ))}
          </select>
        </label>
      ) : null}
      <div className="take-row">
        <button className="cut" onClick={onCut} title="Cut Preview to Program">
          Cut
        </button>
        <button className="auto" onClick={onAuto} title={`${transition.type} Preview to Program over ${transition.rate_frames} frames`}>
          Auto
        </button>
        <button className={`ftb${panel.ftb ? " on" : ""}`} onClick={onFadeToBlack} title="Fade the whole output (keys and sound too) to black, or back">
          FTB
        </button>
        <TBar
          state={tbar ?? { end: "top", position: 0, lever: 0 }}
          rateFrames={transition.rate_frames}
          fps={fps}
          oneShot={transition.type === "stinger" || transition.background === false}
          onLever={onLever}
        />
      </div>
    </aside>
  );
}
