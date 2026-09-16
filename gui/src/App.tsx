import { useEffect, useState } from "react";
import { api, type ConsoleState, type LogicalInput, type StingerSlot, type WorkspaceConfig } from "./api";
import { Monitor } from "./components/Monitor";
import { SettingsModal } from "./components/SettingsModal";
import { SourceSettingsModal } from "./components/SourceSettingsModal";
import { SourceTile } from "./components/SourceTile";
import { StingerSettingsModal } from "./components/StingerSettingsModal";
import { TransitionBank } from "./components/TransitionBank";

/** How many tiles per row: 2 | 2×2 | 3+3 | 4+4. */
function sourceStripColumns(count: number): number {
  if (count <= 2) return Math.max(count, 1);
  if (count <= 4) return 2;
  if (count <= 6) return 3;
  return 4;
}

export default function App() {
  const [snapshot, setSnapshot] = useState<ConsoleState | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [sourceEdit, setSourceEdit] = useState<LogicalInput | null>(null);
  const [stingerEdit, setStingerEdit] = useState<StingerSlot | null>(null);
  const [activePanel, setActivePanel] = useState("me-1");
  const [menu, setMenu] = useState<string | null>(null);

  const refresh = async () => {
    try {
      const next = await api.console();
      setSnapshot(next);
      setError(null);
      setActivePanel((current) =>
        next.panels.some((panel) => panel.id === current) ? current : next.panels[0]?.id ?? "me-1",
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : "console unreachable");
    }
  };

  const command = async (fn: () => Promise<unknown>) => {
    try {
      await fn();
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "command failed");
    }
  };

  useEffect(() => {
    void refresh();
    const stinging =
      snapshot?.mixer.stinger.phase === "playing" || snapshot?.mixer.stinger.phase === "cut";
    const timer = window.setInterval(() => void refresh(), stinging ? 200 : 1000);
    return () => window.clearInterval(timer);
  }, [snapshot?.mixer.stinger.phase]);

  if (!snapshot) {
    return <div className="boot">{error ?? "Connecting to vision mixer…"}</div>;
  }

  const panel = snapshot.panels.find((item) => item.id === activePanel) ?? snapshot.panels[0];
  const webrtc = snapshot.webrtc.enabled;
  const issue = snapshot.resources.issues[0];

  return (
    <div className="console">
      <header className="menu-bar">
        <div className="brand">
          <span className="mark">FX</span>
          <strong>FlowXer</strong>
        </div>
        <nav>
          {["File", "Settings", "Help"].map((name) => (
            <div key={name} className="menu">
              <button onClick={() => setMenu(menu === name ? null : name)}>{name}</button>
              {menu === name ? (
                <div className="dropdown">
                  {name === "File" ? (
                    <>
                      <button
                        onClick={() => {
                          void (snapshot.mixer.state === "running" ? api.stop() : api.start()).then(refresh);
                          setMenu(null);
                        }}
                      >
                        {snapshot.mixer.state === "running" ? "Take mixer off-air" : "Take mixer on-air"}
                      </button>
                    </>
                  ) : null}
                  {name === "Settings" ? (
                    <button
                      onClick={() => {
                        setSettingsOpen(true);
                        setMenu(null);
                      }}
                    >
                      Console layout…
                    </button>
                  ) : null}
                  {name === "Help" ? (
                    <>
                      <a href="/docs" target="_blank" rel="noreferrer">
                        Mixer OpenAPI
                      </a>
                      <a href="https://github.com/dmf-mxl/mxl" target="_blank" rel="noreferrer">
                        EBU MXL SDK
                      </a>
                    </>
                  ) : null}
                </div>
              ) : null}
            </div>
          ))}
        </nav>
        <div className={`resource-band status-${snapshot.resources.status}`}>
          <span>CPU {snapshot.resources.cpu_percent.toFixed(0)}%</span>
          <span>
            RAM {snapshot.resources.memory_percent.toFixed(0)}% (
            {(snapshot.resources.memory_bytes / 1024 / 1024).toFixed(0)} MB)
          </span>
          <span>
            {snapshot.mixer.raster} · {snapshot.mixer.frame_rate} · {snapshot.mixer.video_format}
          </span>
          <span className="issue">
            {issue ? `${issue.level}: ${issue.message}` : "No issues"}
          </span>
        </div>
      </header>

      <section className="me-row">
        {snapshot.panels.length > 1 ? (
          <div className="me-tabs">
            {snapshot.panels.map((item) => (
              <button
                key={item.id}
                className={item.id === panel.id ? "active" : ""}
                onClick={() => setActivePanel(item.id)}
              >
                {item.label}
              </button>
            ))}
          </div>
        ) : null}
        <div className="monitors">
          <div className="bus-column">
            <h2>Preview</h2>
            <Monitor
              streamId={`panel:${panel.id}:pvw`}
              webrtc={webrtc}
              label={panel.preview_input_id ?? "NO SRC"}
              tally="pvw"
            />
          </div>
          <div className="bus-column">
            <h2>Program</h2>
            <Monitor
              streamId={`panel:${panel.id}:pgm`}
              webrtc={webrtc}
              label={panel.program_input_id ?? "NO SRC"}
              tally="pgm"
            />
          </div>
        </div>
        <div className="aux-row">
          {snapshot.keyers.map((keyer) => (
            <button
              key={keyer.id}
              className={keyer.enabled ? "on" : ""}
              onClick={() => void api.patchKeyer(keyer.id, { enabled: !keyer.enabled }).then(refresh)}
            >
              {keyer.label} {keyer.enabled ? "ON" : "OFF"}
            </button>
          ))}
          {snapshot.stinger_slots.map((slot) => (
            <div key={slot.id} className="stinger-chip">
              <button
                onClick={() => {
                  const target =
                    slot.role === "out"
                      ? snapshot.mixer.preview_input_id ?? snapshot.inputs[0]?.id
                      : snapshot.inputs.find((item) => item.kind === "replay")?.id ??
                        snapshot.inputs[0]?.id;
                  if (!target) return;
                  void command(() =>
                    api.stingerPlay(slot.stinger_id, target, slot.role === "out" ? "to_live" : "to_replay"),
                  );
                }}
              >
                {slot.label}
              </button>
              <button className="gear" title="Stinger parameters" onClick={() => setStingerEdit(slot)}>
                ⚙
              </button>
            </div>
          ))}
        </div>
      </section>

      <section className="deck">
        <div
          className="source-strip"
          data-aspect={snapshot.workspace.source_tile_aspect ?? "16:9"}
          style={{ gridTemplateColumns: `repeat(${sourceStripColumns(snapshot.inputs.length)}, minmax(0, 1fr))` }}
        >
          {snapshot.inputs.map((input) => (
            <SourceTile
              key={input.id}
              input={input}
              panel={panel}
              webrtc={webrtc}
              onPreview={() => void command(() => api.preview(input.id, panel.id))}
              onProgram={() => void command(() => api.take(input.id, panel.id))}
              onSettings={() => setSourceEdit(input)}
            />
          ))}
        </div>
        <TransitionBank
          panel={panel}
          stingerPhase={snapshot.mixer.stinger.phase}
          onCut={() => void command(() => api.cut(panel.id))}
          onFade={() => void command(() => api.fade(panel.id))}
          onFadeToBlack={() => void command(() => api.fadeToBlack(panel.id))}
          onWipe={() => void command(() => api.wipe(panel.id))}
        />
      </section>

      {error ? <div className="toast">{error}</div> : null}

      {settingsOpen ? (
        <SettingsModal
          console={snapshot}
          onClose={() => setSettingsOpen(false)}
          onApply={async (payload) => {
            const patch: Partial<WorkspaceConfig> = {};
            (Object.keys(payload) as (keyof WorkspaceConfig)[]).forEach((key) => {
              if (payload[key] !== snapshot.workspace[key]) {
                (patch as Record<string, unknown>)[key] = payload[key];
              }
            });
            if (Object.keys(patch).length === 0) return;
            const displayOnly = Object.keys(patch).every((key) => key === "source_tile_aspect");
            if (snapshot.mixer.state === "running" && !displayOnly) await api.stop();
            await api.workspace(patch);
            await refresh();
          }}
        />
      ) : null}
      {sourceEdit ? (
        <SourceSettingsModal
          input={sourceEdit}
          clips={snapshot.clips}
          onClose={() => setSourceEdit(null)}
          onSave={async (payload) => {
            await api.patchInput(sourceEdit.id, payload);
            await refresh();
          }}
        />
      ) : null}
      {stingerEdit ? (
        <StingerSettingsModal
          slot={stingerEdit}
          console={snapshot}
          onClose={() => setStingerEdit(null)}
          onSave={async (payload) => {
            await api.patchStingerSlot(stingerEdit.id, payload);
            await refresh();
          }}
        />
      ) : null}
    </div>
  );
}
