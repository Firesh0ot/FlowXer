import { useState } from "react";
import type { ConsoleState, WorkspaceConfig } from "../api";

export function SettingsModal({
  console: snapshot,
  onClose,
  onApply,
}: {
  console: ConsoleState;
  onClose: () => void;
  onApply: (payload: Partial<WorkspaceConfig>) => Promise<void>;
}) {
  const [form, setForm] = useState<WorkspaceConfig>(snapshot.workspace);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const set = (key: keyof WorkspaceConfig, value: string | number) =>
    setForm((current) => ({ ...current, [key]: value }));

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal" onClick={(event) => event.stopPropagation()}>
        <h2>Settings</h2>
        <p className="hint">
          Mixer must be off-air to change format, source count, MEs, stingers or downstream keyers.
        </p>
        <label>
          Format
          <select value={form.format_id} onChange={(e) => set("format_id", e.target.value)}>
            {snapshot.formats.map((fmt) => (
              <option key={fmt.id} value={fmt.id}>
                {fmt.label}
              </option>
            ))}
          </select>
        </label>
        <label>
          Logical sources
          <input
            type="number"
            min={1}
            max={24}
            value={form.logical_source_count}
            onChange={(e) => set("logical_source_count", Number(e.target.value))}
          />
        </label>
        <label>
          Mixer panels (ME)
          <input
            type="number"
            min={1}
            max={4}
            value={form.mixer_panel_count}
            onChange={(e) => set("mixer_panel_count", Number(e.target.value))}
          />
        </label>
        <label>
          Stingers
          <select
            value={form.stinger_mode}
            onChange={(e) => set("stinger_mode", e.target.value)}
          >
            <option value="shared">Same sequence for IN and OUT</option>
            <option value="separate">Separate IN and OUT stingers</option>
          </select>
        </label>
        <label>
          Stinger count
          <input
            type="number"
            min={1}
            max={8}
            value={form.stinger_count}
            onChange={(e) => set("stinger_count", Number(e.target.value))}
          />
        </label>
        <label>
          Downstream keyers (HTML graphics)
          <input
            type="number"
            min={0}
            max={8}
            value={form.downstream_keyer_count}
            onChange={(e) => set("downstream_keyer_count", Number(e.target.value))}
          />
        </label>
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
                await onApply(form);
                onClose();
              } catch (err) {
                setError(err instanceof Error ? err.message : "Apply failed");
              } finally {
                setBusy(false);
              }
            }}
          >
            Apply
          </button>
        </div>
      </div>
    </div>
  );
}
