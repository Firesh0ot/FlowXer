import { useEffect, useState, type DragEvent } from "react";
import {
  api,
  uploadFileChunked,
  type ConvertJob,
  type LibraryItem,
} from "../api";

type UploadMode = "video" | "zip" | "folder";

/** Pictures upload as stills (a clip player holds one as a frame memory). */
const STILL_FILE = /\.(png|jpe?g|tiff?|bmp|webp|dpx|exr)$/i;

export function LibraryModal({
  kind,
  onClose,
  onChanged,
}: {
  kind: "clip" | "stinger";
  onClose: () => void;
  onChanged: () => Promise<void>;
}) {
  const [items, setItems] = useState<LibraryItem[]>([]);
  const [jobs, setJobs] = useState<ConvertJob[]>([]);
  const [query, setQuery] = useState("");
  const [error, setError] = useState<string | null>(null);
  // The upload under way: what goes up and how much of it has (0..1); 1 while the mixer takes it.
  const [upload, setUpload] = useState<{ name: string; progress: number } | null>(null);
  const [mode, setMode] = useState<UploadMode>("video");
  const [fit, setFit] = useState("fit");
  const [sequenceFps, setSequenceFps] = useState(50);
  const [cutFrame, setCutFrame] = useState(0);
  const [busy, setBusy] = useState(false);

  const refresh = async () => {
    // The clip library holds the stills too.
    const [listed, nextJobs] = await Promise.all([api.library(kind === "clip" ? undefined : kind, query), api.jobs()]);
    const nextItems = kind === "clip" ? listed.filter((item) => item.kind !== "stinger") : listed;
    setItems(nextItems);
    setJobs(nextJobs.filter((job) => nextItems.some((item) => item.id === job.item_id)));
  };

  useEffect(() => {
    void refresh().catch((err) => setError(err instanceof Error ? err.message : "load failed"));
    const timer = window.setInterval(() => {
      void refresh().catch(() => undefined);
    }, 1500);
    return () => window.clearInterval(timer);
  }, [kind, query]);

  // Conversions under way (the list keeps finished jobs; their items show how they ended).
  const activeJobs = jobs.filter((job) => job.state === "queued" || job.state === "running");
  const jobFor = (itemId: string) => activeJobs.find((job) => job.item_id === itemId);
  const itemName = (itemId: string) => items.find((item) => item.id === itemId)?.name ?? itemId.slice(0, 8);

  const onDrop = async (event: DragEvent) => {
    event.preventDefault();
    const files = Array.from(event.dataTransfer.files);
    if (!files.length) return;
    await ingestFiles(files);
  };

  const ingestFiles = async (files: File[]) => {
    setBusy(true);
    setError(null);
    try {
      if (kind === "stinger" && mode === "folder") {
        // TGA or PNG frames and one optional WAV (the stinger's sound).
        const sequence = files.filter((file) => /\.(tga|png|wav)$/i.test(file.name));
        if (!sequence.some((file) => /\.(tga|png)$/i.test(file.name))) throw new Error("Drop a folder of .tga or .png frames");
        const name = sequence[0].webkitRelativePath?.split("/")[0] || "sequence";
        setUpload({ name, progress: 0 });
        await api.uploadSequence(
          name,
          sequence,
          { sequence_fps: sequenceFps, cut_frame: cutFrame, fit },
          (ratio) => setUpload({ name, progress: ratio }),
        );
      } else {
        for (const [index, file] of files.entries()) {
          const name = files.length > 1 ? `${file.name} (${index + 1}/${files.length})` : file.name;
          setUpload({ name, progress: 0 });
          const uploadMode = kind === "stinger" && mode === "zip" ? "zip" : "video";
          await uploadFileChunked(
            file,
            kind === "clip" && STILL_FILE.test(file.name) ? "still" : kind,
            uploadMode,
            {
              fit,
              sequence_fps: kind === "stinger" ? sequenceFps : undefined,
              cut_frame: kind === "stinger" ? cutFrame : undefined,
            },
            (ratio) => setUpload({ name, progress: ratio }),
          );
        }
      }
      await refresh();
      await onChanged();
    } catch (err) {
      setError(err instanceof Error ? err.message : "upload failed");
    } finally {
      setUpload(null);
      setBusy(false);
    }
  };

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div
        className="modal library-modal"
        onClick={(event) => event.stopPropagation()}
        onDragOver={(event) => event.preventDefault()}
        onDrop={(event) => void onDrop(event)}
      >
        <h2>{kind === "clip" ? "Clip and still library" : "Stinger library"}</h2>
        <p className="hint">
          Upload converts in the background to an intra-frame mezzanine. Items show as converting
          until ready; the mixer is never blocked.
        </p>

        <div className="library-toolbar">
          <input
            placeholder="Search name or tags"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
          {kind === "stinger" ? (
            <select value={mode} onChange={(e) => setMode(e.target.value as UploadMode)}>
              <option value="video">Video file (with alpha)</option>
              <option value="folder">TGA/PNG folder (+ WAV)</option>
              <option value="zip">TGA/PNG zip (+ WAV)</option>
            </select>
          ) : null}
          <select value={fit} onChange={(e) => setFit(e.target.value)}>
            <option value="fit">Fit (letterbox)</option>
            <option value="fill">Fill (crop)</option>
            <option value="center">Center</option>
          </select>
          {kind === "stinger" ? (
            <>
              <label className="inline">
                FPS
                <input
                  type="number"
                  min={1}
                  step={0.01}
                  value={sequenceFps}
                  onChange={(e) => setSequenceFps(Number(e.target.value))}
                />
              </label>
              <label className="inline">
                Cut frame
                <input
                  type="number"
                  min={0}
                  step={1}
                  value={cutFrame}
                  onChange={(e) => setCutFrame(Number(e.target.value))}
                />
              </label>
            </>
          ) : null}
          <label className="file-btn">
            Browse…
            <input
              type="file"
              multiple={mode === "folder"}
              {...(mode === "folder" ? ({ webkitdirectory: "", directory: "" } as Record<string, string>) : {})}
              accept={
                mode === "zip"
                  ? ".zip"
                  : mode === "folder"
                    ? ".tga,.png,.wav"
                    : kind === "clip"
                      ? "video/*,image/*,.mov,.mxf,.mkv,.ts,.dpx,.exr"
                      : "video/*,.mov,.mxf,.mkv,.ts"
              }
              disabled={busy}
              onChange={(e) => {
                const list = e.target.files ? Array.from(e.target.files) : [];
                if (list.length) void ingestFiles(list);
                e.target.value = "";
              }}
            />
          </label>
        </div>

        {/* Stays at the top while the list scrolls: the upload, then each conversion under way. */}
        {upload || activeJobs.length ? (
          <div className="library-status">
            {upload ? (
              <ProgressBar
                value={upload.progress}
                label={
                  upload.progress < 1
                    ? `Uploading ${upload.name} · ${Math.floor(upload.progress * 100)} %`
                    : `Uploaded ${upload.name} · the mixer takes it…`
                }
              />
            ) : null}
            {activeJobs.map((job) => (
              <div key={job.id} className="job-row">
                <ProgressBar
                  value={job.progress}
                  label={`${job.state === "queued" ? "Queued" : "Converting"} ${itemName(job.item_id)} · ${Math.floor(job.progress * 100)} %`}
                />
                <button className="ghost" onClick={() => void api.cancelJob(job.id).then(refresh)}>
                  Cancel
                </button>
              </div>
            ))}
          </div>
        ) : null}

        <div className="library-grid">
          {items.map((item) => (
            <article key={item.id} className={`library-card status-${item.status}`}>
              {jobFor(item.id) ? (
                <ProgressBar value={jobFor(item.id)!.progress} label={`${Math.floor(jobFor(item.id)!.progress * 100)} %`} />
              ) : null}
              {item.thumb_url ? (
                <img src={item.thumb_url} alt="" />
              ) : (
                <div className="thumb-placeholder">{item.status}</div>
              )}
              <div>
                <strong>{item.name}</strong>
                <p>
                  {item.kind === "still" ? "still · " : ""}
                  {item.ready ? "ready" : item.status}
                  {item.playback !== "unknown" ? ` · ${item.playback}` : ""}
                  {item.frame_count ? ` · ${item.frame_count}f` : ""}
                  {item.kind === "stinger"
                    ? ` · ${item.has_alpha ? "alpha" : "opaque"}${item.has_sound ? " · sound" : ""} · cut ${item.cut_frame ?? 0}`
                    : ""}
                  {item.in_use ? " · in use" : ""}
                </p>
                {item.error ? <p className="error">{item.error}</p> : null}
              </div>
              <div className="card-actions">
                <button className="ghost" onClick={() => void api.libraryReconvert(item.id).then(refresh)}>
                  Reconvert
                </button>
                <button
                  className="ghost"
                  disabled={item.in_use}
                  onClick={() =>
                    void api
                      .libraryDelete(item.id)
                      .then(refresh)
                      .then(onChanged)
                      .catch((err) => setError(err instanceof Error ? err.message : "delete failed"))
                  }
                >
                  Delete
                </button>
              </div>
            </article>
          ))}
          {!items.length ? <p className="hint">No items yet — drop media here.</p> : null}
        </div>

        {error ? <p className="error">{error}</p> : null}
        <div className="modal-actions">
          <button className="ghost" onClick={onClose}>
            Close
          </button>
        </div>
      </div>
    </div>
  );
}

/** A bar with its text over it (an upload, a conversion). */
function ProgressBar({ value, label }: { value: number; label: string }) {
  const percent = Math.max(0, Math.min(100, value * 100));
  return (
    <div className="upload-progress" role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.floor(percent)}>
      <div style={{ width: `${percent}%` }} />
      <span>{label}</span>
    </div>
  );
}
