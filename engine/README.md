# flowxer-engine

FlowXer's C++17/CUDA media engine. It reads the live MXL v210 inputs (with their audio), plays
the clip players (clips decoded by NVDEC, stills), renders up to 4 MEs (Program and Preview)
every grain on the GPU with the downstream keyer, writes PGM video + audio and PVW video of
every ME back to MXL, and publishes one preview mosaic (NVENC H.264 → MediaMTX → WHEP).

Since P2 it is FlowXer's media path: the Python control plane (`src/flowxer/engine/runtime.py`)
starts it as a child process with a configuration file, restarts it with backoff, and drives
it over its control API on 127.0.0.1 (NMOS, IS-05 routes, tally, the library and the GUI stay
in Python). The P1 design is [DESIGN.md](DESIGN.md); the P1 sections below (drop-in,
measurements) are kept as its record.

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
  The downstream keyer keys over its ME's Program and Preview there.
- **Clip player threads** (`fx-cp1` …), one per clip player: decode the loaded clip ahead
  (NVDEC) or upload the still once; see [Clip players](#clip-players).
- **Writer thread** (`fx-writer`): owns the MXL writers. It opens grain *i* on each output,
  queues the DMA download straight into the MXL grain on a separate download stream, waits
  for each copy and commits. Then it writes the PGM audio of grain *i* (see below). Upload,
  render and download of consecutive grains overlap on their streams.
- **Encode thread** (`fx-encode`): copies the NV12 mosaic into an NVENC frame on the GPU,
  encodes it (one session, whatever the number of viewers) and sends it over RTSP/TCP to
  MediaMTX: the platform's shared one (`PREVIEW_PUBLISH_URL`) or, without it, the MediaMTX
  the engine starts itself (bundled in the image; restarted if it exits).
- **Stats thread** (`fx-stats`): CPU per thread and GPU/NVENC load once a second; watches the
  own MediaMTX.

Ownership is simple: each MXL reader belongs to its input thread (video) or to the writer
thread (audio), every MXL writer to the writer thread. Grains in flight between render and
writer are bounded (3 slots); input frames come from a fixed pool per input and go back only
after the downloads that read them are done. Every wait sleeps (blocking CUDA sync, futex
waits in MXL, condition variables): no busy loops.

### MEs

- Sources: the live inputs `in1`..`inN`, the clip players `cp1`..`cpC`, `black` and
  `me2`..`me4`. ME *m* may take the Program of ME *n* only when *n > m* (re-entry, 0 frames,
  no loops possible).
- Program renders every grain into its own bus buffer (a copy, or the dissolve during a Mix);
  Preview renders the preview source. Both are written to MXL (`<label> ME<m> PGM`,
  `<label> ME<m> PVW`).
- Cut swaps Program and Preview at the next grain. Auto (Mix) dissolves over N grains, then
  swaps. Its progress follows the grain index, so skipped grains do not stretch it.
- An input without a video flow is a black slot. It keeps the numbering (the TSL tally and the
  operator's habits), e.g. a camera that is not connected.

### PGM audio (audio follows video)

- Each ME's PGM audio is the audio of its Program source: an input's audio flow, a clip
  player's sound, or the PGM audio of the re-entered ME. Preview has no audio; the mosaic has
  none.
- During a Mix the two sources crossfade with **equal power** (gains cos and sin of t·90°, so
  two unrelated sources keep their loudness; −3 dB each in the middle). t ramps sample by
  sample through each grain, so there are no steps.
- Same TAI grid and latency as the video: the audio of output grain *i* (48 kHz samples of
  grain *i*'s time span) is the input audio of grain *i − L*. A/V stay aligned as they came in.
- float32, 48 kHz. Channels: those of the output flow (`FLOWXER_ENGINE_AUDIO_CHANNELS`, default
  2, for a flow the engine creates; an existing flow keeps its own); input channels beyond that
  are dropped, missing ones are silent.
- CPU only, in the writer thread: 0.05 ms per grain for 4 MEs. A source whose audio is not
  there (stalled, missing) is silent for that grain and counted; a skipped video grain still
  gets its audio, so the audio flow has no holes.

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
  cut, frames decoded ahead, decode time, late frames, error.
- GPU memory: 9 v210 pictures per player (50 MB at 1080p) from the start, plus about 170 MB
  for the NVDEC decoder while a clip is loaded (its surfaces and FFmpeg's frame pool, 4:4:4
  16-bit). NVDEC: one 1080p50 player at 40–60 Mbit/s takes about 4 % of the A16 GPU's
  decoders, 6 % of a CPU core.

### Downstream keyer (DSK)

`FLOWXER_ENGINE_KEYS` gives each key input (a v210a flow: the v210 fill followed by a 10-bit
key plane, e.g. mxl-browser-source in `v210a` mode) its DSK on one ME (FlowXer: `DSK 1 Key` on
ME 1). It keys linearly over the ME's Program and Preview (Preview shows what Program will look
like): out = fill · α + background · (1 − α) with α = key / 1023 · gain, per sample (chroma: the
mean α of its pixel pair), straight fill. The key is read at grain *i − L* like an input; an
unrouted key input keys nothing. `POST /dsk/<k>` `{"on": bool}` cuts it, `/dsk/<k>/auto`
`{"frames": n}` fades the gain over n grains.

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
| `FLOWXER_ENGINE_MES` | `1` | rendering MEs (1–4), all rendering every grain |
| `FLOWXER_ENGINE_LATENCY` | `4` | L: output grain *i* is made from input grains *i − L* (1–10) |
| `FLOWXER_ENGINE_FORMAT` | `1080p50` | `720p`/`1080p`/`2160p` with `25`, `2997`, `30`, `50`, `5994`, `60`; inputs and clips must match |
| `FLOWXER_ENGINE_OUTPUT_DOMAIN` | `/Volumes/mxl/flowxer-engine` | MXL output domain directory of the MEs (created if missing) |
| `FLOWXER_ENGINE_OUTPUT_DOMAIN_ID` | (generated) | `id` written to `domain_def.json` when the engine creates the domain (BCP-007-03 fields id, label, description, tags); an existing file is kept |
| `FLOWXER_ENGINE_OUTPUTS` | (generated) | JSON array of `{"me", "domain", "domain_id", "pgm_video", "pgm_audio", "pvw_video", "label", "group"}`, all but `me` optional; an empty id is generated (stable for domain + ME + kind) |
| `FLOWXER_ENGINE_PVW_OUTPUTS` | `true` | write the PVW video flows; `false` renders PVW for the mosaic only |
| `FLOWXER_ENGINE_AUDIO_CHANNELS` | `2` | channels of a PGM audio flow the engine creates |
| `FLOWXER_ENGINE_AUDIO_WAIT_MS` | `8` | longest wait for input audio that is not there yet when its grain is written (0–40) |
| `FLOWXER_ENGINE_LABEL` | `FlowXer` | prefix of the labels of the flows the engine creates |
| `FLOWXER_ENGINE_WORK_FORMAT` | `yuv16` | `yuv16` or `rgba16f` |
| `FLOWXER_ENGINE_GPU` | `0` | CUDA device index (inside the container) |
| `FLOWXER_ENGINE_HTTP_BIND` | `127.0.0.1` | API and metrics: address |
| `FLOWXER_ENGINE_HTTP_PORT` | `9630` | and port |
| `FLOWXER_ENGINE_HOLD_GRAINS` | `0` | a stalled input holds its last grain this long, then black; 0 = until it comes back |
| `FLOWXER_ENGINE_MOSAIC` | `true` | preview mosaic on/off |
| `FLOWXER_ENGINE_MOSAIC_FPS` | `25` | mosaic frame rate (every second grain at 50p) |
| `FLOWXER_ENGINE_MOSAIC_KBPS` | `6000` | NVENC bit rate (CBR) |
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
| `POST /me/{m}/take` | `{"source", "frames", "preview"}` | Program to the source, cut (0 frames) or mixed, with an optional new Preview |
| `POST /me/{m}/cut` | – | the ME |
| `POST /me/{m}/auto` | `{"frames": 25}` (1–1000) | the ME; 409 while a Mix runs |
| `PUT /input/{k}` | `{"video": {"domain", "flow"}, "audio": {"domain", "flow"}}` | opens (or, without a flow, closes) the input's readers |
| `PUT /key/{k}` | `{"domain", "flow"}` | the same for a key input (v210a) |
| `POST /dsk/{k}` | `{"on": true}` | the DSK on or off at the next grain |
| `POST /dsk/{k}/auto` | `{"frames": 25}` | fades the DSK on (or off when it is on) |
| `POST /player/{k}/load` | `{"path", "kind": "clip"\|"still", "audio", "channels", "frames", "loop", "play_on_cut"}` | loads and cues the first frame (`path` "" unloads: black); the file loaded already only changes loop and play on cut; 409 with the reason for a file that is no mezzanine |
| `POST /player/{k}/cue` | `{"frame": n}` | shows frame n and holds |
| `POST /player/{k}/play`, `/pause`, `/stop` | – | transport |
| `GET /status` | – | per-ME state and output flows (`existing` = taken over), timing (GPU ms per stage, lag), counters, inputs and keys (video and audio state, arrival lag), clip players, DSKs, CPU per thread, GPU/NVENC, `preview` (mode own/shared, publish state per stream), `audio` |
| `GET /metrics` | – | Prometheus text (`flowxer_engine_*`) |
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
picture, dissolve within 1 code.

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
