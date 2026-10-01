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

- Default `MXL_REF=218ddaa` (compatible with `v1.1.0`; pin matches
  mxl-fabrics-agent). One build-arg. Label `io.dmf.mxl.revision`.
- `MXL_ENABLE_FABRICS_OFI` stays off (already the case in `docker/build-mxl.sh`).
- `USER 1000:1000`. Own `/storage`, CEF cache, `/tmp` bits we write.
- No runtime network for CEF downloads. HTML keyer URLs remain the operator's
  problem.
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

GUI does not load CDN assets today; keep it that way.

### 3.7 Metrics and probes

Mixer serves Prometheus text at `GET /metrics` and `GET /api/v1/metrics`
(prefix `flowxer_`). `/livez` and `/readyz` as specified. Grafana JSON under
`deploy/grafana/flowxer.json`.

`/readyz` when NMOS is enabled: registry heartbeat recently succeeded **or**
Node API is up and we are still retrying registration (do not fail the pod
forever if the registry is briefly down — match fabrics-agent/decklink practice
and record the exact rule when implementing).

### 3.8 GPU (item 7)

**After** items 1–6. `FLOWXER_PREVIEW_ENCODER=auto\|cpu\|nvenc` for WHEP only.
Compositing stays CPU. Image must run without a GPU.

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

1. **aiortc UDP port range** — confirm the exact aiortc API for pinning ICE
   host ports before implementing item 4. If it cannot, document and use a
   iptables/CNI-friendly range via `RTCConfiguration` ice candidate filter.
2. **`/readyz` vs registry down** — prefer “Node up + output domain writable”
   so a registry blip does not kill the mixer; expose `nmos_registry_up` in
   metrics. Confirm against lab ops.
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
