import type { MixerPanel } from "../api";

export function TransitionBank({
  panel,
  onCut,
  onFade,
  onFadeToBlack,
  onWipe,
}: {
  panel: MixerPanel;
  onCut: () => void;
  onFade: () => void;
  onFadeToBlack: () => void;
  onWipe: () => void;
}) {
  return (
    <aside className="transition-bank" aria-label="Transitions">
      <button className="cut" onClick={onCut} title="Cut Preview to Program">
        Cut
      </button>
      <button className="fade" onClick={onFade} title="Fade Preview to Program">
        Fade
      </button>
      <button className="ftb" onClick={onFadeToBlack} title="Fade Program to Black">
        Fade to Black
      </button>
      <button
        className={`wipe ${panel.wipe_armed ? "armed" : ""}`}
        onClick={onWipe}
        title="Arm Wipe: the next Cut plays the TGA stinger"
      >
        Wipe{panel.wipe_armed ? " · ARMED" : ""}
      </button>
    </aside>
  );
}
