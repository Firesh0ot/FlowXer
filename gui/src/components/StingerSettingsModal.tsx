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
  const [cutFrame, setCutFrame] = useState(slot.cut_frame ?? asset?.cut_frame ?? 0);
  const [durationFrames, setDurationFrames] = useState(() => {
    if (asset?.frame_count) return asset.frame_count;
    const ms = asset?.duration_ms ?? 0;
    return ms ? Math.max(1, Math.round((ms / 1000) * fps)) : Math.round(fps);
  });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const selected = snapshot.stingers.find((item) => item.id === stingerId) ?? asset;
  const frameCount =
    kind === "video" ? Math.max(1, Number(durationFrames) || 1) : Math.max(1, selected?.frame_count ?? 1);
  const cut = Math.max(0, Math.min(Number(cutFrame) || 0, Math.max(frameCount - 1, 0)));

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal" onClick={(event) => event.stopPropagation()}>
        <h2>{slot.label}</h2>
        <p className="hint">
          Choose a TGA sequence or a video, then set the frame when program cuts under the sting.
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
                if (next) setCutFrame(next.cut_frame ?? 0);
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
              Duration (frames)
              <input
                type="number"
                min={1}
                step={1}
                value={durationFrames}
                onChange={(e) => setDurationFrames(Number(e.target.value))}
              />
            </label>
          </>
        )}
        <label>
          Cut at (frame)
          <input
            type="number"
            min={0}
            step={1}
            max={Math.max(frameCount - 1, 0)}
            value={cut}
            onChange={(e) => setCutFrame(Number(e.target.value))}
          />
        </label>
        <input
          type="range"
          min={0}
          step={1}
          max={Math.max(frameCount - 1, 0)}
          value={cut}
          onChange={(e) => setCutFrame(Number(e.target.value))}
        />
        <p className="hint">
          Program switches at frame {cut} of {frameCount} @ {fps.toFixed(0)} fps.
        </p>
        {error ? <p className="error">{error}</p> : null}
        <div className="modal-actions">
          <button className="ghost" onClick={onClose}>
            Cancel
          </button>
          <button
            disabled={busy}
            onClick={async () => {
              setBusy(true);
              setError(null);
              try {
                const payload: Record<string, unknown> = { label, kind, cut_frame: cut };
                if (kind === "sequence") {
                  payload.stinger_id = stingerId;
                } else {
                  if (!videoPath) throw new Error("Select a video file");
                  payload.media_path = videoPath;
                  payload.duration_ms = Math.max(1, Math.round((frameCount / fps) * 1000));
                }
                await onSave(payload);
                onClose();
              } catch (err) {
                setError(err instanceof Error ? err.message : "Save failed");
              } finally {
                setBusy(false);
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
