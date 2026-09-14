import { useEffect, useState } from "react";
import { api, type ConsoleState, type LogicalInput } from "./api";
import { Monitor } from "./components/Monitor";
import { SettingsModal } from "./components/SettingsModal";
import { SourceSettingsModal } from "./components/SourceSettingsModal";
import { SourceTile } from "./components/SourceTile";

export default function App() {
  const [snapshot, setSnapshot] = useState<ConsoleState | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [sourceEdit, setSourceEdit] = useState<LogicalInput | null>(null);
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

  useEffect(() => {
    void refresh();
    const timer = window.setInterval(() => void refresh(), 1000);
    return () => window.clearInterval(timer);
  }, []);

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
            <button
              key={slot.id}
              onClick={() => {
                const target =
                  slot.role === "out"
                    ? snapshot.mixer.preview_input_id ?? snapshot.inputs[0]?.id
                    : snapshot.inputs.find((item) => item.kind === "replay")?.id ??
                      snapshot.inputs[0]?.id;
                if (!target) return;
                void api
                  .stingerPlay(slot.stinger_id, target, slot.role === "out" ? "to_live" : "to_replay")
                  .then(refresh);
              }}
            >
              {slot.label}
            </button>
          ))}
        </div>
      </section>

      <section className="source-strip">
        {snapshot.inputs.map((input) => (
          <SourceTile
            key={input.id}
            input={input}
            panel={panel}
            webrtc={webrtc}
            onPreview={() => void api.preview(input.id, panel.id).then(refresh)}
            onProgram={() => void api.take(input.id, panel.id).then(refresh)}
            onSettings={() => setSourceEdit(input)}
          />
        ))}
      </section>

      {error ? <div className="toast">{error}</div> : null}

      {settingsOpen ? (
        <SettingsModal
          console={snapshot}
          onClose={() => setSettingsOpen(false)}
          onApply={async (payload) => {
            if (snapshot.mixer.state === "running") await api.stop();
            await api.workspace(payload);
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
    </div>
  );
}
