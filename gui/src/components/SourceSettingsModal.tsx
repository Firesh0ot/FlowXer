import { useState } from "react";
import type { LibraryItem, LogicalInput, StingerSlot } from "../api";

const KINDS = ["mxl_live", "clip_player", "black"] as const;

export function SourceSettingsModal({
  input,
  libraryItems,
  stingerSlots,
  pinnedBy,
  onClose,
  onSave,
  onOpenLibrary,
}: {
  input: LogicalInput;
  /** Ready clips and stills of the library. */
  libraryItems: LibraryItem[];
  stingerSlots: StingerSlot[];
  /** Variables that set the inputs' ids, kinds and labels, when the environment does. */
  pinnedBy?: string;
  onClose: () => void;
  onSave: (payload: Record<string, unknown>) => Promise<void>;
  onOpenLibrary: () => void;
}) {
  const [label, setLabel] = useState(input.label);
  const [kind, setKind] = useState(input.kind);
  const [groupHint, setGroupHint] = useState(input.group_hint ?? "");
  const [videoFlow, setVideoFlow] = useState(input.video?.flow_id ?? "");
  const [audioFlow, setAudioFlow] = useState(input.audio?.flow_id ?? "");
  const [libraryItemId, setLibraryItemId] = useState(input.library_item_id ?? "");
  const [loop, setLoop] = useState(Boolean(input.loop));
  const [playOnCut, setPlayOnCut] = useState(Boolean(input.play_on_cut));
  const [stingerSlotId, setStingerSlotId] = useState(input.stinger_slot_id ?? "");
  const [error, setError] = useState<string | null>(null);


  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal" onClick={(event) => event.stopPropagation()}>
        <h2>{input.id}</h2>
        <label>
          Name
          <input
            value={label}
            disabled={Boolean(pinnedBy)}
            onChange={(e) => setLabel(e.target.value)}
          />
        </label>
        <label>
          Kind
          <select
            value={kind}
            disabled={Boolean(pinnedBy)}
            onChange={(e) => setKind(e.target.value as LogicalInput["kind"])}
          >
            {KINDS.map((item) => (
              <option key={item} value={item}>
                {item}
              </option>
            ))}
          </select>
        </label>
        {pinnedBy ? (
          <p className="hint">Name and kind are set by the production (environment: {pinnedBy}).</p>
        ) : null}
        {kind === "mxl_live" ? (
          <>
            <label>
              Group hint
              <input value={groupHint} onChange={(e) => setGroupHint(e.target.value)} />
            </label>
            <label>
              Video flow UUID
              <input value={videoFlow} onChange={(e) => setVideoFlow(e.target.value)} />
            </label>
            <label>
              Audio flow UUID
              <input value={audioFlow} onChange={(e) => setAudioFlow(e.target.value)} />
            </label>
          </>
        ) : null}
        {kind === "clip_player" ? (
          <>
            <label>
              Clip or still
              <select value={libraryItemId} onChange={(e) => setLibraryItemId(e.target.value)}>
                <option value="">Empty (black)</option>
                {libraryItems.map((item) => (
                  <option key={item.id} value={item.id}>
                    {item.name} ({item.kind === "still" ? "still" : `${item.duration_s.toFixed(1)} s`})
                  </option>
                ))}
              </select>
            </label>
            <label className="check">
              <input type="checkbox" checked={loop} onChange={(e) => setLoop(e.target.checked)} />
              Loop
            </label>
            <label className="check">
              <input type="checkbox" checked={playOnCut} onChange={(e) => setPlayOnCut(e.target.checked)} />
              Play on cut (start when taken to Program)
            </label>
            <button type="button" className="ghost" onClick={onOpenLibrary}>
              Open clip and still library…
            </button>
          </>
        ) : null}
        <label>
          Auto stinger
          <select value={stingerSlotId} onChange={(e) => setStingerSlotId(e.target.value)}>
            <option value="">None — hard cut</option>
            {stingerSlots.map((slot) => (
              <option key={slot.id} value={slot.id}>
                {slot.label}
                {slot.ready === false ? " (converting)" : ""}
              </option>
            ))}
          </select>
        </label>
        <p className="hint">
          When this source is taken to Program, or Cut while it is on Preview, play the selected
          stinger. Other sources stay hard cuts.
        </p>
        {error ? <p className="error">{error}</p> : null}
        <div className="modal-actions">
          <button className="ghost" onClick={onClose}>
            Cancel
          </button>
          <button
            onClick={async () => {
              try {
                const payload: Record<string, unknown> = {
                  label,
                  kind,
                  stinger_slot_id: stingerSlotId || null,
                };
                if (kind === "mxl_live") {
                  // Only what changed: an unrouted live input has essences without flows, and
                  // sending null for them would make it invalid; a route keeps its domain.
                  if (groupHint !== (input.group_hint ?? "")) payload.group_hint = groupHint || null;
                  if (videoFlow !== (input.video?.flow_id ?? "")) {
                    payload.video = videoFlow ? { flow_id: videoFlow } : null;
                  }
                  if (audioFlow !== (input.audio?.flow_id ?? "")) {
                    payload.audio = audioFlow ? { flow_id: audioFlow } : null;
                  }
                }
                if (kind === "clip_player") {
                  payload.library_item_id = libraryItemId || null;
                  payload.loop = loop;
                  payload.play_on_cut = playOnCut;
                }
                await onSave(payload);
                onClose();
              } catch (err) {
                setError(err instanceof Error ? err.message : "Save failed");
              }
            }}
          >
            Save
          </button>
        </div>
      </div>
    </div>
  );
}
