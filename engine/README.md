# flowxer-engine (prototype P1)

A C++17/CUDA mix engine for the next FlowXer version. It reads up to 8 MXL v210 inputs,
renders 4 MEs (Program and Preview) every grain on the GPU, writes PGM + PVW of every ME back
to MXL, and publishes one preview mosaic (NVENC H.264 → MediaMTX → WHEP). The design is
[DESIGN.md](DESIGN.md); this file says how to build, run and configure it, and what was
measured.

P1 has no NMOS, no audio, no GUI integration: inputs come from configuration and it is
controlled over a small HTTP API. The Python control plane drives it in P2.

## How it works

Each frame of video is a *grain* with an index on the TAI clock (50 per second at 1080p50).

- **Input threads** (`fx-in1` … `fx-in8`), one per input: wait for the next grain of their
  MXL flow, copy it once to the GPU (a direct DMA from the page-locked MXL memory, on the
  input's own CUDA stream) and keep the newest few grains in a small ring.
- **Render thread** (`fx-render`): wakes when output grain *i* becomes the current grain on
  the TAI grid and composes it from input grains *i − L* (fixed latency, `L` = 2 by default).
  It unpacks each input that an ME uses once, renders ME 4 → ME 1 (so ME 1 can take ME 2's
  Program from the same grain), packs all outputs to v210 and, every second grain, draws the
  mosaic. All of this is queued on one CUDA stream; the thread does not wait for the GPU.
  When it is late it skips to the current grain and counts the skip; it never slides.
- **Writer thread** (`fx-writer`): owns the MXL writers. It opens grain *i* on each output,
  queues the DMA download straight into the MXL grain on a separate download stream, waits
  for each copy and commits. Upload, render and download of consecutive grains overlap on
  their streams.
- **Encode thread** (`fx-encode`): copies the NV12 mosaic into an NVENC frame on the GPU,
  encodes it (one session, whatever the number of viewers) and sends it over RTSP/TCP to
  MediaMTX: the platform's shared one (`PREVIEW_PUBLISH_URL`) or, without it, the MediaMTX
  the engine starts itself (bundled in the image; restarted if it exits).
- **Stats thread** (`fx-stats`): CPU per thread and GPU/NVENC load once a second; watches the
  own MediaMTX.

Ownership is simple: each MXL reader belongs to its input thread, every MXL writer to the
writer thread. Grains in flight between render and writer are bounded (3 slots); input frames
come from a fixed pool per input and go back only after the downloads that read them are done.
Every wait sleeps (blocking CUDA sync, futex waits in MXL, condition variables): no busy loops.

### MEs

- Sources: `in1`..`in8` and `me2`..`me4`. ME *m* may take the Program of ME *n* only when
  *n > m* (re-entry, 0 frames, no loops possible).
- Program renders every grain into its own bus buffer (a copy, or the dissolve during a Mix);
  Preview renders the preview source. Both are written to MXL as `<label> ME<m> PGM` and
  `<label> ME<m> PVW`.
- Cut swaps Program and Preview at the next grain. Auto (Mix) dissolves over N grains, then
  swaps. Its progress follows the grain index, so skipped grains do not stretch it.

### Working format

`FLOWXER_ENGINE_WORK_FORMAT` selects the format the MEs render in:
`yuv16` (10-bit 4:2:2 in 16-bit planes, lossless round trip; default) or `rgba16f`
(non-linear R'G'B' in half floats, for effects that need RGB; round trip within 2 codes).

## Build

```bash
docker build -t flowxer-engine:p1 engine/            # build + unit tests (no GPU needed)
docker run --rm --gpus all flowxer-engine:p1 --selftest   # GPU kernels against the CPU reference
```

The image is CUDA 12.8.2 / Ubuntu 24.04 with MXL 218ddaa (as the multiviewer and replay
images) and Ubuntu's FFmpeg (`h264_nvenc`). The GPU, NVENC and NVML come from the NVIDIA
container toolkit: run with a GPU and `NVIDIA_DRIVER_CAPABILITIES=compute,video,utility`
(the image sets it).

Without Docker: CMake ≥ 3.24, a CUDA toolkit, MXL installed (`CMAKE_PREFIX_PATH`),
`libavcodec/libavformat/libavutil`, `uuid` and nlohmann-json. `-DFXENG_BUILD_ENGINE=OFF`
builds only the unit tests (`fxeng-tests`), which need none of these.

## Run

The engine needs the MXL domains (`/Volumes/mxl`) and a GPU. Without `PREVIEW_PUBLISH_URL`
it runs its own MediaMTX for the mosaic (self-contained); on the platform it publishes to the
shared one.

```bash
docker run -d --name fxeng-engine --network host --user 1000:1000 --gpus '"device=0"'   -v /Volumes/mxl:/Volumes/mxl   -e FLOWXER_ENGINE_INPUTS='[{"domain":"/Volumes/mxl/player","flow":"<flow id>","label":"Cam 1"}, ...]'   flowxer-engine:p1
```

Then:

```bash
curl -s localhost:9630/status | jq .output
curl -s -X POST localhost:9630/me/1/preview -d '{"source":"in3"}'
curl -s -X POST localhost:9630/me/1/auto -d '{"frames":25}'
curl -s -X POST localhost:9630/me/1/program -d '{"source":"me2"}'   # re-entry
curl -s -X POST localhost:9630/me/2/cut
```

The test page is `http://<host>:9630/mosaic` (needs `FLOWXER_ENGINE_HTTP_BIND=0.0.0.0`, or
a tunnel). It opens one WHEP session at `<PREVIEW_WHEP_URL or own MediaMTX>/<prefix>/mosaic/whep`
and shows the same `MediaStream` in one `<video>` per tile, each cropped with CSS
`object-view-box: inset(…)` from `GET /mosaic/map` (Chrome/Edge 104+). `?viewers=10` opens 10
independent WHEP sessions.

## Configuration

Environment variables (or the same keys in a JSON file named by `FLOWXER_ENGINE_CONFIG`; the
environment wins).

| Variable | Default | Meaning |
|---|---|---|
| `FLOWXER_ENGINE_INPUTS` | (required) | JSON array of 1–8 `{"domain": "<MXL domain dir>", "flow": "<flow id>", "label": "…"}` |
| `FLOWXER_ENGINE_MES` | `4` | MEs (1–4), all rendering every grain |
| `FLOWXER_ENGINE_LATENCY` | `2` | L: output grain *i* is made from input grains *i − L* (1–8) |
| `FLOWXER_ENGINE_FORMAT` | `1080p50` | `720p`/`1080p`/`2160p` with `25`, `2997`, `30`, `50`, `5994`, `60`; inputs must match |
| `FLOWXER_ENGINE_OUTPUT_DOMAIN` | `/Volumes/mxl/flowxer-engine` | own MXL output domain (created) |
| `FLOWXER_ENGINE_LABEL` | `FlowXer engine` | prefix of the output flow labels |
| `FLOWXER_ENGINE_WORK_FORMAT` | `yuv16` | `yuv16` or `rgba16f` |
| `FLOWXER_ENGINE_GPU` | `0` | CUDA device index (inside the container) |
| `FLOWXER_ENGINE_HTTP_BIND` | `127.0.0.1` | control API / metrics / test page address |
| `FLOWXER_ENGINE_HTTP_PORT` | `9630` | its port |
| `FLOWXER_ENGINE_HOLD_GRAINS` | `25` | a stalled input holds its last grain this long, then black |
| `FLOWXER_ENGINE_MOSAIC` | `true` | preview mosaic on/off |
| `FLOWXER_ENGINE_MOSAIC_FPS` | `25` | mosaic frame rate (every second grain at 50p) |
| `FLOWXER_ENGINE_MOSAIC_KBPS` | `6000` | NVENC bit rate (CBR) |
| `PREVIEW_PUBLISH_URL` | (empty) | shared MediaMTX to publish to (RTSP, e.g. `rtsp://mxl-mediamtx.mxl-platform.svc:8554`); empty: start the own MediaMTX |
| `PREVIEW_PATH_PREFIX` | `flowxer-engine` | stream path prefix (`<production>/<function>`); the mosaic is `<prefix>/mosaic` |
| `PREVIEW_WHEP_URL` | (empty) | public WHEP base: the page plays `<base>/<prefix>/mosaic/whep`; empty: own MediaMTX on the page's host |
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
| 9630/tcp | control API, `/status`, `/metrics`, `/mosaic` | localhost by default |
| 8654/tcp | own MediaMTX RTSP ingest | localhost only; not used with `PREVIEW_PUBLISH_URL` |
| 8989/tcp | own MediaMTX WHEP | browsers |
| 8988/tcp | own MediaMTX HLS | browsers |
| 8289/udp+tcp | own MediaMTX ICE | browsers |
| 9897/tcp | own MediaMTX API | localhost only |

With `PREVIEW_PUBLISH_URL` set only 9630 is used. The defaults avoid FlowXer (9610/9620), the
multiviewer (8110) and mxl-webrtc-monitor (8100, MediaMTX 8554/8888/8889/8189/9997/9998).
RTMP, SRT and MoQ are off in the own MediaMTX.

## API

| Method and path | Body | Answer |
|---|---|---|
| `POST /me/{m}/preview` | `{"source": "in3"}` | the ME; 400 for a source the ME may not take, 409 during a Mix |
| `POST /me/{m}/program` | `{"source": "me2"}` | same |
| `POST /me/{m}/cut` | – | the ME |
| `POST /me/{m}/auto` | `{"frames": 25}` (1–1000) | the ME; 409 while a Mix runs |
| `GET /status` | – | per-ME state, timing (GPU ms per stage, lag), counters, inputs, CPU per thread, GPU/NVENC, `preview` (mode own/shared, publish state per stream) |
| `GET /metrics` | – | Prometheus text (`flowxer_engine_*`) |
| `GET /mosaic/map` | – | canvas size, stream `path`, `whep_base`/`hls_base` (or own ports), tiles `{id, kind, label, x, y, w, h}` |
| `GET /mosaic` | – | the test page |

## Reuse and attribution

See [NOTICE](NOTICE). From the same author's MXL media functions:
mxl-multiviewer (Apache-2.0): CUDA v210 sample addressing, page-locked MXL grain DMA,
MXL reader handling (resync on TOO_LATE, re-open on FLOW_INVALID), the HTTP server, the CPU
v210 reference; mxl-webrtc-monitor (MIT): the MediaMTX config and the NVENC settings;
mxl-replay: the TAI loop rule (re-implemented on MXL's own index formulas, no code copied:
mxl-replay is GPL-3.0).

## Measurements

See the end of this file (filled in from the lab runs).
