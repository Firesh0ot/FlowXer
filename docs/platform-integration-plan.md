# FlowXer on the SRF MXL proof-of-concept platform

Decision log for running FlowXer as an NMOS media function on two RKE2 /
Ubuntu 24.04 lab clusters. Written **before** the implementation PRs. Update
this file on every deviation from the original prompt.

Related references (read, not copied):

- [NVIDIA NvNmos](https://github.com/NVIDIA/nvnmos) (Apache-2.0)
- [LeeO86/mxl-decklink](https://github.com/LeeO86/mxl-decklink)
- [LeeO86/mxl-fabrics-agent](https://github.com/LeeO86/mxl-fabrics-agent)
- [AMWA BCP-007-03](https://specs.amwa.tv/bcp-007-03/releases/v1.0.0/docs/NMOS-With-MXL.html)
- Pinned MXL: `dmf-mxl/mxl` `release/v1.1` commit `218ddaa`

---

## 1. What exists today (origin/dev after PR #34 / #35)

FlowXer is a FastAPI control plane plus a single GStreamer pipeline. It is
**not** an NMOS node. `src/flowxer/domain/nmos.py` only builds NMOS-shaped
`flow_def.json` for MXL grains.

| Area | Today |
|---|---|
| Settings | `FLOWXER_*` via Pydantic. Host default `0.0.0.0`, port `9610`. One path `FLOWXER_MXL_DOMAIN` (container `/mxl-domain`). |
| Inputs | Kinds `mxl_live`, `file`, `replay`, `test`, `black`. Live inputs take `video.flow_id` / `audio.flow_id` and optional `group_hint`. No `domain_id`. |
| Pipeline | One `Gst.parse_launch` string at `VisionMixer.start()`. Program/preview switching is `input-selector` `active-pad`. No per-branch rebuild while PLAYING. |
| mxlsrc | `video-flow-id` **or** `audio-flow-id` (exactly one) + `domain` (filesystem path). Caps: v210 / F32LE. Also `data-flow-id` in the plugin; unused. |
| mxlsink | `flow-id` + `domain`, plus optional `label` / `description` / `group-hint`. Creates `*.mxl-flow/flow_def.json` from caps. |
| PGM IDs | UUIDv5 from `FLOWXER_GROUP_HINT` + role. Stable across restarts. |
| Domain files | Image and entrypoint copy `configs/domain_def.json` into `/mxl-domain` with a **fixed** `id` (`flowxer-domain`). Fabrics agent would treat every instance as the same domain. |
| API | `/api/v1/*`, `/api/v1/health`, WHEP, JPEG preview. Auth via `FLOWXER_API_TOKEN`. No `/metrics`, `/livez`, `/readyz`. |
| GUI | React on 9620. nginx upstream is Compose service name `vision-mixer:9610`. No CDN fonts/scripts. Status chip has CPU/RAM/mixer, not NMOS. |
| WebRTC | aiortc, host ICE only, no public IP or UDP port range. JPEG fallback exists. |
| Image | Ubuntu 24.04, MXL `v1.1.0` + gstcefsrc built in. Runs as **root**. GHCR tags: `{version}` and `latest` from `main`. |
| Compose | Pulls GHCR. Mixer published on `127.0.0.1:9610`. tmpfs volume named `mxl-domain`. |
| Tests | pytest simulate-mode; GUI `tsc --noEmit`. No nmos-cpp registry, no AMWA testing tool. |

Features that **must keep working**: REST API, GUI, cut/fade/wipe, stingers, DSK/keyers, TSL tally, file/replay player.

---

## 2. Verified APIs (do not assume the prompt)

### 2.1 gst-mxl-rs (`v1.1.0` and `218ddaa` / `release/v1.1`)

Commit `218ddaa` is a cherry-pick of a **README-only** Fabric API heading change
onto `release/v1.1`. gst-mxl-rs properties are unchanged from tag `v1.1.0`.

**mxlsrc** (from `rust/gst-mxl-rs/src/mxlsrc/imp.rs`):

- Properties: `video-flow-id`, `audio-flow-id`, `data-flow-id`, `domain`.
- All are `mutable_ready()` — they can change in NULL/READY, **not** while PLAYING.
- Set **exactly one** flow-id property.
- `domain` is a **filesystem path**, not an MXL domain UUID.
- Caps: `video/x-raw,format=v210` ↔ `video/v210`; `audio/x-raw,format=F32LE` ↔ `audio/float32`.
- Missing flow: `start()` does not attach the reader; `create()` waits for the
  flow (`FLOW_NOT_FOUND`). Pipeline can reach PLAYING before the producer exists.
- Live latency advertised as one grain period for discrete (video) flows.
- **No grain-offset / read-head property.**

**mxlsink**: `flow-id`, `domain`, `label`, `description`, `group-hint`. Writes
`flow_def.json`. Group-hint default is `Media Function <pid> …` if unset.

**Decision:** pass a **resolved path** to `mxlsrc`/`mxlsink` `domain=`. FlowXer
scans `/Volumes/mxl/*/domain_def.json` and maps `id` → path. `mxl://` URIs on
MXL `main` are orchestration-only; they are not gst properties.

### 2.2 MXL on-disk layout

- Domain = directory with `domain_def.json`. Identity is the JSON `id`, never
  the directory name.
- Flows: `{uuid}.mxl-flow/flow_def.json`.
- Ring depth is **domain-global** from `options.json`
  (`urn:x-mxl:option:history_duration/v1.0`, default 200 ms). Write
  `options.json` **only when creating** FlowXer's output domain.
- Mirror domains: `/Volumes/mxl/mirror-<source-domain-id>/` plus
  `x-mxl-fabrics-agent` in `domain_def.json`. Readable; never writable.
- Ignore unknown JSON fields when scanning.

### 2.3 BCP-007-03 (v1.0)

- Sender/Receiver `transport`: `urn:x-nmos:transport:mxl`.
- IS-05 params: `mxl_domain_id`, `mxl_flow_id` on staged/active/constraints.
- Receiver `mxl_flow_id` accepts `null`, **must not** accept `"auto"`.
- Receiver `mxl_domain_id` accepts `null` and may accept `"auto"`.
- Receivers must accept a PATCH that omits `transport_file` or sets it null.
- Spec also says reject a domain the node is **not capable of accessing**.
  Scanning the whole MXL root means we *are* capable of any domain that appears
  later, including mirrors. Receiver constraints for `mxl_domain_id` /
  `mxl_flow_id` are therefore **unconstrained** (any UUID or null).

### 2.4 NVIDIA NvNmos

- License: **Apache-2.0**. Built on Sony nmos-cpp. Supports IS-04 v1.3,
  IS-05 v1.2, BCP-004-01, **BCP-007-03**.
- Three integration modes: C library, `nvnmosd` gRPC daemon, `gst-nmos-rs`
  (`nmossrc`/`nmossink`).
- Identity: UUIDv5 from **node seed** + caller-chosen **name**. Node/Device
  IDs depend only on the seed. Keep seed and names stable across restarts.
- NMOS Flow UUID ≠ MXL `mxl_flow_id`.
- Static registry: `NvNmosNetworkServicesConfig.registration_address` **disables DNS-SD**.
- Advertised addresses: `NvNmosNodeConfig.host_addresses` (management IP, not `0.0.0.0`).
- Controller activation: gRPC `SubscribeActivations` then `AckActivation`.
  Immediate activation: NACK → HTTP 500 to the controller.
- Application-originated change: `SyncResourceState` (REST PATCH → IS-05 active
  + IS-04 `subscription`).
- MXL receiver configuring file: unconstrained domain when
  `urn:x-nvnmos:tag:mxl-domain-id` is omitted/empty; activation then carries
  the controller-supplied `mxl_domain_id` / `mxl_flow_id`.
- HTTP port + next port for nmos-cpp WebSocket (same pattern as mxl-decklink
  3212/3213). We use **3252 / 3253**.

### 2.5 Fabrics agent expectations

From its README “What a media function needs”:

1. Resolve `mxl_domain_id` by scanning the MXL root (including `mirror-*`).
2. If the flow is not present yet, retry with backoff; do not fail permanently.
3. A flow that exists but has no new grains is not a fatal error.
4. Do not build with `MXL_ENABLE_FABRICS_OFI`.
5. Sit at least one grain behind the mirror head.

mxl-decklink **rejects** IS-05 when the domain is not on disk yet (needs
`MIRROR_MODE=eager`). FlowXer is specified as an **on-demand** reader: accept
the activation, state `waiting`, retry scan.

---

## 3. Decisions

### 3.1 NMOS implementation — **Option C (in-process FastAPI node)**

Evaluate order required Option A first.

| Option | Verdict |
|---|---|
| A: NvNmos `nvnmosd` | Evaluated first. Apache-2.0, BCP-007-03, seed IDs, static registry. **Not used:** `AckActivation` NACK becomes HTTP 500, so a missing domain/flow cannot ACK-then-wait. Building nmos-cpp + gRPC into the mixer image is a large extra toolchain. |
| A: `gst-nmos-rs` | **Rejected.** `nmossrc`/`nmossink` own 1:1 pipelines. FlowXer needs `input-selector` + compositor + CEF overlay + stinger. |
| B: nmos-cpp sidecar | **Rejected.** Same C++ stack as NvNmos without a Python control-plane API. |
| **C: FastAPI IS-04/IS-05** | **Chosen.** Same process as the mixer. Immediate activations ACK when params are well-formed; the input goes to `waiting` and retries. Node API on 3252, Connection API IS-05 v1.2, BCP-007-03 `mxl_domain_id` / `mxl_flow_id`. Must still pass AMWA IS-04-01 / IS-05-01 / IS-05-02 (work item 9). Revisit nvnmosd if those suites fail for reasons Option C cannot fix. |

**Ack vs waiting:** NvNmos NACKs become HTTP 500. The prompt requires accepting
activations for missing domains. FlowXer **ACK success** as soon as
parameters are well-formed, puts the input in `waiting`, and retries attach.
Malformed UUIDs still return a standard IS-05 400.

**Process layout:** when `FLOWXER_NMOS_ENABLE=true`, `create_app` lifespan
starts the Node API (uvicorn thread, bind `0.0.0.0:3252`) plus a waiting-retry
thread and, if `FLOWXER_NMOS_REGISTRY_URL` is set, a registration heartbeat.
`FLOWXER_NMOS_BIND=false` skips the listen socket (pytest). Kubernetes stay
at two containers (mixer, GUI). `FLOWXER_NMOS_ENABLE=false` keeps today’s
behaviour and does not bind 3252.

**IDs:**

- Seed: `FLOWXER_NMOS_SEED` (default `{hostname}-flowxer`).
- Device label: `FlowXer Vision Mixer`.
- Receiver names: `{input_id}-video`, `{input_id}-audio` (UUIDv5 from seed + those parts).
- Sender names: `{panel_id}-pgm-video`, `{panel_id}-pgm-audio`.
- IS-04 Flow UUID ≠ MXL `mxl_flow_id` (copied into sender active params).

### 3.2 Multi-domain MXL

- Read root: `FLOWXER_MXL_ROOT` default `/Volumes/mxl`.
- Output dir: `FLOWXER_MXL_OUTPUT_DOMAIN_DIR` default `{root}/flowxer-{seed-short}`.
- Output id: `FLOWXER_MXL_OUTPUT_DOMAIN_ID` or UUIDv5(seed, `mxl-output-domain`).
- Scan direct children with `domain_def.json` on every resolve; no negative cache.
- `domain_id` on each essence (IS-05 `mxl_domain_id` or REST). If REST omits it,
  default to FlowXer's **output** domain (backward compatible with a single
  shared domain).
- Refuse writes to `mirror-*` directories or domains whose `domain_def.json`
  contains `x-mxl-fabrics-agent`.
- Remove the baked-in copy of `configs/domain_def.json` from the image and
  entrypoint. Creating the output domain at start is enough.
- `FLOWXER_MXL_DOMAIN` deprecated: sets root **and** output dir to that path,
  logs a warning.

### 3.3 `FLOWXER_READ_OFFSET_GRAINS` — **cannot drive mxlsrc**

Prompt default is 2. gst-mxl-rs has **no** offset property; it delivers the
latest committed grain (about one grain behind head). **Follow gst reality:**
keep the env var, log once that mxlsrc ignores it, document the live-edge
behaviour. Do not replace mxlsrc with the C API in this round.

A flow that exists but produces no new buffers is `no_signal`, not `error`.
mxlsrc waiting for `FLOW_NOT_FOUND` is `waiting`.

### 3.4 On-air retarget of an input

mxlsrc properties are `mutable_ready`. Changing `video-flow-id` / `domain`
while PLAYING is undefined.

**Plan:** each live input branch is `input-selector` sink N. On activation:

1. Set that mxlsrc to NULL.
2. Update `domain` and flow-id (or swap in `videotestsrc pattern=black` when
   `master_enable` is false or still `waiting`).
3. Set PLAYING again.

Do not `parse_launch` the whole mixer. If the shared pipeline still stalls
(selector `sync-streams`), drop to `sync-streams=false` on that selector and
record it here. Simulate-mode unit tests cover the state machine without GST.

### 3.5 Image and MXL revision

- Default `MXL_REF=218ddaa0a08c12ffe75fc475ae65aa3d9eef16d7` (short `218ddaa`;
  GitHub `git fetch` rejects the abbreviation). Compatible with `v1.1.0`; pin
  matches mxl-fabrics-agent. One build-arg. Label `io.dmf.mxl.revision`.
- `MXL_ENABLE_FABRICS_OFI` stays off (already the case in `docker/build-mxl.sh`).
- `USER 1000:1000`. Own `/storage`, CEF cache, `/tmp` bits we write.
- No runtime network for CEF downloads. HTML keyer URLs remain the operator's
  problem.
- CEF runs inside the mixer process (`cefsrc`). Its `libcef.so` (built against
  glibc < 2.33) reads the malloc totals with the legacy `mallinfo()`, whose
  `int` fields wrap above 2 GiB, and Chromium's memory dumps check them
  (`MallocDumpProvider`: a negative value is SIGILL). The image preloads
  `docker/mallinfo-shim.c` (`LD_PRELOAD`), a `mallinfo()` that caps the values.
  Not chosen: glibc tunables (the totals are live allocations, not cached free
  memory), `--test-memory-log-delay-in-minutes` (stops only the periodic dump),
  CEF in a process of its own (the larger change). Lab numbers: §8.
- GHCR **public**: this repo cannot flip package visibility. Document
  `ghcr.io/firesh0ot/flowxer-vision-mixer` / `flowxer-gui` → Package settings →
  Change visibility → Public. Tags: keep `{version}` + `latest`, add `git-<sha>`.
- Item 3 “bring PR #34/#35 to main”: already on `dev`/`stage`; publishing happens
  on the next merge to `main`. Do not bump `VERSION` by hand.

### 3.6 Network binding

| Variable | Default | Notes |
|---|---|---|
| `FLOWXER_HOST` | **`127.0.0.1`** | Safe under `hostNetwork`. Local **bridge** Compose overrides to `0.0.0.0` so the GUI container can reach the mixer by service name. |
| `FLOWXER_MIXER_URL` | `http://127.0.0.1:9610` | GUI nginx / Vite upstream. Replaces hard-coded `vision-mixer:9610`. |
| `FLOWXER_GUI_PORT` | `9620` | |
| `FLOWXER_WEBRTC_PUBLIC_IP` | host management IP | ICE host candidate. |
| `FLOWXER_WEBRTC_UDP_PORT_MIN/MAX` | TBD, avoid 23500–23599 | aiortc `RTCIceServer` / transport port range. |
| `FLOWXER_API_TOKEN` | empty | **Required on the platform.** |
| `FLOWXER_GPU` | `off` | Media path, §3.8: `off` CPU, `auto` GPU when it works at start, `on` GPU or exit 78. |

GUI does not load CDN assets today; keep it that way.

### 3.7 Metrics and probes

Mixer serves Prometheus text at `GET /metrics` and `GET /api/v1/metrics`
(prefix `flowxer_`). `/livez` and `/readyz` as specified. Grafana JSON under
`deploy/grafana/flowxer.json`.

`/readyz` when NMOS is enabled: registry heartbeat recently succeeded **or**
Node API is up and we are still retrying registration (do not fail the pod
forever if the registry is briefly down — match fabrics-agent/decklink practice
and record the exact rule when implementing).

`/livez` (decided on 10.17.40, `fix/program-pts-retarget-hang`): 503 when a
control-plane operation (IS-05 activation, including waiting for the NMOS lock;
Program start; Program stop; an MXL source restart) has run for more than 60 s
(`engine/watchdog.py`). Each one is bounded on its own: a source restart waits
10 s (stopping mxlsrc can take 5 s: its grain read times out after 5 s), a stop
15 s, a start takes seconds. 60 s is well above all of them, so normal work never
trips it; with the usual probe (period 10 s, 3 failures) a hung pod restarts
about 90 s after the hang. The endpoint is `async` so it answers when the worker
threads are blocked.

### 3.8 GPU (item 7) — **optional GPU media path, `FLOWXER_GPU`**

Planned first: `FLOWXER_PREVIEW_ENCODER` for WHEP only, compositing on the CPU.
Changed (deviation, `feat/gpu-path`): on the platform the mixer needed 4.5–7
CPU cores for 1080p50 with 4 live MXL inputs and reached only 28–34 fps on a
loaded i9-9900K node, while the node's RTX A4000 idled. So the video moves to
the GPU, the image still runs without one:

- `FLOWXER_GPU=auto|on|off` (default `off`), decided once at start.
  `auto` takes the GPU path when it works, else the CPU path, and logs why;
  `on` exits with 78 without it; `off` is the CPU path, its pipeline
  description byte for byte the one before. `flowxer_info{media_path}` and
  `media_path` / `media_path_reason` in `GET /api/v1/mixer`.
- **OpenGL through EGL, not CUDA.** GStreamer 1.24 (Ubuntu 24.04) has no
  `cudacompositor` and no `nvjpegenc`; `glvideomixerelement` has the
  compositor's pad properties (alpha, zorder) and the aggregator signals the
  mixes use, so cut, mix, stingers and the keyer work unchanged. EGL needs no
  display: `GST_GL_PLATFORM=egl`, `GST_GL_WINDOW=egl-device`, and only
  NVIDIA's glvnd vendor (`__EGL_VENDOR_LIBRARY_FILENAMES`) so that Mesa's
  software renderer cannot stand in (the image runs an Xvfb for CEF; GLX there
  is software).
- **v210 in shaders.** Neither the GL nor the CUDA converters know v210.
  `capssetter` relabels an MXL frame as an RGBA image a quarter of the line
  stride wide (one texel = one 32-bit v210 word; no copy, mxlsrc adds no video
  meta and mxlsink copies raw bytes), `glupload` uploads it once, a `glshader`
  unpacks the 10-bit fields. `identity drop-allocation=true` keeps
  capssetter's allocation query (still with the v210 caps) from glupload: its
  GL pool for v210 aborted the process when a source renegotiated (the first
  fade on the lab). Program is packed the same way and downloaded
  once. Between them the pictures are 8-bit Y'CbCr 4:4:4 with alpha in RGBA
  textures, the samples of the CPU path's AYUV compositor (no RGB matrix);
  keyer, stinger and test sources are RGB and converted by a shader (BT.709).
  The mixer background is transparent and the pack shader puts legal black
  where nothing was drawn. A v210 input reaches Program bit for bit for codes
  that 8 bits hold (test in `tests/test_gst_media.py`).
- **Probe.** At start: `/dev/nvidia*`, the glvnd vendor file for
  `libEGL_nvidia.so.0` (the image carries it: the container toolkit mounts the
  library, not the file), `libEGL_nvidia` loadable (driver capability
  `graphics`), the GL elements (`gstreamer1.0-gl`), then one v210 test frame
  through both shaders, which must come back unchanged.
- **First frames back in time.** glvideomixerelement, like the compositor, can
  start its output over at 0 in the first frames; on the GPU path that stopped
  Program in about 4 of 10 starts on the lab. #69's guard at the `vout`/`aout`
  sinks (`_guard_program_output`) covers both paths, so the GPU path has no
  guard of its own; `flowxer_program_buffers_dropped_total` counts for both.
- **#69 on the GPU path:** a source restart (IS-05 retarget, recovery of a
  failed mxlsrc) flushes the branch through the GL elements and renegotiates;
  `drop-allocation` keeps the restarted source from getting a GL pool for v210.
- **Stays on the CPU:** audio, the HTML keyer (CEF renders in software), JPEG
  encoding of the monitor pictures (640×360, downloaded from the GPU) and the
  WebRTC encoder (aiortc).
- **Platform:** `nvidia.com/gpu: 1` (a time-sliced share is enough) and
  `NVIDIA_DRIVER_CAPABILITIES=graphics,video,compute`, as for
  mxl-browser-source, plus `FLOWXER_GPU=auto` or `on`. The default is `off`:
  without the setting nothing changes, no NVIDIA library is loaded (#66: a
  library loaded before GStreamer can change how libmxl unwinds).

### 3.9 PR sequence (deviation: item 2 before item 1)

IS-05 `mxl_domain_id` is meaningless until we can scan the root. Shipping a
Node that cannot resolve domains would fail the fabrics agent.

| PR | Content |
|---|---|
| **1** | This plan (`docs/platform-integration-plan.md`) |
| **2** | Work item 2: multi-domain scan, output domain, `domain_id` on REST, deprecate `FLOWXER_MXL_DOMAIN`, stop baking `domain_def.json` |
| **3** | Work item 1: in-process IS-04/IS-05 node (Option C), receivers/senders, REST↔IS-05, input states, GUI status, `/console`, `docs/nmos.md` |
| **4** | Work item 3: `MXL_REF=218ddaa`, non-root, image label, `git-<sha>` tags, health `mxl_revision` |
| **5** | Work item 4: bind `127.0.0.1`, `FLOWXER_MIXER_URL`, WebRTC ICE/ports |
| **6** | Work item 5: metrics, `/livez` `/readyz`, Grafana |
| **7** | Work item 6: `deploy/kubernetes/flowxer.yaml`, `docker-compose.host.yml`, README platform section |
| **8** | Work item 8–9: AMWA testing script/CI, remaining OpenAPI / `.env.example` |
| later | Work item 7 GPU |

Smaller slices inside 2–3 are allowed if a PR grows past review size.

### 3.10 Production structure from the environment (designer contract)

The platform's production designer sets FlowXer through Kubernetes values and
plans NMOS links by label (its catalog lists `nmos_ports`), so the structure
has to be known before the pod starts. Until now it only lived in the saved
state and the GUI.

| Question | Decision |
|---|---|
| Settings | `FLOWXER_FORMAT`, `FLOWXER_LIVE_INPUTS`, `FLOWXER_INPUT_LABELS` (JSON array or comma-separated), `FLOWXER_TEST_SOURCES`, `FLOWXER_PANELS`, `FLOWXER_PROGRAM_AUTOSTART`. `FLOWXER_` names only: the platform has no common names for these. Invalid values are exit 78. |
| Env or saved state | The environment wins at every start, like mxl-st2110-gateway where env beats the config file. Three groups, each set by its own variables: format; input list; panel count. Unset or empty: the saved state decides, so existing deployments do not change. |
| Kept from the saved state | IS-05 routes, and per input essences, group hint, clip and auto-stinger when id and kind stay; keyers, stingers, tally, the other workspace fields. All of it stays in the export. |
| Input ids | Live `cam-1..N` (the ids the seeded cameras have today, so saved routes on `cam-n` survive the switch), test `test-1..M` (labels `Test n`, not `Camera n`, so they are not taken for cameras), then `black`, `replay`. `FLOWXER_TEST_SOURCES` is 0 when only the live inputs are set. |
| Labels | Unique (receivers are found by label); fewer labels than inputs: `Camera n`; more: exit 78. Panels stay `ME n`. |
| API and GUI | One rule: a field the environment sets may be sent with its current value, another value is 409 naming the variable (`PUT /workspace`, `POST`/`DELETE /inputs`, `PATCH /inputs/{id}` label and kind). `GET /console` `pinned` maps each such workspace field to its variables; the GUI disables them. Not chosen: an `origin` field in `WorkspaceConfig`, which would end up in `state.json` and the export. |
| Import | Replaces the document's structure with the environment's (as at a start) instead of refusing it, so an export from another mixer still brings its routes and UI state. |
| Autostart | In the app lifespan after the NMOS node started: ME 1 Program on the first live input (else the first input), Preview on the next. A failed start is logged and stays visible in `GET /mixer`; the process keeps running. |

---

## 4. Behaviour to implement (normative for later PRs)

### Inputs and receivers

- Kind `mxl_live` only: two BCP-007-03 receivers, labels `<input label> Video/Audio`,
  group hint `{input id}:Video` / `{input id}:Audio`.
- Caps: `video/v210` and `video/v210a` at workspace raster; `audio/float32`
  48 kHz, configured channel counts (BCP-004-01).
- `test`, `black`, `file`, `replay`: **no** receivers.
- Creating/deleting inputs or changing `logical_source_count` adds/removes
  receivers.

### Program senders

- Per mixer panel PGM: video + audio sender, IS-04 Source + Flow, active
  `mxl_domain_id` = output domain id, `mxl_flow_id` = PGM flow UUIDs.
- Raster / group-hint change mints new PGM flow UUIDs (existing UUIDv5 scheme)
  and `sync_senders_from_outputs` on the senders. Crosspoint follows `mxl_flow_id`.

### Activation state machine (per essence)

`not_routed` → (`master_enable` + ids) `waiting` → (flow has grains) `running`.

- Domain/flow missing: stay `waiting`, retry 250 ms → 5 s, show black/slate.
- Flow present, no new grains: `no_signal`.
- `master_enable: false`: black, stop mxlsrc, `not_routed`.
- IS-04 receiver `subscription` (`sender_id`, `active`) updated on every
  activation — fabrics agent depends on it.
- Video and audio of one input may come from different senders / domains.

### REST ↔ IS-05

- IS-05 activation ≡ `PATCH /inputs/{id}` for that essence (`flow_id`,
  `domain_id`, enable).
- REST PATCH ≡ IS-05 active + IS-04 `subscription` on the matching receiver.

---

## 5. Open questions

1. **aiortc UDP port range** — aioice 0.10 binds `local_addr=(host, 0)` and
   `RTCConfiguration` has no port field. FlowXer wraps
   `loop.create_datagram_endpoint` during ICE gather so host sockets land in
   `FLOWXER_WEBRTC_UDP_PORT_MIN/MAX` (default 32600–32631, clear of fabrics
   23500–23599). Advertised host IP is rewritten to
   `FLOWXER_WEBRTC_PUBLIC_IP` (else `FLOWXER_NMOS_HOST_IP`). JPEG snapshot
   fallback is unchanged.
2. **`/readyz` vs registry down** — decided by the platform contract (G7):
   with a registry configured, `/readyz` is 503 until the node is registered
   (last heartbeat within 12 s). Readiness only takes the pod out of its
   Service; liveness (`/livez`) does not depend on the registry, so a registry
   blip does not restart the mixer.
3. **nvnmosd vs Option C** — Option C shipped in PR 3 because ACK-then-wait
   cannot be expressed as an NvNmos NACK. Revisit if AMWA IS-04-01 / IS-05-01
   fail for Node/Connection API gaps (events WebSocket, scheduled activations).
4. **GHCR visibility** — needs a human in GitHub package settings.
5. **Multiple ME program buses** — today one PGM compositor. Senders are
   created per panel; extra MEs still share one program bus until a future
   change. Record if the lab expects otherwise.
6. **Input kind name `clip`** — prompt says clip/file player. Code uses
   `file` / `replay`. No rename; no receivers on either.

---

## 6. Deviations from the prompt

| Prompt | This plan |
|---|---|
| Work-item order 1 then 2 | Item **2 before 1** so `mxl_domain_id` resolves. |
| `FLOWXER_READ_OFFSET_GRAINS` default 2 applied to the reader | **No-op** on mxlsrc; documented. |
| Option A includes evaluating gst-nmos-rs as a data plane | **Not used** for media. |
| Option A nvnmosd | **Option C** in-process FastAPI node so missing-domain activations ACK. |
| Bake nothing / copy domain into `/mxl-domain` | Stop copying; create output domain at runtime. |
| `FLOWXER_HOST` 0.0.0.0 today | Default **127.0.0.1**; bridge Compose overrides. |
| Make GHCR public in-repo | Document the GitHub UI step; cannot toggle from git. |
| AMWA BCP-007-03 reject unknown domains | **Accept** + `waiting` (on-demand), unconstrained receiver params. |
| GPU only for the WHEP encoder, compositing on the CPU | **Optional GPU media path** (`FLOWXER_GPU`, §3.8): upload, composite and pack on the GPU; WHEP stays aiortc. |

---

## 7. Acceptance mapping

Manual criteria stay as specified in the prompt (registry contents, Qvest
route decklink → FlowXer → decklink, mirror-domain play, raster mint, uid 1000
offline image, GUI still works). Automated tests land with the matching PR
(domain scan in PR 2, REST↔IS-05 and state machine in PR 3, registry
integration and AMWA script in PR 8).

---

## 8. Implementation log

- **PR 1** (`cursor/platform-integration-plan-85ef`): this file.
- **PR 2** (`cursor/mxl-multi-domain-85ef`): `FLOWXER_MXL_ROOT` scan, output domain create, `domain_id` on essences, refuse mirrors, stop baking `domain_def.json`, deprecate `FLOWXER_MXL_DOMAIN`. `FLOWXER_READ_OFFSET_GRAINS` logged as ignored.
- **PR 3** (`cursor/nmos-node-85ef`): in-process IS-04 v1.3 / IS-05 v1.2 / BCP-007-03 node (Option C). Live-input receivers, PGM senders, REST↔IS-05, waiting retry, GUI status, `docs/nmos.md`. NvNmos evaluated and not used (cannot ACK missing-domain activations).
- **PR 4** (`cursor/image-nonroot-85ef`): mixer/GUI `USER 1000:1000`, `MXL_REF=218ddaa`, label `io.dmf.mxl.revision`, health `mxl_revision`, `git-<sha>` GHCR tags, CEF flags that skip component updates. GHCR public still a GitHub UI step.
- **PR 5** (`cursor/network-bind-85ef`): `FLOWXER_HOST` default 127.0.0.1 (bridge Compose overrides 0.0.0.0), GUI `FLOWXER_MIXER_URL` / `FLOWXER_GUI_PORT`, WebRTC host ICE IP + UDP range wrap.
- **PR 6** (`cursor/metrics-probes-85ef`): Prometheus `flowxer_*` at `/metrics` and `/api/v1/metrics`, `/livez` `/readyz` (registry blip does not fail ready), Grafana `deploy/grafana/flowxer.json`.
- **PR 7** (`cursor/k8s-amwa-85ef`): `deploy/kubernetes/flowxer.yaml`, `docker-compose.host.yml`, README platform section, `scripts/nmos-testing.sh` + workflow_dispatch CI job for AMWA IS-04-01 / IS-05-01 / IS-05-02.
- **Follow-up** (`cursor/amwa-release-ci-85ef`): AMWA job was `workflow_dispatch`-only, so PRs #43 (dev→stage) and #44 (stage→main) skipped it. It now runs on PRs into `stage`/`main` and on the Stage workflow. Mixer image pin is the full SHA `218ddaa0a08c12ffe75fc475ae65aa3d9eef16d7`.
- **Follow-up** (`cursor/nmos-amwa-fixes-85ef`): AMWA IS-04/IS-05 on the #46 `dev`→`stage` PR failed. Trailing-slash ID lists, source/flow schema fields, BCP-007-03 empty `interface_bindings`, `transporttype`, `transportfile` 404, bulk POST, unknown PATCH 400, and scheduled activations. Local `amwa/nmos-testing` IS-04-01 / IS-05-01 / IS-05-02 exit 0 (registry discovery tests stay `--ignore`d; no DNS-SD). CI now runs AMWA on every PR, not only promotions.
- **Lab run** (`fix/lab-hardware-run`): first run of the mixer image on real hardware (iptv-web-lab-1: 2× Xeon Gold 6136, NVIDIA A16, MXL tmpfs, nmos-cpp registry; sources: mxl-test-player 4× 1080p50 v210 + 16 ch float32). Every run before this one used simulate mode, so these blockers were never seen:
  - `compositor zero-size-is-unconfigured` does not exist in GStreamer 1.24 (Ubuntu 24.04). The parse failed and the mixer silently fell back to **simulate**: the API said on-air, nothing reached MXL. A failed pipeline is now `state=error` with the reason, and `/mixer/start` returns 409. Simulate stays only for `FLOWXER_SIMULATE` / `FLOWXER_GST_MODE=simulate` or when GStreamer is not installed.
  - `timeoverlay` (test inputs) is in `gstreamer1.0-x`, which the image did not install.
  - The TGA stinger branch had no frame rate in its caps and did not negotiate; the video stinger branch now has `videorate` for clips at other rates.
  - The NMOS refresh loop applied every running essence to the mixer again every 250 ms, which set its `mxlsrc` to NULL and back to PLAYING: live inputs restarted four times a second, and with a stalled pipeline `set_state(NULL)` blocked while holding the NMOS lock (`GET /mixer` hung). Only an essence that leaves `waiting` is applied again.
  - GStreamer bus errors are now in `GET /mixer` `error` and `flowxer_pipeline_errors_total`.
  - `PATCH /inputs/{id}` that makes an input invalid returned 500; it is 422.
  - Measured after the fixes: Program 1080p50 at **50.0 grains/s**, 0 late reads, 4.3 cores with 4 live MXL inputs, 2 test inputs and the CEF keyer; 5.8 cores while cutting, keying and running transitions. Program is written about 5 grains (≈90 ms) behind real time. Cut and the CEF downstream keyer work on Program.
  - Still open, seen in the same run: a stinger plays only once (the branch reaches EOS at pipeline start, so later stingers are plain cuts); Fade and Fade to Black are cuts; a 16-channel audio source made `mxlsrc` (audio) fail with a stream error; the GUI monitors are generated cards, not pictures. These are the next PRs (the first three: `feat/media-transitions` below).
- **Platform contract** (`feat/platform-contract`): the mxl-poc-platform media function contract (`docs/requests/leeo86-v1-readiness.md` there, G1–G14).
  - Platform env names (`MXL_DOMAIN_SCAN_PATH`, `MXL_OUTPUT_DOMAIN_*`, `MXL_HISTORY_DURATION`, `MXL_CLEANUP_ON_EXIT`, `NMOS_SEED`, `NMOS_LABEL`, `NMOS_TAGS`, `NMOS_REGISTRY_ADDRESS`/`PORT`, `NMOS_HOST_ADDRESS`, `NMOS_PORT`, `NMOS_DNS_SD`, `SHUTDOWN_TIMEOUT_S`) next to the `FLOWXER_` names; the platform name wins.
  - `flowxer` binds both ports before it starts: a taken port is exit 75, an invalid setting exit 78 (without the value: it may be the token), SIGTERM exit 143 also as PID 1. The entrypoint runs `flowxer` instead of the uvicorn CLI.
  - SIGTERM: stop media, delete the node from the registry, remove the own output domain with `MXL_CLEANUP_ON_EXIT` (only when its id matches; never the root or a mirror). An output domain with another id is an error in the log, not a warning.
  - Registry: register once, then heartbeat; register again only after a change or when the registry lost the node; delete stale resources. Before, every resource was posted every 5 s with a new version.
  - `/readyz` waits for the registration (open question 2).
  - Saved state in `FLOWXER_STATE_DIR` (`/config`), written after each API change and IS-05 activation; `GET /api/v1/config/export`, `POST /api/v1/config/import`. Receiver connections survive a restart.
  - Program flow ids include the NMOS seed: two mixers with the same group hint no longer announce the same flows. The ids of existing deployments change once.
  - `deploy/kubernetes/flowxer-pod-network.yaml`: the platform's pod-network example. OCI labels `source` and `revision`.
- **Media transitions** (`feat/media-transitions`): the open points of the lab run, tested on GStreamer 1.24 in a new CI job (`Pytest (GStreamer)`, the image's packages, fakesink outputs).
  - Fade and Fade to Black dissolve: every source feeds two input-selectors through a `tee`, A (Program) and B. During a mix B shows the incoming source on a compositor pad above A, and an audiomixer pad; their alpha and level are set from the compositor's and audiomixer's `samples-selected` signal for each output frame, so the dissolve follows the output timeline (MXL sources carry absolute timestamps). At the end A takes the source over and B is hidden again. A Cut during a mix ends it. The compositor skips a pad at alpha 0, so the idle B bus costs no conversion.
  - The compositor now outputs AYUV instead of BGRA: Program (v210) no longer goes through an RGB matrix and back.
  - Stingers: each playback is a new bin (`stinger_bin_description`) linked to a new compositor pad at the current running time (+100 ms for the decoder); a probe on its output counts the frames that reach the compositor and drives the cut frame; at EOS the pad is released and the bin removed. Before, the branch was part of the pipeline and reached EOS at start.
  - MXL audio: mxlsrc gives an N-channel flow the first N speaker positions, so audioconvert downmixed all 16 test-player channels into Program; unpositioned channels cannot change their count in 1.24 at all. A probe sets a first-channels `mix-matrix` on `audioconvert name=amap_<input>` when the caps arrive, so Program gets channels 1 and 2 of any flow.
  - `flowxer_frames_rendered_total` counts Program frames at the video sink (it counted JPEG previews).
- **Monitor pictures** (`feat/monitor-pictures`): the last open point of the lab run. Every source's `tee` and Program (after the compositor) feed an `appsink` at `FLOWXER_MONITOR_FPS` (default 10): `videorate` drops first, then one `videoconvertscale` pass makes the 640×360 RGB picture from the full-size frame; the newest one is kept. `render_monitor` (JPEG and WebRTC) uses it while the pipeline runs and draws the card otherwise. Tested in the GStreamer CI job: the pictures change with the test source's time overlay, black stays black, and the Program monitor follows a cut.
- **Designer contract** (`feat/designer-structure-env`): the production structure from the environment (§3.10). `FLOWXER_FORMAT`, `FLOWXER_LIVE_INPUTS`, `FLOWXER_INPUT_LABELS`, `FLOWXER_TEST_SOURCES` and `FLOWXER_PANELS` win over the saved state at every start, the API refuses to change them (409) and `GET /console` reports them in `pinned`; `FLOWXER_PROGRAM_AUTOSTART` starts Program at process start. NMOS labels: receivers `<input label> Video/Audio`, senders `ME <n> PGM Video/Audio`. The GUI's source ⚙ now sends flows and group hint only when they changed (an unrouted live input could not be saved).
- **Program timeline, hung routes, frozen inputs** (`fix/program-pts-retarget-hang`, on 10.17.40), from the small platform (Program audio silent at every start, a route that hung the node, two gateway inputs frozen):
  - Program audio and the 2-frame stall: right after a start the audiomixer and the compositor can start their output over at 0 (`basesink:5` at the sinks: audio `[0, 0.01)` then `[0, 0.02)`, video `[0.04, 0.06)` then `[0, 0.08)`). mxlsink cannot write behind what it wrote and returns an error without a message; the error ran upstream (`queue54` → `asrc_cam-1` → `queue32`, the burst the platform logs) and stopped that essence for good. A probe on the `vout`/`aout` sink pads drops a buffer whose timestamp is not after the last one (audio: that starts before the last one ended). Lab, `fx-stall.sh ok`, 20 starts each: 10.17.40 12/20 with the burst and Program audio dead (0 blocks from `mxl-verify`), fixed 0/20, audio live 20/20, 1–2 buffers dropped in 14/20 starts; `novideo`, `videoonly`, `bothdead` 3/3 at 50 fps with live audio.
  - Hung route: reproduced on the lab with the platform's layout (4 live inputs, 2 test sources, 1 ME) by re-routing after the burst, cam-3 audio into another domain and back: the second route never answered, then GET /mixer, stop and the NMOS API. gdb: the route's thread waited in `gst_pad_stop_task` for `asrc_cam-3`'s stream lock; that source's thread waited in its queue for an answer to its serialized allocation query (sent after the first route restarted it); the queue's thread waited in the audio input-selector (`sync-streams`), whose active input (cam-1) had stopped in the burst. The restart of a source now flushes the source's branch first (FLUSH_START answers the waiting query and wakes the blocked threads, FLUSH_STOP without a time reset) and runs bounded (10 s; stop 15 s). Unrouting uses `UNROUTED_FLOW`. A GStreamer test reproduces the blocked query (fails without the flush).
  - Frozen inputs: mxlsrc returns an error for a grain marked `MXL_GRAIN_FLAG_INVALID` and stops for good; mxl-st2110-gateway RX marks incomplete frames that way. Reproduced with a test writer that marks every 250th grain invalid: the input froze on its last picture. A failed MXL source is started again after 1 s (2, 5, 10, 30 s when it fails again within 3 s); `flowxer_input_restarts_total`, `GET /mixer` `error`. A seamless fix (mxlsrc skipping invalid grains) belongs in gst-mxl-rs.
  - `/livez` watchdog: §3.7.
- **GPU media path** (`feat/gpu-path`, §3.8): `FLOWXER_GPU=auto|on|off` (default `off`), on 10.17.40. Lab iptv-web-lab-1 (2× Xeon Gold 6136, NVIDIA A16 GPU 3, driver 595.84), 4 live MXL inputs from the test player (1080p50 v210, 16 ch) + 4 test/black inputs + the CEF keyer on, GUI monitors at 10/s; mixer container cores (cgroup, 30 s), Program measured by `flowxer_frames_rendered_total` and from outside by `mxl-verify`:

  | Case | CPU path (`off`) | GPU path |
  |---|---|---|
  | On air | 4.35 cores, 50.0 grains/s | 3.01 cores, 50.3 grains/s, GPU 70 % SM |
  | + a 1 s fade every 2 s | 4.29 (p95 5.62), 48.4 grains/s | 2.69 (p95 3.43), 49.7 grains/s |
  | + 6 JPEG monitors at ~10/s | 4.80, 50.2 grains/s | 3.29, 50.0 grains/s |
  | + 4 WebRTC previews | 4.99, 50.0 grains/s | 3.89, 49.9 grains/s |

  Cut, mix, Wipe (armed stinger), a stinger and the DSK (CEF lower third) checked on Program frames read back from MXL and on the GUI monitor; Program keeps moving through a fade. Lab stall check (`ok`, `novideo`, `videoonly`, `bothdead`): 50 fps on both paths, no crash with missing flows on the GPU path. `tests/test_gst_media.py` runs every media test on both paths (GPU cases skipped without a GPU); new: a fade between two pool-negotiating "MXL" sources (aborted before `drop-allocation`) and v210 through Program bit for bit. The A16 has a quarter of the RTX A4000's shader throughput, so the platform node's GPU load should be well lower. Still on the CPU: CEF, audio, mxlsrc/mxlsink copies, test sources, JPEG and WebRTC encoding (aiortc); NVENC for WebRTC is the next step.
  - Rebased on 11.18.41 (#69): the GPU path uses #69's Program guard (its own one is gone), its bounded source restarts with branch flush, MXL source recovery and the `/livez` watchdog; a new media test restarts "MXL" sources on air on both paths. Lab with the 11.18.41 image plus this package: `FLOWXER_GPU` unset gives the same pipeline description as 11.18.41 (byte for byte, `GET /mixer`); stall check on the GPU path `ok` 6/6, `novideo` 2/2, `videoonly` 2/2 at 50 fps with Program audio and no stream errors (the guard dropped 1–2 buffers in 5 of 6 `ok` starts); the platform-layout hang repro (re-routes after start, cam-3 audio into another domain and back) ran clean; on air 3.04 cores (GPU) and 4.17 (CPU), 50 grains/s.
