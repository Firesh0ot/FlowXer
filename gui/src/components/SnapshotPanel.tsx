import { useState } from "react";
import { api, type MixerPanel, type SnapshotInfo, type SnapshotOptions, type SnapshotsState } from "../api";

/** The recalls of a row's ⋯ menu that override the snapshot's own options once. */
const RECALLS: { label: string; options: SnapshotOptions }[] = [
  { label: "Recall: Preview only", options: { program: false, audio: false } },
  { label: "Recall: with Program/on-air", options: { program: true, audio: false } },
  { label: "Recall: with audio", options: { program: false, audio: true } },
  { label: "Recall: with Program/on-air and audio", options: { program: true, audio: true } },
];

/** SNAP (keyer row): the active ME's snapshots and those of all MEs. RECALL uses the snapshot's
 * options (PGM: Program/on-air, AUD: audio; click to change them), ⋯ recalls once with others,
 * renames, stores over, sets the start-up snapshot or deletes. */
export function SnapshotPanel({
  panel,
  snapshots,
  onCommand,
  onNotice,
}: {
  panel: MixerPanel;
  snapshots: SnapshotsState;
  onCommand: (fn: () => Promise<unknown>) => void;
  onNotice: (text: string) => void;
}) {
  const [menu, setMenu] = useState<number | null>(null);
  const [editing, setEditing] = useState<{ number: number; name: string } | null>(null);
  const own = snapshots.snapshots.filter((item) => item.panel_id === panel.id);
  const all = snapshots.snapshots.filter((item) => item.panel_id === "all");

  const recall = (item: SnapshotInfo, options?: SnapshotOptions) =>
    onCommand(async () => {
      const result = await api.recallSnapshot(item.number, options);
      onNotice(result.skipped.length ? `${item.name}: ${result.skipped.join(" · ")}` : `${item.name} recalled`);
    });
  const rename = () => {
    if (!editing) return;
    const { number, name } = editing;
    setEditing(null);
    if (name.trim()) onCommand(() => api.patchSnapshot(number, { name: name.trim() }));
  };
  const store = (panelId: string, label: string, item?: SnapshotInfo) => {
    if (item && !window.confirm(`Store the current look of ${label} over ${item.number} ${item.name}?`)) return;
    onCommand(async () => {
      const stored = await api.storeSnapshot(panelId, item?.number);
      onNotice(`${label} stored as ${stored.number} ${stored.name}`);
    });
  };

  const row = (item: SnapshotInfo) => {
    const label = item.panel_id === "all" ? "all MEs" : panel.label;
    const startup = snapshots.startup === item.number;
    return (
      <div key={item.number} className="snap-row">
        <span className="snap-number">{item.number}</span>
        {editing?.number === item.number ? (
          <input
            className="snap-name"
            autoFocus
            maxLength={40}
            value={editing.name}
            onChange={(event) => setEditing({ number: item.number, name: event.target.value })}
            onKeyDown={(event) => {
              if (event.key === "Enter") rename();
              if (event.key === "Escape") setEditing(null);
            }}
            onBlur={rename}
          />
        ) : (
          <span className="snap-name" title="Double click to rename" onDoubleClick={() => setEditing({ number: item.number, name: item.name })}>
            {item.name}
            {startup ? <span className="snap-start" title="Recalled when the mixer starts">START</span> : null}
          </span>
        )}
        <button
          className={`snap-flag ${item.options.program ? "on" : ""}`}
          title="RECALL also cuts Program, the keys on or off and FTB (Program/on-air)"
          onClick={() => onCommand(() => api.patchSnapshot(item.number, { options: { program: !item.options.program } }))}
        >
          PGM
        </button>
        <button
          className={`snap-flag ${item.options.audio ? "on" : ""}`}
          title="RECALL also sets the audio strips and master"
          onClick={() => onCommand(() => api.patchSnapshot(item.number, { options: { audio: !item.options.audio } }))}
        >
          AUD
        </button>
        <button className="snap-recall" title="Recall with its PGM and AUD" onClick={() => recall(item)}>
          RECALL
        </button>
        <button className="snap-more" title="More" onClick={() => setMenu(menu === item.number ? null : item.number)}>
          ⋯
        </button>
        {menu === item.number ? (
          <div className="snap-menu" role="menu" onClick={() => setMenu(null)}>
            {RECALLS.map((entry) => (
              <button key={entry.label} role="menuitem" onClick={() => recall(item, entry.options)}>
                {entry.label}
              </button>
            ))}
            <button role="menuitem" onClick={() => setEditing({ number: item.number, name: item.name })}>
              Rename
            </button>
            <button role="menuitem" onClick={() => store(item.panel_id, label, item)}>
              Store the current look of {label} here
            </button>
            <button role="menuitem" onClick={() => onCommand(() => api.startupSnapshot(startup ? null : item.number))}>
              {startup ? "No start-up snapshot" : "Recall when the mixer starts"}
            </button>
            <button
              role="menuitem"
              onClick={() => {
                if (window.confirm(`Delete snapshot ${item.number} ${item.name}?`)) onCommand(() => api.deleteSnapshot(item.number));
              }}
            >
              Delete
            </button>
          </div>
        ) : null}
      </div>
    );
  };

  return (
    <div className="snap-panel">
      <div className="snap-head">
        <span>{panel.label}</span>
        <button title={`Store the current look of ${panel.label} (the lowest free number)`} onClick={() => store(panel.id, panel.label)}>
          STORE
        </button>
      </div>
      {own.length ? own.map(row) : <div className="snap-empty">No snapshots of {panel.label} yet.</div>}
      <div className="snap-head">
        <span>All MEs</span>
        <button title="Store the current look of every ME (the lowest free number)" onClick={() => store("all", "all MEs")}>
          STORE
        </button>
      </div>
      {all.length ? all.map(row) : <div className="snap-empty">No snapshots of all MEs yet.</div>}
    </div>
  );
}
