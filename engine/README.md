# flowxer-engine

FlowXer's C++17/CUDA media engine. It reads the live MXL v210 inputs (with their audio), plays
the clip players (clips decoded by NVDEC, stills), renders up to 4 MEs (Program and Preview)
every grain on the GPU with the downstream keyer, writes PGM video + audio and PVW video of
every ME back to MXL, and publishes one preview mosaic (NVENC H.264 → MediaMTX → WHEP).

Since P2 it is FlowXer's media path: the Python control plane (`src/flowxer/engine/runtime.py`)
starts it as a child process with a configuration file, restarts it with backoff, and drives
it over its control API on 127.0.0.1 (NMOS, IS-05 routes, tally, the library and the GUI stay
in Python). The P1 design is [DESIGN.md](DESIGN.md); the P1 sections below (drop-in,
measurements) are kept as its record. P3 (transitions, wipes, keyers, stingers) is designed in
[docs/engine-p3.md](../docs/engine-p3.md).

## How it works

Each frame of video is a *grain* with an index on the TAI clock (50 per second at 1080p50).

- **Input threads** (`fx-in1` … `fx-in8`), one per input: wait for the next grain of their
  MXL flow, copy it once to the GPU (a direct DMA from the page-locked MXL memory, on the
  input's own CUDA stream) and keep the newest few grains in a small ring. A flow that stalls
  keeps its last grain on screen (counted as late); a re-created flow (FLOW_INVALID) is
  opened again; a missing one is retried.
- **Render thread** (`fx-render`): wakes when output grain *i* becomes the current grain on
  the TAI grid and composes it from input grains *i − L* (fixed latency, `L` = 4 by default).
  It unpacks each input that an ME uses once, renders ME 4 → ME 1 (so ME 1 can take ME 2's
  Program from the same grain), packs all outputs to v210 and, every second grain, draws the
  mosaic. All of this is queued on one CUDA stream; the thread does not wait for the GPU.
  When it is late it skips to the current grain and counts the skip; it never slides.
  The downstream keyer keys over its ME's Program there (keys have no preview).
- **Clip player threads** (`fx-cp1` …), one per clip player: decode the loaded clip ahead
  (NVDEC) or upload the still once; see [Clip players](#clip-players).
- **Stinger player threads** (`fx-st1` …): decode the loaded stinger's colour and alpha in
  lockstep; see [Stingers](#stingers).
- **Writer thread** (`fx-writer`): owns the MXL video writers. It opens grain *i* on each
  output, queues the DMA download straight into the MXL grain on a separate download stream,
  waits for each copy and commits. Then it hands grain *i* to the audio thread (the MEs' plans
  and a copy of the clip and stinger players' sound). Upload, render and download of
  consecutive grains overlap on their streams. The frame grabs of grain *i* are copied after its
  downloads on the same stream (see [Frame grab](#frame-grab)).
- **Audio thread** (`fx-audio`, P5): owns the audio readers, the live inputs' delay lines, the
  MEs' audio mixes, the meters and the PGM audio writers; it writes the PGM audio of grain *i*
  (see [PGM audio](#pgm-audio-p5-strips-afv-delays-meters)). At most 8 grains wait for it; a
  grain beyond that is dropped and the next one covers it.
- **Grab thread** (`fx-grab`): writes each frame grab's raw v210 file.
- **Encode thread** (`fx-encode`): copies the NV12 mosaic into an NVENC frame on the GPU,
  encodes it (one session, whatever the number of viewers) and sends it over RTSP/TCP to
  MediaMTX: the platform's shared one (`PREVIEW_PUBLISH_URL`) or, without it, the MediaMTX
  the engine starts itself (bundled in the image; restarted if it exits).
- **Stats thread** (`fx-stats`): CPU per thread and GPU/NVENC load once a second; watches the
  own MediaMTX.

Ownership is simple: each MXL reader belongs to its input thread (video) or to the audio
thread (audio), every MXL video writer to the writer thread, every audio writer to the audio
thread. Grains in flight between render and
writer are bounded (3 slots); input frames come from a fixed pool per input and go back only
after the downloads that read them are done. Every wait sleeps (blocking CUDA sync, futex
waits in MXL, condition variables): no busy loops.

### MEs

- Sources: the live inputs `in1`..`inN`, the clip players `cp1`..`cpC`, `black` and
  `me2`..`me4`. ME *m* may take the Program of ME *n* only when *n > m* (re-entry, 0 frames,
  no loops possible).
- Program renders every grain into its own bus buffer in one fused compose pass: the background
  (a copy, the dissolve during a Mix, or the wipe during a Wipe), then the key layers (the
  upstream keyers, a running stinger, the DSKs, Fade to Black), written once. Preview renders the preview source. Both are written to MXL (`<label> ME<m> PGM`,
  `<label> ME<m> PVW`).
- Cut swaps Program and Preview at the next grain. Auto runs the ME's next transition (Mix or
  Wipe) over N grains, then swaps. Its progress follows the grain index, so skipped grains do not
  stretch it. A stinger runs from a take with its stinger player ([Stingers](#stingers)); Auto
  and the T-bar with the type stinger have no player and cut (FlowXer sends the take).
- Wipes (`core/wipe.hpp`): the SMPTE codes 1, 2, 3–6, 21–26, 41, 42, 45, 46, 101, 102, 119 and
  201–204 map each pixel to s in [0, 1]; the front F = t · (1 + soft + border) gives the mask,
  with the soft edge split around the border; reverse is 1 − s; t = 0 is all A and t = 1 all B.
  The kernel and the CPU reference are one code path, built without FMA contraction
  (`-fmad=false`, `-ffp-contract=off`): they agree bit for bit.
- An input without a video flow is a black slot. It keeps the numbering (the TSL tally and the
  operator's habits), e.g. a camera that is not connected.

### PGM audio (P5: strips, AFV, delays, meters)

Design and answers: `docs/engine-p5.md`.

- **Each ME mixes strips:** one per live input, clip player, re-entered ME (`me n`, n > m) and
  the stinger, each with a fader (−90 dB = off … +10 dB), a mute and audio follow video (AFV;
  not on the stinger strip), and a master per ME. With AFV on (the default) a strip is heard only
  while its source is on the ME's Program, and along a transition's curve; with AFV off it is
  always in the mix at its fader (a commentary, a music bed, the sound of a PiP). Preview has no
  audio; the mosaic has none.
- **The defaults are P4's sound bit for bit:** every strip AFV on at 0 dB, the master at 0 dB,
  no delay, 1:1. Exceptions: a Cut crossfades (below), the first grain of sound of a new route
  fades in, and a 1-channel live input now plays on every PGM channel.
- **Transitions:** a Mix or Wipe (and the T-bar) crossfades with **equal power** (cos and sin of
  t·90°, per sample, so two unrelated sources keep their loudness; −3 dB each in the middle). A
  stinger crossfades the background over its frames cut − 2 … cut + 1 and adds its sound, scaled
  by the ME's stinger strip. A **Cut** crossfades on the same curve over its grain (P4 switched
  hard). Fade to Black takes the ME's whole sound, AFV-off strips included.
- **Every change ramps** over one grain on an S-shaped curve, g0 + (g1 − g0) · w(s) with
  w(s) = 0.5 − 0.5 · cos(π (s + 1) / n) (no corners, so no click): fader, mute, AFV, master; a
  delay or channel map change crossfades from the old reading to the new one on the same curve.
- **Order:** the strips' sum, plus the stinger's sound, then the master, then FTB, then MXL (and
  the re-entry into lower MEs).
- **Per live input** (for every ME): a delay of 0 … `FLOWXER_ENGINE_AUDIO_MAX_DELAY_MS` (whole
  48 kHz samples; audio later than its video only) and a channel map (per PGM channel the input
  channel, 1-based, 0 silent; empty: 1:1, a 1-channel flow on every channel). Every routed input
  is read every grain into its **delay line** (all channels of its flow; the longest delay and
  L + 3 grains long). A heard input is waited for (bounded, below); the others take what is there
  and get the rest from MXL's history on the next grain. After a route an input is silent for its
  delay (counted as start-up).
- **Timing:** the same TAI grid and latency as the video: the audio of output grain *i* is the
  input audio of grain *i − L*, d samples earlier for a delay d. Samples that are not there yet
  are waited for until 1 ms before the end of grain *i*'s window, at most
  `FLOWXER_ENGINE_AUDIO_WAIT_MS`; then they are silence for that grain and counted (`missing`).
  A skipped video grain still gets its audio, so the audio flow has no holes.
- float32, 48 kHz. Channels: those of the output flow (`FLOWXER_ENGINE_AUDIO_CHANNELS`, default
  2, for a flow the engine creates; an existing flow keeps its own).
- **Meters** on 100 ms blocks of the 48 kHz sample index (the same at every frame rate):
  - every live input (after its delay and map, before the fader), clip player and stinger
    player: sample peak and RMS per channel;
  - every ME's PGM (what goes to MXL): sample peak, true peak (BS.1770-4 annex 2, 4×), RMS per
    channel, overs (samples at or above 0 dBFS), and per channel pair the BS.1770-4 / EBU R 128
    loudness: momentary (400 ms), short-term (3 s), integrated (gated, since the start or
    `POST /me/{m}/audio/loudness/reset`);
  - `GET /audio/meters` answers the last block; the metrics take the last second. Values below
    −120 dB are −120.
- CPU only (`fx-audio`); its time per grain is `/status` `audio.ms_per_grain` (measured: 0.5–0.8
  ms at 2 channels, 2.6–2.8 ms at 16, with 4 MEs and 8 inputs; [below](#p5-m4-audio-cpu-and-soak-lab-2026-10-13)).
  Host memory: the delay lines, channels × (the longest delay + L + 3 grains) × 192 kB/s per
  input (0.44 MB stereo, 3.5 MB with 16 channels at 1 s and L = 4).

### Clip players

A clip player (`FLOWXER_ENGINE_PLAYERS`, `cp<k>`) shows a clip or a still from FlowXer's
library, or black when it is empty. FlowXer loads it (`POST /player/<k>/load` with the files
of the library item for the current format) and drives the transport.

- **Clips** are the library's mezzanine: HEVC Main 4:4:4 10 (Ampere's NVDEC decodes HEVC
  4:4:4 but no 4:2:2) at the engine's raster and rate, with a raw float32 sound file of exactly
  the clip's frames. The player's thread decodes up to 5 frames ahead into CUDA frames (FFmpeg
  with NVDEC, on the engine's CUDA context), converts each to v210 on the GPU (chroma 4:4:4 →
  4:2:2 as the mean of each pixel pair: the library widened the 4:2:2 picture by repeating each
  chroma sample, so this is exact) and reads its sound. Anything else (e.g. a ProRes file) is
  refused: the library converts it.
- **Stills** are one v210 picture at the raster, uploaded once: a frame memory.
- **Transport** (`core/playout.cpp`): load cues the first frame; `cue` shows a frame and holds;
  `play`; `pause` holds the frame shown; `stop` goes back to the first frame and holds. A
  playing clip shows one frame per output grain on the TAI grid (grains the render thread
  skipped advance it too). With **loop** it goes on over the end without a break; without,
  it holds its last frame (`ended`; `play` starts it over). With **play on cut** a cued,
  paused or ended clip starts when the player goes on air (into an ME's Program, or as the
  incoming source of a Mix). Loading the file that is loaded already changes only loop and play
  on cut.
- **Sound:** the frame's samples on the library's grid (frame *f* starts at sample
  ⌊f · 48000 · den / num⌋), so a loop has no drift; it plays only while the clip plays (held
  frames are silent) and goes into audio-follows-video like an input's audio. A mono clip
  plays on every channel.
- A frame the decoder does not have in time (right after a load or cue) repeats the last
  picture and counts as `late`. `GET /status` → `players`: state, frame, frames, loop, play on
  cut, frames decoded ahead, decode time, late frames, error, and `end_index`: the output grain
  of the clip's last frame while it plays without loop (or the grain it ended in).
  `GET /players` has just these and the last grain planned (`index`): FlowXer reads it every
  100 ms to time a clip player's end action (`docs/clip-end.md`).
- GPU memory: 9 v210 pictures per player (50 MB at 1080p) from the start, plus about 170 MB
  for the NVDEC decoder while a clip is loaded (its surfaces and FFmpeg's frame pool, 4:4:4
  16-bit). NVDEC: one 1080p50 player at 40–60 Mbit/s takes about 4 % of the A16 GPU's
  decoders, 6 % of a CPU core.

### Stingers

A stinger player (`FLOWXER_ENGINE_STINGER_PLAYERS`, 0–4) is a clip player whose pictures are
v210a: FlowXer loads a library stinger onto it (`POST /stinger/<p>/load` with its colour, alpha
and sound files and its frames); it decodes the colour (the clip mezzanine) and the alpha (the
luma of a lossless HEVC 4:2:0 10-bit stream, `alphaToKey` into the key plane) in lockstep, up to
5 frames ahead; without an alpha file the key is full (an opaque stinger).

- **Take:** `POST /me/<m>/take` `{"source", "type": "stinger", "stinger": p, "cut": c,
  "preview"}`. The stinger starts at the first grain its player has frame 0 decoded (cued: the
  next grain; else a few grains, counted as `stingers.waits`; after 50 grains it is a cut,
  `stingers.cuts`). Started at grain s, it shows frame f in grain s + f; the background is the
  old Program until grain s + c, then the new one; after its last frame the buses swap. Skipped
  grains advance it. A Cut or a take during a stinger ends it as if it had run to its end.
- **Layer:** the stinger is a linear key (α = key / 1023) over the upstream keyers and under the
  DSKs and Fade to Black; Preview shows only its source.
- **Sound:** frame f's samples are added to the ME's PGM audio, on the library's grid (to the
  sample); the background crossfades over 4 grains around the cut point (above).
- After a stinger its player cues frame 0 again. `GET /status` → `stingers`: per player its
  state, file, alpha, frames ahead, late frames; `played`, `waits`, `cuts`; `mes[].stinger` while one runs.
- GPU memory per stinger player: 9 v210a pictures (71 MiB at 1080p) from the start, plus the two
  NVDEC decoders while a stinger is loaded (276 MiB on the lab: 347 MiB per player).

### Keyers

`FLOWXER_ENGINE_KEYS` lists the keyers (`{"label", "me", "stage": "upstream"|"downstream",
"domain", "flow"}`; FlowXer: per rendering ME its upstream keyers, then its DSKs). They key over
the ME's Program only (keys have no preview, Preview shows only its source), as layers of the
fused compose: the background, the upstream keyers, the DSKs, Fade to Black.
- **Linear:** the keyer's key input, a v210a flow (the v210 fill followed by a 10-bit key plane,
  e.g. mxl-browser-source in `v210a` mode). out = fill · α + background · (1 − α) with
  α = key / 1023 · gain, per sample (chroma: the mean α of its pixel pair), straight fill. The key
  is read at grain *i − L* like an input; an unrouted key input keys nothing.
- **Luma:** a self key of a source the ME may take (an input, a clip player, `me<n>` with n > m):
  α = clamp(((Y − 64) / 876 − clip) / soft) (hard with no softness), inverted, times the gain;
  the fill is the source. A clip player keyed on air counts as on air.
- **Pip** (upstream keyers): a source the ME may take, opaque, in a box.
- **DME boxes** (P4 M1, `docs/engine-p4.md` §3, `core/dme.hpp`): an upstream keyer with
  `dme.enabled` (always for a pip) crops, scales and places its fill (and a linear key's key,
  premultiplied) in a box: one weight-table kernel and one resample kernel per box (Catmull-Rom,
  widened by 1 / size when shrinking, at most 32 taps; the horizontal pass into shared memory per
  tile of box rows, then the vertical pass), into the keyer's box buffer (12.4 MB at 1080p,
  allocated by the control thread when the box is first enabled, so the render thread never
  allocates). A linear box resamples its key flow unpacked once per grain (#99, `gpu::dmeUnpack`):
  float planes of the premultiplied Y, Cb, Cr and the key (23.7 MiB at 1080p per key flow that a
  linear box uses, allocated by the control thread with the box, shared by all linear boxes on that
  flow). The fused compose draws it as a `box` layer: a black
  rectangular shadow (smoothstep edges), the outer border, then the picture (anti-aliased or soft
  edges), each rounded to 10 bits; the keyer's gain scales all three. A linear box limits its
  premultiplied fill to what its key allows, so the cubic's ringing at a hard key edge leaves no
  fringe. yuv16 only. The CPU reference (`dmeResampleCpu`, `dmeLayerCpu`) and the kernels share
  the code and agree bit for bit.
- **Keyframes** (P4 M2, `docs/engine-p4.md` §5, `core/keyframes.hpp`): up to 8 box states per
  upstream keyer, each reached from the one before over its `frames` with an ease (linear, ease,
  ease-in, ease-out, hold); size in log space (0 as 0.01), the rest linear. The box of every grain
  is computed on the CPU from the grain index (a run started at grain s shows step f in grain
  s + f − 1, skipped grains do not stretch it) and handed to the GPU with the plan; only + − × ÷
  and exact scaling (own ln and exp), so the CPU reference of the tests computes the same bits.
  A run goes from where the box is to a keyframe through the ones in between (a box off the
  keyframes' path, e.g. edited by hand, first goes straight to the run's first keyframe over that
  keyframe's frames and ease); loops: wrap (to the last keyframe, then keyframe 0 the next grain)
  and pingpong; stop holds. A change of the box's animated fields stops a run there. **Fly**
  (`fly`, 2 keyframes): the keyer's Auto on turns it on and runs keyframe 0 → last, Auto off runs
  back and turns it off after the grain at keyframe 0, an Auto during a fly turns back, cut on and
  off jump to the last keyframe and to 0. `/status` shows each keyer's box and run.
- **Ties** (P4 M3, `docs/engine-p4.md` §5): an ME's next transition names its tied upstream
  keyers (`ties`). Cut and Auto (`/me/<m>/cut`, `/auto`, a take with `ties`) and the T-bar take
  them along: each goes to its other state. A cut cuts it at the next grain; with a Mix or Wipe a
  fading keyer's gain is `tieAt(from, to, t)` with the transition's t, and a flying keyer (fly, a
  box, 2 keyframes) is on air while its box runs along its keyframes at timeline position
  `tieAt(u0, u1, t)` (squeezed into the transition; from off the path straight to its keyframe with
  that keyframe's ease); with a stinger they cut at its cut point. The grain at t = 1 shows the
  end; a fly off is off from the next grain; the T-bar back at 0 leaves them as they were. A Cut
  during a tied transition ends it as at its end; the keyer's own Cut, Auto (a tied fly turns back
  from where it is), run, stop or new keyframes take it out of the tie where it is.
- **Key flows:** keyers routed to the same flow share one reader and upload; a flow's frames
  are allocated when it is first routed (stream-ordered), not at start.
- `POST /keyer/<k>` `{"on": bool}` cuts it, `/keyer/<k>/auto` `{"frames": n}` fades the gain over
  n grains, `/keyer/<k>/settings` sets the mode and its values.

### Frame grab

P4 M3, `docs/engine-p4.md` §6. `POST /grab` queues a grab of a live input, a clip player or an
ME's Program or Preview and answers its id at once (the control thread never waits for the GPU).
The render thread takes up to 4 grabs per output grain *i* (more wait for the next one) with the
picture that grain uses: a live input's grain *i − L* (or the one it held), a clip player's
picture, the ME's packed Program or Preview (both are packed every grain). The writer thread
copies each one (one 5.5 MB DMA at 1080p) into a page-locked buffer after the grain's downloads
on the download stream: the render stream and the commits do not wait for it. The grab thread
writes `path.part` and renames it to `path`. At most 8 grabs are in flight (their buffers are
allocated on the first grabs and kept). `GET /grab/<id>` gives its state (pending, copying,
writing, done, failed), output grain and TAI time, a live input's own grain and the ms from the
request to the file; `/status` `grabs` and the metrics count them per source kind.

### Working format

`FLOWXER_ENGINE_WORK_FORMAT` selects the format the MEs render in:
`yuv16` (10-bit 4:2:2 in 16-bit planes, lossless round trip; default) or `rgba16f`
(non-linear R'G'B' in half floats, for effects that need RGB; round trip within 2 codes).

## Build

The FlowXer image (the repository's `Dockerfile`) builds the engine in a CUDA stage, runs its
unit tests and installs it as `/usr/local/bin/flowxer-engine`:

```bash
docker build -t flowxer .
docker run --rm --gpus all --entrypoint flowxer-engine flowxer --selftest   # GPU kernels against the CPU reference
```

CUDA 12.8 / Ubuntu 24.04 with MXL 218ddaa, Ubuntu's FFmpeg (NVDEC, NVENC) and MediaMTX. The
GPU, NVDEC, NVENC and NVML come from the NVIDIA container toolkit: run with a GPU and
`NVIDIA_DRIVER_CAPABILITIES=compute,video,utility` (the image sets it). CI builds the unit
tests (`fxeng-tests`) and the image.

Without Docker: CMake ≥ 3.24, a CUDA toolkit, MXL installed (`CMAKE_PREFIX_PATH`),
`libavcodec/libavformat/libavutil` and nlohmann-json. `-DFXENG_BUILD_ENGINE=OFF` builds only
the unit tests (`fxeng-tests`), which need none of these except nlohmann-json.

## Run

FlowXer starts the engine (`FLOWXER_ENGINE_PORT`, default 9630, on 127.0.0.1). For tests it
runs alone; it needs the MXL domains (`/Volumes/mxl`) and a GPU. Without `PREVIEW_PUBLISH_URL`
it runs its own MediaMTX for the mosaic; on the platform it publishes to the shared one.

```bash
docker run -d --name engine --network host --user 1000:1000 --gpus '"device=0"' \
  -v /Volumes/mxl:/Volumes/mxl -v $PWD/media:/media:ro \
  -e FLOWXER_ENGINE_INPUTS='[{"label":"Cam 1"},{"label":"Cam 2"}]' \
  -e FLOWXER_ENGINE_PLAYERS='[{"label":"Clip 1"}]' \
  --entrypoint flowxer-engine flowxer
docker exec engine flowxer-engine --check   # the resolved configuration
```

The API with curl:

```bash
curl -s localhost:9630/status | jq .output
curl -s -X PUT localhost:9630/input/1 -d '{"video":{"domain":"/Volumes/mxl/player","flow":"<id>"},"audio":{"domain":"/Volumes/mxl/player","flow":"<id>"}}'
curl -s -X POST localhost:9630/player/1/load -d '{"path":"/media/mezz-1080p50.mkv","kind":"clip","audio":"/media/mezz-1080p50.f32","channels":2,"loop":true}'
curl -s -X POST localhost:9630/me/1/preview -d '{"source":"cp1"}'
curl -s -X POST localhost:9630/me/1/auto -d '{"frames":25}'
curl -s -X POST localhost:9630/player/1/play
curl -s -X POST localhost:9630/me/1/program -d '{"source":"me2"}'   # re-entry
```

## Configuration

Environment variables, or the same keys in a JSON file named by `FLOWXER_ENGINE_CONFIG`
(arrays as JSON, numbers as numbers; the environment wins). FlowXer writes that file and
passes no other `FLOWXER_ENGINE_*` variable. `flowxer-engine --check` prints what the engine
will read and write.

| Variable | Default | Meaning |
|---|---|---|
| `FLOWXER_ENGINE_INPUTS` | (none) | JSON array of the live inputs in slot order, `{"label", "domain", "video_flow", "audio_flow", "audio_domain"}`; the routes are optional (`PUT /input/{k}` sets them at run time); no video flow = black slot, no audio flow = silent |
| `FLOWXER_ENGINE_PLAYERS` | (none) | JSON array of the clip players `{"label"}`; inputs and players together at most 23 |
| `FLOWXER_ENGINE_KEYS` | (none) | JSON array of up to 4 key inputs `{"label", "me", "domain", "flow"}`: a v210a flow and the ME of its DSK |
| `FLOWXER_ENGINE_STINGER_PLAYERS` | `0` | stinger players (0–4) |
| `FLOWXER_ENGINE_MES` | `1` | rendering MEs (1–4), all rendering every grain |
| `FLOWXER_ENGINE_LATENCY` | `4` | L: output grain *i* is made from input grains *i − L* (1–10) |
| `FLOWXER_ENGINE_FORMAT` | `1080p50` | `720p`/`1080p`/`2160p` with `25`, `2997`, `30`, `50`, `5994`, `60`; inputs and clips must match |
| `FLOWXER_ENGINE_OUTPUT_DOMAIN` | `/Volumes/mxl/flowxer-engine` | MXL output domain directory of the MEs (created if missing) |
| `FLOWXER_ENGINE_OUTPUT_DOMAIN_ID` | (generated) | `id` written to `domain_def.json` when the engine creates the domain (BCP-007-03 fields id, label, description, tags); an existing file is kept |
| `FLOWXER_ENGINE_OUTPUTS` | (generated) | JSON array of `{"me", "domain", "domain_id", "pgm_video", "pgm_audio", "pvw_video", "label", "group"}`, all but `me` optional; an empty id is generated (stable for domain + ME + kind) |
| `FLOWXER_ENGINE_PVW_OUTPUTS` | `true` | write the PVW video flows; `false` renders PVW for the mosaic only |
| `FLOWXER_ENGINE_AUDIO_CHANNELS` | `2` | channels of a PGM audio flow the engine creates |
| `FLOWXER_ENGINE_AUDIO_WAIT_MS` | `8` | longest wait for input audio that is not there yet when its grain is written (0–40) |
| `FLOWXER_ENGINE_AUDIO_MAX_DELAY_MS` | `1000` | longest audio delay of a live input (0–5000): its delay line holds this much (P5) |
| `FLOWXER_ENGINE_LABEL` | `FlowXer` | prefix of the labels of the flows the engine creates |
| `FLOWXER_ENGINE_WORK_FORMAT` | `yuv16` | `yuv16` or `rgba16f` |
| `FLOWXER_ENGINE_GPU` | `0` | CUDA device index (inside the container) |
| `FLOWXER_ENGINE_HTTP_BIND` | `127.0.0.1` | API and metrics: address |
| `FLOWXER_ENGINE_HTTP_PORT` | `9630` | and port |
| `FLOWXER_ENGINE_HOLD_GRAINS` | `0` | a stalled input holds its last grain this long, then black; 0 = until it comes back |
| `FLOWXER_ENGINE_MOSAIC` | `true` | preview mosaic on/off |
| `FLOWXER_ENGINE_MOSAIC_FPS` | `25` | mosaic frame rate (every second grain at 50p) |
| `FLOWXER_ENGINE_MOSAIC_KBPS` | `6000` | NVENC bit rate (CBR) |
| `FLOWXER_ENGINE_PGM_PREVIEW` | `true` | each ME's Program as a stream of its own (`<prefix>/me<n>-pgm`), not a mosaic tile (only with the mosaic). Needs 1 + MEs NVENC sessions, opened at start; without them the Programs stay mosaic tiles (`/mosaic/map` → `programs_error`) |
| `FLOWXER_ENGINE_PGM_PREVIEW_WIDTH` | `960` | its width (even, at most the raster's; the height keeps the raster's aspect: 540 at 16:9) |
| `FLOWXER_ENGINE_PGM_PREVIEW_FPS` | `25` | its frame rate |
| `FLOWXER_ENGINE_PGM_PREVIEW_KBPS` | `2500` | its NVENC bit rate (CBR) |
| `PREVIEW_PUBLISH_URL` | (empty) | shared MediaMTX to publish to (RTSP, e.g. `rtsp://mxl-mediamtx.mxl-platform.svc:8554`); empty: start the own MediaMTX |
| `PREVIEW_PATH_PREFIX` | `flowxer` | stream path prefix (`<production>/<function>`); the mosaic is `<prefix>/mosaic` |
| `PREVIEW_WHEP_URL` | (empty) | public WHEP base: the pages play `<base>/<prefix>/mosaic/whep`; empty: own MediaMTX on the page's host |
| `PREVIEW_HLS_URL` | (empty) | public HLS base (`<base>/<prefix>/mosaic/index.m3u8`); empty: own MediaMTX |
| `MEDIAMTX_RTSP_PORT` | `8654` | own MediaMTX: RTSP ingest, localhost only |
| `MEDIAMTX_WHEP_PORT` | `8989` | own MediaMTX: WHEP |
| `MEDIAMTX_HLS_PORT` | `8988` | own MediaMTX: low-latency HLS |
| `MEDIAMTX_ICE_PORT` | `8289` | own MediaMTX: WebRTC ICE, UDP and TCP |
| `MEDIAMTX_API_PORT` | `9897` | own MediaMTX: API, localhost only |
| `MEDIAMTX_PUBLIC_IP` | (empty) | own MediaMTX: extra ICE host candidate |
| `MEDIAMTX_BIN` | `mediamtx` | own MediaMTX binary (bundled in the image) |
| `FLOWXER_ENGINE_STATE_DIR` | `/tmp/flowxer-engine` | where the own MediaMTX config is written |

### Ports

| Port | What | Notes |
|---|---|---|
| 9630/tcp | API, `/status`, `/metrics`, `/mosaic/map` | localhost by default |
| 8654/tcp | own MediaMTX RTSP ingest | localhost only; not used with `PREVIEW_PUBLISH_URL` |
| 8989/tcp | own MediaMTX WHEP | browsers |
| 8988/tcp | own MediaMTX HLS | browsers |
| 8289/udp+tcp | own MediaMTX ICE | browsers |
| 9897/tcp | own MediaMTX API | localhost only |

With `PREVIEW_PUBLISH_URL` set only 9630 is used. The defaults avoid FlowXer (9610/9620), the
multiviewer (8110) and mxl-webrtc-monitor (8100, MediaMTX 8554/8888/8889/8189/9997/9998).
RTMP, SRT and MoQ are off in the own MediaMTX.

## API

POST and PUT are the same. Sources: `in<n>`, `cp<n>`, `black`, `me<n>` (re-entry).

| Method and path | Body | Answer |
|---|---|---|
| `POST /me/{m}/preview` | `{"source": "in3"}` | the ME; 400 for a source the ME may not take, 409 during a Mix |
| `POST /me/{m}/program` | `{"source": "me2"}` | same |
| `POST /me/{m}/transition` | `{"type": "mix"\|"wipe"\|"stinger", "frames", "pattern", "reverse", "soft", "border", "border_color": "#rrggbb", "ties": [k, …]}` (each optional) | the ME's next transition (`transition` in the answer); a running one keeps its own; 400 for an unknown wipe code or a tie that is no upstream keyer of the ME |
| `POST /me/{m}/take` | `{"source", "frames", "preview", "type", "stinger", "cut", "ties", "at"}` | Program to the source with an optional new Preview: cut (0 frames, `cut`, or `stinger` without a player), mixed (`mix`, the default), wiped with the ME's wipe (`wipe`), or by the stinger on player `stinger` with its cut frame `cut` (`stinger`; 404 for no such player; a player without a stinger cuts); `ties: true` takes the ME's tied keyers along. With `at` (an output grain) the take is held and made right before that grain is planned (a grain already planned: the next one; one held take per ME, a newer one replaces it); any other command of the ME, or of a keyer, makes it first. `GET /status` → `mes[]` `deferred_at`, `deferred_made` |
| `POST /me/{m}/cut` | – | the ME (the tied keyers cut; during a transition: its end) |
| `POST /me/{m}/auto` | `{"frames": 25}` (1–1000, default the ME's rate) | the ME's next transition, or the rest of a held T-bar transition; 409 while one runs |
| `POST /me/{m}/ftb`, `/me/{m}/ftb/auto` | `{"on": true}`, `{"frames": 30}` | Fade to Black of the ME's whole output (a black layer after the DSKs; the PGM audio fades with it); the buses stay |
| `POST /me/{m}/position` | `{"t": 0.4}` (0–1) | the T-bar: t > 0 starts the ME's next transition and holds it at t (audio ramps to it within a grain); 1 completes it after that grain, 0 ends it without the swap; a stinger runs as an Auto; 409 during an Auto |
| `PUT /input/{k}` | `{"video": {"domain", "flow"}, "audio": {"domain", "flow"}}` | opens (or, without a flow, closes) the input's readers |
| `POST /input/{k}/audio` | `{"delay_ms": 40, "map": [1, 2]}` (each optional) | live input k's audio delay (0 … `FLOWXER_ENGINE_AUDIO_MAX_DELAY_MS`, whole samples) and channel map (one entry per PGM channel: the input channel, 1-based, 0 silent; `[]` 1:1); a change crossfades over one grain; answers the input's audio (`delay_ms`, `delay_samples`, `map`, `channels`, `held_ms`); 400 for a bad value |
| `POST /me/{m}/audio` | `{"master_db": 0, "strips": {"in1": {"fader_db": -6, "mute": false, "afv": false}, "cp1": {…}, "me3": {…}, "stinger": {"fader_db": -3}}}` (any subset) | the ME's master and strips (fader −90 … +10 dB; −90 is off); each change ramps over the next grain; all or nothing; 404 for a strip the ME does not have (`me n` with n ≤ m), 400 for `afv` on `stinger` or a bad value; answers the ME's audio |
| `POST /me/{m}/audio/loudness/reset` | – | ME m's integrated loudness from the next 100 ms block on |
| `GET /audio` | – | per ME its `master_db` and `strips` (settings, `gain_db` now and `heard`), per live input its delay, map, flow channels and delay line (`held_ms`), the PGM's `channels`, `max_delay_ms` |
| `GET /audio/meters` | – | the last 100 ms `block`: `sources` (`in<k>`, `cp<k>`, `st<k>`: `peak_dbfs`, `rms_dbfs` per channel), `mes` (per ME `peak_dbfs`, `true_peak_dbtp`, `rms_dbfs`, `loudness` per pair: `momentary`, `short_term`, `integrated`; `overs`; `strips`: `gain_db`, `heard`); `block` null before the first one |
| `PUT /key/{k}` | `{"domain", "flow"}` | routes keyer k's key input (v210a); a flow another keyer reads is shared |
| `POST /keyer/{k}` | `{"on": true}` | the keyer on or off at the next grain |
| `POST /keyer/{k}/auto` | `{"frames": 25}` | fades the keyer on (or off when it is on) |
| `POST /keyer/{k}/settings` | `{"mode": "linear"\|"luma"\|"pip", "source", "clip", "soft", "gain", "invert", "fly", "dme"}` | the key; a luma or pip source must be one the keyer's ME may take; `dme` changes the given fields of the keyer's live box `{enabled, x, y, size, soft, crop {left, right, top, bottom}, border {width, soft, color "#RRGGBB", opacity}, shadow {dx, dy, blur, opacity}}` (without `dme` the box stays; a change of position, size, crop, soft, border width, soft or opacity or shadow stops a run there); pip and dme on upstream keyers only |
| `PUT /keyer/{k}/keyframes` | `{"keyframes": [{box fields, "frames": 25, "ease": "linear"\|"ease"\|"ease-in"\|"ease-out"\|"hold"}]}` | up to 8 (upstream keyers; missing box fields: the keyer's box); a run stops where the box is |
| `POST /keyer/{k}/run` | `{"to", "reverse", "loop": "off"\|"wrap"\|"pingpong", "frames": 0}` | a run of the box from the next grain: to keyframe `to` (the last; 0 with `reverse`), a loop, or (`frames` 0) a jump; needs a box and the keyframe |
| `POST /keyer/{k}/stop` | – | stops a run: the box holds (a keyer flying off stays on) |
| `POST /player/{k}/load` | `{"path", "kind": "clip"\|"still", "audio", "channels", "frames", "loop", "play_on_cut"}` | loads and cues the first frame (`path` "" unloads: black); the file loaded already only changes loop and play on cut; 409 with the reason for a file that is no mezzanine |
| `POST /player/{k}/cue` | `{"frame": n}` | shows frame n and holds |
| `POST /stinger/{p}/load` | `{"path", "alpha", "audio", "channels", "frames"}` | loads a stinger (colour, alpha and sound files; `alpha` "" is opaque, `audio` "" silent) and cues its first frame; `path` "" unloads; 409 with the reason for a file that is no mezzanine |
| `POST /player/{k}/play`, `/pause`, `/stop` | – | transport |
| `GET /players` | – | `{index, players}`: the last output grain planned and each clip player's status (with `end_index`) |
| `POST /grab` | `{"source": "in3"\|"cp1"\|"me1", "bus": "pgm"\|"pvw", "path"}` | a frame grab of the next grain as raw v210 into `path` (`bus` for an ME); answers at once with its id and state; 409 with 8 in flight |
| `GET /grab/{id}` | – | `{grab, source, bus, path, state, error, index, tai, source_index, ms}` (the last 32 grabs) |
| `GET /status` | – | per-ME state and output flows (`existing` = taken over), timing (GPU ms per stage, lag), counters, inputs and keys (video and audio state, arrival lag; audio is `no_signal` when its head stood still for a second), `recommended_latency_grains` (the latest arrival in the last minute (`recent`; `peak` is since the route) of what runs: inputs' video and audio, key flows in use; the start-up window and 2 s after each (re)route are not counted), clip players, stinger players, keyers (mode, `fly`, the box of the last grain, keyframe count, `run`: running, fly, loop, start grain, step, keyframe, on the path at `u`; `tie`: the ME it is tied to), `mes[]` (`start`: the first grain of the running or last transition, `ties`, `tied`), `grabs`, `gpu_ms.dme` (per ME the resample of its boxes), CPU per thread, GPU/NVENC, `preview` (mode own/shared, publish state per stream), `audio` (as `GET /audio`, plus fx-audio's `ms_per_grain`, `wait_ms`, `jobs_dropped`); each input's `audio` also has its `delay_ms`, `map`, `channels`, `held_ms` and `heard` (some ME hears it in the last grain), and `recommended_latency_grains` counts an input's audio lag only while it is heard, minus its delay |
| `GET /metrics` | – | Prometheus text (`flowxer_engine_*`; the list is in the top README's metrics table): output, GPU ms per stage (and `dme_ms` per ME), grabs, inputs and key flows (with their upload ms), clip and stinger players (state, played, waits, cuts), keyers (on air, key flow running), audio (fx-audio ms and waits; levels, true peak, loudness, overs and heard strips over the last second), mosaic, CPU, GPU |
| `GET /mosaic/map` | – | canvas size, stream `path`, `whep_base`/`hls_base` (or own ports), tiles `{id, kind, label, x, y, w, h}` |
| `GET /livez` | – | `ok` |

## Drop-in for a FlowXer instance

(P1. Since P2 the engine is FlowXer's media path, and ME 1 PGM keeps FlowXer's flow ids.)

The engine can take over a FlowXer instance's outputs, so its consumers (multiviewer, monitor,
replay) keep receiving without new IS-05 activations: they are activated on MXL flow ids.

- Set `FLOWXER_ENGINE_OUTPUTS` ME 1 `pgm_video`/`pgm_audio` (and the domain) to the FlowXer
  instance's Program flows; give the other outputs new ids in the same domain.
- **One writer at a time.** Stop FlowXer first. The engine refuses to start (exit 75, "has an
  active writer") while another process still writes one of its output flows.
- The engine **opens the flows FlowXer left** (MXL create-or-open: same flow, same inode,
  `existing: true` in `/status`); the old flow directories need not be removed. Readers see new
  grains on the flow they already have open. The existing flow must be v210 in the engine's
  format (video) and 48 kHz float32 (audio; its channel count is used).
- **Rollback:** stop the engine (it releases its writers; MXL deletes the flows when the last
  writer goes), start FlowXer: it creates the flows again and readers re-open them
  (FLOW_INVALID), as after any FlowXer restart.
- No NMOS in P1: the engine registers no senders. The PGM flow ids stay the same, so
  activations made to FlowXer's senders keep working; the registry no longer lists them while
  FlowXer is down.
- Mirrors (fabrics) only carry grains while some receiver on the node is routed to them. If a
  mirror stalls the engine holds its last grain and counts it (`late`); if it is re-created the
  input opens it again.

Lab check (`fxeng-dropin.sh`): see [Drop-in on the lab](#drop-in-on-the-lab) below.

### friday-night-show vmix1 on small

[examples/friday-night-show-vmix1.json](examples/friday-night-show-vmix1.json) maps vmix1
(FlowXer 14.21.46, mxl-host-03): inputs Camera 1 and 2 (fabrics mirror domain
`95c0b941…`), Camera 3 (browser source), black slots for Camera 4, Test 1/2, Black and Replay
(the FlowXer slot order, so the TSL numbering stays); ME 1 PGM video/audio = vmix1's
`42a6614e…`/`429c984c…` in `/Volumes/mxl/friday-night-show-vmix1` (domain `9528879f…`);
new ids for ME 1 PVW and ME 2–4; preview to the platform's MediaMTX; the raw tally to
`mxl-tally`. Use it as `FLOWXER_ENGINE_CONFIG` (or as env). The Deployment needs: the MXL
root hostPath at `/Volumes/mxl`, uid/gid 1000, the nvidia runtime with one GPU slot,
`NVIDIA_DRIVER_CAPABILITIES=compute,video,utility`, and port 9630 reachable for the control
page (Service or port-forward).

## Reuse and attribution

See [NOTICE](NOTICE). From the same author's MXL media functions:
mxl-multiviewer (Apache-2.0): CUDA v210 sample addressing, page-locked MXL grain DMA,
MXL reader handling (resync on TOO_LATE, re-open on FLOW_INVALID), the HTTP server, the CPU
v210 reference; mxl-webrtc-monitor (MIT): the MediaMTX config and the NVENC settings;
mxl-replay: the TAI loop rule (re-implemented on MXL's own index formulas, no code copied:
mxl-replay is GPL-3.0); FlowXer's own TSL 5.0 export (`src/flowxer/engine/tsl.py`): the raw
tally format.

## Changes from DESIGN.md

- The writer thread, not the render thread, opens the MXL grains and queues the downloads:
  an MXL writer has one open grain at a time, so one thread owns all writers.
- Added to P1 for the drop-in test on small (user decision): configurable output flow ids,
  PGM audio (DESIGN.md had audio in a later phase), the control page, black input slots, the
  raw TSL tally.
- The preview follows the platform's preview contract (`PREVIEW_*`), with a bundled MediaMTX
  when no shared one is given.
- A stalled input holds its last grain until it comes back (configurable).

## Measurements (P1a, lab, 2026-10-10)

Lab host iptv-web-lab-1: NVIDIA A16 GPU 0 (one GA107 of the board, PCIe Gen4 **x4** link),
driver 595.84, 2 × Xeon Gold 6136 (48 threads). Image `fxeng:p1` (318 MB). 1080p50, L = 2,
4 MEs, 8 inputs: test player Out 1–4, replay PGM and PVW, a lab pattern writer, the browser
source (v210a, fill only). Mosaic on (no viewers yet). Numbers from `/status` (CUDA events,
average/maximum of the last 5 s window) and from the host (`/proc`, `nvidia-smi`).

### Functional check

36 of 36 checks passed, read back from the MXL outputs: on every ME, Program and Preview
routing, Cut, Auto over 50 grains (the mix factor of the Program grains rises by 0.02 per
grain), a Preview change during a Mix refused, the buses swapped after the Mix; re-entry
ME 2 → ME 1 gives the same picture in the same output grain; ME 2 may not take `me1` or
`me2`. Output grain *i* equals the source's grain *i − 2* byte for byte (yuv16).
GPU self-test: yuv16 round trip and dissolve exact, rgba16f round trip exact on the test
picture, dissolve within 1 code. P3 adds every wipe code (5 positions, 4 soft/border settings, both
directions: 920 cases) bit for bit against the CPU reference, and the fused compose (a wipe and two
key layers) exact. P4 adds 9 DME boxes (pip, luma and linear; sizes 0.1–1.5, crops, sub-pixel
positions, soft edges, borders, shadows, partly off the raster): box buffers and composed pictures
bit for bit; since #99 the 3 linear boxes also through the unpack (12 cases).

DME cost on the lab (A16 GPU 3, 1080p, border + shadow, resample + box layer, CUDA events over
300 runs): corner PiP 1/3 0.26 ms, half-size PiP 0.29 ms, split 2 × 1/2 0.31 ms per box, quad
0.29 ms per box, 8 × 1/4 0.24 ms per box, full size 0.48 ms, upscale 1.5 0.42 ms. In the running
engine a half-size pip adds 0.27 ms to its ME (2 boxes: 0.23 ms per box).

P4 M2 on the lab (`fxp4-functional-m2.py`, GPU 3): every grain of a fly read back from MXL equals
the CPU reference of its step (fly in and off with the keyer's Auto over 3 keyframes, a turn-back,
presets flying on air, pingpong and wrap loops over 61 grains each); no output grain skipped; a
moving half-size box costs what a static one does (0.51 against 0.50 ms on ME 1).

P4 M3 on the lab (`fxp4-functional-m3.py`, GPU 3): a tied fade and a tied fly keyer equal the CPU
reference in every grain of an Auto mix and an Auto wipe (with the background, grain by grain), at
held T-bar positions, and cut with the background at a stinger's cut grain; grabs of a live input,
a clip player and ME 1 PGM and PVW equal their grains on MXL bit for bit (16 grabs, 4 at a time,
too), a grab loaded into a clip player is on air bit for bit, its FFV1 original decodes to the
grab; no output grain skipped; a grab is in the library 22–45 ms after the request.

### Against the success criteria

| Criterion | Target | Quiet (120 s) | Bounded load, host 81 % (120 s) |
|---|---|---|---|
| Output rate, every output | 50.0 fps | 50.03 | 50.03 |
| Skipped / late output grains | 0 on a quiet host | 0 / 0 | 0 / 0 |
| Output lag | ≤ L + 1 = 3 grains | 2.0 (max 2); commit 10.5 ms (max 12.0) into the grain's window | 2.0 (max 2); 11.2 ms (max 16.3) |
| GPU time per output grain | ≤ 8 ms (budget 20) | render 3.4 (max 4.2); + download 10.3 (max 11.8) | 3.4; 10.4 (max 11.5) |
| Engine CPU | ≤ 1.5 cores | 0.13 cores | 0.10 cores |
| NVENC | one session | 1 session, 3 % | 1 session, 3 % |

The render work is 3.4 ms. The rest is PCIe DMA: the A16's GPUs run on a x4 link
(6.4 GB/s measured), so writing 8 outputs back (8 × 5.5 MB) takes 6.9 ms and uploading 8 inputs
another 7.6 ms (on the input streams, in the other direction, overlapping). An RTX A4000 has
a x16 link. NVML's "GPU utilization" (78 %) counts this copy time; the CUDA event times are
the meaningful numbers.

Unattended run before that (dev build of the same code, quiet, no viewers): 3 h 54 min,
703 008 grains on every output, lag 2.0 grains throughout, 0 skips after the first second (the
6 start-up skips are gone since the writer page-locks its grains before the first grain).

### GPU time per stage (ms, average / maximum, yuv16)

| Stage | 4 MEs | 1 ME |
|---|---|---|
| Upload, per input (own stream, overlapping) | 0.95 / 4.5 | 0.90 / 3.5 |
| Unpack (the inputs the MEs use) | 1.21 / 1.25 (8 inputs) | 0.32 / 0.36 (2 inputs) |
| ME render (PGM + PVW), each | 0.22 / 0.25 | 0.22 |
| Pack to v210 (all outputs) | 0.70 / 0.75 (8 outputs) | 0.19 (2 outputs) |
| Mosaic (every second grain: 1.3 when drawn) | 0.64 / 1.30 | 0.41 / 0.83 |
| Download into the MXL grains | 6.87 / 7.81 | 1.83 / 2.06 |
| Render total / render + download | 3.41 / 10.28 | 1.13 / 2.96 |
| Mosaic encode (NVENC, wall time in `fx-encode`) | 2.6 per mosaic frame | 2.8 |

### Working format

| | yuv16 | rgba16f |
|---|---|---|
| Unpack (8 inputs) | 1.21 ms | 4.94 ms |
| ME render, each | 0.22 ms | 0.41 ms |
| Pack (8 outputs) | 0.70 ms | 1.19 ms |
| Render total | 3.41 ms | 8.39 ms |
| GPU memory (process) | 769 MiB | 897 MiB |
| Round trip | lossless | within 2 codes (measured max 0) |

yuv16 is the default; rgba16f only where an effect needs RGB.

### GPU memory per ME count (process, `nvidia-smi`; yuv16, mosaic on)

| MEs | 1 | 2 | 3 | 4 | 4, no mosaic |
|---|---|---|---|---|---|
| Process | 613 MiB | 665 MiB | 717 MiB | 769 MiB | 718 MiB |
| Engine buffers | 459 MB | | | 602 MB | |

About 52 MiB per ME (PGM/PVW bus buffers and 3 grains of output in flight); the rest is the CUDA
context, the 8 input rings (8 grains each) and NVENC.

### CPU per thread (percent of one core, quiet, 4 MEs)

`fx-encode` 3.7, `fx-in1..8` 3.1 together, CUDA event handler 2.1, `fx-writer` 2.1,
`fx-render` 1.5; process 0.13 cores.

### Against FlowXer 14.21.46

Same host and GPU (A16 GPU 0), one after the other; FlowXer with `fx-mi.sh` (vmix layout:
cam-1..4 = test player Out 1–4, internal test-1/2, black, replay; 1 ME, GPU path, CEF overlay
and DSK on). Same bounded load (40 busy threads, 35-core quota).

| | Engine P1: 4 MEs, 8 inputs, mosaic | FlowXer 14.21.46: 1 ME |
|---|---|---|
| Output rate, quiet / load | 50.03 / 50.03 fps | 50.13 / 50.06 fps |
| CPU, quiet / load | 0.13 / 0.10 cores | 3.14 / 2.88 cores (container) |
| Busiest thread, quiet / load | `fx-encode` 3.7 % | GL thread (`gstglcontext`) 98.7 % / 84.5 % |
| GPU utilization (NVML) | 78 % (mostly DMA) | 63 % |
| GPU memory | 769 MiB | about 1110 MiB (device) |

FlowXer's single GL thread is at its limit with one ME; the engine renders four MEs with
PGM + PVW and the mosaic at about 4 % of FlowXer's CPU. (FlowXer also renders a CEF overlay,
a DSK and internal sources; the engine has no keyers yet.)

## Drop-in, audio and tally (lab, 2026-10-10)

### Drop-in on the lab

FlowXer 14.21.46 (`fx-mi.sh`, vmix layout, A16 GPU 3) writes ME 1 PGM video + audio into its
domain; a multiviewer 1.3.0 reads them on input 1 (IS-05 activation with the MXL flow ids).

| Step | Flows | Multiviewer input 1 |
|---|---|---|
| FlowXer running | created by FlowXer | video running, audio −18.6 dBFS (the test tone) |
| Engine started while FlowXer runs | untouched | unchanged; the engine exits 75 ("has an active writer") |
| FlowXer stopped (`docker compose stop`) | stay (same inodes) | no signal |
| Engine started on the same ids | opened (`existing: true`), same inodes | running again 2.5 s after the engine start (its start-up included), audio −18.5 dBFS |
| Rollback: engine stopped, FlowXer started | deleted with the engine's writers, re-created by FlowXer | running, −18.7 dBFS |
| FlowXer killed (`docker kill`) | stay | no signal |
| Engine started | opened, same inodes | running, −18.5 dBFS |

### PGM audio on the lab

Read back from the ME 1 PGM audio flow: the test player's tone passes without gaps (sine fit
residual 2·10⁻⁴ across block borders); an Auto over 50 grains to a silent source follows the
equal-power curve within 0.0001 of the level (−3 dB in the middle) and is silent after exactly
50 grains; Cut back brings the tone again. Audio costs 0.05 ms CPU per grain.

### TSL tally on the lab

A UDP listener decoded 37 packets in 9 s (one per ME per second plus changes): after ME 1
Program = `me2` and a Cut on ME 2 it showed SCREEN 1 with INDEX 1002 (ME 2) red and SCREEN 2
with the new Program red and Preview green.

### An input that stalls or is re-created

Input 7 fed by a test writer (`mxl-flow-writer`) that ends, restarts, is killed and restarts
again, shown on ME 4 Program: when the writer ends (MXL deletes its flow) the input re-opens,
reports `flow_not_found` and holds its last grain (counted as `late`, audio silent and counted);
when the writer re-creates the flow the input runs again; when the writer is killed the flow
stays, the input reports `no_signal` and holds; a writer that opens the left flow again is
picked up without a re-open. The outputs kept 50 fps with 0 skipped grains throughout.

## P1b: mosaic, viewers, soak (lab, 2026-10-10)

Image `fxeng:p1` (= `proto-5db091f7`), A16 GPU 3, own MediaMTX, everything as in P1a plus PGM
audio on all 4 MEs.

- **Control page in Edge** (from a laptop over the VPN): 16 `<video>` elements on one WHEP
  session, all playing 1920×1080 cropped to their tiles with `object-view-box`; PVW and CUT
  buttons switch the ME and the tally colours follow.
- **10 WHEP viewers** (aiortc, decoding, on the lab host): one NVENC session (3 %), MediaMTX
  lists 10 readers, every viewer decodes 25.0 fps.
- **30 min soak** with the 10 viewers and an operator loop (a Cut or a 25-grain Auto on a
  rotating ME every 15 s):

| | Result | Target |
|---|---|---|
| Output rate, every output | 50.0 fps | 50.0 |
| Skipped / late / failed grains | 0 / 0 / 0 | 0 |
| Output lag | 2.0 grains (max 2); commit 10.4 ms (max 12.1) into the window | ≤ 3 grains |
| GPU render / render + download | 3.41 ms / 10.23 ms (max 11.85) | ≤ 8 ms render |
| Engine CPU | 0.12 cores | ≤ 1.5 |
| Container CPU (engine + MediaMTX serving 10 viewers) | 0.45 cores | |
| NVENC | 1 session, 3.2 % | 1 session |
| Mosaic | 25.0 fps, 45 706 frames, 2 dropped (encoder start) | 25 fps |
| GPU memory | 769 MiB | |

The same container then ran on unattended: 3 h 54 min in total, 701 959 grains on every
output (video and PGM audio), 0 skipped, 2 late grains at start-up, lag 2.0 throughout, the
mosaic encoder connected once with 0 errors, the own MediaMTX never restarted. The replay
outputs it reads have gaps of their own (157 and 166 invalid grains); the engine held the last
grain and counted them per input.

## P2 M2: clip players and DSK (lab, 2026-10-11)

Lab host iptv-web-lab-1: A16 GPU 0, driver 595.84, Xeon Gold 6136; 1080p50.

### Clip mezzanine

`cuvidGetDecoderCaps` on the A16 (Ampere): HEVC 4:2:0 and 4:4:4 at 8, 10 and 12 bits are
supported (2 NVDECs, up to 8192 × 8192), HEVC 4:2:2 is not. The mezzanine is therefore HEVC
Main 4:4:4 10 with each 4:2:2 chroma sample repeated (never 4:2:0).

10 s ProRes 422 HQ 4:2:2 10-bit sources, the library's settings (x265 `medium`, CRF 8, a key
frame every second, no B-frames) on 8 cores; PSNR of the round trip back to 4:2:2; NVDEC flat
out to CUDA frames (`ffmpeg -hwaccel cuda`):

| Source | ProRes 422 HQ | Mezzanine | PSNR | x265 | NVDEC |
|---|---|---|---|---|---|
| player-perf clip (made from a 1080p H.264 file) | 1044 MB/min | 318 MB/min (42 Mbit/s) | 57.7 dB | 10.1 fps | 605 fps |
| the same scene from its UHD ProRes | 1156 MB/min | 286 MB/min (38 Mbit/s) | 57.0 dB | 10.1 fps | |
| a promo (1080p59.94 HEVC 4:2:2, conformed) | 2968 MB/min | 154 MB/min (21 Mbit/s) | 56.5 dB | 8.0 fps | 610 fps |
| heavy grain (the UHD scene plus temporal noise) | 2914 MB/min | 1535 MB/min (204 Mbit/s) | 48.4 dB | 3.7 fps | 293 fps |

- The preset `fast` is 1.3–1.6 × faster but up to a third larger (the first clip: 419 MB/min);
  `medium` stays.
- Round trip: a lossless HEVC 4:4:4 clip (repeated chroma) through NVDEC and the engine gives
  every source frame bit for bit, and its sound sample for sample.
- Not used, for comparison: NVENC (`hevc_nvenc`, Rext, constant QP 10) makes the first clip
  at 364 MB/min and 58.2 dB at about 90 fps, on the GPU instead of the CPU.

### Clip players

Engine alone (1 ME, 4 players, no inputs, no mosaic), the CRF 8 clip looping on 0–4 players:

| Playing | NVDEC | GPU (NVML) | Engine CPU | GPU memory |
|---|---|---|---|---|
| 0 | 0 % | 10 % | 0.02 cores | 377 MiB |
| 1 | 4 % | 11 % | 0.07 cores | 547 MiB |
| 2 | 7 % | 14 % | 0.16 cores | 713 MiB |
| 3 | 11 % | 16 % | 0.21 cores | 880 MiB |
| 4 | 14 % | 19 % | 0.25 cores | 1048 MiB |

Decode and conversion 2.4–4.6 ms per frame on the player's own stream; the render total went
from 2.10 to 2.25 ms. A loaded clip takes about 170 MiB for the decoder. Late frames only
right after a load followed at once by play (2–3 per player).

`fxp2-clip-test.py` (engine alone): 19 of 19 with the lossless clip (cued frame 0 holds bit for
bit; play: every grain a source frame, one per grain, 153 pairs in order; the sound sample for
sample; pause; cue 123; loop over the end; stop; the end without loop holds frame 499; play on
cut; the bars still bit for bit; unload is black; a ProRes file and a wrong-size still are
refused), 18 of 18 with the CRF 8 clip.

### FlowXer with the engine (`fxp2-functional.py`)

FlowXer image of this branch, 4 live inputs (test player), 2 clip players, 2 MEs, DSK 1,
`docker --cpus 6`: the 37 M1 checks (IS-05, MEs, re-entry, NMOS, tally, arrival lag) and 27 new:

- library: the colour bars are seeded and play on ME 1 PGM bit for bit (the still's v210);
  a ProRes 422 HQ clip uploaded on air becomes the HEVC 4:4:4 mezzanine (319 MB/min) and its
  sound file next to the original;
- clip-1: cued holds; play makes a new picture every grain with the clip's sound on the PGM
  audio (RMS 0.062); pause holds with silence; cue 480 + play loops; stop; play on cut starts
  it with the Cut; without loop it ends on frame 499; an empty player is black;
- DSK 1 from mxl-browser-source 1.1.0 (`BROWSER_KEY_MODE=v210a`, `transparency.html`: 0, 25,
  50, 75, 100 % white, a red ramp) routed over IS-05 to "DSK 1 Key": ME 1 PGM over Black and
  ME 1 PVW over a camera equal mix10(background, fill, key/1023) with a maximum error of 0
  codes; the opaque white is 940; off; auto over 1 s rises 64 → 940 in even steps and falls
  back; an unrouted key keys nothing.

2 of 2 runs passed all 64 checks. GPU memory of that FlowXer (2 MEs, 4 inputs, a clip and the
bars loaded, 1 key input, mosaic): 867 MiB.

### Conversions on air

A clip conversion runs in FlowXer's container next to the engine. With x265's own threading
it filled the container's CPU limit and the whole container was throttled:

| 10 s clip under a 6-core limit | x265 | Throttled periods | Engine skipped / late |
|---|---|---|---|
| x265 and ffmpeg threads automatic | 9.8 fps | 78 | 8 / 45 |
| 4 x265 workers, decoding and filters on 2 threads (7 runs) | 7.0–9.5 fps | 0 | 0 / 0 |
| 4 x265 workers, decoding and filters on 1 thread (2 runs) | 9.5 fps | 0 | 0 / 0 |

So the library gives the encoder the limit less two cores and everything else one thread
(`conversion_threads`). The first clip of a process still got 1–2 periods throttled at the end
of its conversion: its sound crossfade is the first use of numpy, whose OpenBLAS started a
spinning thread per host core (a burst of 17 cores within 20 ms, `fxp2-cgsample.py`). With
`OPENBLAS_NUM_THREADS=1` in the image, `fxp2-functional.py` (upload and conversion on air)
saw 0 throttled periods, 0 skipped and 0 late grains in 2 of 2 runs.

### NVENC import (2026-10-11, after M2)

NVENC encodes the mezzanine by default since then; x265 is the fallback when NVENC cannot be
opened. Same format (HEVC Rext 4:4:4 10-bit from FFmpeg's `yuv444p16le`, repeated chroma, GOP
50 with an IDR each, no B-frames), constant QP 8: the lowest QP step that keeps the promo at
56 dB. 10 s clips, PSNR of the round trip as in the table above:

| Encoder | player-perf clip | its UHD scene | promo | heavy grain |
|---|---|---|---|---|
| NVENC QP 8 | 406 MB/min, 60.0 dB | 382 MB/min, 59.9 dB | 208 MB/min, 56.0 dB | 2399 MB/min, 52.1 dB |
| NVENC QP 10 | 364 MB/min, 58.2 dB | 339 MB/min, 58.0 dB | 170 MB/min, 55.5 dB | 2037 MB/min, 50.7 dB |
| x265 CRF 8 | 318 MB/min, 57.7 dB | 286 MB/min, 57.0 dB | 154 MB/min, 56.5 dB | 1535 MB/min, 48.4 dB |

- A lossless NVENC clip (`-tune lossless`) plays bit for bit through NVDEC and the engine
  (`fxp2-clip-test.py` 19/19), the QP 8 clip 18/18.
- Speed is the CPU's decoding and scaling: with the 6-core limit, 1 + 1 threads 48 frames/s,
  2 + 2 (what the library uses there) 93–99, 4 + 2 128, 4 + 4 159, all without a throttled
  period. In FlowXer on air (`fxp2-functional.py`, upload of the 10 s ProRes clip): 5.6–6.2 s
  including probe, sound and thumbnail (80–89 frames/s; a minute of clip in about 35 s), NVENC
  at most 55 %, beside the mosaic's NVENC session: 0 skipped or late grains, 0 throttled
  periods, the mosaic at 25 frames/s with no dropped frame.

## P2 M4: reconnect, upgrade, soak (lab, 2026-10-11)

### Preview reconnect

Built-in MediaMTX killed under a console page (headless Edge 155 and Firefox 140.9): every
picture plays again 2.3–2.4 s later (was 12 s). The page connects again when no video packet
came for 1 s and retries every 0.5 s; the engine checks its MediaMTX every 200 ms and the
mosaic publisher retries within 0.5 s.

### Upgrade from FlowXer 14.21.46 (`fxp2-upgrade.py`)

Same `/config`, NMOS seed and output domain: ME 1 PGM keeps its flow ids, the live inputs'
IS-05 activations are restored, their tally INDEX and Black's are unchanged; a multiviewer 1.4.1
reading ME 1 PGM played again 3.2 s after the new image started. The top-level README
(Upgrading from FlowXer 14) lists what an operator loses.

### DSK 1 keys Program only

Keys have no preview (user decision, 2026-10-11): DSK 1 keys ME 1 PGM; PVW, and the mosaic's PVW
tile with it, shows only its source (FlowXer 14 showed the overlay on PVW too). `fxp2-functional.py`
with an image of this change (`fxp2:m4c`): 66 of 66 checks, among them PGM over Black =
mix10(black, fill, key) with a maximum error of 0 codes while PVW is the camera bit for bit, and
an Auto on and off fading PGM only, PVW the camera in every sample. The soak below ran before
this change (DSK over PGM and PVW).

### 2 h soak (`fxp2-soak.py`)

Image `fxp2:m4a`, A16 GPU 0, `docker --cpus 6`, L = 4. 4 MEs (PVW on), 8 live inputs (test
player Out 1–4, replay PGM/PVW, a lab pattern, mxl-browser-source), clip-1 looping a clip,
clip-2 the colour bars, DSK 1 keyed from mxl-browser-source (v210a), 5 WHEP viewers (aiortc,
decoding), a Cut or a 500 ms fade every 10 s on a rotating ME, the 10 s ProRes clip imported
again every 15 min. One sample a minute, 121 samples:

| | Result |
|---|---|
| Output, every ME | 0 skipped, 0 write failures |
| Late grains (committed after their grain time) | 7 at start-up, 1 at about minute 100 (when the test player's own grains came late too) |
| Output lag | 4.0 grains (L) in every sample; commit 10.4 ms into the window on average, max 16.4 |
| GPU render / render + download | 3.59 ms (max 5.05) / 10.43 ms (max 12.21) |
| Container CPU (engine, control plane, MediaMTX for 5 viewers, imports) | 0.71 cores average, max 0.91; 0 throttled periods |
| GPU memory (`nvidia-smi`, GPU 0) | 1268 MiB from start to end (engine process 1255 MiB, its buffers 884 MB) |
| NVENC / NVDEC | 1 session (the mosaic) in every sample, at most 4 %; NVDEC at most 5 % |
| Mosaic | 25.00 fps (180 001 frames), 1 dropped at the encoder start |
| Viewers | 5 readers throughout, each decoding 25.0 fps |
| Clip-1 | 361 155 frames decoded, 3.0 ms each on average, 0 late |
| Operator | 719 Cuts and fades, 0 API errors |
| Imports | 8 of 8 ready, 5.1–5.6 s each |
| Inputs | 0 missing; late grains of their own: test player 5–20 each (all after minute 90), replay PGM/PVW 4527 and 4049 |

NVML's GPU utilization was 86 % on average; it counts the DMA copy time (P1a: 78 % with 4 MEs and
no clip players).

## P3 M4: worst case and soak (lab, 2026-10-12)

The tables are in `docs/engine-p3.md` ("M4 as built"). In short, on A16 GPU 0 (PCIe Gen4 x4):

- **Worst case** (4 MEs, 8 inputs, 24 linear keys on, 4 stinger players, the mosaic): a held wipe
  on every ME renders in 5.52 ms (render + download 12.41 ms, max 14.0), as estimated. Stingers on
  all 4 MEs at once: 7.03 ms (14.09 ms, max 17.6), 0 skipped, 0 late.
- **Upload:** 1.34 ms per key flow per grain. 8 distinct key flows next to 8 inputs keep the link
  busy about 20 ms per grain: with stingers on top, 2 late grains in 2 min. 8 is the edge on
  Gen4 x4 / Gen3 x8 (occasional late grains under full load); FlowXer sets no limit.
- **GPU memory:** 80 MiB per key flow, 347 MiB per stinger player with a stinger loaded.
- **2 h soak** (keys, mixes, wipes, stingers, T-bar, FTB, imports, 5 viewers): 0 skipped, 3 single
  late grains, lag 4, render + download 10.81 ms (max 12.74), GPU memory flat at 1993 MiB, 0 API
  errors.

## P4 M4: worst case with boxes and soak (lab, 2026-10-12)

The tables are in `docs/engine-p4.md` ("M4 as built"). In short, on A16 GPU 3 (PCIe Gen4 x4):

- **Worst case with boxes**, on top of P3's worst case (4 MEs, 8 inputs, 24 linear keys, held
  wipes, the mosaic: render + download 12.61 ms): a half-size pip on every ME 14.06 ms (max 15.46),
  a quad on ME 1 and a pip on the others 15.13 ms (max 16.36), 2 half-size pips on every ME (the
  defaults) 15.47 ms (max 16.86), 8 pips at 1/4 on ME 1 14.57 ms (max 16.69): 0 skipped, 0 late.
  6 pips at 1/4 on every ME (24) 19.23 ms: late grains; 32 boxes at 1/4 render in 22 ms (> 20 ms:
  skipped grains).
- **Per box:** a half-size pip 0.31–0.33 ms, a pip at 1/4 0.22–0.23 ms, a linear box at 1/4 about
  1.33 ms (it resamples the packed v210a fill and key; since #99 0.32 ms, see below);
  `flowxer_engine_dme_ms` shows each ME's resample; a moving box costs what a static one does;
  12.4 MB of GPU memory per keyer with a box.
- **A4000 estimate:** the defaults 10.9 ms (max 12.0), 24 pips 13.0 ms (max 13.6). Recommended at
  1080p50 with 4 MEs at full load: at most 4 boxes per ME (16 in all, about 12 ms); up to 8 per ME
  on one or two MEs; a linear box counts as 1.5 since #99 (4 before), and each key flow with linear
  boxes 1.5 more for its unpack.
- **2 h soak** (2 MEs, 4 pips fading, flying and tied; tied Autos, Cuts, T-bar, stingers, 849
  grabs): 0 skipped, 0 late, lag 4, render + download 6.01 ms (max 7.78), engine GPU memory flat at
  594 MiB, 0 failed grabs, 91 of 91 grabs of the bars exact, 0 API errors.

## Linear boxes unpacked once (#99, lab 2026-10-13)

The tables are in `docs/engine-p4.md` ("Linear boxes unpacked once"). In short, on A16 GPU 0:

- **Per linear box at 1/4:** 0.32 ms in the running engine instead of 1.33 ms (a pip at 1/4:
  0.22 ms); the resample alone 0.31 ms. The unpack of its key flow: 0.23 ms in the isolated bench,
  about 0.4–0.5 ms in the running engine, once per key flow and grain.
- **On top of P3's worst case:** 8 linear boxes on ME 1 render in 8.64 ms (max 9.45) instead of
  16.77 ms (max 19.25): 0 late grains instead of every grain late. 32 boxes at 1/4 (8 of them
  linear) render in 13.93 ms instead of 22.04 ms: 0 skipped. Pips cost what they cost before.
- **GPU memory:** + 23.7 MiB per key flow with a linear box.
- **Bit-exact:** the running engine's linear box against the CPU reference on the key grain from
  MXL, 5 of 5 exact (sizes 1/4 to 1.5, crop, soft edge, border, shadow); GPU self-test 12 boxes exact.
- **A4000 estimate:** a linear box at 1/4 about 0.14 ms (before 0.6 ms). Box limit: a linear box
  counts as 1.5 instead of 4, and each key flow with linear boxes 1.5 more for its unpack.

## P5 M4: audio CPU and soak (lab, 2026-10-13)

The tables are in `docs/engine-p5.md` ("M4 as built"). In short, on the lab host (Xeon Gold 6136,
A16 GPU 3):

- **fx-audio in the running engine** (4 MEs, 8 inputs, 2 clip players, the stinger; typical, busy
  and worst as the bench): 0.51–0.78 ms per grain at 2 channels (max 1.72), 2.59–2.80 ms at 16
  channels (max 6.67): 0.03–0.04 and 0.13–0.14 cores, 1.2–3 × the isolated bench (it adds the MXL
  reads and writes); 0 skipped, 0 late, no wait, no dropped job.
- **Host memory:** the delay lines 3.5 MB for 8 stereo inputs, 28.0 MB for 8 inputs of 16 channels.
- **2 h soak** (2 MEs, 4 inputs, 2 channels; every 1.5–4 s a fader drag, mute, AFV, delay, channel
  map, master move or loudness reset next to P3's and P4's tied Autos, Cuts, T-bar, stingers, FTB,
  boxes and grabs; 2185 actions): 0 skipped, 0 late, lag 4, 0 audio `missing`, fx-audio 0.25 ms per
  grain (max 2.94), the meters sane (a steady tone on ME 2 at −12.0 LUFS in all 721 samples), host
  and GPU memory flat after the first stinger, 0 API errors.
