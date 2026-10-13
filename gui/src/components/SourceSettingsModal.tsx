import { useState } from "react";
import type { AudioState, ClipEnd, LibraryItem, LogicalInput, StingerSlot } from "../api";

const KINDS = ["mxl_live", "clip_player", "black"] as const;

export function SourceSettingsModal({
  input,
  audio,
  fps,
  onAudio,
  libraryItems,
  stingerSlots,
  pinnedBy,
  onClose,
  onSave,
  onOpenLibrary,
}: {
  input: LogicalInput;
  /** The audio settings (P5): a live input's delay and channel map are in its Audio section. */
  audio?: AudioState;
  fps: number;
  onAudio: (payload: { delay_ms?: number; channel_map?: number[] }) => Promise<void>;
  /** Ready clips and stills of the library. */
  libraryItems: LibraryItem[];
  stingerSlots: StingerSlot[];
  /** Variables that set the inputs' ids, kinds and labels, when the environment does. */
  pinnedBy?: string;
  onClose: () => void;
  onSave: (payload: Record<string, unknown>) => Promise<void>;
  onOpenLibrary: () => void;
}) {
  const [label, setLabel] = useState(input.label);
  const [kind, setKind] = useState(input.kind);
  const [groupHint, setGroupHint] = useState(input.group_hint ?? "");
  const [videoFlow, setVideoFlow] = useState(input.video?.flow_id ?? "");
  const [audioFlow, setAudioFlow] = useState(input.audio?.flow_id ?? "");
  const [libraryItemId, setLibraryItemId] = useState(input.library_item_id ?? "");
  const [loop, setLoop] = useState(Boolean(input.loop));
  const [playOnCut, setPlayOnCut] = useState(Boolean(input.play_on_cut));
  const [onEnd, setOnEnd] = useState<ClipEnd>(input.on_end ?? "off");
  const [stingerSlotId, setStingerSlotId] = useState(input.stinger_slot_id ?? "");
  const [error, setError] = useState<string | null>(null);
  const inputAudio = audio?.inputs[input.id];
  const pgmChannels = audio?.channels ?? 2;
  const flowChannels = inputAudio?.channels ?? input.audio?.channels ?? 2;
  const [delayMs, setDelayMs] = useState(inputAudio?.delay_ms ?? 0);
  // Per PGM channel the input channel (1-based, 0 silent); the default is 1:1.
  const identity = Array.from({ length: pgmChannels }, (_, c) => (flowChannels === 1 ? 1 : c < flowChannels ? c + 1 : 0));
  const [channelMap, setChannelMap] = useState<number[]>(inputAudio?.channel_map?.length ? inputAudio.channel_map : identity);
  const frameMs = 1000 / fps;
  const maxDelay = audio?.max_delay_ms ?? 1000;
  const stepDelay = (ms: number) => setDelayMs((d) => Math.min(maxDelay, Math.max(0, Math.round((d + ms) * 48) / 48)));

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal" onClick={(event) => event.stopPropagation()}>
        <h2>{input.id}</h2>
        <label>
          Name
          <input
            value={label}
            disabled={Boolean(pinnedBy)}
            onChange={(e) => setLabel(e.target.value)}
          />
        </label>
        <label>
          Kind
          <select
            value={kind}
            disabled={Boolean(pinnedBy)}
            onChange={(e) => setKind(e.target.value as LogicalInput["kind"])}
          >
            {KINDS.map((item) => (
              <option key={item} value={item}>
                {item}
              </option>
            ))}
          </select>
        </label>
        {pinnedBy ? (
          <p className="hint">Name and kind are set by the production (environment: {pinnedBy}).</p>
        ) : null}
        {kind === "mxl_live" ? (
          <>
            <label>
              Group hint
              <input value={groupHint} onChange={(e) => setGroupHint(e.target.value)} />
            </label>
            <label>
              Video flow UUID
              <input value={videoFlow} onChange={(e) => setVideoFlow(e.target.value)} />
            </label>
            <label>
              Audio flow UUID
              <input value={audioFlow} onChange={(e) => setAudioFlow(e.target.value)} />
            </label>
            {audio ? (
              <fieldset className="audio-settings">
                <legend>Audio</legend>
                <label>
                  Delay (ms; audio later than its video, 0–{maxDelay})
                  <span className="delay-row">
                    <button type="button" title="One frame less" onClick={() => stepDelay(-frameMs)}>
                      −1 fr
                    </button>
                    <button type="button" title="1 ms less" onClick={() => stepDelay(-1)}>
                      −1
                    </button>
                    <input
                      type="number"
                      min={0}
                      max={maxDelay}
                      step={0.1}
                      value={Math.round(delayMs * 100) / 100}
                      onChange={(e) => setDelayMs(Math.min(maxDelay, Math.max(0, Number(e.target.value) || 0)))}
                    />
                    <button type="button" title="1 ms more" onClick={() => stepDelay(1)}>
                      +1
                    </button>
                    <button type="button" title="One frame more" onClick={() => stepDelay(frameMs)}>
                      +1 fr
                    </button>
                  </span>
                </label>
                <div className="map-row">
                  <span>Channels (PGM ← input; the flow has {flowChannels})</span>
                  {channelMap.map((value, c) => (
                    <label key={c}>
                      {c + 1}
                      <select
                        value={value}
                        onChange={(e) => setChannelMap((map) => map.map((v, k) => (k === c ? Number(e.target.value) : v)))}
                      >
                        <option value={0}>silent</option>
                        {Array.from({ length: Math.max(flowChannels, 2) }, (_, k) => (
                          <option key={k + 1} value={k + 1}>
                            {k + 1}
                          </option>
                        ))}
                      </select>
                    </label>
                  ))}
                  <button type="button" onClick={() => setChannelMap(identity)}>
                    1:1
                  </button>
                  <button type="button" onClick={() => setChannelMap(channelMap.map(() => 1))}>
                    Mono
                  </button>
                </div>
              </fieldset>
            ) : null}
          </>
        ) : null}
        {kind === "clip_player" ? (
          <>
            <label>
              Clip or still
              <select value={libraryItemId} onChange={(e) => setLibraryItemId(e.target.value)}>
                <option value="">Empty (black)</option>
                {libraryItems.map((item) => (
                  <option key={item.id} value={item.id}>
                    {item.name} ({item.kind === "still" ? "still" : `${item.duration_s.toFixed(1)} s`})
                  </option>
                ))}
              </select>
            </label>
            <label className="check">
              <input type="checkbox" checked={loop} onChange={(e) => setLoop(e.target.checked)} />
              Loop
            </label>
            <label className="check">
              <input type="checkbox" checked={playOnCut} onChange={(e) => setPlayOnCut(e.target.checked)} />
              Play on cut (start when taken to Program)
            </label>
            <label>
              At the end (not looping): each ME with it on Program takes its Preview
              <select value={onEnd} onChange={(e) => setOnEnd(e.target.value as ClipEnd)}>
                <option value="off">Off: hold the last frame</option>
                <option value="cut">Cut, on the frame after the last</option>
                <option value="auto">Auto: the ME's mix or wipe, ending on the last frame</option>
                <option value="stinger">Stinger: the ME's stinger, its cut point on the last frame</option>
              </select>
            </label>
            <button type="button" className="ghost" onClick={onOpenLibrary}>
              Open clip and still library…
            </button>
          </>
        ) : null}
        <label>
          Auto stinger
          <select value={stingerSlotId} onChange={(e) => setStingerSlotId(e.target.value)}>
            <option value="">None — hard cut</option>
            {stingerSlots.map((slot) => (
              <option key={slot.id} value={slot.id}>
                {slot.label}
                {slot.ready === false ? " (converting)" : ""}
              </option>
            ))}
          </select>
        </label>
        <p className="hint">
          When this source is taken to Program, or Cut while it is on Preview, play the selected
          stinger. Other sources stay hard cuts.
        </p>
        {error ? <p className="error">{error}</p> : null}
        <div className="modal-actions">
          <button className="ghost" onClick={onClose}>
            Cancel
          </button>
          <button
            onClick={async () => {
              try {
                const payload: Record<string, unknown> = {
                  label,
                  kind,
                  stinger_slot_id: stingerSlotId || null,
                };
                if (kind === "mxl_live") {
                  // Only what changed: an unrouted live input has essences without flows, and
                  // sending null for them would make it invalid; a route keeps its domain.
                  if (groupHint !== (input.group_hint ?? "")) payload.group_hint = groupHint || null;
                  if (videoFlow !== (input.video?.flow_id ?? "")) {
                    payload.video = videoFlow ? { flow_id: videoFlow } : null;
                  }
                  if (audioFlow !== (input.audio?.flow_id ?? "")) {
                    payload.audio = audioFlow ? { flow_id: audioFlow } : null;
                  }
                }
                if (kind === "clip_player") {
                  payload.library_item_id = libraryItemId || null;
                  payload.loop = loop;
                  payload.play_on_cut = playOnCut;
                  payload.on_end = onEnd;
                }
                await onSave(payload);
                if (kind === "mxl_live" && audio) {
                  const isIdentity = channelMap.every((v, c) => v === identity[c]);
                  const map = isIdentity ? [] : channelMap;
                  const before = inputAudio?.channel_map ?? [];
                  if (delayMs !== (inputAudio?.delay_ms ?? 0) || map.join(",") !== before.join(",")) {
                    await onAudio({ delay_ms: delayMs, channel_map: map });
                  }
                }
                onClose();
              } catch (err) {
                setError(err instanceof Error ? err.message : "Save failed");
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
