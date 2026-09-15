import { useState } from "react";
import type { ConsoleState, StingerSlot } from "../api";

function fpsFrom(console: ConsoleState): number {
  const raw = console.mixer.frame_rate || "50/1";
  if (raw.includes("/")) {
    const [num, den] = raw.split("/");
    return Number(num) / Math.max(Number(den) || 1, 1);
  }
  return Number(raw) || 50;
}

export function StingerSettingsModal({
  slot,
  console: snapshot,
  onClose,
  onSave,
}: {
  slot: StingerSlot;
  console: ConsoleState;
  onClose: () => void;
  onSave: (payload: Record<string, unknown>) => Promise<void>;
}) {
  const asset = snapshot.stingers.find((item) => item.id === slot.stinger_id) ?? snapshot.stingers[0];
  const fps = fpsFrom(snapshot);
  const [label, setLabel] = useState(slot.label);
  const [kind, setKind] = useState<"sequence" | "video">(
    (slot.kind as "sequence" | "video") || asset?.kind || "sequence",
  );
  const [stingerId, setStingerId] = useState(slot.stinger_id);
  const [videoPath, setVideoPath] = useState(slot.media_path?.split("/").pop() ?? "");
  const [cutSeconds, setCutSeconds] = useState(() => {
    const ms = slot.cut_ms ?? asset?.cut_ms ?? 0;
    return (ms / 1000).toFixed(3);
  });
  const [durationSeconds, setDurationSeconds] = useState(() => {
    const ms = asset?.duration_ms ?? 0;
    return ms ? (ms / 1000).toFixed(3) : "1.000";
  });
  const [error, setError] = useState<string | null>(null);

  const selected = snapshot.stingers.find((item) => item.id === stingerId) ?? asset;
  const durationMs =
    kind === "video"
      ? Math.max(1, Math.round(Number(durationSeconds) * 1000))
      : selected?.duration_ms || Math.round(((selected?.frame_count ?? 1) / fps) * 1000);
  const cutMs = Math.max(0, Math.round(Number(cutSeconds) * 1000));
  const cutFrame = Math.round((cutMs / 1000) * fps);

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal" onClick={(event) => event.stopPropagation()}>
        <h2>{slot.label}</h2>
        <p className="hint">
          Choose a TGA sequence or a video, then set the time when program cuts under the sting.
        </p>
        <label>
          Name
          <input value={label} onChange={(e) => setLabel(e.target.value)} />
        </label>
        <label>
          Media
          <select value={kind} onChange={(e) => setKind(e.target.value as "sequence" | "video")}>
            <option value="sequence">TGA sequence</option>
            <option value="video">Video file</option>
          </select>
        </label>
        {kind === "sequence" ? (
          <label>
            Sequence
            <select
              value={stingerId}
              onChange={(e) => {
                const next = snapshot.stingers.find((item) => item.id === e.target.value);
                setStingerId(e.target.value);
                if (next) setCutSeconds(((next.cut_ms ?? 0) / 1000).toFixed(3));
              }}
            >
              {snapshot.stingers
                .filter((item) => item.kind !== "video")
                .map((item) => (
                  <option key={item.id} value={item.id}>
                    {item.id} ({item.frame_count} frames)
                  </option>
                ))}
            </select>
          </label>
        ) : (
          <>
            <label>
              Video
              <select value={videoPath} onChange={(e) => setVideoPath(e.target.value)}>
                <option value="">Select video</option>
                {snapshot.clips.map((clip) => (
                  <option key={clip.name} value={clip.name}>
                    {clip.name}
                  </option>
                ))}
              </select>
            </label>
            <label>
              Duration (seconds)
              <input
                type="number"
                min={0.04}
                step={0.001}
                value={durationSeconds}
                onChange={(e) => setDurationSeconds(e.target.value)}
              />
            </label>
          </>
        )}
        <label>
          Cut at (seconds)
          <input
            type="number"
            min={0}
            step={0.001}
            max={Math.max(durationMs / 1000, 0)}
            value={cutSeconds}
            onChange={(e) => setCutSeconds(e.target.value)}
          />
        </label>
        <input
          type="range"
          min={0}
          max={Math.max(durationMs, 1)}
          value={Math.min(cutMs, durationMs)}
          onChange={(e) => setCutSeconds((Number(e.target.value) / 1000).toFixed(3))}
        />
        <p className="hint">
          Program switches at {cutSeconds}s — frame {cutFrame} of{" "}
          {kind === "sequence" ? selected?.frame_count ?? "?" : Math.round((durationMs / 1000) * fps)} @{" "}
          {fps.toFixed(0)} fps ({cutMs} ms).
        </p>
        {error ? <p className="error">{error}</p> : null}
        <div className="modal-actions">
          <button className="ghost" onClick={onClose}>
            Cancel
          </button>
          <button
            onClick={async () => {
              try {
                const payload: Record<string, unknown> = { label, kind, cut_ms: cutMs };
                if (kind === "sequence") {
                  payload.stinger_id = stingerId;
                } else {
                  if (!videoPath) throw new Error("Select a video file");
                  payload.media_path = videoPath;
                  payload.duration_ms = durationMs;
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
