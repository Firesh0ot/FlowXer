import { useEffect, useState } from "react";
import type { MixerStatus, ResourceInfo } from "../api";

function mb(bytes: number): string {
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function uptime(seconds: number): string {
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = Math.floor(seconds % 60);
  if (h) return `${h}h ${m}m ${s}s`;
  if (m) return `${m}m ${s}s`;
  return `${s}s`;
}

export function StatusChip({
  resources,
  mixer,
  formatId,
}: {
  resources: ResourceInfo;
  mixer: MixerStatus;
  formatId: string;
}) {
  const [open, setOpen] = useState(false);

  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open]);

  const issueCount = resources.issues.length;
  return (
    <div className="status-menu">
      <button
        type="button"
        className={`status-chip status-${resources.status}${open ? " open" : ""}`}
        aria-expanded={open}
        aria-haspopup="dialog"
        title="Show detailed status"
        onClick={() => setOpen((current) => !current)}
      >
        <span className="status-dot" aria-hidden="true" />
        <span>CPU {resources.cpu_percent.toFixed(0)}%</span>
        <span>RAM {resources.memory_percent.toFixed(0)}%</span>
        <span>{formatId}</span>
      </button>
      {open ? (
        <>
          <div className="status-overlay-backdrop" onClick={() => setOpen(false)} />
          <div className="status-overlay" role="dialog" aria-label="System status">
            <header>
              <h2>Status</h2>
              <span className={`status-badge status-${resources.status}`}>{resources.status}</span>
            </header>
            <dl>
              <dt>Mixer</dt>
              <dd>
                {mixer.state}
                {mixer.backend ? ` · ${mixer.backend}` : ""}
                {mixer.error ? ` · ${mixer.error}` : ""}
              </dd>
              <dt>CPU</dt>
              <dd>
                {resources.cpu_percent.toFixed(1)}% of {resources.cpu_count} cores
                {resources.load
                  ? ` · load ${resources.load.m1.toFixed(2)} / ${resources.load.m5.toFixed(2)} / ${resources.load.m15.toFixed(2)}`
                  : ""}
              </dd>
              <dt>Memory</dt>
              <dd>
                {mb(resources.memory_bytes)} / {mb(resources.memory_limit_bytes)} (
                {resources.memory_percent.toFixed(1)}%)
              </dd>
              <dt>Raster</dt>
              <dd>
                {mixer.raster} · {mixer.frame_rate} · {mixer.video_format}
              </dd>
              <dt>Format</dt>
              <dd>{formatId}</dd>
              <dt>WebRTC</dt>
              <dd>{mixer.webrtc_enabled ? "enabled" : "disabled"}</dd>
              {resources.uptime_s != null ? (
                <>
                  <dt>Uptime</dt>
                  <dd>{uptime(resources.uptime_s)}</dd>
                </>
              ) : null}
              {resources.pid != null ? (
                <>
                  <dt>PID</dt>
                  <dd>{resources.pid}</dd>
                </>
              ) : null}
            </dl>
            <h3>Issues {issueCount ? `(${issueCount})` : ""}</h3>
            {issueCount ? (
              <ul className="status-issues">
                {resources.issues.map((item, index) => (
                  <li key={`${item.level}-${index}`} className={`issue-${item.level}`}>
                    <strong>{item.level}</strong> {item.message}
                  </li>
                ))}
              </ul>
            ) : (
              <p className="hint">No issues</p>
            )}
          </div>
        </>
      ) : null}
    </div>
  );
}
