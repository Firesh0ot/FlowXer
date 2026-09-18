import { useState } from "react";
import type { TallyConfig, TallyPreset, TallyReceiver } from "../api";

function nextId(kind: TallyReceiver["kind"], existing: TallyReceiver[]): string {
  let n = 1;
  const ids = new Set(existing.map((item) => item.id));
  while (ids.has(`${kind}-${n}`)) n += 1;
  return `${kind}-${n}`;
}

function fromPreset(preset: TallyPreset, existing: TallyReceiver[]): TallyReceiver {
  return {
    id: nextId(preset.kind, existing),
    kind: preset.kind,
    label: preset.label,
    host: "",
    port: preset.port,
    transport: preset.transport,
    enabled: true,
    screen: 0,
    index_offset: 0,
  };
}

export function TallySettingsModal({
  tally,
  onClose,
  onSave,
  onRefresh,
}: {
  tally: TallyConfig;
  onClose: () => void;
  onSave: (receivers: TallyReceiver[]) => Promise<void>;
  onRefresh: () => Promise<void>;
}) {
  const presets = tally.presets.length
    ? tally.presets
    : ([
        { kind: "companion", label: "Bitfocus Companion", port: 8900, transport: "udp", hint: "" },
        { kind: "vsm", label: "Lawo VSM", port: 8900, transport: "udp", hint: "" },
        { kind: "bfe", label: "BFE Commander", port: 8900, transport: "udp", hint: "" },
        { kind: "hi", label: "Riedel HI", port: 8900, transport: "udp", hint: "" },
        { kind: "custom", label: "Custom TSL 5.0", port: 8900, transport: "udp", hint: "" },
      ] satisfies TallyPreset[]);
  const [receivers, setReceivers] = useState<TallyReceiver[]>(
    tally.receivers.map((item) => ({ ...item })),
  );
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const update = (id: string, patch: Partial<TallyReceiver>) =>
    setReceivers((current) => current.map((item) => (item.id === id ? { ...item, ...patch } : item)));

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal tally-modal" onClick={(event) => event.stopPropagation()}>
        <h2>Tally &amp; UMD</h2>
        <p className="hint">
          TSL UMD Protocol 5.0. Program lights the right-hand lamp red, Preview the left-hand lamp
          green, and the source name is the UMD label. Display INDEX is the source slot plus the
          offset. Works while the mixer is on-air.
        </p>
        <label>
          Add receiver
          <select
            defaultValue=""
            onChange={(event) => {
              const preset = presets.find((item) => item.kind === event.target.value);
              event.target.value = "";
              if (!preset) return;
              setReceivers((current) => [...current, fromPreset(preset, current)]);
            }}
          >
            <option value="">Choose Companion, VSM, BFE, Riedel HI…</option>
            {presets.map((preset) => (
              <option key={preset.kind} value={preset.kind}>
                {preset.label}
              </option>
            ))}
          </select>
        </label>
        {receivers.length === 0 ? (
          <p className="hint">No receivers yet. Add Companion, Lawo VSM, BFE Commander, or Riedel HI.</p>
        ) : null}
        {receivers.map((receiver) => (
          <fieldset key={receiver.id} className="tally-receiver">
            <legend>
              <label className="tally-enable">
                <input
                  type="checkbox"
                  checked={receiver.enabled}
                  onChange={(e) => update(receiver.id, { enabled: e.target.checked })}
                />
                {receiver.label}
              </label>
              <button
                type="button"
                className="ghost"
                onClick={() => setReceivers((current) => current.filter((item) => item.id !== receiver.id))}
              >
                Remove
              </button>
            </legend>
            <label>
              Kind
              <select
                value={receiver.kind}
                onChange={(e) => {
                  const kind = e.target.value as TallyReceiver["kind"];
                  const preset = presets.find((item) => item.kind === kind);
                  update(receiver.id, {
                    kind,
                    label: preset?.label ?? receiver.label,
                    port: preset?.port ?? receiver.port,
                    transport: preset?.transport ?? receiver.transport,
                  });
                }}
              >
                {presets.map((preset) => (
                  <option key={preset.kind} value={preset.kind}>
                    {preset.label}
                  </option>
                ))}
              </select>
            </label>
            <label>
              Name
              <input value={receiver.label} onChange={(e) => update(receiver.id, { label: e.target.value })} />
            </label>
            <div className="tally-grid">
              <label>
                Host
                <input
                  value={receiver.host}
                  placeholder="192.168.10.20"
                  onChange={(e) => update(receiver.id, { host: e.target.value.trim() })}
                />
              </label>
              <label>
                Port
                <input
                  type="number"
                  min={1}
                  max={65535}
                  value={receiver.port}
                  onChange={(e) => update(receiver.id, { port: Number(e.target.value) })}
                />
              </label>
              <label>
                Transport
                <select
                  value={receiver.transport}
                  onChange={(e) => update(receiver.id, { transport: e.target.value as "udp" | "tcp" })}
                >
                  <option value="udp">UDP</option>
                  <option value="tcp">TCP (DLE/STX)</option>
                </select>
              </label>
              <label>
                Screen
                <input
                  type="number"
                  min={0}
                  max={65534}
                  value={receiver.screen}
                  onChange={(e) => update(receiver.id, { screen: Number(e.target.value) })}
                />
              </label>
              <label>
                Index offset
                <input
                  type="number"
                  min={0}
                  max={65534}
                  value={receiver.index_offset}
                  onChange={(e) => update(receiver.id, { index_offset: Number(e.target.value) })}
                />
              </label>
            </div>
            {receiver.last_error ? <p className="error">{receiver.last_error}</p> : null}
          </fieldset>
        ))}
        {error ? <p className="error">{error}</p> : null}
        <div className="modal-actions">
          <button
            className="ghost"
            disabled={busy}
            onClick={async () => {
              setBusy(true);
              setError(null);
              try {
                await onSave(receivers);
                await onRefresh();
              } catch (err) {
                setError(err instanceof Error ? err.message : "Send failed");
              } finally {
                setBusy(false);
              }
            }}
          >
            Send now
          </button>
          <button className="ghost" onClick={onClose}>
            Cancel
          </button>
          <button
            disabled={busy}
            onClick={async () => {
              setBusy(true);
              setError(null);
              try {
                if (receivers.some((item) => item.enabled && !item.host)) {
                  throw new Error("Each enabled receiver needs a host address");
                }
                await onSave(receivers);
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
