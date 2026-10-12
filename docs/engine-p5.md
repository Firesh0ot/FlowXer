# FlowXer engine P5: audio

Status: the design; nothing is implemented. P5 builds on P2 (the C++/CUDA engine is FlowXer's
media path, #86), P3 (transitions and keying, #89) and P4 (DME boxes and frame grab, draft #91).
The numbers come from a CPU prototype of the audio path on the lab host (Xeon Gold 6136) on
2026-10-12 ([Appendix](#appendix-lab-measurements-2026-10-12)).

## Decisions this design follows

1. **P5 scope** (user, 2026-10-10, roadmap decision 4): audio follows video (AFV) plus a fader,
   mute and AFV on/off per input, a PGM mix per ME, a delay per input, and metering through the
   status and metrics API. No full audio console.
2. **No audio preview** (P2): no PFL or solo, and no audio in the mosaic or the GUI.
3. **Stinger sound** goes into PGM; the background crossfades over the 4 grains around the cut
   point (P3 answer 4).
4. **Key fills and boxes carry no audio** so far: P3 §5 and P4 §2 left this to P5.

## 1. Scope

In:
- per ME, a PGM mix of strips: one per live input, clip player, re-entered ME and the stinger,
  each with a fader, mute and AFV on/off;
- a master fader per ME;
- a delay and a channel map per live input;
- click-free changes (a ramp per grain), and a short crossfade at a Cut;
- meters per source and per PGM (peak, true peak, RMS, loudness), published every 100 ms through
  the engine and control plane APIs and as metrics;
- a compact audio strip in the GUI, an `audio` widget, and the contract changes.

Out (later, or never): EQ; dynamics (compressor, limiter, gate); aux and mix-minus buses;
panning and downmix matrices; PFL, solo and any other audio preview; audio-only inputs (an
audio flow without a source slot); IS-08 channel mapping; loudness per source; loudness range
(LRA); negative delays (audio earlier than its video); delays on clip players.

## 2. Where P5 starts

The audio path of P2–P4 (`engine/src/engine/engine.cpp`, `core/audio`):
- **Thread:** fx-writer. After it has committed grain *i*'s video, it reads, mixes and writes the
  PGM audio of grain *i* for every ME (48 kHz, float32).
- **Timing:**
  - the audio of output grain *i* is the input audio of grain *i − L* (the grain's TAI span on
    the 48 kHz sample index);
  - samples that are not there yet are waited for until 1 ms before the end of grain *i*'s
    window, at most `FLOWXER_ENGINE_AUDIO_WAIT_MS` (8 ms). After that the source is silent for
    the grain and counted (`audio_missing`);
  - a skipped video grain still gets its audio.
- **What is read:** only the sources on an ME's Program, and the incoming one during a
  transition. The other routed inputs keep their readers open; only their head is measured (the
  arrival lag).
- **Mix:**
  - Program's source;
  - during a mix or wipe, an equal-power crossfade (cos and sin of t · 90°), per sample from the
    previous grain's t;
  - a stinger crossfades the background over its frames cut − 2 … cut + 1 and adds its sound;
  - FTB ramps the sound out;
  - a Cut switches at the grain boundary, without a crossfade;
  - re-entry takes ME *n*'s finished PGM audio.
- **Channels:** those of the PGM flow (`FLOWXER_AUDIO_CHANNELS`, default 2). Input channel c goes
  to PGM channel c; extra channels are dropped and missing ones are silent. A mono clip plays on
  every channel.
- **Cost:** 0.05 ms per grain for 4 MEs (P2).

What P5 changes:
- every routed input is read every grain, into a **delay line**. MXL's default history is 200 ms,
  and the platform sizes its flows for 200 ms too;
- the mix becomes a sum of strips with gains;
- meters;
- the audio moves to a thread of its own.

## 3. The audio model per ME

### Strips

Each rendering ME *m* has a mix of strips, one per source it can hear:

| Strip | Sources | Fader, mute | AFV | Notes |
|---|---|---|---|---|
| Live input | every live input (`in1` …) | yes | yes | delay and channel map per input, for all MEs ([§4](#4-channels), [§6](#6-timing-and-delay)) |
| Clip player | every clip player (`cp1` …) | yes | yes | its sound only while it plays (as today) |
| Re-entry | `me n` for every rendering ME *n > m* | yes | yes | ME *n*'s finished PGM audio |
| Stinger | the stinger that plays on ME *m* | yes | – | heard only while its stinger runs |

Black, key inputs (v210a) and keyer fills have no strip.

**Per strip:**
- `fader_db`: −90 … +10 dB, where −90 is off (−∞). Default 0.
- `mute`: default off. A mute keeps the fader where it is.
- `afv`: default on.
  - On: the strip is heard only while its source is on the ME's Program, and during a transition
    along its curve.
  - Off: the strip is always in the mix at its fader level, whatever is on Program. Examples: a
    commentary, ambience, a music bed from a clip player, the sound of a PiP.

**Per ME:** `master_db` (−90 … +10, default 0).

**Per live input** (one setting for all MEs): `delay_ms` and `channel_map`.

**The defaults reproduce today's sound:** every strip AFV on at 0 dB and unmuted, the master at
0 dB, no delay and a 1:1 channel map. After an upgrade the PGM audio is P4's, bit for bit, with
two exceptions: a Cut, if the crossfade of [open question 7](#14-open-questions-for-the-user) is
taken, and the first grain of a new route, which fades in ([below](#fades-without-clicks)).

### The mix

For output grain *i*, sample *s* and PGM channel *c* of ME *m*:

```
PGM_m[c][s] = (1 − ftb(s)) · master(s) · ( Σ_k g_k(s) · x_k[c][s]  +  g_st(s) · sting[c][s] )

g_k(s) = fader_k · (mute_k ? 0 : 1) · w_k(s)

w_k(s) = 1                 AFV off
       = the AFV weight    AFV on: 1 for the source on Program, 0 for the others;
                           during a transition cos(t · 90°) for A and sin(t · 90°) for B
```

- **x_k** is the strip's sound for the grain: an input's delayed and mapped samples, a clip
  player's frame sound, or ME *n*'s PGM of the same grain.
- **AFV weights** come from the ME's plan of the grain, as today:
  - a mix or wipe follows t, per sample from the previous grain's t;
  - a T-bar holds the weights where it is;
  - a stinger crossfades the background over cut − 2 … cut + 1.
  
  An AFV-off strip is not touched by a transition, so a commentary stays at its level while the
  pictures change.
- **The stinger strip** scales the stinger's own sound. Its background crossfade is the AFV
  weight of A and B.
- **Order:** the sum of the strips, plus the stinger, then the master, then FTB, then MXL (and the
  re-entry).
  - FTB takes the ME's whole sound, AFV-off strips included, as it takes the DSKs.
- **No limiter:** the float32 sum can go above 0 dBFS. The meters count overs
  ([open question 9](#14-open-questions-for-the-user)).

### Re-entry

- ME *n*'s PGM audio (after its master and FTB) is the strip `me n` on ME *m* (n > m). The MEs mix
  in render order (ME 4 → ME 1), so it is the same grain, as for the picture.
- With AFV on (the default), ME *m* hears ME *n* only while `me n` is on its Program. This is
  today's behaviour.
- **Per-ME strips avoid double sound:**
  - a commentary that is AFV off on ME 1 only is heard once, also when ME 1 takes ME 2;
  - a source that is AFV off on both MEs is heard twice when ME *m* takes ME *n* (+6 dB when the
    samples are the same). The GUI shows each ME's mix, so this stays visible.

### Keys and boxes

- Key inputs (v210a) have no audio receiver.
- Key fills and boxes (luma, pip) do not change AFV: AFV follows the background (Program and the
  transition) only.
- **The sound of a PiP:** set the strip of its source to AFV off, and use its fader or mute
  ([open question 8](#14-open-questions-for-the-user)).

### Fades without clicks

Every change ramps; nothing jumps.

| Change | How |
|---|---|
| Fader, master, mute, AFV on/off | Linear in amplitude, from the previous grain's gain to the new one, over one grain (20 ms at 50p, 16.7 ms at 60p, 40 ms at 25p). A fader moved in the GUI sends a value about every 50 ms: a chain of ramps without steps. |
| Mix, wipe, T-bar, stinger | As today: the equal-power curve, per sample. |
| Cut | Today a hard switch at the grain boundary. Proposed: an equal-power crossfade over the cut grain, A out and B in; the picture cuts at the start of that grain ([open question 7](#14-open-questions-for-the-user)). |
| Delay or channel map | A linear crossfade from the old reading to the new one over one grain: no jump and no pitch change. |
| A new route (IS-05) | The new flow fades in over one grain after its start-up window. |
| FTB | As today: the ME's sound ramps with the FTB gain. |

- **When a change applies:** at the next grain the audio thread works on, so within one or two
  grains, as video commands do.
- **Sample-accurate gains:** float32, per sample g0 + (g1 − g0) · (s + 1) / n, built without
  FMA contraction (`-ffp-contract=off`, as `fxeng-core` is already built). The unit tests check
  exact values.

## 4. Channels

- **The PGM keeps its channel count:** `FLOWXER_AUDIO_CHANNELS` (default 2; an existing flow keeps
  its own), as in P2. Every strip and the mix have that many channels.
- **A channel map per live input:** for each PGM channel, the input channel it takes (1-based;
  0 is silent). Examples:
  - `[1, 2]`: the default for 2 channels;
  - `[3, 4]`: an international sound on channels 3–4;
  - `[1, 1]`: a mono microphone on both channels.
- **The default is 1:1:** PGM channel c takes channel c. A 1-channel flow feeds every PGM channel,
  as a mono clip does today.
- **A 16-channel input into a stereo PGM** gives channels 1–2, as today, unless the map picks
  others.
- **Keeping all channels:** set `FLOWXER_AUDIO_CHANNELS=16`; every input then passes 1:1. The CPU
  cost grows with the channels ([§11](#11-cost)).
- **Clip players and the stinger** keep today's rule and have no map: the library conforms clips
  to mono or stereo, and mono plays on every channel.
- No downmix matrix, no panning, no gain per channel
  ([open question 1](#14-open-questions-for-the-user)).

## 5. DSP: on the CPU

**Choice: the CPU.**
- Audio is small. A 16-channel input is 61 KB per grain (16 × 960 samples × 4 bytes); all
  sources together are under 1 MB.
- MXL audio is host memory. A GPU path would copy every input up and every mix down each grain,
  and it would add a sync with the GPU, the resource the budget is tight on (P3 §10: at most
  15 ms per grain on the A4000).
- The loudness filter is an IIR, serial per channel: a poor fit for the GPU.
- **Measured** on the lab CPU ([Appendix](#appendix-lab-measurements-2026-10-12)): the whole P5
  audio path takes 0.16–0.33 ms per grain at 2 channels and 1.4–2.3 ms at 16 channels on one
  core, which is 1–11 % of a core.

### The audio thread

- **A new thread, `fx-audio`,** owns the audio readers, the delay lines, the strips, the meters
  and the PGM audio writers.
  - fx-writer hands it grain *i* right after the video commit, where it calls the audio today:
    the MEs' plans of the grain and the sound of its clip and stinger frames (copied, so the
    pictures are released with the slot as now).
  - The timing and the bounded wait stay as they are.
- **Why a thread of its own:** at 16 channels the audio takes up to 2.3 ms per grain (3.5 ms at
  most), after a bounded wait of up to 8 ms. In fx-writer this can push the next grain's
  downloads back; on its own thread it cannot.
- **Reading:**
  - every grain, the new samples of every routed live input go into its delay line, not only
    those of Program's sources: an AFV-off strip, a meter and a delay all need them;
  - the thread waits (bounded, as today) only for samples that a strip heard in this grain
    needs. For the others it takes what is there and fills the rest from MXL's history on the
    next grain.
- **A delay line per live input:**
  - a ring with all channels of the flow, indexed by the 48 kHz sample index (MXL's index, on
    TAI);
  - max delay + (L + 2) grains long;
  - allocated when the input is first routed, with the flow's channel count;
  - a delayed read is a copy from the ring at index − delay, through the channel map.
- **Clip players and the stinger:** their sound comes with the grain's frame, as today.
- **Mix:** per ME, in render order. The AFV curve (cos, sin) is computed once per grain and ME,
  and used for every channel. Today it is computed per sample and channel; once per grain makes
  the mix of a transition about 4 × faster in the bench.
- **Meters** ([§7](#7-metering)): per source after the delay and the map (before the fader); per
  PGM after FTB (what goes to MXL).
- **Code:**
  - `core/audiomix`: strips, gains and ramps, AFV weights, delay-line index maths, channel maps
    (CPU only, unit-tested);
  - `core/meter`: peak, RMS, true peak, loudness;
  - `engine/engine.cpp`: the thread, readers and delay lines; `engine/api.cpp`: the API.

## 6. Timing and delay

```
                      input time (TAI)                               output grain i
live input video:     grain i − L                               ──→  PGM video i
live input audio:     samples [T(i − L) − d, T(i − L + 1) − d)   ──→  PGM audio i
clip player, stinger: the sound of the frame shown in grain i   ──→  PGM audio i
```

T(k) is the start of grain *k* on the TAI clock, and d is the input's delay.

- **A delay makes an input's audio later than its own video by d.** Video processing (frame
  syncs, an encoder and decoder, a gateway) usually delays a source's video more than its audio,
  so its sound arrives early; the delay holds it back. EBU R 37 asks for audio at most 40 ms
  early and at most 60 ms late.
- **L does not change.**
  - A delay only reads older samples. A delayed input waits less, and not at all once its delay
    covers how late its audio arrives; L still only has to cover the inputs' arrival.
  - The recommended latency (`recommended_latency_grains`) counts a delayed input's audio
    arrival lag minus its delay.
- **Range:** 0 to `FLOWXER_AUDIO_MAX_DELAY_MS` (default 1000 ms, at most 5000), in whole samples
  (1/48 ms). The GUI steps by 1 ms and by 1 frame.
- **Not in P5: negative delays** (audio earlier than its video). They would read samples newer
  than T(i − L + 1), which uses up the latency L and the input's arrival margin. A source whose
  audio comes late is fixed at the source, or with L
  ([open question 3](#14-open-questions-for-the-user)).
- **Start-up:** after a route or an engine start, a delayed input is silent for its delay. This
  counts as start-up, not as missing.
- **History:** the delay line holds the past, not MXL. MXL keeps 200 ms by default; with L = 4 at
  50p, a delay read straight from MXL could reach only about 100 ms back (200 ms − L grains −
  the 11 ms into the window at which the audio runs − the input's arrival lag).

## 7. Metering

| Where | Per channel | Per channel pair | Counter |
|---|---|---|---|
| Each source (live input, clip player, stinger player), after the delay and the map, before the fader | sample peak, RMS | – | – |
| Each ME's PGM, after FTB (what goes to MXL) | sample peak, true peak (dBTP), RMS | loudness (LUFS): momentary (400 ms), short-term (3 s), integrated | overs (samples at or above 0 dBFS) |

- **Loudness** follows ITU-R BS.1770-4 and EBU R 128:
  - K-weighting (a shelf and a high pass: two biquads, in double), with the mean square in
    100 ms blocks;
  - momentary is the last 4 blocks, short-term the last 30;
  - integrated runs over 400 ms blocks with 75 % overlap, gated at −70 LUFS absolute and −10 LU
    relative. A histogram of 0.1 LU bins keeps its cost the same after hours;
  - per channel pair (1–2, 3–4 …), because a 16-channel PGM can carry several programmes;
  - integrated runs since the engine start or the last reset (`POST …/loudness/reset`, a RESET
    button).
- **True peak:** 4 × oversampling with the 48-tap filter of BS.1770-4 annex 2, run as a block
  filter (vectorised).
- **The grid:** 100 ms blocks on the 48 kHz sample index (4800 samples), not on grains, so it is
  the same at 50p, 59.94p and 25p. Peak is the maximum in the block; RMS is over the block.
- **Published:**
  - every block (10 Hz): the engine's `GET /audio/meters` returns the last finished block;
  - the control plane's `GET /audio/meters` passes it on with FlowXer's ids. It is at most
    100 ms old, with one engine call however many viewers poll;
  - metrics take the last second ([§10](#metrics)).
- Values below −120 dB are −120 (silence). Peak hold, decay and the scale are the GUI's.

## 8. Control

### Engine API (localhost; only the control plane uses it)

| Call | Body | Behaviour |
|---|---|---|
| `POST /input/{k}/audio` | `{"delay_ms": 40, "map": [1, 2]}` | Delay (0–max, whole samples) and channel map (one entry per PGM channel, 0 = silent, `[]` = default) of live input k. A change crossfades over one grain. |
| `POST /me/{m}/audio` | `{"master_db": 0, "strips": {"in1": {"fader_db": -6, "mute": false, "afv": false}, "cp1": {…}, "me3": {…}, "stinger": {"fader_db": -3}}}` | Any subset; each change ramps over the next grain. 404 for a strip the ME does not have (`me n` with n ≤ m); 400 for `afv` on `stinger`. |
| `POST /me/{m}/audio/loudness/reset` | – | Integrated loudness of ME *m* from now on. |
| `GET /audio/meters` | – | The last 100 ms block: its index; per source and per ME the values of [§7](#7-metering); per strip its gain now and whether it is heard. |
| `GET /status` → `audio` | – | Per ME the master and strips; per input the delay, map and delay-line fill; the thread's ms per grain and its waits. |

The control plane writes the new configuration value `FLOWXER_ENGINE_AUDIO_MAX_DELAY_MS`.

### Control plane (public API)

| Call | Body |
|---|---|
| `GET /audio` | Every setting: per live input `delay_ms`, `channel_map` and its flow's channels; per panel `master_db` and the strips; `max_delay_ms` and `channels` (PGM). |
| `PATCH /audio/inputs/{input_id}` | `{delay_ms, channel_map}` (live inputs only) |
| `PATCH /audio/panels/{panel_id}` | `{master_db}` |
| `PATCH /audio/panels/{panel_id}/strips/{strip_id}` | `{fader_db, mute, afv}`. `strip_id` is an input or clip player id, a panel id (re-entry of a higher ME) or `stinger`. |
| `POST /audio/panels/{panel_id}/loudness/reset` | – |
| `GET /audio/meters` | The meters, with FlowXer's ids: live inputs and clip players by their ids, stinger players as `stinger-<p>`, panels by their ids (below). |

```json
{"block": 17921903012, "sources": {"cam-1": {"peak_dbfs": [-12.3, -12.1], "rms_dbfs": [-20.4, -20.2]}},
 "panels": {"me-1": {"peak_dbfs": [-9.8, -9.9], "true_peak_dbtp": [-9.5, -9.6], "rms_dbfs": [-18.1, -18.3],
   "loudness": [{"pair": 1, "momentary": -22.8, "short_term": -23.1, "integrated": -23.0}], "overs": 0,
   "strips": {"cam-1": {"gain_db": 0.0, "heard": true}, "cam-2": {"gain_db": -90.0, "heard": false}}}}}
```

- `GET /console` gains `audio`: the settings, not the meters.
- After every engine start the control plane pushes the whole audio state, as it does for the
  keyers and buses (`_on_engine_ready`).

### Saved state

- The state file (`flowxer-config/1`) and the export gain `audio`:
  - `inputs`: per input id, `delay_ms` and `channel_map`;
  - `panels`: per panel id, `master_db` and `strips`.
- Missing values are the defaults, so a P4 state loads unchanged.
- Strips of sources or panels that no longer exist are dropped when the structure changes, as
  keyers are.
- While a fader moves, the GUI sends up to 20 values a second; the state is saved at most once a
  second.

### GUI

- **An AUDIO strip for the active ME,** below the transition section. An AUDIO button opens and
  closes it (remembered in the browser).
  - One narrow column per strip, in slot order (inputs, clip players, re-entered MEs, stinger),
    then the master.
  - About 56 px per column: 8 inputs, 2 players, 3 re-entries, the stinger and the master fit in
    840 px.
- **Each column:**
  - the label;
  - a meter: one bar per PGM channel, at most 2 (with more channels, the maximum of the odd and
    of the even channels), with a 1 s peak hold and an over mark;
  - a fader with a dB scale (+10 … −∞; a double-click sets 0 dB);
  - MUTE and AFV;
  - an ON AIR dot while the strip is heard in PGM;
  - a delayed input shows its delay.
- **Master:** the PGM meter (peak and true peak), the loudness (M, S, I) of pair 1–2, RESET, and
  the master fader.
- **Delay and channel map** in the source's ⚙ (a new Audio section):
  - the delay in ms, with ±1 ms and ±1 frame buttons;
  - the map as one selector per PGM channel, with the presets 1:1 and mono.
- Meters are polled at 10 Hz, and only while the strip is open.
- No audio preview: no solo, no PFL, no listening.

### Widget

- **New `audio`:** `/widget/audio?me=<n>[&faders=false]`. It shows one ME's strips (meter, MUTE,
  AFV, and the fader unless `faders=false`) and its master meter with the loudness
  ([open question 5](#14-open-questions-for-the-user)).
- Listed in `GET /widgets`. Its minimum size is about 480 × 240 (to be checked).

## 9. Tally and NMOS

- **NMOS: nothing new.**
  - The live inputs' audio receivers (`<input label> Audio`) and the senders `ME <n> PGM Audio`
    exist since P2.
  - Key inputs stay video only.
  - The channel map is FlowXer's own API, not IS-08.
- **Tally: nothing new.** TSL tally stays a picture tally: an AFV-off strip that is heard lights
  nothing. Whether a strip is heard is in `/audio/meters` and in the metrics.

## 10. Contract changes for the platform

Relative to the P4 contract.

### Environment of the mixer container

| Variable | Values | Default | Designer value |
|---|---|---|---|
| `FLOWXER_AUDIO_MAX_DELAY_MS` | 0–5000 | 1000 | none needed |

Unchanged: `FLOWXER_AUDIO_CHANNELS` (PGM channels, default 2) and everything else.

### Metrics

Engine metrics, labelled with the engine's names (as `flowxer_engine_input_*` are):

| Metric | Labels | Meaning |
|---|---|---|
| `flowxer_engine_audio_level_dbfs` | `source` (`in<k>`, `cp<k>`, `st<k>`), `channel`, `stat` (`peak`, `rms`) | A source's level over the last second, before its faders. |
| `flowxer_engine_pgm_audio_level_dbfs` | `me`, `channel`, `stat` (`peak`, `true_peak`, `rms`) | PGM over the last second. |
| `flowxer_engine_pgm_loudness_lufs` | `me`, `pair`, `window` (`momentary`, `short_term`, `integrated`) | BS.1770-4 loudness. |
| `flowxer_engine_pgm_audio_overs_total` | `me` | Samples at or above 0 dBFS. |
| `flowxer_engine_audio_strip_heard` | `me`, `strip` | 1 while the strip is heard (its gain in PGM above −90 dB). |
| `flowxer_engine_audio_ms` | `stat` | fx-audio's time per grain (read, mix, meters, write). |
| `flowxer_engine_audio_wait_ms` | `stat` | Its bounded waits. |

- Values below −120 dB are −120.
- The Grafana dashboard gains an audio row: PGM loudness and true peak per ME, overs, silent
  sources.

### Widgets

New `audio` ([§8](#widget)).

### NMOS, tally, GPU

No changes. P5 adds no GPU work and no GPU memory.

### CPU and memory

- **CPU:** + 0.01–0.02 cores at 2 channels, + 0.07–0.11 cores at 16 channels (Xeon Gold 6136;
  the platform's i9-9900K clocks higher and should be faster per core, not measured).
- **Host memory:** the delay lines, up to 3.4 MB per live input with 16 channels and a 1 s
  maximum (27.5 MB for 8 inputs), or 0.43 MB per stereo input.

### Backward compatibility (P4 → P5)

- The defaults reproduce P4's PGM audio, bit for bit except at a Cut with the crossfade of
  [open question 7](#14-open-questions-for-the-user) and in the first grain of a new route (the
  fade-in).
- The state file and the export gain `audio`; older states load with the defaults.
- No route, label, id or flow changes.

## 11. Cost

Measured on the lab CPU with `fxp5-bench`: one thread pinned to a core of the Xeon Gold 6136,
built as the engine is (`-O3 -ffp-contract=off`, no `-march`), 3000 grains at 50p (960 samples),
8 live inputs, 2 clip players and 1 stinger, 4 MEs, the meters of [§7](#7-metering)
([Appendix](#appendix-lab-measurements-2026-10-12)).

Scenarios:
- **typical:** per ME, Program's source (AFV) and one AFV-off strip are heard; no transition;
- **busy:** every ME in a transition (the AFV curve), 2 strips heard with faders moving every
  grain, a stinger playing, the master and FTB ramping;
- **worst:** every input and clip player heard on every ME (all AFV off), plus every higher ME
  re-entered, all ramping, with transitions and stingers on all MEs (54 strips summed per grain).

| PGM and input channels | typical | busy | worst (max) | Cores at 50p |
|---|---|---|---|---|
| 2 | 0.16 ms | 0.24 ms | 0.33 ms (0.74) | 0.008–0.016 |
| 8 | 0.70 ms | 0.81 ms | 1.13 ms (2.03) | 0.035–0.057 |
| 16 | 1.41 ms | 1.59 ms | 2.28 ms (3.54) | 0.070–0.114 |

Per item, ms per grain, at 16 channels (2 channels in brackets):

| Item | Cost |
|---|---|
| A live input: its new samples into the delay line, and the delayed, mapped read | 0.023 (0.002) |
| A source meter (peak, RMS) | 0.010 (0.001) |
| A strip heard on one ME (gain ramp and sum) | 0.018–0.020 (0.002–0.003) |
| A PGM meter (peak, RMS, true peak, loudness), per ME | 0.23 (0.03) |
| A transition on one ME (the AFV curve, a stinger's sound, the master ramp) | 0.04 (0.02) |

- **Per ME** at 16 channels: about 0.23 ms for its meters plus 0.02 ms per strip it hears.
  **Per source:** about 0.033 ms.
- **CPU per second scales with the sample rate, not the frame rate:** it is the same at 25p, 50p
  and 59.94p.
- **What made it this cheap.** A first, straightforward version of the bench did the same work in
  up to 5.1 ms per grain (16 channels, worst case):
  - true peak per sample with a 12-tap loop: 2.0 ms for the 4 PGMs (with the K-weighting); as a
    block filter 0.95 ms;
  - the AFV curve per sample and channel: the busy mix took 1.27 ms; once per grain 0.33 ms;
  - loudness per source (K-weighting of 11 sources × 16 channels) costs 0.92 ms; P5 gives sources
    peak and RMS only.
- **AVX2** (`-march=x86-64-v3`) saves another 15–19 %. It is not needed.
- **Today:** 0.05 ms per grain (2 channels, only Program's sources read, no meters).
- **Memory:**
  - delay lines: channels × (max delay + (L + 2) grains) × 192 kB/s. With 1 s and L = 4 at 50p
    that is 215 kB per channel: 3.4 MB for a 16-channel input, 0.43 MB for a stereo one, 27.5 MB
    for 8 inputs of 16 channels, 126 MB at the 5 s maximum;
  - meters and buffers: under 1 MB;
  - no GPU memory.

## 12. Milestones

Each milestone is tested on the lab before the next one starts. Small and the release follow only
with the user's OK. One PR into `dev`, growing per milestone, as #86, #89 and #91 did.

| Milestone | Builds | Lab test |
|---|---|---|
| **M1: the mix** | `core/audiomix` (strips, gains and ramps, AFV weights and the Cut crossfade, delay-line index maths, channel maps). The `fx-audio` thread; delay lines, with every routed input read each grain. The engine API and `/status`. The control plane's model, API, saved state, and the push after an engine start. | The defaults reproduce P4's PGM audio bit for bit; gains and ramps exact to the sample; the delay to the sample (`bin/mxl-delay`); AFV off, re-entry, the stinger's level, FTB; changes without clicks; no extra skipped or late grain. |
| **M2: meters** | Peak, RMS, true peak, loudness; the engine's and the control plane's `/audio/meters`; the metrics. | EBU Tech 3341 signals within ±0.1 LU; true peak within +0.2 / −0.4 dB; peaks and RMS against an offline reference of the same MXL samples; the cost at 2 and 16 channels. |
| **M3: GUI and widget** | The AUDIO strip, the ⚙ Audio section (delay, map), the `audio` widget, the docs. | The GUI on the lab (Edge over DevTools): faders, MUTE, AFV, meters at 10 Hz; the widget in a frame. |
| **M4: budget, soak, release readiness** | The dashboard's audio row; README, engine/README, CHANGELOG; the platform note. | The CPU in the running engine (4 MEs, 2 and 16 channels, the worst case); a 2 h soak with audio changes every few seconds next to P3's and P4's transitions, keys and boxes. Then small, with the user's OK. |

## 13. Test plan

### Unit tests (CPU `fxeng-tests`, pytest, GUI)

- **Gains:** a step from g0 to g1 gives exactly g0 + (g1 − g0) · (s + 1) / n at sample s; mute
  and AFV toggles ramp; −90 dB is silence; 0 dB is the identity.
- **AFV:**
  - the weights equal today's crossfade (mix, wipe, T-bar, the stinger's cut point);
  - the defaults give P4's samples bit for bit;
  - an AFV-off strip ignores transitions;
  - the Cut crossfade.
- **Delay:** the read index at 50p, 59.94p (800 and 801 samples) and 25p; the ring wrap; a delay
  change (the crossfade); the start-up silence counted as start-up.
- **Channel map:** 1:1, mono to all, pairs, 0 = silent, a flow with fewer channels than the PGM.
- **Meters:**
  - peak and RMS of sines;
  - the K-weighting at known frequencies;
  - the EBU Tech 3341 cases (momentary, short-term and integrated within ±0.1 LU), generated in
    the test;
  - true-peak test signals;
  - gating with two levels.
- **Control plane** (simulate mode, fake engine): schemas; validation (strip ids, `afv` on
  `stinger`, the delay range); the state round trip and the migration of a P4 state; the push
  after an engine start.
- **GUI:** `tsc`; the fader law (dB ↔ position) against vectors.

### Lab (`fxp5-functional.py`, through FlowXer's API and IS-05 only)

- **Gain:** the test player's 997 Hz tone through a strip at −6.02 dB: after the ramp grain, PGM is
  the input × 0.5 to float rounding; the ramp's samples follow the formula.
- **Delay:** `bin/mxl-delay` between the input flow and ME 1 PGM gives L grains + d exactly (in
  samples), for d = 0, 1 ms, 1 frame, 500 ms and 1 s; a delay change on air leaves no gap.
- **Clicks:** a 997 Hz tone through fader moves, mutes, AFV toggles, Cuts and delay changes. The
  PGM's residual above 5 kHz stays below −80 dBFS: a step would show as a broadband spike (today's
  hard Cut does).
- **Mix:** an AFV-off commentary through mixes, wipes, stingers and Cuts; re-entry; the stinger
  strip's level; FTB with AFV-off strips.
- **Meters:** against an offline reference computed from the PGM read back from MXL.
- **Performance and soak:** the CPU matrix in the engine, and a 2 h soak.

## 14. Open questions (for the user)

1. **Channels:** the PGM keeps `FLOWXER_AUDIO_CHANNELS` (default 2), and each live input maps 1:1
   with an optional channel map (pairs, mono to all). No downmix, panning or gain per channel.
   Should the platform stay stereo, or keep 16 channels 1:1? *Suggestion: stereo by default;
   16 where several programmes or languages are needed (8 × the CPU, still at most 0.11 cores).*
2. **Metering:**
   - sources: sample peak and RMS, after the delay and map, before the fader;
   - PGM: sample peak, true peak and RMS per channel, and EBU R 128 loudness (momentary,
     short-term, integrated with RESET) per channel pair;
   - every 100 ms.
   
   Loudness per source would cost +0.9 ms per grain at 16 channels. *Suggestion: as listed; no
   loudness per source and no LRA.*
3. **Delay:** 0–1000 ms per live input (`FLOWXER_AUDIO_MAX_DELAY_MS`, up to 5000), to the sample,
   audio later than its video only; the GUI steps by 1 ms and 1 frame. Are longer delays,
   negative delays (audio earlier, which costs latency) or delays on clip players needed?
   *Suggestion: as listed.*
4. **Per-ME or global faders:** fader, mute and AFV per ME and strip (each ME has its own mix, so a
   commentary that is AFV off on ME 1 only is not heard twice through a re-entered ME); delay and
   channel map per input, for all MEs. Or one fader, mute and AFV per source for every ME?
   *Suggestion: per ME.*
5. **`audio` widget** for operator screens: meters, MUTE, AFV, faders (hidden with
   `faders=false`) and the master's loudness? *Suggestion: yes.*
6. **Clip-player levels:** each clip player has a strip per ME, and the library's loudness option
   at import (−23 LUFS) already evens out clips. Is a gain per library item needed (a trim saved
   with the clip, applied wherever it plays), or level presets per player? *Suggestion: no presets
   in P5; a gain per item later if operators ask for it.*
7. **Cut:** today a Cut switches the sound hard at the grain boundary, which can click. Change it
   to an equal-power crossfade over the cut grain (20 ms at 50p)? *Suggestion: yes.*
8. **Keys and boxes:** AFV follows the background only; the sound of a PiP or a keyed clip comes
   from its strip set to AFV off. Or "audio follows key": a strip fades with the keyer that shows
   its source? *Suggestion: not in P5.*
9. **Limiter:** no limiter or clip protection on PGM. Overs (at or above 0 dBFS) are counted, shown
   and exported, and the true peak is metered. *Suggestion: none in P5; a safety limiter later if
   needed.*
10. **Stinger level:** one stinger strip per ME (fader and mute, default 0 dB), or a level per
    stinger in the library? *Suggestion: the strip per ME.*
11. **GUI place:** a collapsible AUDIO strip for the active ME below the transition section, closed
    by default. Or always open, or all MEs at once? *Suggestion: collapsible, the active ME only.*

## Appendix: lab measurements (2026-10-12)

**Setup:** lab host iptv-web-lab-1, 2 × Xeon Gold 6136 (Skylake-SP, 3.0 GHz, 48 threads), g++
13.3. One thread pinned with `taskset` to CPU 46; the host ran other lab work at the same time
(load about 4 of 48 threads). 100 warm-up grains, then 3000 grains; ms per grain as average, 99th
percentile and maximum. The source, `fxp5-bench.cpp`, is on the lab host in `~/mxl-lab/fxp5/`,
not in this PR. It becomes engine code in M1 and M2.

**What a grain does** (960 samples at 50p; uniform noise with peaks at −12 dBFS, in RAM; MXL's
layout of one ring per channel, without MXL itself):
- **fill:** each live input's new samples from its "MXL" ring into its delay line (1 s + 5 grains);
- **read:** each live input's window 40 ms back through its channel map (input 2 is a mono map),
  and the clip and stinger sound as `copyPlayerAudio` copies it;
- **source meters:** peak and sum of squares per channel (and, as an option, the K-weighting);
- **mix:** per ME (4 → 1), the heard strips with a gain ramp per sample, a transition's two
  sources with the equal-power curve, the stinger's sound, re-entered MEs, the master and FTB;
- **PGM meters:** peak, sum of squares, K-weighting and true peak per channel; per pair the
  loudness of each 100 ms block (momentary, short-term, a gating histogram);
- **out:** a copy into an "MXL" output ring.

**Two versions:**
- **v1, straightforward:** true peak per sample (4 phases × 12 taps), the AFV curve per sample and
  channel, the K-weighting on sources too;
- **v2, as designed:** peak and squares in 8 lanes, true peak as a block filter (one pass per tap
  over the grain), the AFV curve once per grain and ME, sources without K-weighting. Both vectorise
  without `-ffast-math`.

### v2, built as the engine (`-O3 -DNDEBUG -ffp-contract=off`, x86-64)

| Channels | Scenario | avg / p99 / max ms | fill | read | source meters | mix | PGM meters | out |
|---|---|---|---|---|---|---|---|---|
| 2 | typical | 0.164 / 0.179 / 0.199 | 0.009 | 0.004 | 0.014 | 0.020 | 0.117 | 0.001 |
| 2 | busy | 0.242 / 0.256 / 0.267 | 0.010 | 0.004 | 0.013 | 0.096 | 0.117 | 0.001 |
| 2 | worst | 0.329 / 0.681 / 0.738 | 0.011 | 0.005 | 0.014 | 0.178 | 0.119 | 0.002 |
| 8 | typical | 0.695 / 0.750 / 0.970 | 0.054 | 0.034 | 0.052 | 0.079 | 0.466 | 0.010 |
| 8 | busy | 0.810 / 0.883 / 1.082 | 0.054 | 0.034 | 0.052 | 0.193 | 0.467 | 0.010 |
| 8 | worst | 1.132 / 1.398 / 2.034 | 0.055 | 0.035 | 0.053 | 0.510 | 0.468 | 0.010 |
| 16 | typical | 1.408 / 1.508 / 2.275 | 0.109 | 0.078 | 0.105 | 0.161 | 0.935 | 0.021 |
| 16 | busy | 1.594 / 2.394 / 2.571 | 0.111 | 0.081 | 0.109 | 0.333 | 0.940 | 0.021 |
| 16 | worst | 2.283 / 3.111 / 3.543 | 0.113 | 0.086 | 0.113 | 0.992 | 0.958 | 0.022 |
| 16 | typical, with loudness per source | 2.393 / 2.973 / 3.050 | 0.113 | 0.090 | 1.027 | 0.183 | 0.958 | 0.022 |
| 16 | worst, with loudness per source | 3.218 / 3.997 / 4.591 | 0.115 | 0.091 | 1.025 | 1.010 | 0.955 | 0.022 |

Strips summed per grain: typical 8 (2 per ME), busy 16, worst 54.

### v1, built as the engine

| Channels | Scenario | avg / p99 / max ms | source meters | mix | PGM meters |
|---|---|---|---|---|---|
| 2 | typical | 0.460 / 0.824 / 0.837 | 0.138 | 0.023 | 0.275 |
| 2 | busy | 0.602 / 1.008 / 1.055 | 0.138 | 0.163 | 0.276 |
| 2 | worst | 0.685 / 1.341 / 1.366 | 0.137 | 0.251 | 0.274 |
| 8 | typical | 1.744 / 2.306 / 2.913 | 0.494 | 0.092 | 1.054 |
| 8 | busy | 2.282 / 2.778 / 3.019 | 0.494 | 0.637 | 1.045 |
| 8 | worst | 2.584 / 3.137 / 3.214 | 0.492 | 0.946 | 1.039 |
| 16 | typical | 3.437 / 4.019 / 4.119 | 0.987 | 0.179 | 2.044 |
| 16 | busy | 4.512 / 5.043 / 5.561 | 0.989 | 1.271 | 2.022 |
| 16 | worst | 5.102 / 5.670 / 7.115 | 0.988 | 1.881 | 2.004 |

### AVX2 (`-march=x86-64-v3`), v2

2 channels: typical 0.135, worst 0.281 ms; 16 channels: typical 1.139, worst 1.849 ms. That is
15–19 % less than the engine's build.
