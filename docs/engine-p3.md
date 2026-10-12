# FlowXer engine P3: transitions and keying

Status: the design; M1–M3 are implemented in #89 (M3 as built: [§6](#m3-as-built)). P3 builds on P2 (the C++/CUDA engine is FlowXer's media path,
#86). The user's P3 decisions of 2026-10-11 are binding. The numbers come from prototype kernels
and test encodes on the lab host (A16 GPU 0) on 2026-10-11 ([Appendix](#appendix-lab-measurements-2026-10-11)).

## Decisions this design follows (user, 2026-10-11)

1. **Wipes:** a basic set to start: horizontal, vertical, box (centre and corners), circle/iris,
   diagonal, clock, plus a few SMPTE wipe codes. Each has soft edge, border width and colour,
   direction and reverse. More patterns later as needed.
2. **Upstream keyers:** 2 per ME by default, up to 8 per ME.
3. **DSKs:** up to 4, available on every ME.
4. **Chroma key:** not in FlowXer. A separate media function delivers a v210a fill + key, which
   FlowXer keys linearly. The luma key stays in FlowXer.
5. **Stingers:** TGA and PNG sequences and video with alpha. Stinger audio goes into PGM.
   FlowXer's stinger concept stays (slots, cut point, library, API).
6. **Keys have no preview:** PVW shows only its source. Keys show only on PGM, when they are on.

## 1. Scope

In:
- a transition engine per ME: Cut, Mix, Wipe and Stinger, with the rate in frames, Auto,
  reverse and flip-flop, re-entry, and audio that follows video;
- the wipe patterns of §4;
- upstream keyers and DSKs on every ME, with linear (v210a) and luma keys;
- stingers on the GPU, with alpha and sound;
- the new NMOS receivers, settings and tally for the keys;
- the GUI: a transition section, keyer and DSK panels, the stinger library, and a type selector
  in the `bus` widget.

Out (later): DME/PiP (P4); keyer ties (a key mixing or wiping on with the next transition);
chroma key (decision 4); audio of key fills (P5); a clean feed before the DSKs; more wipe
patterns; a wipe positioner.

## 2. The ME pipeline

For each output grain *i*, per ME (ME 4 → ME 1, as today):

```
Program A ──┐
            ├─ background: cut | mix (t) | wipe (mask at t) | stinger (A until the cut point, then B)
target B ───┘
   → upstream keyers 1 … K   (those that are on or fading; linear v210a or luma)
   → stinger overlay         (its fill + alpha, while a stinger runs)
   → DSKs 1 … D
   → PGM                     (MXL, the mosaic, re-entry for the lower MEs)
Preview source ─────────────→ PVW (no keys)
```

- **Upstream keyers sit before the transition.** A key that is on belongs to both the outgoing
  and the incoming picture, so a mix or a wipe carries it unchanged.
  - The same key is on both sides, and the maths is linear per pixel, so key(T(A, B)) =
    T(key(A), key(B)). The engine therefore draws each key once, over the transitioned
    background. This halves the work.
  - A wipe's border lies under the keys.
- **A stinger covers the upstream keys.** It hides everything on the ME that changes. The DSKs
  stay above the stinger.
- **PVW** shows the preview source only (decision 6).
- **Re-entry:** when ME *m* takes `me n`, it gets ME *n*'s PGM of the same grain, with ME *n*'s
  transition, keys and DSKs. This is what P2 does, and it is what ME *n*'s PGM flow carries.
- **Implementation:** one fused compose kernel per ME. It computes the background and up to 14
  layers in registers and writes once. A layer costs 0.064 ms in the fused kernel and 0.13 ms
  as a separate pass, like P2's DSK kernel ([§10](#10-gpu-budget)).

## 3. Transition engine (per ME)

The engine's `Mixer` holds this state per ME. The control plane pushes it after every engine
start and saves it.

- **Next transition:**
  - `type`: `mix`, `wipe` or `stinger`;
  - `rate`: 1–1000 frames (a stinger lasts its own length);
  - the wipe settings ([§4](#4-wipes));
  - the stinger slot.
- **Running transition:** its type, start grain, frames, target, the Preview after it, the wipe
  direction and the stinger player.

| Operation | Engine API | Behaviour |
|---|---|---|
| Cut | `POST /me/{m}/cut` | As in P2: the buses swap at the next grain. A running transition ends first, as if it had run to its end; a stinger's overlay and sound stop. |
| Auto | `POST /me/{m}/auto {frames?}` | Runs the next transition type over its rate (a stinger over its own length), then swaps the buses (as P2). 409 while a transition runs. |
| Take | `POST /me/{m}/take {source, type, frames, preview, stinger}` | Program goes to the source with any type. P2's take with frames > 0 is a mix. |
| Manual position (T-bar) | `POST /me/{m}/position {t}` | Optional ([open question 2](#13-open-questions-for-the-user)). See the list below. |
| Fade to Black | (control plane) | As in P2: a mix of Program to Black, under the DSKs ([open question 3](#13-open-questions-for-the-user)). |

- **Progress** follows the grain index, as in P2: t = step / frames. Skipped grains do not
  stretch a transition. Its last grain shows the target in full; the buses swap after it.
- **Manual position (T-bar):**
  - a mix or a wipe follows t (0–1); t = 1 completes the transition and swaps the buses;
  - the next t > 0 starts a new transition;
  - an Auto from a held position finishes the rest at the rate;
  - a stinger does not scrub: moving t off 0 starts it as an Auto.
- **Reverse** runs the wipe pattern backwards (s → 1 − s): a circle closes, a bar runs from right
  to left, and the clock runs counter-clockwise from its start.
- **Flip-flop:** after each completed wipe, the ME toggles reverse, so the next wipe goes back.
  ATEM calls this flip flop. The API field is `alternate`, because `flip_flop` already means the
  bus swap in `/stinger/play`.
- **Re-entry (n > m)** is unchanged. ME *n* renders first, so a wipe or a stinger on ME *n* shows
  in ME *m*'s picture in the same grain. Any transition type works on ME *m* with `me n` as A
  or B. Loops stay impossible.
- **Preview change during a transition:** as in P2. It is allowed, and the transition then ends
  without the bus swap.
- **Audio follows video:**
  - Mix and wipe: P2's equal-power crossfade, driven by the transition's t (not by the wiped
    area). It ramps per sample from the previous grain's t and holds at a manual position.
  - Stinger: the background audio cuts with the picture, as an equal-power crossfade over the
    4 grains around the cut point (cut − 2 … cut + 1; 80 ms at 50p). The stinger's own sound,
    if it has one, is added to the ME's PGM audio for its whole length, sample-accurate with its
    frames ([open question 4](#13-open-questions-for-the-user)).
  - Re-entry carries all of it, through ME *n*'s PGM audio.
- **Tally:** wipes and stingers count like mixes: both sources are LH for the whole transition.
  FlowXer 14 did the same for stingers.

## 4. Wipes

### Model (`core/wipe.hpp`, one code path for the CPU and the GPU)

- **Pattern value:** every pattern maps a pixel (x, y) to s ∈ [0, 1], the moment the edge
  reaches it (0 first, 1 last), with u = (x + ½) / W and v = (y + ½) / H. Reverse is s → 1 − s.
- **Front:** at position t the front is F = t · (1 + soft + border); d = F − s.
- **Without a border:** the pixel is lerp(A, B, m), with m = clamp(d / soft). With soft = 0 the
  edge is hard: m = [d > 0].
- **With a border:** the soft edge is split around the border:
  - mA = clamp(d / (soft / 2)) and mB = clamp((d − soft / 2 − border) / (soft / 2));
  - the pixel is lerp(A, lerp(colour, B, mB), mA).
- **End points:** t = 0 is A everywhere and t = 1 is B everywhere, for every pattern, softness,
  border and direction. The lab check found 0 end-point errors in 920 cases.
- **Chroma (4:2:2):** a chroma sample takes the mean of its pixel pair's masks.
- **Soft and border** are fractions of the pattern's travel (0–1; the GUI shows 0–100 %). Their
  width in pixels therefore depends on the pattern, as on most switchers: at 1080p a horizontal
  wipe with soft 0.05 has a 96-pixel edge.
- **Border colour:** `#RRGGBB`, converted to BT.709 limited-range 10-bit Y'CbCr by the engine's
  existing conversion.
- **Bit-exact:** the code uses only + − × ÷ √ and a polynomial arctangent, compiled without FMA
  contraction (`-fmad=false` for the kernel, `-ffp-contract=off` on the host).
  - The GPU and the CPU reference then agree bit for bit: 0 of 3.8·10⁹ samples differed in the
    lab check.
  - With FMA contraction, 1251 samples differed by one code.
  - `-fmad=false` costs ≤ 2 % of the kernel time.

### Patterns (SMPTE ST 258 / SMIL 2.0 wipe codes)

| Code | Pattern | s(u, v) | Basic set |
|---|---|---|---|
| 1 | bar, left to right | u | horizontal |
| 2 | bar, top to bottom | v | vertical |
| 3, 4, 5, 6 | box from the top-left, top-right, bottom-right, bottom-left corner | max(u, v), max(1−u, v), max(1−u, 1−v), max(u, 1−v) | box (corners) |
| 23, 24, 25, 26 | box from the top, right, bottom, left centre | max(\|2u−1\|, v), max(1−u, \|2v−1\|), max(\|2u−1\|, 1−v), max(u, \|2v−1\|) | |
| 21 | barn door, vertical edges | \|2u − 1\| | |
| 22 | barn door, horizontal edges | \|2v − 1\| | |
| 41, 42 | diagonal from the top-left, top-right corner | (u + v) / 2, (1 − u + v) / 2 | diagonal |
| 45, 46 | diagonal barn door (along the bottom-left–top-right, top-left–bottom-right diagonal) | \|u + v − 1\|, \|u − v\| | |
| 101 | box from the centre (rectangle iris) | max(\|2u−1\|, \|2v−1\|) | box (centre) |
| 102 | diamond | (\|2u−1\| + \|2v−1\|) / 2 | |
| 119 | circle (iris), round in pixels | √(X² + Y²) / r<sub>max</sub>, X = (u − ½) · W/H, Y = v − ½ | circle/iris |
| 201, 202, 203, 204 | clock, clockwise from 12, 3, 6, 9 o'clock | the angle in turns | clock |

- **Direction:** the code sets it for the patterns that have one (1, 2, 3–6, 23–26, 41, 42,
  201–204). Reverse runs any pattern backwards.
- **Set:** 23 codes from 9 shapes. More (vee, star, heart …) later as needed, at one line of
  s(u, v) each.

### Cost

- **Measured** (A16, 1080p, per ME):
  - 0.150–0.160 ms for the bar, box, diagonal, barn-door and diamond patterns, hard or soft;
  - up to 0.185 ms with a border on the centre box, and 0.167 ms on the circle;
  - the clock 0.16–0.20 ms;
  - P2's dissolve takes 0.149 ms for comparison.
- So a wipe costs what a mix costs: both are memory-bound. The clock's arctangent adds at most
  0.05 ms.
- In the fused kernel the wipe is the background stage: no extra pass and no extra memory.

### API and picker

- **Engine:** the ME's next transition, for example `{"type": "wipe", "frames": 25,
  "pattern": 119, "reverse": false, "alternate": false, "soft": 0.05, "border": 0.02,
  "border_color": "#ffffff"}`.
- **Control plane:** `PATCH /panels/{panel_id}/transition` ([§7](#7-engine-and-control-plane-changes)),
  shown in `GET /console` → `panels[].transition`.
- **GUI picker:** a grid of pattern icons, each with its SMPTE code as a tooltip. The browser
  draws them from the same s(u, v) with a TypeScript port that is checked against vectors from
  the C++ reference.

## 5. Keyers

### Model

- **Counts per rendering ME:** K upstream keyers (`FLOWXER_KEYERS_PER_ME`, 0–8, default 2) and
  D DSKs (`FLOWXER_DSKS_PER_ME`, 0–4, default 1).
  - Every ME has the same K and D. Every ME has its own DSKs (decision 3).
  - The alternative is a pool of 4 DSKs that an operator assigns to an ME
    ([open question 1](#13-open-questions-for-the-user)).
- **One keyer type for both.** Only its place in the chain differs ([§2](#2-the-me-pipeline)).
- **Modes:**
  - `linear`: fill + key from the keyer's own key input, a v210a receiver. α = key / 1023 · gain,
    with a straight fill (as P2's DSK 1). The chroma-key function (decision 4) and
    mxl-browser-source deliver this.
  - `luma`: a self key of any source the ME may take (a live input, a clip player, or `me n`
    with n > m). α = clamp(((Y − 64) / 876 − clip) / soft) · gain, with an optional invert. The
    fill is the source itself.
    - A clip player keyed on air counts as on air, so play on cut starts it.
    - Key fills carry no audio (P5).
- **On, off and auto** per keyer, as P2's DSK: cut on or off at the next grain, or fade the gain
  over the keyer's rate in frames. An auto during an auto turns back from where it is.
- **Order:** upstream keyer 1 is lowest, K highest; DSK 1 is lowest, D highest.
- **PGM only:** keys never show on PVW (decision 6).

### Key inputs

- **One v210a receiver per keyer** ([§8](#8-contract-changes-for-the-platform)). The engine reads
  it at grain *i − L*, like a live input. An unrouted key input keys nothing (as P2).
- **One reader per flow:** the engine opens one reader per distinct routed flow. Keyers routed
  to the same flow (one graphic on several MEs) share its reader, upload and frames.
- **Frames on demand:** the frames are allocated when a flow is first routed (stream-ordered, so
  the render never waits), not at start.
  - P2 allocates every key input's frames at start: (L + 6) × 8.3 MB = 83 MB at L = 4.
  - For 4 MEs × (2 + 1) keyers that would be 1 GB, most of it unused.
- **PCIe:** each distinct routed flow uploads 8.3 MB per grain (415 MB/s at 1080p50). On the
  lab's x4 link that is 1.32 ms per grain.

### Cost and latency

- **GPU:** 0.064 ms per active layer in the fused kernel (lab). A keyer that is off costs
  nothing.
- **Latency:** none added.
  - Keys use the inputs' latency L; only the render time grows by the layers.
  - The engine reports a key input's arrival lag like an input's (P2), so L can be checked.
  - The chroma-key function must keep grain indices (its output grain *i* is made from its input
    grain *i*) and arrive within L. Then the key and the background stay in step.

### DSK 1 of P2

- **P2:** the keyer `dsk-1` ("DSK 1") on ME 1, with the receiver `DSK 1 Key` (its id comes from
  the NMOS seed, `dsk-1` and `key`), `PATCH /keyers/dsk-1` and `POST /keyers/dsk-1/auto`.
- **P3, ME 1's DSKs:** DSK k keeps the id `dsk-k`, the label `DSK k`, the receiver `DSK k Key`
  and its receiver id. DSK 1 therefore keeps its IS-05 activation, its state (on, rate) and its
  API.
- **P3, the other keyers:**
  - the DSKs of ME *n* ≥ 2 are `me-n-dsk-k` ("ME n DSK k"), receiver `ME n DSK k Key`;
  - the upstream keyers are `me-n-key-k` ("ME n Key k"), receiver `ME n Key k`.
- **Saved states:** `downstream_keyer_count` (0 or 1 in P2) becomes the DSKs per ME. A P2 keyer
  without an ME is a DSK of ME 1.
- **Labels:** with the counts set by the environment, keyer labels are fixed (409 on a rename),
  as input labels are. The platform finds the receivers by label.

## 6. Stingers

### Concept (kept)

- **Stinger slots:** `shared`, or `in` and `out`, 1–8 of them. Each has a library stinger and a
  cut point (`cut_frame`, `cut_ms`).
- **Triggers, as today:**
  - the auto-stinger of a source;
  - an armed Wipe makes the next Cut a stinger. This is FlowXer 14's meaning of `/mixer/wipe`,
    kept for its clients; the GUI moves to the transition type;
  - `POST /stinger/play`, and a take with `transition: "stinger"`;
  - the slot by direction: going to a clip player uses `in`, anything else `out`, as in P2's GUI.
- **New:** `stinger` as an ME's next transition type, with its slot.
- **What changes:** stingers play, on the GPU, with alpha and sound. In P2 they are cuts.

### Import

- **Sequences:**
  - TGA (as today) and PNG, 8- or 16-bit RGBA, in a ZIP or through `/uploads/sequence`;
  - the frame rate as today (`sequence_fps`);
  - one optional WAV in the ZIP is the sound, aligned to frame 0.
- **Video with alpha:**
  - accepted: ProRes 4444 and 4444 XQ, QuickTime Animation (qtrle), PNG or uncompressed RGBA in
    MOV, FFV1 with alpha, and VP9 with alpha in WebM (decoded with libvpx, which the image has);
  - video without alpha is an opaque (full-frame) stinger;
  - HEVC or H.264 with an alpha layer (Apple) is read without its alpha: it is imported as
    opaque, with a warning.
- **Sound:** the video's audio, conformed like a clip's: 48 kHz float32, padded or cut to the
  frames.

### Mezzanine with alpha

NVDEC decodes no alpha. Ampere's NVDEC has no monochrome HEVC either: `cuvidGetDecoderCaps` on the
A16 reports HEVC 4:0:0 unsupported and HEVC 4:2:0 and 4:4:4 at 8 and 10 bits supported. AV1 4:0:0
decodes, but Ampere's NVENC cannot encode AV1. The measured candidates:

| | Photo stinger (2 s: a camera fill, a soft band of alpha) | Graphic stinger (1 s: flat colours) |
|---|---|---|
| ProRes 4444 (today's mezzanine) | 3234 MB/min | 157 MB/min |
| Colour: HEVC 4:4:4 10-bit, NVENC QP 8 (the clip mezzanine) | 411 MB/min | 3 MB/min |
| Alpha as the luma of HEVC 4:2:0 10-bit, NVENC QP 8 | 1 MB/min; max error 15 codes (69 dB) | 0.2 MB/min; max error 3 codes |
| Alpha as the luma of HEVC 4:2:0 10-bit, NVENC lossless | 7 MB/min; exact | 0.2 MB/min; exact |
| One stream: colour above, alpha below (1920 × 2160 4:4:4, QP 8) | 413 MB/min; alpha max error 15 codes | 3 MB/min |

NVDEC speed (one A16 GPU, 2 decoders):

| Stream | Frames per second |
|---|---|
| Colour | 629 |
| Lossless alpha | 1540 |
| Colour and alpha in lockstep | 584 |
| The stacked stream | 387 |

One stinger playing at 50 frames/s uses 5 % of the decoders.

**Choice: two streams.**

| File | Content |
|---|---|
| `mezz-<format>.mkv` | Colour, exactly the clip mezzanine: HEVC Main 4:4:4 10, repeated chroma, NVENC QP 8 (x265 CRF 8 as the fallback) |
| `mezz-<format>-alpha.mkv` | Alpha as the luma of HEVC Main10 4:2:0, lossless (chroma 512). Absent for an opaque stinger. |
| `mezz-<format>.f32` | Sound. Absent when the stinger is silent. |

- Lossless alpha is exact and costs almost no space.
- One decoder per stream keeps the colour path the clip path.
- The stacked stream decodes 1.6 × slower and its alpha is lossy.
- The ProRes file `mezz-<format>.mov` goes away. Existing stingers are converted again at the
  next start; their originals stay.

### Playback

- **Stinger players** (`FLOWXER_STINGER_PLAYERS`, default 1) work like clip players with two
  decoders in lockstep. Each frame becomes a v210a picture: the colour through P2's 4:4:4 → v210
  kernel, the alpha into the key plane. The overlay is therefore a linear key layer.
- **Cue:** the control plane keeps cued on a player (decoders open, the first 5 frames decoded)
  the stinger that the ME's next Cut or Auto would play:
  - the ME's stinger slot, when its type is stinger or Wipe is armed;
  - or the auto-stinger of the source on Preview.
  
  A take then starts at the next grain.
- **Not cued:** a stinger that is not cued (a direct take, a second ME) is cued on demand. It
  starts when its first frame is ready (P2's clip players need 2–3 grains after a load) and the
  wait is counted. A stinger whose mezzanine still converts is a cut, as today.
- **Timing:**
  - a stinger started at grain *s* shows its frame *f* in output grain *s + f*;
  - the background switches to B at grain *s + cut*;
  - the transition ends after the stinger's last frame and the buses swap (Program has shown B
    since the cut);
  - progress follows the grain index, so skipped grains advance it;
  - a frame not decoded in time repeats the last one and is counted (`late`).
- **Sound:** the samples of frame *f*, on the library's grid, are added to the ME's PGM audio
  ([§3](#3-transition-engine-per-me)).
- **GPU memory per stinger player while cued:** about 0.35 GB; measured in M3: 347 MiB (the
  pool 71 MiB, the colour and alpha decoders 276 MiB). The estimate:
  - P2's clip decoder takes 170 MB;
  - the 4:2:0 alpha decoder takes about half of that (the FFmpeg CLI measured alpha / colour =
    0.54);
  - 9 v210a frames take 75 MB.
- **NVDEC:** one playing stinger uses 5 % of an A16 GPU's two decoders. The A4000 may have fewer
  decoders (check `nNumNVDECs` on host-03); its share would then be about twice that.

### M3 as built

- **Auto and the T-bar with the type stinger:** the engine's next transition carries no stinger
  player, so FlowXer sends a take with the player and the cut frame; the engine's own Auto with
  the type stinger is a cut.
- **Waiting for the first frame:** at most 50 grains (1 s at 50p), then the take is a cut
  (`stingers.cuts`); the wait is counted (`stingers.waits`).
- **Which stinger player k holds:** the stinger ME k's next Cut or Auto plays, in this order:
  Preview's auto-stinger, an armed Wipe's slot, the slot of the type stinger. A player is free
  again 0.1 s after its stinger (1 s for one loaded on demand).
- **Armed Wipe:** as in FlowXer 14, a Cut that plays Preview's auto-stinger leaves Wipe armed;
  the Cut that plays the armed Wipe's stinger uses it up.
- **Alpha of 8-bit sources:** FFmpeg widens it to 10 bits (255 → 1023); the mezzanine stores the
  10-bit alpha losslessly, and the engine keys with key / 1023.
- **The background sound** at the cut point: t at the end of frames cut − 2 … cut + 1 is 0.25,
  0.5, 0.75, 1; frame 0 starts from 0 and the last frame ends at 1, so a cut point near either
  end does not jump.
- **Removed:** `FLOWXER_RAM_CLIP_MAX_S` and `FLOWXER_RAM_BUDGET_MB` (only P2's stinger conversion
  used them).

## 7. Engine and control plane changes

### Engine

- **Code:**
  - `core/wipe.hpp`: patterns and masks; the CPU reference is the GPU code;
  - `core/mixer`: the transition type, wipe settings, stinger runs, keyer layers and the T-bar;
  - one fused compose kernel in `engine/gpu.cu` (background + layers) and an alpha → key-plane
    kernel;
  - stinger players: `engine/player.cpp` gains a second decoder and the sound;
  - key inputs allocated on their first route, one reader per flow;
  - `--selftest` gains the wipe and key checks.
- **Configuration** (internal; the control plane writes it):
  - `FLOWXER_ENGINE_KEYERS` and `FLOWXER_ENGINE_DSKS` (labels per ME) replace
    `FLOWXER_ENGINE_KEYS`;
  - new `FLOWXER_ENGINE_STINGER_PLAYERS`.
- **API:** on localhost; only FlowXer's control plane uses it, so it needs no compatibility.
  - `POST /me/{m}/transition`, `/auto`, `/take` (with `type` and `stinger`), `/position`;
  - `POST /me/{m}/key/{k}` and `/me/{m}/dsk/{k}`: `on`, `/auto`, and the settings `mode`,
    `source`, `clip`, `soft`, `gain`, `invert`;
  - `PUT /me/{m}/key/{k}/input` and `/me/{m}/dsk/{k}/input`;
  - `POST /stinger/{p}/load`;
  - `/status` and `/metrics`: per ME (transition, keys on air, compose ms), per key flow (state,
    arrival lag, uploads) and per stinger player (cued item, frames ahead, late frames).

### Control plane (public API; P2's calls keep working)

- **Transitions:**
  - `PATCH /panels/{panel_id}/transition` `{type, rate_frames, wipe: {pattern, reverse,
    alternate, soft, border, border_color}, stinger_slot_id}`, readable in `GET /console`
    (`panels[].transition`) and `GET /mixer`;
  - `POST /mixer/auto` `{panel_id, frames?}` runs the ME's next transition;
  - `POST /mixer/fade` stays a mix, `/mixer/cut` a cut (with auto-stinger and an armed Wipe as in
    FlowXer 14), `/mixer/fade-to-black` as in P2;
  - `POST /mixer/take`: `transition` gains `wipe` (with the ME's wipe settings), and `frames`
    joins `duration_ms`;
  - `POST /mixer/position` `{panel_id, position}`, if the T-bar is wanted.
- **Keyers:**
  - new `GET /keyers`;
  - `PATCH /keyers/{id}` gains `mode`, `source_id`, `clip`, `soft`, `gain` and `invert`;
  - `POST /keyers/{id}/auto` as in P2;
  - keyer objects gain `panel_id` and `stage` (`upstream` or `downstream`).
- **Stingers:** `/stinger/play` plays the stinger; stinger slots as in P2. The library takes PNG
  sequences and a WAV in a ZIP, and stinger items gain `has_sound`.
- **Workspace:** `keyers_per_me`, `dsks_per_me` (replaces `downstream_keyer_count`) and
  `stinger_players`, pinned by the environment like the panels.

## 8. Contract changes for the platform

Relative to the P2 contract.

### Environment of the mixer container

| Variable | Values | Default | Designer value |
|---|---|---|---|
| `FLOWXER_KEYERS_PER_ME` | 0–8 | 2 | `keyersPerMe` |
| `FLOWXER_DSKS_PER_ME` | 0–4 | 1 | `dsksPerMe` |
| `FLOWXER_STINGER_PLAYERS` | 0–4 (at most the rendering MEs) | 1 | `stingerPlayers` |

- When set, they are pinned, like `FLOWXER_PANELS`: the API cannot change them and the keyer
  labels are fixed.
- Unchanged: `format`, `liveInputs`, `inputLabels`, `clipPlayers`, `panels`, `renderMes`,
  `latency`, `pvwOutputs`, `programAutostart`, the tally, NMOS, preview and widget settings.

### NMOS receivers (new)

One receiver per keyer of each rendering ME: BCP-007-03 `urn:x-nmos:transport:mxl`, caps
`video/v210a` at the production raster.

| Receiver label | Keyer | Count | Group hint | Id name |
|---|---|---|---|---|
| `ME {n} Key {k}` | upstream keyer k of ME n | renderMes × keyersPerMe | `me-{n}-key-{k}:Key` | `me-{n}-key-{k}`, `key` |
| `DSK {k} Key` | DSK k of ME 1 (P2's `DSK 1 Key`, same id) | dsksPerMe | `dsk-{k}:Key` | `dsk-{k}`, `key` |
| `ME {n} DSK {k} Key` | DSK k of ME n ≥ 2 | (renderMes − 1) × dsksPerMe | `me-{n}-dsk-{k}:Key` | `me-{n}-dsk-{k}`, `key` |

- No new senders.
- With the defaults (2 + 1 per ME) that is 3 receivers per rendering ME.
- The catalog's `nmos_ports` needs a count per ME × keyer, or the label list from these rules.

### Tally (raw TSL 5.0, `FLOWXER_TALLY_TSL`)

- **Wipes and stingers:** both sources are LH for the whole transition, as for mixes.
- **Luma keys:** while a luma key is on air (on or fading) on ME *n*, its source's INDEX (or
  1000 + *n'* for a re-entered ME) is LH on SCREEN *n*. Never RH: keys have no preview.
- **Linear keys:** each key receiver gets a new INDEX, LH on SCREEN *n* while its keyer is on air:
  - `2000 + 100·n + k` for `ME n Key k`;
  - `3000 + 100·n + k` for ME *n*'s DSK *k* (ME 1 DSK 1 = 3101);
  - TEXT is the receiver label;
  - the calculator maps these to the key receivers and follows their IS-05 connection, for
    example through the chroma-key function to its camera. This is new for the platform
    ([open question 7](#13-open-questions-for-the-user)).
- The README's "lights nothing: the downstream keyer" goes away.

### GPU, memory, PCIe

- **GPU:** `gpu: required`, as in P2.
- **`gpu_memory_mb`** ≈ 1300 + 85 × distinct routed key flows + 350 × `stingerPlayers`.
  - Per key flow: 85 MB at 1080p with L = 4; 100 MB at L = 6; × 4 at 2160p.
  - From the values, with every keyer routed to a flow of its own:
    1300 + 85 × renderMes × (keyersPerMe + dsksPerMe) + 350 × stingerPlayers.
  - With the defaults that is 2670 MB for 4 MEs and 1905 MB for 1 ME.
- **PCIe (host → GPU):** +415 MB/s per distinct routed key flow at 1080p50.
- **NVDEC:** a stinger's two streams while it plays.

### Limits

- **Sources** stay at most 23 + Black. Key inputs are not sources: they have no slot and no PVW.
- **Keyers:** at most 4 × (8 + 4) = 48 receivers.
- **Distinct routed key flows** at 1080p50, next to 8 live inputs:
  - at most 8 on a link like the lab's (PCIe Gen4 x4 or Gen3 x8, 6.3 GB/s);
  - at most 16 on Gen3 x16 ([§10](#10-gpu-budget)).
  
  The engine reports the upload time per grain.
- **2160p50:** 1 ME with keys on the lab; at most 2 on an A4000 with a x16 link
  ([§10](#10-gpu-budget)).

### Backward compatibility (P2 → P3)

- `DSK 1 Key` keeps its label, id and group hint, and its IS-05 activation.
- `/keyers/dsk-1`, `/keyers/dsk-1/auto` and `rate_ms` are unchanged.
- `downstream_keyer_count` becomes `dsks_per_me` (0 or 1 stays 0 or 1).
- These calls are unchanged: `/mixer/cut`, `/mixer/fade`, `/mixer/fade-to-black`, `/mixer/take`
  (`cut`, `mix`, `stinger`), `/mixer/wipe`, `/stinger/play`, the stinger slots and the
  auto-stinger. Stingers and an armed Wipe now play instead of cutting.
- Stinger library items are converted again (ProRes 4444 → HEVC + alpha). Until a stinger is
  converted, its take is a cut.
- New receivers appear (the upstream keyers', and `ME n DSK 1 Key` on MEs ≥ 2). Nothing that is
  routed changes.

## 9. GUI and widgets

- **Transition section per ME** (replaces the bank's Cut / Fade / Fade to Black / Wipe):
  - the type (MIX | WIPE | STING) and the rate in frames;
  - WIPE: the pattern picker (icons drawn from s(u, v), the SMPTE code as a tooltip), reverse,
    flip-flop, soft and border sliders (0–100 %) and the border colour;
  - STING: the slot, with its cut frame and whether it has sound;
  - CUT, AUTO and FTB, the progress of the running transition, and a T-bar if it is accepted.
- **Keyers per ME** (on the active ME tab): for each upstream keyer ON/OFF, AUTO and the rate,
  and its mode:
  - linear, with NO KEY when its key input has no signal;
  - or luma: source, clip, soft, gain, invert.
- **DSKs per ME:** P2's DSK control for each DSK of the active ME.
- **Stinger library and import** (File → Stinger library…):
  - upload a video with alpha, or a TGA/PNG ZIP with an optional WAV;
  - each stinger shows its frames, alpha, sound, cut frame and conversion state;
  - the slot's gear sets the stinger and the cut frame.
- **Settings → Console layout:** keyers per ME, DSKs per ME and stinger players, greyed out when
  the environment sets them.
- **Widgets:**
  - `bus` gains the ME's transition type selector (MIX / WIPE / STING) next to Cut and Auto. A
    new parameter `transition` (boolean, default true) hides it. The minimum size stays 480 × 150
    (to be checked).
  - New `keys` (optional, [open question 11](#13-open-questions-for-the-user)): one ME's keyers
    and DSKs (ON, AUTO), parameter `me`.

## 10. GPU budget

**Target:** at most 15 ms per grain on the A4000, render plus download.

**Worst case of the brief:** 4 MEs × (a wipe + 2 upstream keys + 4 DSKs), plus a stinger on each
ME, plus the mosaic, at 1080p50 in yuv16.

| Item | A16 lab (measured) | A4000 (estimate) |
|---|---|---|
| P2 render (8 unpacks, 4 MEs PGM + PVW, 8 packs, the mosaic): 2 h soak | 3.59 ms (max 5.05) | 1.6 ms |
| A wipe instead of a mix, per ME | +0.00 to +0.05 ms | ≈ 0 |
| One key layer (USK, DSK or stinger overlay), fused | +0.064 ms | +0.03 ms |
| One key layer as a separate pass (P2's DSK kernel) | +0.095 (sparse key) to +0.177 ms (dense key) | |
| 4 × (wipe + 2 USK + 4 DSK) | +1.6 ms fused (+3.2 separate) | +0.7 ms |
| Plus a stinger on every ME | +0.26 ms fused | +0.1 ms |
| **Render, worst case** | **≈ 5.5 ms** fused (7.3 separate) | **≈ 2.5 ms** |
| Download, 8 outputs (44 MB) | 6.87 ms (6.5 GB/s) | 3.5 ms (Gen3 x16) to 6.9 ms (Gen3 x8) |
| **Render + download** | **≈ 12.4 ms (max ≈ 14)** fused; ≈ 14.2 ms (max ≈ 16) separate | **≈ 6–9.4 ms** |
| Upload of 8 inputs and K key flows (own streams; overlaps the render) | 7.0 + 1.32 · K ms per 20 ms grain | 3.5–7 + 0.66–1.32 · K ms |

- **Synthetic check** of the whole render (4 MEs, no mosaic):
  - P2-like: 2.86 ms;
  - wipe + 6 layers: 4.49 ms fused, 6.14 ms separate;
  - wipe + 7 layers: 4.71 ms fused, 6.68 ms separate.
- **A4000:** the kernels are memory-bound. The A4000 has 448 GB/s against 200 GB/s for an A16
  GPU (the lab copies at 164 GB/s, 82 % of that), so the estimate divides by 2.24.
  - Its PCIe link on host-03 is not known. The i9-9900K has 16 lanes, shared with the RDMA NICs,
    so a Gen3 x8 link is possible. `nvidia-smi --query-gpu=pcie.link.gen.current,pcie.link.width.current`
    under load settles it.
  - The A4000 is time-sliced with the node's other GPU functions. The 15 ms target leaves 5 ms
    for that.
- **Where it does not fit:**
  - **Separate passes on the lab** (max ≈ 16 ms): the fused kernel is a must.
  - **Upload:** more than about 8 distinct key flows at 1080p50 next to 8 inputs on a 6.3 GB/s
    link (8 × 1.32 + 7.0 = 17.6 ms of 20). The limit there is 8, and 16 on Gen3 x16. If it is
    ever needed: key inputs whose keyers are all off stop uploading, and such a key then goes on
    2 grains later.
  - **2160p50** (everything × 4): on the lab, 1 ME with 7 layers takes ≈ 4.7 ms to render plus
    6.7 ms to download. 2 MEs fit only on an A4000 with a x16 link (≈ 11.3 ms).
- **GPU memory:** P2's 1268 MiB (4 MEs, 8 inputs, 2 clip players, 1 key, the mosaic) + 85 MB per
  routed key flow + 0.35 GB per cued stinger player. Wipes and the fused kernel add none.

### M4 as built

- **Metrics** (`/metrics`; the full list is in the README):
  - stingers: `flowxer_engine_stingers_played_total`, `_stinger_wait_grains_total`,
    `_stinger_cuts_total`, per player `_stinger_player_state{player,state}`,
    `_stinger_frames_decoded_total`, `_stinger_frames_late_total`, `_stinger_decode_ms`;
  - keyers: `flowxer_engine_keyer_on_air{keyer,me,stage,mode}`, `_keyer_key_running`;
  - the upload ms of each input and key flow: `flowxer_engine_input_upload_ms{input,stat}`;
  - `flowxer_transitions_total{type="wipe"}`.
- **Worst case in the engine** (`fxp3-worst.py`, lab GPU 0, PCIe Gen4 x4 like an A4000 on Gen3 x8):
  4 MEs with PGM and PVW, 8 live inputs, 2 clip players, the mosaic; 2 upstream keyers and 4 DSKs
  per ME (24 linear keys, all on), 4 stinger players. A wipe and a stinger cannot run on one ME at
  once, so it measures two cases: every ME holding a wipe at t = 0.5 (circle, soft edge, border)
  under its 6 keys, and every ME running a 50-frame ProRes 4444 stinger again and again under its
  6 keys; each with 1 key flow and with 8 distinct key flows. Results: after the run.
- **2 h soak** (`fxp3-soak.py`, the P3 defaults with 2 stinger players): 4 MEs with PVW, 8 live
  inputs, a looping clip and the bars; per ME Key 1 linear (browser source v210a), Key 2 luma of
  the clip, DSK 1 linear: all on. Every 10 s on a rotating ME a mix, a wipe (the 23 codes, soft
  and border varied, flip-flop) or a stinger (a PNG stinger with sound and a ProRes 4444 graphic,
  loaded on demand); a T-bar move every minute; FTB on and off every 3 min; a clip import every
  15 min; 5 WHEP viewers. Results: after the run.

## 11. Milestones

Each milestone is tested on the lab (A16 GPU 0) before the next one starts. Small and the
release follow only with the user's OK. One PR into `dev`, growing per milestone, as #86 did.

| Milestone | Builds | Lab test |
|---|---|---|
| **M1: transitions and wipes** | The wipe model and patterns: CPU reference, fused compose kernel (`-fmad=false`), self-test. The transition type per ME (mix, wipe), Auto, take with a type, reverse and flip-flop, the T-bar if wanted, AFV with t, tally. The control plane API. The GUI transition section with the pattern picker, and the `bus` widget's selector. | Every code on MXL output bit-exact against the CPU reference of the input grains; exact end points; the audio crossfade; re-entry with a wipe on the higher ME; GPU ms. |
| **M2: keyers** | Upstream keyers and DSKs per ME (linear, luma) as layers of the fused kernel. Key inputs allocated on first route, one reader per flow. NMOS receivers, environment and values, tally for keys. The P2 DSK 1 mapping and state migration. The GUI keyer and DSK panels. | Key maths exact (0 codes) for each layer on each ME; the order (USK < stinger < DSK) on known patterns; PVW clean; receivers and caps in the registry; IS-05 routing; tally INDEX; upgrade from a P2 state with `DSK 1 Key` routed. |
| **M3: stingers** | Library import (TGA, PNG, video with alpha, WAV) into the colour + lossless alpha + sound mezzanine. Stinger players: cue, overlay, cut point, sound into PGM, AFV at the cut. The control plane (slots, auto-stinger, armed Wipe, type stinger). The GUI library. | Frame timing and cut point to the grain; sound to the sample; exact alpha; decoder memory; NVDEC load; cue on demand; import time on air. |
| **M4: budget, soak, release readiness** | Metrics; docs (README, engine/README, CHANGELOG); the platform note. | The worst case of [§10](#10-gpu-budget) measured in the engine (render, download, upload, memory); a 2 h soak with transitions, keys and stingers every few seconds. Then small, with the user's OK. |

## 12. Test plan

### Unit tests (CPU `fxeng-tests`, pytest, GUI)

- **Wipe:**
  - every code at known points (corners, centre);
  - t = 0 is all A and t = 1 all B for every code × softness × border × direction;
  - reverse = 1 − s;
  - the soft ramp and the border width.
- **GPU self-test (`--selftest`):**
  - the wipe kernel equals the CPU reference bit for bit (23 codes × 5 positions × 4
    soft/border settings × 2 directions, as in the lab check);
  - for key layers, the fused compose kernel equals the separate passes and the reference.
- **Key maths:**
  - a linear key is mix10(bg, fill, key / 1023 · gain) per sample, with the pair's mean α for
    chroma;
  - the luma key formula;
  - the layer order.
- **Mixer:** per ME:
  - the transition type and Auto per type, with the rate in frames;
  - the reverse/flip-flop toggle;
  - a Cut during a wipe and during a stinger;
  - a take with Preview, and re-entry;
  - T-bar progress and completion;
  - the stinger's cut grain and end, and skipped grains;
  - keyer gains;
  - AFV t per sample, and the crossfade at the cut.
- **Control plane** (simulate mode and a fake engine):
  - API schemas;
  - migration of a P2 state (`downstream_keyer_count`, `dsk-1`);
  - receivers: labels, caps, and the unchanged `DSK 1 Key` id;
  - the tally INDEX of keys;
  - the choice of stinger slot (auto-stinger, armed Wipe, in/out);
  - PNG and WAV ingest, and the mezzanine commands.
- **GUI:** `tsc`, and the TypeScript pattern port against vectors from the C++ reference.

### Lab (`fxp3-functional.py`, from `fxp2-functional.py`, through FlowXer's API and IS-05 only)

- **Wipes:** at a held position (T-bar) or during a long Auto, PGM grain *i* read from MXL equals
  the reference of input grains *i − L*, bit for bit, for each code.
- **Keys:**
  - each USK and DSK on each ME, keyed from mxl-browser-source v210a (the transparency page),
    and a luma key on the colour bars still: 0 codes of error;
  - PVW is the source, bit for bit.
- **Stingers:** a test stinger with its frame number burnt into the fill, a known alpha and a
  click per frame:
  - frame *f* is in grain *s + f*;
  - B is on air from grain *s + cut* exactly;
  - click *f* is at the samples of grain *s + f*.
- **Performance and soak:** the worst-case matrix, a 2 h soak, and an upgrade from P2.

Then small, with the user's OK.

## 13. Open questions (for the user)

1. **DSK model:** does each ME have its own D DSKs (0–4, the same on every ME; proposed), or is
   there a pool of 4 that an operator assigns to any ME? And does ME 1 keep `DSK k Key`
   (proposed, P2-compatible) or become `ME 1 DSK k Key`?
2. **T-bar:** add a manual position (`POST /mixer/position` and a GUI T-bar)? FlowXer has none
   today. It costs little and makes the wipe tests exact.
3. **Fade to Black:** keep P2's mix to Black under the DSKs, or add a real FTB after the DSKs
   that fades the keys too?
4. **Stinger audio:** crossfade the background over 4 grains at the cut point (proposed), across
   the whole stinger, or duck it under the stinger's sound?
5. **Stinger players:** is 1 by default enough (one ME stings at a time; about 0.35 GB)?
6. **Keyer ties:** add "next transition: background + key k" (a key that mixes or wipes on with
   the background) later (proposed), or in P3?
7. **Tally for v210a keys:** are the INDEX ranges 2000/3000 + 100·n + k OK? The platform's
   calculator must map and follow them.
8. **Wipe codes:** are 1, 2, 3–6, 21–26, 41, 42, 45, 46, 101, 102, 119 and 201–204 the right
   start set?
9. **Re-entry:** keep the higher ME's DSKs in the re-entered picture (as P2), and add a clean
   feed (before the DSKs) only if it is needed later?
10. **DSKs on the other MEs:** with the default of 1 DSK per ME, MEs 2–4 also get one. Or should
    they get 0 (this needs counts per ME)?
11. **`keys` widget:** add it for operator screens in P3?

## Appendix: lab measurements (2026-10-11)

**Setup:** lab host iptv-web-lab-1, NVIDIA A16 GPU 0 (GA107, 10 SMs, PCIe Gen4 x4), driver
595.99.02, 2 × Xeon Gold 6136. The prototype sources and scripts are on the lab host in
`~/mxl-lab/fxp3/` (`fxp3-bench.cu`, `cuvidcaps2.c`, `fxp3-codec.sh`, `fxp3-decode*.sh`), not in
this PR. The kernels become engine code in M1–M3.

### Kernels

`fxp3-bench`: CUDA events, 300 runs each, 1080p yuv16, random pictures, built with CUDA 12.8 for
sm_86 with `-fmad=false`.

| Kernel | ms |
|---|---|
| unpack (v210 → yuv16) / pack / copy | 0.142 / 0.086 / 0.101 |
| mix (P2's dissolve) | 0.149 |
| wipe, codes 1, 3, 21, 41, 45 (hard, soft, soft + border) | 0.150–0.153 |
| wipe 101 / 102 / 119 with soft + border | 0.185 / 0.155 / 0.167 |
| wipe 201 (clock): hard / soft / soft + border | 0.163 / 0.183 / 0.202 |
| linear key from v210a, one pass: lower third / dense key | 0.095 / 0.177 |
| luma key, one pass | 0.165 |
| one ME, circle wipe + 0 / 2 / 4 / 6 / 7 / 13 layers, separate passes | 0.157 / 0.434 / 0.718 / 0.972 / 1.074 / 1.855 |
| the same, fused | 0.193 / 0.324 / 0.438 / 0.565 / 0.632 / 1.022 |
| 4 MEs (8 unpacks, 4 × background + layers, 4 PVW copies, 8 packs): P2-like / wipe + 6 / wipe + 7 layers, fused | 2.86 / 4.49 / 4.71 |
| the same, separate passes | – / 6.14 / 6.68 |
| PCIe: H2D of a v210a picture (8.3 MB) / D2H of a v210 picture (5.5 MB) | 1.31 (6.3 GB/s) / 0.85 (6.5 GB/s) |

- **Without `-fmad=false`** the kernels are 1–2 % faster.
- **At 2160p** everything is about 4 × slower:
  - the 4-ME P2-like render is 11.3 ms;
  - wipe + 7 layers: 18.6 ms fused, 26.3 ms separate;
  - one ME with 7 layers: 2.49 ms fused.
- **Bit-exact check** (`fxp3-bench 1080 exact`): 920 cases (23 codes × t ∈ {0, 0.13, 0.5, 0.77,
  1} × 4 soft/border settings × 2 directions), each compared over every sample with the CPU
  reference:
  - with `-fmad=false`, 0 samples differ;
  - with the default FMA contraction, 1251 samples differ;
  - 0 end-point errors in both builds.

### NVDEC capabilities (`cuvidcaps2`, A16)

| Codec | Supported (8 and 10 bit) | Not supported |
|---|---|---|
| HEVC | 4:2:0, 4:4:4 | 4:0:0, 4:2:2 |
| AV1 | 4:0:0, 4:2:0 | 4:2:2, 4:4:4 |
| VP9 | 4:2:0 | 4:0:0, 4:2:2, 4:4:4 |
| H.264 | 4:2:0, 8-bit only | everything else |

Every supported format reports 2 NVDECs and up to 8192 × 8192 (H.264: 4096 × 4096).

### Stinger mezzanine (`fxp3-codec.sh`, `fxp3-decode2.sh`; the FlowXer P2 image's FFmpeg)

- **Sources** (ProRes 4444, 1080p50):
  - photo: 2 s of a camera clip as the fill, with an alpha band (500 px solid, 150 px soft edges)
    sweeping diagonally across;
  - graphic: FlowXer's own replay-wipe TGA sequence, 50 frames.
- **Encodes:**
  - colour as the clip mezzanine;
  - alpha with `extractplanes=a` into the luma of `yuv420p10le` (chroma 512, built with
    `mergeplanes`, no range conversion);
  - NVENC HEVC Main10, QP 8 or `-tune lossless`.
- **Alpha error:** the decoded luma against the source alpha. Sizes and errors are in
  [§6](#mezzanine-with-alpha).
- **Decode speed:** `-hwaccel cuda -hwaccel_output_format cuda`, 30 loops, `-benchmark`.
- **Real-time load:** colour and alpha with `-re`, decoder utilisation sampled by `nvidia-smi`.
- **GPU memory of the FFmpeg CLI process:** colour 638 MiB, lossless alpha 372 MiB, both 952 MiB,
  stacked 1182 MiB; a tiny decode for comparison 63 MiB. The CLI keeps a large frame pool, so
  these numbers serve only as ratios. The engine's clip decoder measured 170 MB in P2.
