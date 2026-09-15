import type { MixerPanel } from "../api";

export function TransitionBank({
  panel,
  stingerPhase,
  onCut,
  onFade,
  onFadeToBlack,
  onWipe,
}: {
  panel: MixerPanel;
  stingerPhase: string;
  onCut: () => void;
  onFade: () => void;
  onFadeToBlack: () => void;
  onWipe: () => void;
}) {
  const wipeArmed = Boolean(panel.wipe_armed);
  const stinging = stingerPhase === "playing" || stingerPhase === "cut";
  return (
    <aside className="transition-bank" aria-label="Transitions">
      <button
        className={`cut ${wipeArmed || stinging ? "sting" : ""}`}
        onClick={onCut}
        title={
          wipeArmed
            ? "Cut Preview to Program through the armed TGA stinger"
            : "Cut Preview to Program"
        }
      >
        {wipeArmed ? "Cut · Sting" : "Cut"}
      </button>
      <button className="fade" onClick={onFade} title="Fade Preview to Program">
        Fade
      </button>
      <button className="ftb" onClick={onFadeToBlack} title="Fade Program to Black">
        Fade to Black
      </button>
      <button
        className={`wipe ${wipeArmed ? "armed" : ""} ${stinging ? "playing" : ""}`}
        onClick={onWipe}
        title="Arm Wipe: the next Cut plays the TGA stinger"
      >
        {stinging ? "Wipe · STING" : wipeArmed ? "Wipe · ARMED" : "Wipe"}
      </button>
    </aside>
  );
}
