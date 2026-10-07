import { useState } from "react";
import type { ConsoleState, LibraryItem, StingerSlot } from "../api";

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
  libraryStingers,
  onClose,
  onSave,
  onOpenLibrary,
}: {
  slot: StingerSlot;
  console: ConsoleState;
  libraryStingers: LibraryItem[];
  onClose: () => void;
  onSave: (payload: Record<string, unknown>) => Promise<void>;
  onOpenLibrary: () => void;
}) {
  const asset = snapshot.stingers.find((item) => item.id === slot.stinger_id) ?? snapshot.stingers[0];
  const fps = fpsFrom(snapshot);
  const [label, setLabel] = useState(slot.label);
  const [libraryItemId, setLibraryItemId] = useState(slot.library_item_id ?? "");
  const [kind, setKind] = useState<"sequence" | "video" | "library">(
    slot.library_item_id ? "library" : (slot.kind as "sequence" | "video") || asset?.kind || "sequence",
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

  const selectedLib = libraryStingers.find((item) => item.id === libraryItemId);
  const selected = snapshot.stingers.find((item) => item.id === stingerId) ?? asset;
  const frameCount =
    kind === "library"
      ? Math.max(1, selectedLib?.frame_count || 1)
      : kind === "video"
        ? Math.max(1, Number(durationFrames) || 1)
        : Math.max(1, selected?.frame_count ?? 1);
  const cut = Math.max(0, Math.min(Number(cutFrame) || 0, Math.max(frameCount - 1, 0)));
  const cutThumb = selectedLib?.thumb_url;

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal" onClick={(event) => event.stopPropagation()}>
        <h2>{slot.label}</h2>
        <p className="hint">
          Prefer a library stinger (converted mezzanine). Legacy TGA sequences and raw videos remain
          available. Program cuts at the chosen frame.
        </p>
        <label>
          Name
          <input value={label} onChange={(e) => setLabel(e.target.value)} />
        </label>
        <label>
          Media
          <select
            value={kind}
            onChange={(e) => setKind(e.target.value as "sequence" | "video" | "library")}
          >
            <option value="library">Library item</option>
            <option value="sequence">Legacy TGA sequence</option>
            <option value="video">Legacy video file</option>
          </select>
        </label>
        {kind === "library" ? (
          <>
            <label>
              Stinger
              <select
                value={libraryItemId}
                onChange={(e) => {
                  const next = libraryStingers.find((item) => item.id === e.target.value);
                  setLibraryItemId(e.target.value);
                  if (next?.cut_frame != null) setCutFrame(next.cut_frame);
                }}
              >
                <option value="">Select library stinger</option>
                {libraryStingers.map((item) => (
                  <option key={item.id} value={item.id}>
                    {item.name}
                    {item.ready ? "" : " (converting)"} — {item.frame_count || "?"}f
                  </option>
                ))}
              </select>
            </label>
            <button type="button" className="ghost" onClick={onOpenLibrary}>
              Open stinger library…
            </button>
            {cutThumb ? (
              <div className="cut-preview">
                <img src={cutThumb} alt="Cut frame preview" />
                <span>Preview (thumb); cut at frame {cut}</span>
              </div>
            ) : null}
          </>
        ) : null}
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
        ) : null}
        {kind === "video" ? (
          <>
            <label>
              Video
              <select value={videoPath} onChange={(e) => setVideoPath(e.target.value)}>
                <option value="">Select video</option>
                {snapshot.clips
                  .filter((clip) => !clip.library_item_id)
                  .map((clip) => (
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
        ) : null}
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
          {selectedLib && !selectedLib.ready ? " This stinger is still converting — triggers hard-cut." : ""}
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
                const payload: Record<string, unknown> = { label, cut_frame: cut };
                if (kind === "library") {
                  if (!libraryItemId) throw new Error("Select a library stinger");
                  payload.library_item_id = libraryItemId;
                } else if (kind === "sequence") {
                  payload.kind = "sequence";
                  payload.stinger_id = stingerId;
                  payload.library_item_id = null;
                } else {
                  if (!videoPath) throw new Error("Select a video file");
                  payload.kind = "video";
                  payload.media_path = videoPath;
                  payload.duration_ms = Math.max(1, Math.round((frameCount / fps) * 1000));
                  payload.library_item_id = null;
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
