import { useEffect, useState } from "react";
import { api, type ConsoleState, type LogicalInput, type StingerSlot, type TallyReceiver, type WorkspaceConfig } from "./api";
import { LibraryModal } from "./components/LibraryModal";
import { Monitor } from "./components/Monitor";
import { DskControl } from "./components/DskControl";
import { previewSession } from "./preview";
import { SettingsModal } from "./components/SettingsModal";
import { SourceSettingsModal } from "./components/SourceSettingsModal";
import { SourceTile } from "./components/SourceTile";
import { StingerSettingsModal } from "./components/StingerSettingsModal";
import { StatusChip } from "./components/StatusChip";
import { TallySettingsModal } from "./components/TallySettingsModal";
import { TransitionBank } from "./components/TransitionBank";

/** Frames per second of a frame rate such as "50/1" or "60000/1001". */
function framesPerSecond(rate: string | undefined): number {
  const [num, den] = (rate ?? "50/1").split("/").map(Number);
  return num > 0 ? num / (den > 0 ? den : 1) : 50;
}

/** Landscape: 2 | 2×2 | 3+3 | 4+4. Portrait: one row so 9:16 tiles stay readable. */
function sourceStripColumns(count: number, aspect: string = "16:9"): number {
  if (aspect === "9:16") return Math.max(count, 1);
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
  const [tallyOpen, setTallyOpen] = useState(false);
  const [libraryKind, setLibraryKind] = useState<"clip" | "stinger" | null>(null);
  const [activePanel, setActivePanel] = useState("me-1");
  const [menu, setMenu] = useState<string | null>(null);

  const refresh = async () => {
    try {
      const next = await api.console();
      previewSession.setMap(next.preview);
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
  const fps = framesPerSecond(snapshot.formats.find((item) => item.id === snapshot.workspace.format_id)?.frame_rate);
  const playable = (snapshot.library ?? []).filter((item) => item.ready && (item.kind === "clip" || item.kind === "still"));

  return (
    <div className="console">
      <header className="menu-bar">
        <div className="brand">
          <span className="mark">FX</span>
          <strong>FlowXer</strong>
        </div>
        <nav>
          {["File", "Settings", "Tally", "Help"].map((name) => (
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
                      <button
                        onClick={() => {
                          setLibraryKind("clip");
                          setMenu(null);
                        }}
                      >
                        Clip and still library…
                      </button>
                      <button
                        onClick={() => {
                          setLibraryKind("stinger");
                          setMenu(null);
                        }}
                      >
                        Stinger library…
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
                  {name === "Tally" ? (
                    <button
                      onClick={() => {
                        setTallyOpen(true);
                        setMenu(null);
                      }}
                    >
                      Receivers…
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
        <StatusChip
          resources={snapshot.resources}
          mixer={snapshot.mixer}
          formatId={snapshot.workspace.format_id}
          nmos={snapshot.nmos ?? snapshot.mixer.nmos}
        />
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
            <Monitor tileId={`panel:${panel.id}:pvw`} label={panel.preview_input_id ?? "NO SRC"} tally="pvw" />
          </div>
          <div className="bus-column">
            <h2>Program</h2>
            <Monitor tileId={`panel:${panel.id}:pgm`} label={panel.program_input_id ?? "NO SRC"} tally="pgm" />
          </div>
        </div>
        <div className="aux-row">
          {snapshot.keyers.map((keyer) => (
            <DskControl
              key={keyer.id}
              keyer={keyer}
              keyState={snapshot.sources?.[keyer.id]?.state}
              fps={fps}
              onCommand={(fn) => void command(fn)}
            />
          ))}
          {snapshot.stinger_slots.map((slot) => (
            <div key={slot.id} className="stinger-chip">
              <button
                title="Sting Preview to Program"
                onClick={() => {
                  const target = panel.preview_input_id;
                  if (!target) return;
                  const direction = snapshot.inputs.find((item) => item.id === target)?.kind === "clip_player"
                    ? "to_replay"
                    : "to_live";
                  void command(() =>
                    api.stingerPlay(slot.stinger_id, target, direction, {
                      flip_flop: true,
                      panel_id: panel.id,
                    }),
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
          style={{
            gridTemplateColumns: `repeat(${sourceStripColumns(
              snapshot.inputs.length,
              snapshot.workspace.source_tile_aspect ?? "16:9",
            )}, minmax(0, 1fr))`,
          }}
        >
          {snapshot.inputs.map((input) => (
            <SourceTile
              key={input.id}
              input={input}
              panel={panel}
              runtime={snapshot.sources?.[input.id]}
              library={playable}
              fps={fps}
              onPreview={() => void command(() => api.preview(input.id, panel.id))}
              onProgram={() => void command(() => api.take(input.id, panel.id))}
              onSettings={() => setSourceEdit(input)}
              onPlayer={(fn) => void command(fn)}
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
          libraryItems={playable}
          stingerSlots={snapshot.stinger_slots}
          pinnedBy={snapshot.pinned?.logical_source_count}
          onClose={() => setSourceEdit(null)}
          onOpenLibrary={() => {
            setSourceEdit(null);
            setLibraryKind("clip");
          }}
          onSave={async (payload) => {
            await api.patchInput(sourceEdit.id, payload);
            setSourceEdit(null);
            await refresh();
          }}
        />
      ) : null}
      {stingerEdit ? (
        <StingerSettingsModal
          slot={stingerEdit}
          console={snapshot}
          libraryStingers={(snapshot.library ?? []).filter((item) => item.kind === "stinger")}
          onClose={() => setStingerEdit(null)}
          onOpenLibrary={() => {
            setStingerEdit(null);
            setLibraryKind("stinger");
          }}
          onSave={async (payload) => {
            await api.patchStingerSlot(stingerEdit.id, payload);
            setStingerEdit(null);
            await refresh();
          }}
        />
      ) : null}
      {libraryKind ? (
        <LibraryModal
          kind={libraryKind}
          onClose={() => setLibraryKind(null)}
          onChanged={refresh}
        />
      ) : null}
      {tallyOpen ? (
        <TallySettingsModal
          tally={
            snapshot.tally ?? {
              protocol: "TSL UMD 5.0",
              receivers: [],
              presets: [],
            }
          }
          onClose={() => setTallyOpen(false)}
          onSave={async (receivers: TallyReceiver[]) => {
            await api.tallyReceivers(receivers);
            await refresh();
          }}
          onRefresh={async () => {
            await api.tallyRefresh();
            await refresh();
          }}
        />
      ) : null}
    </div>
  );
}
