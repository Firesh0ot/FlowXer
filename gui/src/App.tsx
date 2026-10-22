import { useEffect, useState, type MouseEvent } from "react";
import { api, type ConsoleState, type LogicalInput, type StingerSlot, type TallyReceiver, type WorkspaceConfig } from "./api";
import { AudioStrip, useAudioMeters } from "./components/AudioStrip";
import { LibraryModal } from "./components/LibraryModal";
import { BoxEditor } from "./components/BoxEditor";
import { Monitor } from "./components/Monitor";
import { KeyerControl } from "./components/KeyerControl";
import { MeTile } from "./components/MeTile";
import { previewSession } from "./preview";
import { SettingsModal } from "./components/SettingsModal";
import { SnapshotPanel } from "./components/SnapshotPanel";
import { SourceSettingsModal } from "./components/SourceSettingsModal";
import { SourceTile } from "./components/SourceTile";
import { StingerSettingsModal } from "./components/StingerSettingsModal";
import { StatusChip } from "./components/StatusChip";
import { TallySettingsModal } from "./components/TallySettingsModal";
import { TransitionSection } from "./components/TransitionSection";

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
  // A picture's context menu (GRAB) at the mouse, and the toast that names a new still.
  const [grabMenu, setGrabMenu] = useState<{ source: string; label: string; x: number; y: number } | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  // The SNAP panel (the active ME's snapshots and those of all MEs), open or closed.
  const [snapOpen, setSnapOpen] = useState(false);
  // The AUDIO strip of the active ME, open or closed (remembered in the browser).
  const [audioOpen, setAudioOpen] = useState(() => {
    try {
      return localStorage.getItem("flowxer.audioOpen") === "1";
    } catch {
      return false;
    }
  });
  const meters = useAudioMeters(audioOpen);
  const toggleAudio = () =>
    setAudioOpen((open) => {
      try {
        localStorage.setItem("flowxer.audioOpen", open ? "0" : "1");
      } catch {
        // Not remembered.
      }
      return !open;
    });

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
    if (!notice) return;
    const timer = window.setTimeout(() => setNotice(null), 4000);
    return () => window.clearTimeout(timer);
  }, [notice]);

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
  const format = snapshot.formats.find((item) => item.id === snapshot.workspace.format_id);
  const fps = framesPerSecond(format?.frame_rate);
  // The active ME's boxes (upstream keyers with a box), for the box editor over its Program.
  const boxes = snapshot.keyers.filter(
    (keyer) => keyer.panel_id === panel.id && keyer.stage === "upstream" && (keyer.mode === "pip" || keyer.dme.enabled),
  );
  // A luma key may key any source the ME may take: the inputs and the Programs of higher MEs.
  // The higher MEs, whose Programs the active ME may take (re-entry).
  const higherPanels = snapshot.panels.slice(snapshot.panels.indexOf(panel) + 1);
  const keySources = [
    ...snapshot.inputs.map((item) => ({ id: item.id, label: item.label })),
    ...higherPanels.map((item) => ({ id: item.id, label: item.label })),
  ];
  const playable = (snapshot.library ?? []).filter((item) => item.ready && (item.kind === "clip" || item.kind === "still"));
  const players = snapshot.inputs.filter((item) => item.kind === "clip_player");
  // A frame grab (the next grain) into the library, optionally into a clip player (frame memory).
  const grab = (source: string, player?: string) =>
    void command(async () => {
      const item = await api.grab(source, player);
      const into = players.find((entry) => entry.id === player);
      setNotice(`${item.name}${into ? ` → ${into.label}` : ""}`);
    });
  const openGrab = (source: string, label: string) => (event: MouseEvent) => {
    event.preventDefault();
    setGrabMenu({ source, label, x: event.clientX, y: event.clientY });
  };

  return (
    <div className={`console ${audioOpen && snapshot.audio ? "with-audio" : ""}`}>
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
            <Monitor
              tileId={`panel:${panel.id}:pvw`}
              label={panel.preview_input_id ?? "NO SRC"}
              tally="pvw"
              onMenu={openGrab(`${panel.id}:pvw`, `${panel.label} PVW`)}
            />
          </div>
          <div className="bus-column">
            <h2>Program</h2>
            <Monitor
              tileId={`panel:${panel.id}:pgm`}
              label={panel.program_input_id ?? "NO SRC"}
              tally="pgm"
              onMenu={openGrab(panel.id, `${panel.label} PGM`)}
            >
              {boxes.length > 0 && format ? (
                <BoxEditor
                  keyers={boxes}
                  width={format.width}
                  height={format.height}
                  onDone={() => void refresh()}
                  onError={setError}
                />
              ) : null}
            </Monitor>
          </div>
        </div>
        <div className="aux-row">
          {/* The active ME's upstream keyers, then its DSKs (keys show on its Program only). */}
          {snapshot.keyers
            .filter((keyer) => keyer.panel_id === panel.id)
            .map((keyer) => (
              <KeyerControl
                key={keyer.id}
                keyer={keyer}
                keyState={snapshot.sources?.[keyer.id]?.state}
                sources={keySources}
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
          {snapshot.snapshots ? (
            <div className="snap-wrap">
              <button className={`snap-toggle ${snapOpen ? "on" : ""}`} title="Snapshots of the active ME and of all MEs" onClick={() => setSnapOpen(!snapOpen)}>
                SNAP
              </button>
              {snapOpen ? (
                <SnapshotPanel panel={panel} snapshots={snapshot.snapshots} onCommand={(fn) => void command(fn)} onNotice={setNotice} />
              ) : null}
            </div>
          ) : null}
          {snapshot.audio ? (
            <button className={`audio-toggle ${audioOpen ? "on" : ""}`} title="The active ME's audio strip" onClick={toggleAudio}>
              AUDIO
            </button>
          ) : null}
        </div>
      </section>

      <section className="deck">
        <div
          className="source-strip"
          data-aspect={snapshot.workspace.source_tile_aspect ?? "16:9"}
          style={{
            gridTemplateColumns: `repeat(${sourceStripColumns(
              snapshot.inputs.length + higherPanels.length,
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
              onAir={snapshot.on_air?.[panel.id]?.includes(input.id)}
              library={playable}
              fps={fps}
              onPreview={() => void command(() => api.preview(input.id, panel.id))}
              onProgram={() => void command(() => api.take(input.id, panel.id))}
              onSettings={() => setSourceEdit(input)}
              onPlayer={(fn) => void command(fn)}
              onGrab={openGrab(input.id, input.label)}
            />
          ))}
          {higherPanels.map((item) => (
            <MeTile
              key={item.id}
              me={item}
              panel={panel}
              onAir={snapshot.on_air?.[panel.id]?.includes(item.id)}
              onPreview={() => void command(() => api.preview(item.id, panel.id))}
              onProgram={() => void command(() => api.take(item.id, panel.id))}
              onGrab={openGrab(item.id, `${item.label} PGM`)}
            />
          ))}
        </div>
        <TransitionSection
          panel={panel}
          keyers={snapshot.keyers.filter((keyer) => keyer.panel_id === panel.id && keyer.stage === "upstream")}
          stingerSlots={snapshot.stinger_slots}
          fps={fps}
          onPosition={(position) => void api.position(panel.id, position).catch((err: Error) => setError(err.message))}
          onChange={(patch) => void command(() => api.transition(panel.id, patch))}
          onCut={() => void command(() => api.cut(panel.id))}
          onAuto={() => void command(() => api.auto(panel.id))}
          onFadeToBlack={() => void command(() => api.fadeToBlack(panel.id))}
          onGrab={() => grab(panel.id)}
        />
      </section>

      {audioOpen && snapshot.audio ? (
        <section className="audio-section">
          <AudioStrip panelId={panel.id} audio={snapshot.audio} meters={meters} onChanged={() => void refresh()} onError={setError} />
        </section>
      ) : null}

      {grabMenu ? (
        <>
          <div className="grab-menu-backdrop" onClick={() => setGrabMenu(null)} onContextMenu={(event) => {
            event.preventDefault();
            setGrabMenu(null);
          }} />
          <div className="grab-menu" role="menu" style={{ left: grabMenu.x, top: grabMenu.y }}>
            <div className="title">{grabMenu.label}</div>
            <button
              role="menuitem"
              onClick={() => {
                grab(grabMenu.source);
                setGrabMenu(null);
              }}
            >
              Grab still
            </button>
            {players.map((player) => (
              <button
                key={player.id}
                role="menuitem"
                onClick={() => {
                  grab(grabMenu.source, player.id);
                  setGrabMenu(null);
                }}
              >
                Grab to {player.label}
              </button>
            ))}
          </div>
        </>
      ) : null}
      {notice ? <div className="toast notice">{notice}</div> : null}
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
          audio={snapshot.audio}
          fps={fps}
          onAudio={async (payload) => {
            await api.patchAudioInput(sourceEdit.id, payload);
          }}
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
