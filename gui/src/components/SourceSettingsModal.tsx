import { useState } from "react";
import type { LogicalInput, StingerSlot } from "../api";

const KINDS = ["test", "black", "mxl_live", "file", "replay"] as const;

export function SourceSettingsModal({
  input,
  clips,
  stingerSlots,
  pinnedBy,
  onClose,
  onSave,
}: {
  input: LogicalInput;
  clips: { name: string }[];
  stingerSlots: StingerSlot[];
  /** Variables that set the inputs' ids, kinds and labels, when the environment does. */
  pinnedBy?: string;
  onClose: () => void;
  onSave: (payload: Record<string, unknown>) => Promise<void>;
}) {
  const [label, setLabel] = useState(input.label);
  const [kind, setKind] = useState(input.kind);
  const [groupHint, setGroupHint] = useState(input.group_hint ?? "");
  const [videoFlow, setVideoFlow] = useState(input.video?.flow_id ?? "");
  const [audioFlow, setAudioFlow] = useState(input.audio?.flow_id ?? "");
  const [filePath, setFilePath] = useState(input.file_path ?? clips[0]?.name ?? "");
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
        {kind === "file" || kind === "replay" ? (
          <label>
            Clip
            <select value={filePath} onChange={(e) => setFilePath(e.target.value)}>
              <option value="">Select clip</option>
              {clips.map((clip) => (
                <option key={clip.name} value={clip.name}>
                  {clip.name}
                </option>
              ))}
            </select>
          </label>
        ) : null}
        <label>
          Auto stinger
          <select value={stingerSlotId} onChange={(e) => setStingerSlotId(e.target.value)}>
            <option value="">None — hard cut</option>
            {stingerSlots.map((slot) => (
              <option key={slot.id} value={slot.id}>
                {slot.label}
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
                if ((kind === "file" || kind === "replay") && filePath) {
                  payload.file_path = filePath;
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
