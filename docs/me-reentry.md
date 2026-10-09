# ME re-entry: every ME renders, an ME's Program as a source (design, task R)

Status: **design for review, nothing is built.** Written on `dev` 14.21.47 (#81).
The numbers come from a throwaway lab prototype on iptv-web-lab-1 (§5.1), not
from shipped code. Decisions here are proposals until the platform and the user
answer §9.

---

## 1. Summary

- **Structure:** one GStreamer pipeline, one compositor (and audiomixer) per
  rendering ME. Each input is decoded/uploaded **once** and its `tee` feeds the
  A/B selectors of every rendering ME. One GL context, as today.
- **Re-entry:** inside the process. ME *n*'s Program (video and audio, before the
  v210 pack) is one more source of every ME *m* with *m* < *n*. ME 1 can use
  ME 2..4, ME 2 can use ME 3..4, ME 4 none. This fixed order makes feedback
  impossible by construction, so a take needs no cycle check and the pipeline
  graph stays acyclic. Measured: ME 1 shows ME 2's frame of the **same** output
  time (0 frames delay, 6000 of 6000 frames). Raw TSL: INDEX `1000 + n`.
- **Opt-in:** `FLOWXER_RENDER_MES` (default `1`) says how many MEs render,
  from ME 1 up. Unset, the pipeline, the flows and the tally are today's.
- **Cost per extra rendering ME** (lab, GPU path, 1080p50, platform vmix
  layout): about +0.3 container cores, +5–6 % GPU (A16), +160–200 MiB GPU memory,
  +80 MiB host memory, and 2.5–3 % of the GL thread (5–8 % with its GUI
  monitor at 10/s). The GL thread is dominated by the inputs (upload + unpack,
  11–15 % each), not by the MEs. 1–4 MEs ran at 50 fps on the lab (4 MEs in
  2 of 3 runs; the third locked up, see below). CPU path:
  +0.4–0.8 cores per ME, 3 MEs already skip frames: GPU path only above 2 MEs.
- **host-03 (RTX A4000, ~2× the lab's cost per thread):** 2 MEs fit, 3 are
  borderline, 4 need relief (§5.5).
- **Blocker found on the way (not caused by R):** the GL thread can lock up at
  100 %, and every ME's Program then stays at 6–20 fps until a restart (§5.6).
  Seen with 10 WebRTC previews from 1 ME up, and once without previews at 4
  MEs. It has to be fixed (R0) before more MEs go on air.

---

## 2. Today (dev 14.21.47)

| Area | Today | Where |
|---|---|---|
| Panels | `FLOWXER_PANELS` 1–4, `me-1`..`me-4`, each with its own Program, Preview, Wipe, last transition | `MixerPanel`, `_sync_panels` |
| Rendering | Only ME 1: one `vsel`/`vselb`/`asel`/`aselb`, one `comp` (`glvideomixerelement` on the GPU path, `compositor` on the CPU path), one `amix`, one `vout`/`aout` (`mxlsink`) pair | `build_pipeline_description` |
| ME 2..4 | Control and tally state only. `_put_on_program` applies media only for `panels[0]`; a stinger on ME 2..4 switches that ME at once (14.21.46, #79) | `mixer.py` |
| Mix | One `_Mix`, alpha/volume set from `samples-selected` of `comp`/`amix` | `GstRuntime.mix` |
| Stinger | One `StingerPlayer`, one dynamic pad on `comp` | `GstRuntime.play_stinger` |
| Keyer | `cefsrc` (one CEF browser), pad `sink_2` of `comp`; DSK 1 drives it | `overlay_bin` |
| Timeline guard (#81) | Probes on `vout`/`aout` (back in time, counters) and a QoS probe that makes `comp` skip to real time | `_guard_program_output`, `_late_probe` |
| NMOS | Sender, source and flow "ME *n* PGM Video/Audio" per panel. **All of them advertise ME 1's MXL flow ids** (`_sender_mxl_flow`, `flows()` tag `mxl_flow_id`) | `nmos/service.py` |
| Monitors | `panel:me-1:pgm` is the mixed Program (`mon__program`); `panel:me-n:pgm` for n > 1 shows the picture of the input on that ME's Program | `preview.py` |
| Raw TSL (#78) | SCREEN = ME, INDEX per input, `1000 + ME` reserved, nothing sent there | `tally_export.py` |

Note on NMOS: a receiver connected to "ME 2 PGM" today gets **ME 1's** Program,
while the platform's tally calculator treats that subscription as ME 2's
Program (re-entry 2, `tally/mxl_tally/graph.py`). And a FlowXer input routed to
its own "ME 2 PGM" sender is ME 1 fed back into itself. R fixes both (§4.10).

---

## 3. Goals and non-goals

Goals:

1. Every rendering ME publishes its own Program, video and audio, as MXL
   flows behind its existing NMOS senders.
2. An ME's Program can be a source of another ME, inside the process, without
   feedback.
3. Raw TSL: re-entry sources are INDEX `1000 + ME`, so the platform's
   calculator propagates on-air through them.
4. Without new settings nothing changes (pipeline, flows, tally, API).

Non-goals (for R): per-ME HTML graphics (more CEF browsers), keys with a fill
from an input, ME re-entry across mixer instances (that stays an MXL route),
re-linking the pipeline on air.

---

## 4. Decisions

### 4.1 Pipeline: one pipeline, one compositor per ME

| Option | For | Against | Decision |
|---|---|---|---|
| **A. One pipeline, N compositors, inputs shared by `tee`** | Each input uploaded once (the dominant GL cost, §5.3); one clock and base time, so ME flows are written at the same MXL index and re-entry is a link; mixes, stingers, guard reused per ME | One GL thread for all MEs; one failure domain | **Chosen** |
| B. One pipeline per ME, own GL context each | GL work spread over threads | Every input uploaded again per ME: measured 4 MEs = about 270 % GL in total, 10–31 fps on the A16 (§5.3); re-entry needs a bridge between pipelines | No |
| C. One pipeline per ME, GL contexts shared (textures via appsink/appsrc) | ME work on its own thread | The ME work is only about 3 % of a thread; the uploads stay on the first thread; the bridge costs a Python callback per frame and input (GIL); 48.2–50 fps in the test | No |
| D. MEs as separate processes reading the inputs from MXL | Isolation | Uploads duplicated, one more MXL hop (latency), re-entry through MXL | No |

Element names: ME 1 keeps today's names (`vsel`, `comp`, `amix`, `vout`, …), so
with `FLOWXER_RENDER_MES=1` the pipeline description stays byte for byte
today's (the same rule as `FLOWXER_GPU=off`). ME *n* > 1 gets the suffix
`_me<n>`: `vsel_me2`, `vselb_me2`, `asel_me2`, `aselb_me2`, `comp_me2`,
`amix_me2`, `pgmt_me2`, `vout_me2`, `aout_me2`, `mon__program_me2`.

Per rendering ME *n* (GPU path; the CPU path the same with `compositor`,
AYUV and `videoconvert` to v210):

```
input-selector vsel_me<n>, vselb_me<n>, asel_me<n>, aselb_me<n>   (sync-streams, cache-buffers)
vt_<input>. ! queue ! vsel_me<n>.sink_<slot>      (and vselb_me<n>, at_<input> → asel/aselb)
vsel_me<n>. ! queue ! comp_me<n>.sink_0           (Program, A bus)
vselb_me<n>. ! queue ! comp_me<n>.sink_1          (mix, B bus, alpha 0 when idle)
comp_me<n>. ! <GL caps> ! tee pgmt_me<n>
  pgmt_me<n>. ! glshader gpu_pack_me<n> ! gldownload ! capssetter v210 ! queue ! mxlsink vout_me<n> qos=true
  pgmt_me<n>. ! <GUI monitor tap mon__program_me<n>>
  pgmt_me<n>. ! queue ! vsel.sink_<k> / vselb.sink_<k>     (re-entry into ME m < n, §4.6)
amix_me<n>. ! audioconvert ! audioresample ! <F32 caps> ! tee apgmt_me<n>
  apgmt_me<n>. ! queue ! mxlsink aout_me<n>
  apgmt_me<n>. ! queue ! asel.sink_<k> / aselb.sink_<k>    (re-entry audio)
```

The prototype (§5.1) builds this (re-entry only ME 2 → ME 1, without the QoS
drop of §4.13) and ran 1–4 MEs at 50 fps.

### 4.2 Inputs: one decode/upload per input

Each input keeps one source branch (mxlsrc → v210 words → `glupload` → unpack
shader; stills made once) and one `tee` per essence. A rendering ME adds two
`queue` branches per input and essence (A and B selector). On the GPU path
these carry references to the same texture; nothing is copied or converted per
ME. The input-selector only lets the active pad through, and a pad at alpha 0
is skipped by the compositor, so an input that no ME shows costs only its
upload (as today). On the CPU path each ME's compositor converts the pads it
shows (v210 → AYUV) itself; that, the v210 conversion of its Program and the
`mxlsink` copy (5.5 MB per frame) are most of an ME's CPU cost (§5.4).

The per-input source monitor (`mon_<input>`) stays one per input.

### 4.3 Audio: one audiomixer per ME

`amix_me<n>` with the A/B pads, levels set per ME from its own
`samples-selected` (fades dissolve picture and sound per ME, as on ME 1
today). The audio follows the video: a source on an ME's Program brings its
sound, a re-entry brings the other ME's Program sound. All MEs run on one clock
from the same input timestamps, so the ME flows are written at the same MXL
indices; measured lag behind TAI: video 0.08–0.10 s and audio 0.07–0.08 s for
every ME (§5.2).

### 4.4 Keyers: on ME 1 only

The DSKs are downstream keyers of the main Program, which is ME 1 (the ME the
platform takes to air). ME 2..4 have no keyer in R. The one CEF browser stays
one: a second `cefsrc` costs a CPU-rendered browser per ME and the process
already sits near the malloc limit CEF trips over (#74).

Option for later (not in R): a keyer may be put on several MEs from the same
CEF picture (one upload, `tee` into each compositor's keyer pad). Measured in
the prototype with the keyer on all 4 MEs: 50 fps on every ME, no measurable
extra GL or GPU load (§5.2, `me4k`).

### 4.5 Mixes and stingers per ME

- `GstRuntime` keeps `_mix` and `_stinger` per ME (keyed by ME number); the
  `samples-selected` handler finds the ME from the aggregator's name.
- One `StingerPlayer` per rendering panel; a stinger on ME *n* requests its pad
  on `comp_me<n>` and cuts that ME at its cut frame. Stingers on different MEs
  can run at the same time; each playing stinger decodes on the CPU and
  uploads every frame (an RGBA upload is 12–19 % of the GL thread on the lab,
  #81), so two at once cost twice that for their length.
- Stinger slots stay global (one library, one set of slots); auto-stinger of an
  input applies on whichever ME it is taken.
- A panel that does **not** render keeps today's behaviour (state and tally
  only; a stinger switches at once).
- `GET /mixer` keeps `stinger` (ME 1) and adds `stinger` per panel.

### 4.6 Re-entry: in the process, in a fixed order

Rule: **ME *m* can use the Program of ME *n* only when *n* > *m* and ME *n*
renders.** ME 1 (the main Program, with the keyers) can take ME 2..4.

Why a fixed order:

- No feedback by construction: there is no path from an ME back to itself, so
  no take, preview, mix or stinger needs a cycle check and none is refused for
  it.
- The pipeline's link graph stays acyclic. GStreamer's latency, caps and
  allocation queries walk upstream through every link, selected or not; a
  graph with a static cycle (ME 1 → ME 2 → ME 1 through idle selector pads)
  makes them loop.
- A take stays an `active-pad` switch: nothing is linked or unlinked on air.

Alternatives not chosen:

| Alternative | Why not |
|---|---|
| Any ME into any other, cycle check (DAG) on take/preview | Needs links in both directions: a cyclic pipeline graph (above) |
| Any ME into any other, links made on the take | Relinking a running GL pipeline on a take: renegotiation and latency changes on air, the class of hangs #64/#69 fixed |
| Re-entry through MXL (an input routed to the own `ME n PGM` sender) | One more MXL hop, read and upload per frame, and no loop rule: FlowXer does not know where an external route ends up. It stays possible as a normal IS-05 route (the calculator already treats it as a re-entry); FlowXer logs a warning when an input is routed to one of its own flows |

An API call that puts a re-entry on an ME that may not use it (ME 1's Program
on ME 2, or a non-rendering ME) is 409 with the rule in the message.

### 4.7 Re-entry timing

The re-entry link carries ME *n*'s composited texture for running time *T* into
ME *m*'s selector. ME *m*'s compositor (a live aggregator) waits for all its pads
up to its deadline (*T* + latency; mxlsrc reports one grain), so ME *n*'s frame
for *T* is used when it arrives before that deadline: **0 frames delay**; when
ME *n* is late, ME *m* repeats ME *n*'s last frame.

Measured in the prototype (`re`: ME 2 on ME 1's Program; for each ME 1 output
frame the running time of the picture on ME 1's Program pad against the output
frame's, `get_current_buffer()` in `samples-selected`; both compositors run on
the same output grid, so the difference is exact):

| Case | Same frame (0) | One frame old (−1) | ME 1 Program lag behind TAI |
|---|---|---|---|
| `re`, 2 × 30 s | 100 % of 3000 + 3000 frames | 0 | 0.078–0.101 s (as without re-entry) |
| `re` + 1 frame more compositor latency on ME 1 | 99.9 % / 100 % | 0.1 % / 0 | 0.106–0.127 s |

Decision: **0 frames, no added latency.** ME 1 uses ME 2's frame of the same
output time; extra compositor latency buys nothing measurable and adds a frame
to ME 1's Program. When ME *n* is late, ME *m* repeats ME *n*'s previous frame
for that output frame (the compositor keeps a pad's last buffer); ME *n*'s own
skipped frames are counted per ME (§4.12). Audio follows the same path
(ME 1's audio lag 0.070–0.092 s with ME 2 re-entered, 0.070–0.080 s without).

### 4.8 Re-entry sources in API, GUI and console

- Re-entry sources are **derived, not stored**: one per rendering ME *n* ≥ 2,
  id `me-<n>-pgm`, label `<panel label> PGM` (`ME 2 PGM`). They are not
  logical inputs: no NMOS receivers, not in `state.json` or the export, not
  counted in `logical_source_count` / the 24-source limit, not editable
  (`PATCH`/`DELETE /inputs/me-2-pgm` 404).
- `GET /console` and `GET /mixer` list them in `reentries`
  (`id`, `label`, `panel_id`, `available_on`: the panels that may use it).
- `take`, `preview`, `cut`, `fade`, `fade-to-black`, `wipe`, `stinger/play`
  accept a re-entry id where they accept an input id (409 per §4.6 where it is
  not available).
- Selector pads: inputs keep pads `0..k-1` (their slot); ME *m*'s re-entry pads
  follow at `k`, `k+1`, … in ME order. The pad map is per ME and internal; the
  API shows ids.
- GUI: on ME *m* the source row gets one tile per available re-entry after the
  inputs (`source:me-<n>-pgm`, the picture of `mon__program_me<n>`). Left click
  Preview, right click Program, as for inputs.
- UI tally receivers (Companion, VSM, …): unchanged in R (inputs only).

### 4.9 Raw TSL tally (`FLOWXER_TALLY_TSL`)

- SCREEN *m* (ME *m*) gets one display per re-entry available on ME *m*:
  INDEX `1000 + n`, TEXT `ME n PGM` (UTF-16LE), LH red while ME *n*'s Program is
  on ME *m*'s Program or is a source of a running mix or stinger on ME *m*, RH
  green while it is on ME *m*'s Preview. Same rules as for inputs.
- Input INDEX numbering is unchanged (re-entries never take an input number).
- Example (the platform's case): ME 2 on ME 1's Program → SCREEN 1 INDEX 1002
  LH red; SCREEN 2 carries ME 2's own Program sources; the calculator
  (`engine._reach`, `index > 1000`) propagates on-air into them.
- Nothing is sent at `1000 + n` while ME *n* does not render.

### 4.10 NMOS and MXL

- **Flow ids:** ME 1 unchanged (`flow_uuid(group_hint, "video"|"audio", …)`), so
  running deployments keep their ids. ME *n* > 1:
  `flow_uuid(group_hint, "me-<n>:video"|"me-<n>:audio", …)` (seed, raster and
  rate in the key as today). All in the own output domain.
- **Senders of a rendering ME:** IS-05 active `mxl_domain_id` = output domain
  id, `mxl_flow_id` = that ME's flow; IS-04 `subscription.active` true; the
  IS-04 flow's `urn:x-nmos:tag:mxl_flow_id` tag is that ME's flow.
- **Senders of a non-rendering ME:** still registered (the platform plans links
  by the labels `ME <n> PGM …` from `panels`), but IS-05 active
  `master_enable: false`, `mxl_flow_id: null`, IS-04 `subscription.active:
  false`, no `mxl_flow_id` tag. This ends "ME 2 PGM is ME 1's flow" (§2).
  It changes what such a sender says; see open question 2.
- `OutputFlows` per panel (`GET /mixer` `panels[].outputs`); top-level
  `outputs` stays ME 1.
- The flows exist while Program runs (mxlsink creates them at start and they
  go away with the last writer, as today for ME 1).

### 4.11 Configuration

| Setting | Values | Default | Effect |
|---|---|---|---|
| `FLOWXER_RENDER_MES` | `1`..`FLOWXER_PANELS`, or `all` | `1` | MEs 1..N render and publish; the rest are state-only panels as today. More than the panels: exit 78 |
| Workspace `rendered_panel_count` | 1..4 | 1 | The same for deployments without the env (GUI Settings → Console layout, mixer stopped); pinned (409) when the env sets it |

Re-entry needs no switch of its own: an ME's Program is offered to the MEs
below it as soon as it renders. Rendering changes only at a start (the pipeline
is built once), like the panel count.

### 4.12 Monitors and metrics

- Each rendering ME has a Program monitor tap (`mon__program_me<n>`, 10/s);
  `panel:me-<n>:pgm` and `source:me-<n>-pgm` show it. A non-rendering ME's
  `pgm` monitor stays the input's picture.
- Per-ME metrics, label `me`: `flowxer_me_frames_rendered_total`,
  `flowxer_me_frames_dropped_total`, `flowxer_me_program_stalled`,
  `flowxer_me_rendering`. The existing unlabelled metrics stay ME 1.

### 4.13 Timeline guard (#81) and failure isolation

- The guard is per ME: the back-in-time probes on `vout_me<n>`/`aout_me<n>`,
  and one `_late_probe` per ME that makes only `comp_me<n>` skip. The prototype
  does this; with 4 MEs each ME kept to the TAI timeline independently.
- QoS isolation: `comp`'s QoS skip travels upstream through its pads; through a
  re-entry link it would reach `comp_me<n>` and make the lower ME skip frames
  it does not need to skip. The re-entry `queue` drops upstream QoS events.
- Output failure: a flow error from one ME's `mxlsink` runs up its branch into
  the shared input `tee` and can stop that input for **every** ME (what #69 saw
  for ME 1 alone). The known cause (buffers back in time) is guarded per ME. R
  adds a media test that fails one ME's sink and checks that the other MEs keep
  running, and ends such an error at the ME's branch if the test shows it
  spreading.

---

## 5. Measured cost

### 5.1 Setup

- Lab iptv-web-lab-1: 2× Xeon Gold 6136, NVIDIA A16 GPU 3 (driver 595.84),
  MXL tmpfs. FlowXer image `14.21.46` with the `dev` (14.21.47, #81) Python
  package and a prototype patch on top (local branch
  `lab/me-reentry-proto`, not pushed). Lab-only switches: `FXRE_MES=N` (ME 2..N
  render as in §4.1), `FXRE_KEYER=shared` (keyer on every ME), `FXRE_REENTRY=1`
  (ME 2's Program as one more source of ME 1).
- Layout: the platform's vmix via `~/mxl-lab/fx-gpu/mi/fx-mi.sh` (cam-1..4 from
  the test player, 1080p50 v210 + 16 ch; test-1/2, Black, Replay; CEF overlay
  and DSK 1 on ME 1), GPU path, mixer limited to 6 cores like the pod, GUI
  monitors at 10/s, ME *n* Program on cam-*n*. Measured 20–30 s per case with
  `~/mxl-lab/fxre/fxre-measure.py` (fps and skipped frames per ME, lag of every
  ME's Program flow behind TAI, container cores, the GL thread `gstglcontext`,
  GPU utilisation and memory).
- Synthetic GL items (`mecost.py`, `glsplit.py` in `~/mxl-lab/fxre`): one ME or
  one pipeline layout alone, on the idle GPU 0.
- The host was shared (other functions on the lab, at times compiler jobs of
  other work): host CPU 13–29 % in the runs below, with the exceptions noted.
  The later runs are pinned to NUMA node 0 (the GPUs' node), still with the
  6-core limit; pinned and unpinned runs gave the same picture.

### 5.2 The mixer with 1–4 rendering MEs (no WebRTC previews)

One row per ME count over all quiet runs (20–30 s windows; runs per row in brackets):

| Rendering MEs | Program per ME | Container cores | GL thread | GPU 3 (A16) | GPU memory | Container memory |
|---|---|---|---|---|---|---|
| 1 (today) | 50.0 fps, 0 skipped (6 runs) | 1.70–2.05 | 51–74 % | 39–40 % | 640–810 MiB | 0.71–0.73 GiB |
| 2 | 50.0 fps, 0 skipped (3 runs) | 1.99–2.32 | 53–78 % | 44–46 % | 800–990 MiB | 0.78–0.85 GiB |
| 3 | 49.8–50.0 fps, ≤ 6 skipped per 30 s (2 runs) | 2.33–2.68 | 60–82 % | 50–51 % | 974 MiB | 0.86 GiB |
| 4 | 49.3–50.0 fps in 2 of 3 runs; 1 run 44 fps, then the lock-up of §5.6 | 2.62–2.93 | 68–81 % | 55–57 % | 1300–1310 MiB | 0.96 GiB (1.32 while it degraded) |
| 4, keyer on every ME (`me4k`) | 50.0 fps (1 run) | 2.91 | 74 % | 57 % | | |
| 2 with re-entry (`re`) | 50.0 fps (2 runs) | 2.20–2.28 | 60–72 % | 45 % | 822–852 MiB | 0.77–0.80 GiB |

- Every ME's Program flow stays 0.08–0.10 s behind TAI (video), ME 1's audio
  0.07–0.08 s, for every ME count: the MEs are written grain-aligned.
- Per extra ME: **+0.3 cores**, **+5–6 % GPU**, **+160–200 MiB GPU memory**,
  **+80 MiB host memory**. No CFS throttling in any run.
- The GL thread figure is not additive: it moves ±15 points between runs of
  the same case. Most of its CPU time is the driver waiting for the GPU (§5.6),
  and more work fills that time first. The per-ME GL cost is therefore taken
  from the synthetic runs (§5.3).
- With 10 WebRTC previews (8 sources, PGM, PVW): 2 MEs and 4 MEs with the
  keyer on every ME ran at 50 fps (1 run each); the other runs locked up
  (§5.6), 1 ME included.

### 5.3 GL thread per ME, and more GL contexts

Each ME alone (`mecost.py`, stills as sources, 2 runs):

| One ME, GPU path | GL thread | Process CPU |
|---|---|---|
| Composite (Program + hidden mix pad) + pack + download | 2.5–2.7 % | 4.4–4.7 % |
| + keyer pad | 3.1–3.2 % | 5.7–6.2 % |
| + Program GUI monitor at 10/s | 5.5–8.0 % | 9.0–11.5 % |

Whole layouts (`glsplit.py`, 4 MXL-like v210 inputs uploaded every frame,
each with its GUI monitor, each ME with its Program monitor, 2 runs):

| Layout | 1 ME | 2 MEs | 4 MEs |
|---|---|---|---|
| **one** pipeline, one context (today, option A) | 52–56 %, 50 fps | 54–57 %, 50 fps | 55 %, 50 fps |
| **per_me_dup**: a pipeline and context per ME, inputs uploaded per ME (option B) | | 57+54 to 70+63 % | 80+65+64+62 % = 270 %, **10–31 fps** |
| **split_me**: inputs + ME 1 in one context, ME 2..4 each in its own (shared) context (option C) | | 46–49 % + 6–9 % | 54–59 % + 3–8 % each, 48.2–50 fps |
| **split_in**: each input in its own (shared) context, all MEs in one | 11–15 % per input + 3–4 % | 9–15 % per input + 5 % | 12–15 % per input + **11–12 %** for 4 MEs, 49.4–50.1 fps |

Read:

- The MEs themselves are cheap on the GL thread: 4 MEs together take 11–12 %
  of a thread (`split_in`, the MEs' own context), **≈ 3 % per ME**; the GUI
  monitor of each ME's Program is the larger part on top.
- The inputs are the load: 11–15 % of a thread each (upload + unpack + their
  GUI monitor). Uploading them again per ME (option B) breaks down at 4 MEs.
- **A second GL context per ME does not help:** it moves only the ~3 % of the
  ME and leaves the uploads on the first thread (`split_me`).
- **What would help, if the GL thread becomes the limit:** a context per input
  for the uploads (`split_in`): no thread above 15 % with 4 inputs and 4 MEs.
  Not part of R; the test bridge (appsink/appsrc in Python) is not production
  code.

### 5.4 CPU path

`FLOWXER_GPU=off`, same layout, 6-core limit, 2 × 30 s:

| Rendering MEs | Program per ME | Container cores |
|---|---|---|
| 1 | 50.0 fps | 2.76–2.78 |
| 2 | 50.0 fps | 3.15–3.16 |
| 3 | 47.8–50.0 fps, up to 65 skipped per 30 s | 3.86–3.98 |

One ME alone on the CPU (`mecost.py`): 0.53–0.76 cores without a keyer pad,
1.0–1.2 cores with the BGRA keyer converted per frame. The compositor threads
(about 40 % of a core each) and `videoconvert`'s pool grow per ME; at 3 MEs
Program skips frames although the container is not throttled. **CPU path: at
most 2 rendering MEs.**

### 5.5 What fits on host-03

host-03: i9-9900K + RTX A4000, and (as given) about **2× the lab's CPU cost
per thread**. The GL thread is the one thread that cannot be spread, so it is
the budget. The ×2 is applied to the per-ME cost; applied to the lab's 1-ME
figure it would give 100–150 %, which the running platform contradicts, so the
baseline comes from the platform's own measurement:

| | Lab (A16) | host-03 (×2) |
|---|---|---|
| GL thread, today's vmix with 1 ME (#81) | 51–74 % | not measured with #81; the platform measured 102 % on 13.20.43, and #81 cut the lab's figure from 77 % to 55 % (−29 %), so about **70–75 %** |
| Per extra ME without (with) its Program monitor at 10/s | 3 % (5–8 %) | 6 % (10–16 %) |
| 2 MEs | | 76–91 % |
| 3 MEs | | 82–107 % |
| 4 MEs | | 88–123 % |
| Cores per extra ME | 0.3 | ~0.6 |
| GPU per extra ME | +5–6 % of an A16 | ~+1.5 % of an A4000 (4× the A16's shader throughput) |

- **2 MEs fit.** 3 are borderline, 4 do not fit without relief.
- Relief, cheapest first: the GUI monitors of ME 2..4's Programs at a lower
  rate or only while a client watches them (the monitor is about half of an
  ME's GL cost); `FLOWXER_MONITOR_FPS` lower; later the upload contexts per
  input of §5.3.
- Cores: the pod limit (7) has room for +2 cores (4 MEs) on top of today's
  vmix.
- The A4000 itself is not the limit.
- All of this only after R0 (§5.6): the lock-up comes before the GL budget.
- To be confirmed on the platform: `gstglcontext` CPU of vmix on 14.21.47
  (#81), one ME.

### 5.6 Finding: the GL thread locks up (dev, independent of R)

What was seen (prototype image; its 1-ME pipeline is `dev`'s with frame
counters added):

- The GL thread goes to 100 %, GPU load drops (4 MEs: 55–57 % → 40–43 %),
  every ME's Program falls to 6–20 fps (ME 1 with the keyer lowest), #81's
  guard skips the rest (lag 0.3–1.0 s), memory grows (+0.4–0.6 GiB host,
  +50–130 MiB GPU).
  It **does not recover**: still there 40 s later and after the WebRTC peers
  left; only a restart ends it.
- When: with 10 WebRTC previews, 1 ME: 6 of 10 runs (within 10–60 s); 3 MEs
  1 of 1; 4 MEs 2 of 2; 2 MEs 0 of 1; 4 MEs with the shared keyer 0 of 1.
  Without previews only once: 4 MEs, after 30 s. The unpatched `dev` image
  (`fxre:dev`): 0 of 3 runs with previews (3 min each), so whether plain
  `dev` has it is not proven yet. NUMA placement does not explain it: 1 ME
  pinned to the GPUs' node 0 of 2, to the other node 1 of 2 (host at 89 % CPU
  then), and the 4-ME lock-ups were pinned to the GPUs' node.
- Where (perf, DWARF call graph of the GL thread during a lock-up): 68 % under
  `gst_gl_filter_filter_texture` → `gst_video_frame_map` →
  `gst_gl_memory_texsubimage`, i.e. the **upload of an input texture**
  (glupload defers the copy to the first GL map in the unpack shader), and the
  time inside it is a tight loop in `libnvidia-eglcore` with `clock_gettime`:
  the driver spinning while it waits. Composite, pack and download are 2 %.
- 13.20.43 showed the same 99 % GL thread with 10 previews on the lab (#81's
  table), there with a growing lag instead of skipped frames.

R0 (§7) should reproduce it on `dev`, then look at the upload path: PBO reuse
while the GPU still reads it, the buffer pools of the input branches and the
queues that fill when the compositor slows down (the memory growth), and
whether the skip of #81 keeps the uploads going while Program skips.
`~/mxl-lab/fxre/fxre-hunt.sh` runs until Program falls below 30 fps and then
profiles the GL thread.

---

## 6. Migration and compatibility

- **Defaults:** `FLOWXER_RENDER_MES` unset = 1: the pipeline description, the
  flow ids, the TSL export and the API are today's, with one exception that
  needs a decision (open question 2): the senders of ME 2..4 stop advertising
  ME 1's flow.
- **Saved state:** panels are unchanged; one new workspace field
  (`rendered_panel_count`, default 1). Re-entry sources are derived, never
  saved; an import from an older FlowXer works.
- **Platform production files and designer contract:**
  - New value `renderMes` (1..`panels`, default 1) → `FLOWXER_RENDER_MES`, in
    the catalog's `values_schema` and the chart's env.
  - `nmos_ports.senders` (`ME {n} PGM Video/Audio`, `count: panels`) and
    `default_flows` (`count: panels`) already assume a flow pair per ME:
    unchanged.
  - Tally: `tally: {role: mixer, mes_value: panels}` unchanged; the calculator
    already reads `1000 + n` and propagates (`tally/mxl_tally/engine.py`).
  - Resources: per rendering ME add about half a core (GPU path) or about one
    core (CPU path) to `requests`/`limits` (§5); more than 2 MEs only on the
    GPU path.
  - A production that wires an `ME n PGM` sender to something must render ME n.
- **#81 timeline guard:** per ME (§4.13); `flowxer_frames_dropped_total` stays
  ME 1, per ME in `flowxer_me_frames_dropped_total{me}`.
- **TSL export:** unchanged until an ME ≥ 2 renders; then the extra displays
  of §4.9.

---

## 7. Work breakdown

One PR per row into `dev`, in this order; days are working days for one agent
including tests.

| # | Work | Days |
|---|---|---|
| R0 | Prerequisite: the GL thread lock-up (§5.6): reproduce on the unpatched `dev`, find the cause, fix. Independent of R, but R adds load where it hurts | 1–3 |
| R1 | Pipeline: `FLOWXER_RENDER_MES`, per-ME selectors/compositor/audiomixer/outputs/monitor, element names, per-ME guard and late probe, per-ME metrics. Media tests on both paths: 2 MEs to 2 flow pairs, ME 2 on its own source, description byte for byte with 1 ME | 2–3 |
| R2 | Control: `_put_on_program`, mix, stinger and Wipe for every rendering ME; `StingerPlayer` and `_Mix` per ME; status per panel; tests incl. a mix and a stinger on ME 2 | 2 |
| R3 | NMOS: per-ME flow ids, senders/flows/IS-05 per ME, non-rendering senders; AMWA run | 1 |
| R4 | Re-entry: derived sources, fixed order, pad map, links, QoS drop, API (409s), GUI tiles and monitors; media test: ME 2 on ME 1 reaches ME 1's flow, timing as decided in §4.7 | 2–3 |
| R5 | Raw TSL `1000 + n` displays; tests against the platform's parity vectors | 0.5–1 |
| R6 | Failure isolation test (one ME's sink fails), stall check per ME | 1 |
| R7 | Docs (README, `docs/nmos.md`, plan §8, CHANGELOG), `.env.example`, lab run of the platform layout with 1–4 MEs, measurements into the PR | 1–2 |
| | **Total** | **10.5–16** |

---

## 8. Risks

| Risk | Effect | Mitigation |
|---|---|---|
| GL thread lock-up (§5.6) | Program at 6–20 fps until restart, on any ME count | R0 first |
| One GL thread for all MEs | Ceiling for inputs × MEs | Measured: per ME 2–8 % of the thread; the inputs dominate. If needed later: upload contexts per input (`split_in`, §5.3) |
| One failure domain | An error in one ME's output can stop shared inputs | §4.13, R6 test |
| Concurrent stingers | Each playing stinger decodes on the CPU and uploads every frame | Documented; no limit in R |
| Audio sync across MEs | Re-entered audio passes ME *n*'s audiomixer | Same clock and timestamps; R4 test checks ME 1's audio with ME 2 re-entered |
| Memory | Per ME a compositor pool, pack texture and download buffers | Measured per ME in §5.2; CEF's `mallinfo` cap (#74) stays in place |
| Platform resources | More cores per mixer pod | `renderMes` default 1; resources in the catalog |
| Starting a larger pipeline | More sinks that have to start (the 2-frame start stall of 10.17.40) | Stall check per ME in R6; `async=false` monitor taps already |

---

## 9. Open questions (platform and user)

1. **Order rule:** ME *m* may use ME *n* only for *n* > *m* (ME 1 takes ME 2..4).
   Is that enough, or is ME 1 into ME 2 needed (e.g. a clean feed that
   contains the main Program)?
2. **Senders of non-rendering MEs:** inactive (`master_enable: false`, no flow;
   proposed) or keep advertising ME 1's flow as today?
3. **Platform default:** `renderMes` 1 (today's behaviour) or all panels? With
   `panels: 2` (friday-night-show) ME 2 would cost about half a core.
4. **Keyers:** DSK only on ME 1 in R; is a keyer on other MEs needed later?
5. **Re-entry timing:** measured 0 frames (same output frame), and a repeat of
   ME *n*'s last frame when ME *n* is late. Is that right, or is a fixed,
   always-the-same delay (one frame) preferred?
6. **GUI:** re-entry tiles after the inputs in the ME's source row: fine?
7. **UI tally receivers** (Companion etc.): should they get the re-entries too
   (INDEX 1000 + n + offset), or stay inputs only?

---

## Appendix: prototype and scripts

On the lab in `~/mxl-lab/fxre/`, with the run logs (`seq-gpu.log`,
`rep-me1.log`, `final.log`, `split.log`, `hunt*.log`). The images were removed
after the runs; `Dockerfile.thin` rebuilds them with `~/mxl-lab/bin/lab-build`
from `src.tgz` (prototype, `fxre:proto`) or `dev-src.tgz` (control `fxre:dev`,
`dev` without the patch). Scripts: `fxre-up.sh`/`fxre-down.sh` (vmix
layout, instance `fxre-<name>`, ports 9810/3452/9820, own output domain id),
`fxre-seq.sh`, `fxre-quiet.sh`, `fxre-rep.sh`, `fxre-hunt.sh` (runs until
Program falls under 30 fps, then profiles the GL thread), `fxre-measure.py`,
`mecost.py`, `glsplit.py`, `fxre-gl.sh`. The patch lives on the local branch
`lab/me-reentry-proto` (not pushed); it is not the implementation.
