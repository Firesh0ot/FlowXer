import { useEffect, useState, type DragEvent } from "react";
import {
  api,
  uploadFileChunked,
  type ConvertJob,
  type LibraryItem,
} from "../api";

type UploadMode = "video" | "zip" | "folder";

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
  const [progress, setProgress] = useState<number | null>(null);
  const [mode, setMode] = useState<UploadMode>("video");
  const [fit, setFit] = useState("fit");
  const [sequenceFps, setSequenceFps] = useState(50);
  const [cutFrame, setCutFrame] = useState(0);
  const [busy, setBusy] = useState(false);

  const refresh = async () => {
    const [nextItems, nextJobs] = await Promise.all([api.library(kind, query), api.jobs()]);
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

  const onDrop = async (event: DragEvent) => {
    event.preventDefault();
    const files = Array.from(event.dataTransfer.files);
    if (!files.length) return;
    await ingestFiles(files);
  };

  const ingestFiles = async (files: File[]) => {
    setBusy(true);
    setError(null);
    setProgress(0);
    try {
      if (kind === "stinger" && mode === "folder") {
        const tga = files.filter((file) => file.name.toLowerCase().endsWith(".tga"));
        if (!tga.length) throw new Error("Drop a folder of .tga frames");
        await api.uploadSequence(tga[0].webkitRelativePath?.split("/")[0] || "sequence", tga, {
          sequence_fps: sequenceFps,
          cut_frame: cutFrame,
          fit,
        });
      } else {
        for (const file of files) {
          const uploadMode = kind === "stinger" && mode === "zip" ? "zip" : "video";
          await uploadFileChunked(
            file,
            kind,
            uploadMode,
            {
              fit,
              sequence_fps: kind === "stinger" ? sequenceFps : undefined,
              cut_frame: kind === "stinger" ? cutFrame : undefined,
            },
            setProgress,
          );
        }
      }
      await refresh();
      await onChanged();
      setProgress(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "upload failed");
      setProgress(null);
    } finally {
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
        <h2>{kind === "clip" ? "Clip library" : "Stinger library"}</h2>
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
              <option value="video">Video file</option>
              <option value="folder">TGA folder</option>
              <option value="zip">TGA zip</option>
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
              accept={mode === "zip" ? ".zip" : mode === "folder" ? ".tga" : "video/*,.mov,.mxf,.mkv,.ts"}
              disabled={busy}
              onChange={(e) => {
                const list = e.target.files ? Array.from(e.target.files) : [];
                if (list.length) void ingestFiles(list);
                e.target.value = "";
              }}
            />
          </label>
        </div>

        {progress != null ? (
          <div className="upload-progress">
            <div style={{ width: `${Math.round(progress * 100)}%` }} />
            <span>Uploading {Math.round(progress * 100)}%</span>
          </div>
        ) : null}

        {jobs.length ? (
          <div className="job-list">
            {jobs.map((job) => (
              <div key={job.id} className="job-row">
                <span>
                  {job.item_id.slice(0, 8)} · {job.state} · {Math.round(job.progress * 100)}%
                </span>
                {job.state === "queued" || job.state === "running" ? (
                  <button className="ghost" onClick={() => void api.cancelJob(job.id).then(refresh)}>
                    Cancel
                  </button>
                ) : null}
              </div>
            ))}
          </div>
        ) : null}

        <div className="library-grid">
          {items.map((item) => (
            <article key={item.id} className={`library-card status-${item.status}`}>
              {item.thumb_url ? (
                <img src={item.thumb_url} alt="" />
              ) : (
                <div className="thumb-placeholder">{item.status}</div>
              )}
              <div>
                <strong>{item.name}</strong>
                <p>
                  {item.ready ? "ready" : item.status}
                  {item.playback !== "unknown" ? ` · ${item.playback}` : ""}
                  {item.frame_count ? ` · ${item.frame_count}f` : ""}
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
