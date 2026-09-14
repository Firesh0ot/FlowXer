import { useState } from "react";
import type { LogicalInput } from "../api";

const KINDS = ["test", "black", "mxl_live", "file", "replay"] as const;

export function SourceSettingsModal({
  input,
  clips,
  onClose,
  onSave,
}: {
  input: LogicalInput;
  clips: { name: string }[];
  onClose: () => void;
  onSave: (payload: Record<string, unknown>) => Promise<void>;
}) {
  const [label, setLabel] = useState(input.label);
  const [kind, setKind] = useState(input.kind);
  const [groupHint, setGroupHint] = useState(input.group_hint ?? "");
  const [videoFlow, setVideoFlow] = useState(input.video?.flow_id ?? "");
  const [audioFlow, setAudioFlow] = useState(input.audio?.flow_id ?? "");
  const [filePath, setFilePath] = useState(input.file_path ?? clips[0]?.name ?? "");
  const [error, setError] = useState<string | null>(null);

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal" onClick={(event) => event.stopPropagation()}>
        <h2>{input.id}</h2>
        <label>
          Name
          <input value={label} onChange={(e) => setLabel(e.target.value)} />
        </label>
        <label>
          Kind
          <select value={kind} onChange={(e) => setKind(e.target.value as LogicalInput["kind"])}>
            {KINDS.map((item) => (
              <option key={item} value={item}>
                {item}
              </option>
            ))}
          </select>
        </label>
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
        {error ? <p className="error">{error}</p> : null}
        <div className="modal-actions">
          <button className="ghost" onClick={onClose}>
            Cancel
          </button>
          <button
            onClick={async () => {
              try {
                const payload: Record<string, unknown> = { label, kind };
                if (kind === "mxl_live") {
                  payload.group_hint = groupHint || null;
                  payload.video = videoFlow ? { flow_id: videoFlow } : null;
                  payload.audio = audioFlow ? { flow_id: audioFlow } : null;
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
