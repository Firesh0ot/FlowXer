# FlowXer mix engine: prototype P1

Status: design for the first prototype of the next FlowXer version (decided by the user on
2026-10-10). The prototype answers one question: does a C++/CUDA media engine, driven by
FlowXer's existing Python control plane, give us the headroom a real vision mixer needs?
It is measured on the lab server and then on the platform's small cluster before the full
feature set is built.

## Why

Today's media path is a GStreamer pipeline with GStreamer GL. All GPU work of a context runs
on one GL thread (`gstglcontext`):
- the upload and v210 unpack of every input;
- the composite;
- the Program pack and download;
- every monitor download.

Frames are copied several times between MXL and the GPU. CEF (software GL) and per-viewer
WebRTC encodes run inside the mixer process.

Measured: the GL thread is at ~100 % on the platform's host-03 with one ME. Each input costs
11–15 % of that thread. The planned features cannot fit: 4 MEs rendering PGM + PVW permanently,
DME/PiP, wipe patterns, frame stores, several DSKs.

For comparison, mxl-multiviewer reads MXL v210, unpacks, scales, composites and writes back in
about 2 ms per frame on CUDA.

## Scope of P1

In:
- `flowxer-engine`: one C++17/CUDA process.
- **Inputs:** 8 MXL v210 video inputs, read directly from MXL. Each grain is uploaded once by
  DMA from page-locked MXL memory, as mxl-multiviewer does.
- **4 MEs, all rendering every frame.** Each has:
  - Program and Preview source selection;
  - transitions: Cut, and Mix (dissolve, duration in frames);
  - two outputs, PGM and PVW (v210, MXL flows). PVW shows the Preview source.
- **Re-entry:** ME m may select ME n's PGM when n > m. MEs render in descending order, so the
  re-entry frame is the same output frame (0-frame re-entry, no loops possible).
- **Fixed processing latency L** (default 2 grains):
  - output grain i is composed from input grains i − L;
  - upload, compose and pack/download of consecutive grains overlap on separate CUDA streams;
  - the output index always comes from TAI;
  - when the engine is late, it skips to the current grain and counts the skip. It never slides.
- **Preview mosaic:**
  - one 1920×1080 canvas, 4×4 tiles of 480×270 (8 inputs, 4 × PGM, 4 × PVW), at 25 fps;
  - encoded once with NVENC (H.264) and published to MediaMTX (RTSP in, WHEP out), as
    mxl-webrtc-monitor does;
  - a tile map JSON (`GET /mosaic/map`) with each tile's x, y, w, h;
  - a test page shows several tiles from the one WebRTC stream: the same `MediaStream` in
    several `<video>` elements, cropped with CSS `object-view-box: inset(…)`;
  - no audio preview.
- **Control:** a small HTTP JSON API on localhost.
  - `POST /me/{m}/preview {source}`, `/me/{m}/program {source}`, `/me/{m}/cut`,
    `/me/{m}/auto {frames}`.
  - `GET /status`: per-ME state, timing, counters.
  - `GET /metrics` (Prometheus).
  - Sources are `in1..in8` and `me2..me4` (re-entry).
- **Instrumentation:**
  - GPU time per stage (CUDA events): upload, compose per ME, pack, download, mosaic, encode;
  - grains late or skipped;
  - output lag versus TAI;
  - CPU per thread;
  - GPU and NVENC utilisation (NVML).

Out (later phases, the full feature set): audio (AFV / mixing), wipes with patterns, DME/PiP,
keyers and DSKs (v210a key inputs from mxl-browser-source), frame stores, NMOS, the Python
control-plane integration, tally export, the GUI.

P1 talks to MXL and to its own HTTP API only. NMOS routing and the FlowXer GUI stay in the
current FlowXer until the integration phase.

## Architecture

- **Input threads:** one per input. Each one:
  - reads grain i from its MXL reader (blocking with timeout, TOO_LATE → resync to the head);
  - queues the DMA upload into a device ring slot (depth ≥ L + 1) on the input's own CUDA stream;
  - records an event per slot.
- **Render thread:** one, on the TAI grid. For each output grain i it:
  1. waits for the upload events of grains i − L of the sources the MEs need;
  2. composes ME 4 → ME 1 on the render stream (PGM: A/B dissolve from the source textures; PVW: the
     preview source), plus the mosaic (scaled tiles);
  3. packs the outputs to v210 and queues the DMA downloads into the MXL writers' page-locked
     grains on a download stream;
  4. hands the mosaic NV12 frame to NVENC every second grain.
- **Writer thread:** waits for each output's download event, then commits the grain at index i.
- **Encode thread:** NVENC → RTSP to MediaMTX (`MEDIAMTX_RTSP_URL`, as in mxl-webrtc-monitor).

Device memory per input or output frame: 1080p v210 ≈ 5.5 MB, or an unpacked working format of
choice. Pick the internal format by measurement: YUV 4:2:2 16-bit planar, or RGBA16F if later
effects need it. Report the memory budget.

### Reuse
mxl-multiviewer (Apache-2.0, same author):
- `src/media/cuda_compose.cu/.hpp` (page-locked MXL grain DMA, unpack, scale, compose, pack);
- `src/media/v210.cpp`;
- the MXL reader/writer handling in `src/mxlio/engine.cpp` (resync, FLOW_INVALID re-open).

mxl-replay:
- the TAI playout loop in `src/main.cpp` (one grain per period, resync when more than 2 grains late).

mxl-webrtc-monitor:
- MediaMTX publishing (`src/ops/mediamtx.cpp`) and its encoder setup.

Copy what is needed into `engine/` and keep attribution.

## Measurements and success criteria

Lab (A16, the vmix-like input set from the test player plus internal patterns), 1080p50,
8 inputs, 4 MEs with PGM + PVW, mosaic on, 10 WHEP viewers of the mosaic:
- 50.0 fps on every output for 30 min; output lag ≤ L + 1 grains; 0 skips on a quiet host.
- GPU time per output frame (all stages): target ≤ 8 ms. The budget is 20 ms.
- Engine CPU: target ≤ 1.5 cores in total (the current FlowXer uses 3.6–4.4 cores for 1 ME).
- NVENC: one session regardless of the number of viewers.
- **Under load** (host CPU at ~80 %, bounded stress): still 50 fps. Any skips are counted and the
  output stays on the TAI grid.
- **A/B comparison:** the same inputs through FlowXer 14.21.46 (`fx-mi.sh`, 1 ME), as a fps / CPU / GL
  table next to the engine's numbers.

Then on small (host-03, RTX A4000) with the same test, once the lab numbers hold. The platform
deploys a prototype image; see Delivery.

## Test plan
1. **Unit tests:** v210 pack/unpack round trip, the dissolve maths, the re-entry order, the tile map,
   the TAI index arithmetic, skip handling.
2. **Lab:**
   - functional: cut and auto on every ME, re-entry ME 2 → ME 1, PVW outputs, the mosaic page with
     cropped tiles;
   - performance matrix: quiet / load / 10 viewers;
   - a 30 min soak.
3. **Small:** the same matrix via the platform agent.

## Delivery
- **Code:** FlowXer repo, branch `proto/mix-engine` from `dev`, directory `engine/` (CMake, CUDA).
  A draft PR into `dev`, not merged during the prototype. `VERSION` untouched.
- **Image:** `engine/Dockerfile` (CUDA runtime + MXL 218ddaa + FFmpeg/NVENC as in the
  monitor/replay images).
  - Lab: built with `~/mxl-lab/bin/lab-build`.
  - small: a prototype tag. Where the platform can pull it from is decided with the user before
    the small test.
- **Report:** numbers, the comparison table, and the risks found.
- **For the platform's small test, report:**
  - image digest;
  - GPU required, with GPU memory per ME count (1–4);
  - env names and defaults;
  - ports (HTTP control/metrics, MediaMTX RTSP/WHEP/ICE; must not collide with FlowXer 9610/9620,
    the multiviewer or the monitor).
- **P1 configuration:** P1 has no NMOS. Its inputs come from configuration: env/JSON with an MXL
  domain path + flow id per input. On small, the platform points them at flows that already exist
  on the node, e.g. the vmix production's sources on host-03. The result is read from `/status`,
  `/metrics` and the mosaic page.
- **What P2 adds** (the integration phase), so the platform can plan:
  - NMOS (ME n PGM/PVW sender labels, input and key receiver labels);
  - the designer contract (values_schema, liveInputs, inputLabels, panels);
  - `FLOWXER_RENDER_MES` (default all panels).
- **Production for the small test:** the platform runs P1 in its own production, not in test-all
  or friday-night-show.

## Risks to watch
- **GPU sharing on host-03:** the A4000 is time-sliced with other functions.
- **NVENC session limits:** the A16 and A4000 limits are OK for one session per engine.
- **Page-locking MXL grain memory:** fabrics mirrors and re-created flows (FLOW_INVALID) → re-register.
- **Audio** comes in the next phase. It must stay aligned with video at a fixed L.
