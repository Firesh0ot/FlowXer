# FlowXer engine P4: DME boxes and frame grab

Status: the design, not implemented. P4 builds on P3 (transitions and keying, #89) and on P2
(the C++/CUDA engine is FlowXer's media path, #86). The numbers come from a prototype kernel on
the lab host (A16 GPU 3) on 2026-10-12 ([Appendix](#appendix-lab-measurements-2026-10-12)).

## Decisions this design follows

From the roadmap and the user's P3 decisions (2026-10-11):

1. **P4 scope:** DME / picture-in-picture per keyer (position, scale, crop, border, drop shadow,
   fly-in and fly-out with keyframes, several boxes per ME), and a frame grab of any input or ME
   output into the library. A grabbed still loaded into a clip player is the frame memory; there
   is no frame-store type.
2. **Keys show only on PGM** (P3 decision 6). PVW shows only its source.
3. **Layer order:** background (cut, mix, wipe) → upstream keyers 1 … K → stinger → DSKs 1 … D →
   FTB (P3 §2 and answer 3).
4. **Upstream keyers per ME:** 2 by default, up to 8 (P3 decision 2). DSKs: 0–4 per ME, 1 by
   default (P3 answers 1 and 10).
5. **Keyer ties** were "later, not in P3" (P3 answer 6). P4 needs them for "the box flies in with
   Auto" ([§5](#5-keyframes-and-animation), [open question 3](#12-open-questions-for-the-user)).

## 1. Scope

In:
- a DME transform on every **upstream** keyer: position with sub-pixel accuracy, size, crop,
  border (width, colour, soft edge), drop shadow (offset, blur, opacity);
- a new keyer mode `pip`: any source the ME may take, opaque, in a box;
- keyframes per keyer, with linear, ease and hold, run forward or backward, loop; fly on and off
  with the keyer's Auto, and with the ME's Auto through keyer ties;
- presets (corner PiP, split, quad, full);
- a frame grab of any live input, clip player, or ME PGM or PVW, as a still in the library;
- the GUI: a DME section in the keyer panel, a box editor drawn over the ME's mosaic tile, a
  keyframe list, GRAB buttons;
- the GPU budget and the contract changes.

Out (later): DME on DSKs; rotation, perspective and 3D; a drop shadow that follows a key's shape
(the shadow is the box's rectangle in P4); a dedicated DVE channel pool; grabbing a key input with
its alpha; motion-blurred flies; boxes on PVW (decision 2).

## 2. The model: a box is a keyer's transform

**Choice:** a DME box is an attribute of an upstream keyer, not a layer type of its own. Each
upstream keyer gets a `dme` block. When it is enabled, the keyer's fill and key are transformed
before they are keyed. "Several boxes per ME" are the ME's upstream keyers (2, up to 8). ATEM's
DVE key works the same way.

| Keyer mode | Fill | Key | With `dme` |
|---|---|---|---|
| `linear` (P3) | the keyer's v210a input | its key plane | a graphic with alpha, moved and scaled; fill and key go through the same resample |
| `luma` (P3) | any ME source | luma of the fill | the keyed source in a box |
| `pip` (new) | any ME source: a live input, a clip player (a grabbed still too), `me n` with n > m | the box itself (opaque) | always on: a `pip` keyer is a box |

Why a keyer attribute:
- The keyer already has everything a box needs: on, off and auto with a rate, the order (key 1
  lowest), the fused compose layer, PGM only, tally, the NMOS receiver (linear) and the source
  selector (luma). A separate type would repeat all of it and need a second ordering rule (boxes
  against keys).
- The compose kernel keeps its 14 layers (8 USK + stinger + 4 DSK + 1 spare); a box is a layer of
  kind `box`.
- The layer order stays: boxes are upstream keys, so the stinger covers them and the DSKs are
  above them. Like every upstream key, a box is drawn once over the transitioned background.

DSKs get no DME in P4 ([open question 2](#12-open-questions-for-the-user)).

Pipeline per ME (P3 §2, the new parts marked):

```
Program A ──┐
            ├─ background: cut | mix (t) | wipe | stinger (A until the cut point, then B)
target B ───┘
   → upstream keyers 1 … K   (linear | luma | pip; each with its box: resample pass → box layer)   ← new
   → stinger overlay
   → DSKs 1 … D
   → FTB
   → PGM
Preview source ─────────────→ PVW (no keys, no boxes)
```

- **Re-entry:** `me n` carries ME *n*'s boxes, as it carries its keys. A box on ME *m* may show
  `me n` (n > m): a PiP of another ME.
- **A `pip` of a clip player** counts as on air while the keyer is on air, so play on cut starts
  it (as P3's luma key).
- **Audio:** boxes carry no audio (P5).

## 3. Transform maths on the GPU

Each box costs two steps per grain: a **resample pass** that crops and scales its fill (and key)
into a box buffer, and a **box layer** in the fused compose kernel that places the buffer with
its border and shadow.

### Geometry

All values are floats in the production raster (W × H luma pixels).

| Field | Meaning | Range |
|---|---|---|
| `x`, `y` | the box centre, as a fraction of the raster (0.5, 0.5 is the centre) | −1 … 2 (outside 0–1 is off screen, for flies) |
| `size` | the scale of the source picture (1 = full raster) | 0 … 2 |
| `crop` | `left`, `right`, `top`, `bottom`: fractions of the source cut away | 0 … 1 each; left + right < 1 |
| `border` | `width` (px), `soft` (px), `color` (`#RRGGBB`), `opacity` | 0–64 px, 0–64 px, 0–1 |
| `shadow` | `dx`, `dy` (px), `blur` (px), `opacity` | ±128 px, 0–128 px, 0–1 |
| `soft` | the soft edge of the picture when there is no border (px) | 0–64 px |

- The crop cuts the picture; the box shrinks with it and does not rescale (as ATEM).
- The picture's rectangle in the raster: L = x·W − ½·W·size + crop.left·W·size, and so on.
- **Mapping:** an output pixel centre X + ½ maps to the source position u = c<sub>l</sub> +
  (X + ½ − L) / size. Luma takes the continuous index u − ½. **Chroma (4:2:2):** BT.709 chroma is
  co-sited with the even luma samples, so chroma sample *k* sits at luma position 2*k* and takes
  the chroma index (u − ½) / 2. Rows are full resolution for both. Chroma is never converted to
  4:4:4: each plane is resampled on its own grid.
- **Sub-pixel position:** L and T are floats. The filter phase carries the fraction, and the
  edges are anti-aliased (below), so a slow move glides instead of stepping by whole pixels.

### Resample

- **Separable, two passes:** a horizontal pass over the crop rows the vertical pass needs (box
  width × rows) into an intermediate, then a vertical pass into the box buffer. Each plane (Y,
  Cb, Cr, and for `linear` the key) on its own grid.
- **Filter: Catmull-Rom cubic**, with the support widened by 1 / size when the box is smaller than
  the source (an anti-aliased downscale, like Pillow's `BICUBIC`). Upscaled it is the plain cubic.
  - **Bilinear** is soft when enlarging and aliases when shrinking (text and fine detail shimmer
    at 1/4 size). The widened triangle (area-like) does not alias, but it is soft.
  - **Lanczos-3** is a little sharper, but it costs 2.5–3.3 × the cubic (measured) and needs a
    sine, so the GPU and the CPU would not agree bit for bit.
  - The cubic is a polynomial: with `-fmad=false` (P3's rule) the GPU and the CPU reference
    agree exactly ([Appendix](#appendix-lab-measurements-2026-10-12)).
  - The cubic overshoots a little at hard edges; the result is clamped to 10 bits as everywhere.
- **Edges of the crop:** the filter repeats the edge samples of the crop, so pixels outside the
  crop never bleed in.
- **Weights:** per box and grain, one small kernel computes the normalised weights of every output
  column (luma, chroma) and row from the geometry (start index, up to 34 taps). The passes then
  only load and multiply. The intermediate is 16-bit fixed point (the value × 16).
- **`linear` boxes** resample a premultiplied fill: (fill − black) · α, with black = 64 for Y and
  512 for Cb and Cr, and the key with the same weights. The box layer keys with "premultiplied
  over". Without this, the soft edges of a scaled graphic get dark or bright fringes.
- **`luma` boxes** compute the luma key from the resampled fill in the compose kernel (no extra
  plane).
- **When a box is static** and its fill is a still, the box buffer is kept and the resample is
  skipped. Live sources resample every grain.

### The box layer (in the fused compose kernel)

`gpu::KeyLayer` gains the kind `box`: the box buffer (Y, Cb, Cr, optional key), its rectangle,
and the border, soft and shadow settings. For each pixel inside the box's outer rectangle (box +
border + shadow + soft; groups outside skip the layer at once), in this order:

1. **Shadow:** mix towards black (Y 64, C 512) with α<sub>s</sub> = opacity · r(x) · r(y). Here r
   is a smoothstep ramp over `blur` px at each edge of the outer rectangle moved by (dx, dy). This
   is an analytic soft rectangle: no blur pass, no extra memory.
2. **Border:** mix towards the border colour with α<sub>o</sub> = the coverage of the outer
   rectangle (picture + border) · `border.opacity`, with the soft ramp on its outer edge.
3. **Picture:** mix towards the box buffer with α<sub>p</sub> = the coverage of the picture
   rectangle × the key (linear) or the luma key (luma) × 1 (pip).

- **Coverage:** a hard edge uses the exact area of the pixel inside the rectangle (a one-pixel
  anti-aliased edge); a soft edge a linear ramp over `soft` px.
- **The keyer's gain** (mix on and off, P3) multiplies all three αs.
- **Chroma:** the mean α of the pixel pair, as in P3.
- **Rounding:** each step rounds to 10 bits, as P3's layers do, so the CPU reference is a sequence
  of `mix10`.
- **Arguments:** the box layers add about 120 bytes per layer to the kernel arguments (2.3 KB in
  all, under the 4 KB limit).

### Exactness

- `core/dme.hpp` holds the geometry, the weights, the edges and the shadow as one code path for
  the CPU and the GPU (as `core/wipe.hpp`).
- The prototype's GPU output equals its CPU reference bit for bit over a whole 1080p frame: 2
  boxes with crop, sub-pixel position, border, soft edge and shadow, 0 samples differ.
- The self-test (`--selftest`) gains these checks.

## 4. Keyers with DME: state and API

Per upstream keyer, in addition to P3's settings:
- `mode`: `linear`, `luma` or `pip`; `source` for `luma` and `pip`;
- `dme`: `enabled` (always on for `pip`) and the geometry of [§3](#geometry);
- `keyframes`: up to 8 ([§5](#5-keyframes-and-animation));
- `fly`: whether the keyer's Auto flies it (keyframes) or fades it (gain, as P3).

| Operation | Engine API (localhost) | Control plane (public) |
|---|---|---|
| Mode, source, box | `POST /me/{m}/key/{k}` with `mode`, `source`, `dme` | `PATCH /keyers/{id}` with `mode`, `source_id`, `dme` |
| Keyframes | `PUT /me/{m}/key/{k}/keyframes` | `PUT /keyers/{id}/keyframes` |
| Run, stop | `POST /me/{m}/key/{k}/run {to?, reverse?, loop?}`, `/stop` | `POST /keyers/{id}/run`, `/keyers/{id}/stop` |
| Go to a keyframe | `POST /me/{m}/key/{k}/run {to, frames: 0}` | `POST /keyers/{id}/run {to, frames: 0}` |
| Preset | (the control plane writes the box and keyframes) | `POST /keyers/{id}/preset {name}`, `GET /dme/presets` |
| Tie to the next transition | `POST /me/{m}/transition {ties: [k…]}` | `PATCH /panels/{id}/transition {ties}` |

`GET /console`, `GET /keyers` and the engine's `/status` show each box's current state, the
running keyframe and its step.

## 5. Keyframes and animation

### Keyframes

- A keyframe is a full box state: `x`, `y`, `size`, `crop`, border width and opacity, shadow
  dx, dy, blur and opacity. The border colour and the source are not animated.
- Each keyframe *j* > 0 has `frames` (1–1000, the time from keyframe *j* − 1 to *j*) and `ease`:
  - `linear`;
  - `ease` (smoothstep in and out), `ease-in`, `ease-out`;
  - `hold` (stays at *j* − 1, then jumps to *j* at the end).
- **Interpolation:** linear in position, crop, border and shadow; **size in log space**, so that a
  zoom from 0.1 to 1 looks even (a linear size would rush at the small end).
- **Timing:** per grain on the CPU (`core/mixer`), from the grain index like a transition: a run
  started at grain *s* shows step *f* in grain *s + f*; skipped grains do not stretch it. The GPU
  only sees the state of its grain.

### Runs

- `run {to: j}`: from where the box is to keyframe *j*, through the keyframes in between, each at
  its own frames and ease.
- `run {reverse: true}`: back to keyframe 0.
- `loop`: `off`, `wrap` (0 → n, jump to 0) or `pingpong` (0 → n → 0 …).
- `stop` holds the current state. A new run starts from where the box is.

### Fly on and off

- **The keyer's own Auto** with `fly` on: keyframe 0 is the "off" state (for example off screen
  or size 0) and the last keyframe the "on" state. Auto on turns the keyer on and runs 0 → n; Auto
  off runs n → 0 and turns the keyer off at the end. Cut on and off jump. An Auto during a fly
  turns back from where it is (as P3's fade).
- **The ME's Auto (keyer ties):** the ME's next transition gets `ties` (key *k* … ). On Auto the
  background transition and the tied keyers run together over the transition's frames: a tied
  fade keyer fades with t, a tied fly keyer flies with t (its keyframes squeezed into the
  transition's rate). With a stinger, tied keyers cut at the cut point. A tie with no background
  transition ("key only") is the keyer's own Auto. This is P3's deferred keyer tie, needed here
  ([open question 3](#12-open-questions-for-the-user)).
- **Tally:** a keyer is on air while it is on or flying ([§8](#tally-raw-tsl-50-flowxer_tally_tsl)).

### Presets

Written by the control plane as box state plus keyframes (0 = off screen on the near side, 1 = on):

| Preset | Boxes |
|---|---|
| `corner-tl`, `corner-tr`, `corner-bl`, `corner-br` | one box at 1/3 size in a corner, inside the 5 % action-safe margin, 4 px white border, soft shadow |
| `split` | keyers 1 and 2: each source cropped to its centre half, full size, side by side |
| `quad` | keyers 1–4 at 1/2 size, 2 × 2 (needs 4 upstream keyers) |
| `full` | size 1, centred (a fly to and from full screen) |

## 6. Frame grab

- **Sources:** live input *k*, clip player *k*, ME *n* PGM, ME *n* PVW.
- **What is grabbed:** the picture as it is used in the next output grain *i*: a live input's
  grain *i − L* (what the ME shows), a clip player's picture, ME *n*'s PGM or PVW of grain *i*.
  The production raster, 10-bit 4:2:2, no alpha.
- **Engine:** `POST /grab {source, path}` returns the grain index and its TAI time.
  - The render thread only copies at grain *i*: PGM and PVW from the download that MXL writes
    anyway (a PVW without download gets one pack and one 5.5 MB copy, 0.85 ms once); a live input
    from its host grain (read from MXL before the upload); a clip player with one copy of its
    device picture.
  - The control thread writes the raw v210 file. At most 4 grabs per grain; more wait for the
    next grain.
- **Control plane:** `POST /library/grab {source, name?, player?}`:
  - a new library item of kind `still`, `source: "grab"`, tag `grab`;
  - the mezzanine for the production format is the grabbed v210 file itself (no conversion, exact);
  - the original is the same picture as a lossless file (FFV1 10-bit 4:2:2 in MKV, like the colour
    bars), from which other formats convert as any still; the thumbnail as for any still;
  - **name:** `name`, or by default `Grab <source label> <YYYY-MM-DD HH:MM:SS>` in local time, for
    example `Grab ME 1 PGM 2026-10-12 09:12:03`;
  - **`player`:** load the still into that clip player once it is ready: the frame memory in one
    step. The still is ready within a few grains (a 5.5 MB write); the lossless original follows in
    the background.
- **GUI:** GRAB in the context menu of every mosaic tile (inputs, clip players, ME PGM and PVW),
  and a GRAB button in the ME's transition section (grabs that ME's PGM). A toast names the new
  still; it appears in the library's stills.
- **Metrics:** `flowxer_engine_grabs_total{source}`, `flowxer_engine_grab_ms`.

## 7. GUI and widgets

- **DME section in the upstream keyer panel** (on the active ME tab):
  - mode (LINEAR | LUMA | PIP) and source; DME on or off (on for PIP);
  - numeric fields for position, size, crop, border and shadow;
  - the keyframe list: STORE (the current state as keyframe *j*), RECALL, `frames` and `ease` per
    keyframe, delete; RUN ◀ ▶, LOOP, STOP; FLY (the Auto flies);
  - the preset buttons.
- **Box editor on the mosaic:** the ME's PGM tile shows the outline of each box of the active
  keyer panel, drawn by the browser over the tile (CSS, no engine cost): drag to move, corner
  handles to scale (aspect kept), Alt + edge handles to crop. A box that is off air shows a dashed
  outline only, because boxes never show on PVW. Changes to a box that is on air go to PGM at once;
  the editor marks the box ON AIR.
- **Transition section:** the tie buttons (BKGD, KEY 1 … K) next to the type selector.
- **Widgets:** a new `dme` widget (parameters `me`, `keyer`): ON / AUTO, RUN ◀ ▶ and the preset
  buttons. Editing stays in the console ([open question 8](#12-open-questions-for-the-user)).

## 8. Contract changes for the platform

Relative to the P3 contract.

### Environment of the mixer container

No new variables. The boxes are the upstream keyers: `FLOWXER_KEYERS_PER_ME` (0–8, default 2)
sets how many boxes an ME can have. The engine's internal configuration gains nothing either.

### NMOS

None. A `linear` box keys its existing `ME n Key k` receiver; `luma` and `pip` boxes use ME
sources. No new senders or receivers.

### Tally (raw TSL 5.0, `FLOWXER_TALLY_TSL`)

- **`pip` and `luma` boxes:** while the keyer is on air (on, fading or flying) on ME *n*, its
  source's INDEX (or 1000 + *n'* for `me n'`) is LH on SCREEN *n*, as P3's luma keys. Never RH.
- **`linear` boxes:** P3's key INDEX (2000 + 100·n + k), unchanged.
- A box that is on air but fully off screen or at size 0 still lights its source. Tally follows the
  keyer's state, not the pixels.
- A grab lights nothing.

### Widgets

- New widget `dme` ([§7](#7-gui-and-widgets)), minimum size to be checked (about 320 × 120).

### GPU, memory

- **`gpu_memory_mb`** ≈ P3's formula + 25 × renderMes × keyersPerMe at 1080p:
  - per keyer with DME: a box buffer (Y, Cb, Cr, key at the full raster: 12.4 MB) and its share of
    the intermediate (12.4 MB), allocated on first use;
  - × 4 at 2160p; the weight tables are under 1 MB per box.
  - With the defaults (4 MEs, 2 keyers) that is +200 MB; at the maximum (4 × 8) +800 MB.
- **PCIe:** none added. A grab of an unloaded PVW copies 5.5 MB once.

## 9. GPU budget

**Target:** at most 15 ms per grain on the A4000, render plus download (P3 §10). P3's worst case
is ≈ 12.4 ms on the lab (max ≈ 14) and ≈ 6–9.4 ms on the A4000.

### Measured cost per box (A16 GPU 3, 1080p, Catmull-Rom, with border and shadow)

Two prototype versions ([Appendix](#appendix-lab-measurements-2026-10-12)): v1 computes every
weight per sample; v2 uses weight tables, a 16-bit intermediate, only the needed rows, and keeps
each column's weights over 16 rows.

| Layout | v1: resample + box layers | v2: resample + box layers | Box layers alone (in v2) |
|---|---|---|---|
| corner PiP, 1/3 size | 0.75 ms | **0.53 ms** | +0.03 ms |
| half-size PiP | 0.97 ms | **0.60 ms** | +0.06 ms |
| full size (a fly at size 1) | 2.03 ms | **1.41 ms** | +0.24 ms |
| split, 2 boxes at 1/2 | 1.96 ms | **1.24 ms** | +0.18 ms |
| quad, 4 boxes at 1/2 | 3.93 ms | **2.43 ms** | +0.35 ms |
| 8 boxes at 1/4 | 5.00 ms | **4.27 ms** | +0.29 ms |

- **The box layer is cheap:** about what a P3 key layer costs, scaled by the box area.
- **The resample is the cost, and it is still above its memory floor.**
  - The floor: a half-size box reads its crop (8.3 MB) and writes 4 + 2 MB, about 0.1 ms at the
    lab's 164 GB/s.
  - v2 is 1.4–1.7 × faster than v1, but a box still costs ≥ 0.5 ms however small it is. The
    horizontal pass reads every source row with up to 16 taps and keeps its weights in local
    memory.
  - **M1 work:** the tap count as a template parameter (weights in registers); shared-memory
    tiles; a 2:1 pre-reduction for sizes ≤ 1/2; all boxes of an ME in one launch per pass; the
    resample skipped for a static box on a still.
  - **M1 gate:** a half-size box with border and shadow ≤ 0.3 ms on the A16 (≤ 0.15 ms on the
    A4000).
- **Lanczos-3** costs 1.1–1.7 × the cubic in v2 (2.5–3.3 × in v1). Catmull-Rom is the default.
- **Bilinear** saves little in v2 (0.43 against 0.52 ms at 1/2 size): it is no reason to give up
  quality.

### Limits

The A4000 has 2.24 × the A16's memory bandwidth (P3 §10), so its estimate divides by 2.24.

| Case (1080p50) | A16 lab, v2 measured | A4000, v2 | A4000 with the M1 gate |
|---|---|---|---|
| P3 worst case | ≈ 12.4 ms | ≈ 6–9.4 ms | ≈ 6–9.4 ms |
| + 1 half PiP on each of 4 MEs | + 2.4 → ≈ 14.8 ms | + 1.1 → ≈ 7.1–10.5 ms | + 0.6 → ≈ 6.6–10 ms |
| + a quad on one ME, 1 PiP on the others | + 4.2 → ≈ 16.6 ms: no | + 1.9 → ≈ 7.9–11.3 ms | + 1.0 → ≈ 7–10.4 ms |
| + 8 boxes at 1/4 on each of 4 MEs (32) | + 17 ms: no | + 7.6 → ≈ 13.6–17 ms: no on Gen3 x8 | + 3.7 → ≈ 9.7–13.1 ms |

- **Per ME on the A4000:** with v2, 2 boxes on every ME fit (the defaults). Up to 8 per ME need
  the M1 gate.
- **On the lab A16:** about 4 half-size boxes in all next to P3's worst case.
- The A4000 is time-sliced with the node's other GPU functions. The engine reports the box ms per
  ME (`flowxer_engine_dme_ms{me}`), and M4 checks the budget.
- **2160p50:** everything × 4; boxes only on 1 ME, as P3's keys.

## 10. Milestones

Each milestone is tested on the lab before the next one starts. Small and the release follow only
with the user's OK. One PR into `dev`, growing per milestone, as #86 and #89 did.

| Milestone | Builds | Lab test |
|---|---|---|
| **M1: the box** | `core/dme.hpp` (geometry, weights, edges, shadow; one code path for the CPU and the GPU); the weight and resample kernels; the `box` layer in the fused compose; the `pip` mode and `dme` on linear and luma keyers; engine API, control plane API, state and migration; the GUI DME fields. | Bit-exact against the CPU reference for each mode, crop, sub-pixel position, border, soft, shadow, chroma siting; the M1 gate on the cost; PVW clean; tally of pip sources; re-entry with a box on the higher ME. |
| **M2: keyframes and presets** | Keyframes, ease, runs, loop, stop; fly on the keyer's Auto; presets; the box editor on the mosaic and the keyframe list; the `dme` widget. | Each grain's state equals the reference interpolation (grain-exact start and end, skipped grains); a 25-frame fly is smooth (no whole-pixel steps); presets. |
| **M3: ties and frame grab** | Keyer ties on the ME's Auto (fade and fly keys, stinger cut); the grab in the engine and the library, `player`, the GUI buttons. | Tied fly in grain step with the background transition; a grab of each source type equals the grain on MXL bit for bit; grab to clip player as a frame memory on air; no skipped grain during grabs. |
| **M4: budget, soak, release readiness** | Metrics (`dme_ms`, grabs); docs (README, engine/README, CHANGELOG); the platform note. | The worst case with boxes in the engine (render, download, memory); a 2 h soak with flies, ties and grabs every few seconds. Then small, with the user's OK. |

## 11. Test plan

### Unit tests (CPU `fxeng-tests`, pytest, GUI)

- **DME maths:** the mapping (centre, edges, chroma siting) at known points; size 1 at an integer
  position is the identity; crop edges never read outside; coverage sums to 1 across an edge;
  shadow and border at known pixels; the premultiplied linear key.
- **GPU self-test:** the resample and the box layer equal the CPU reference bit for bit (several
  sizes 0.1–2, sub-pixel positions, crops, every decoration; each mode).
- **Keyframes:** each ease at known steps; log-size interpolation; run to *j*, reverse, wrap and
  pingpong; stop and resume; skipped grains; the fly on Auto and a turn-back.
- **Ties:** fade and fly with t; a stinger with ties; a Cut during a tied Auto.
- **Control plane** (simulate mode, fake engine): schemas; presets; tally INDEX of pip sources;
  grab naming, the library item, `player`.
- **GUI:** `tsc`; the box editor's geometry against vectors from the C++ reference.

### Lab (`fxp4-functional.py`, through FlowXer's API and IS-05 only)

- **Boxes:** a test pattern with a pixel grid as the source; PGM grain *i* read from MXL equals
  the reference of input grain *i − L* bit for bit, per mode and decoration; PVW is its source.
- **Flies:** the state per grain from MXL (the box's edge found in the picture) against the
  keyframe reference.
- **Grab:** each source type; the still's v210 equals the grain read from MXL.
- **Performance and soak:** the matrix of [§9](#9-gpu-budget) and a 2 h soak.

## 12. Open questions (for the user)

1. **Boxes per ME:** the boxes are the upstream keyers (2 by default, up to 8). Is that enough, or
   should a separate box count exist? *Suggestion: keyers = boxes, no new setting.*
2. **DME on DSKs:** only upstream keyers get DME (as ATEM). *Suggestion: upstream only in P4.*
3. **Keyer ties:** "the box flies in with Auto" needs P3's deferred keyer ties. *Suggestion: ties
   in P4 (M3), for fade and fly keys.*
4. **What a box can show:** any ME source (`pip`, and `luma`), plus the keyer's own v210a input
   (`linear`). *Suggestion: all three.*
5. **Keyframes for v1:** up to 8 keyframes, linear / ease / ease-in / ease-out / hold, run,
   reverse, loop (wrap, pingpong). *Suggestion: as listed; no curves editor, no per-field easing.*
6. **Border and shadow:** one outer border (width, soft, colour, opacity) and a rectangular shadow
   (offset, blur, opacity, black). Needed: an inner border, a bevel, a shadow colour, or a shadow
   that follows a keyed graphic's shape? *Suggestion: as listed; shape shadows later.*
7. **Scaling filter:** Catmull-Rom cubic only, or also Lanczos-3 as an option (2.5–3.3 × the
   cost)? *Suggestion: cubic only.*
8. **`dme` widget:** add it for operator screens (ON, AUTO, RUN, presets)? *Suggestion: yes.*
9. **Frame grab:** naming `Grab <source> <date time>`, production raster, no alpha, an optional
   load into a clip player. Is a grab of a key input with its alpha needed? *Suggestion: as
   listed; alpha later.*
10. **Presets:** corner PiPs, split, quad and full. Others (a 3-way split, a lower-third box)?
    *Suggestion: these four; more as data later.*
11. **Live editing:** changes to a box that is on air go to PGM at once (as on ATEM). Or lock the
    editor while on air? *Suggestion: live, with an ON AIR mark.*

## Appendix: lab measurements (2026-10-12)

**Setup:** lab host iptv-web-lab-1, NVIDIA A16 GPU 3 (GA107, 10 SMs), CUDA 12.8 for sm_86 with
`-fmad=false -Xcompiler -ffp-contract=off`, in the `fxeng:deps` image. The prototype
`fxp4-bench.cu` is on the lab host in `~/mxl-lab/fxp4/`, not in this PR. It becomes engine code
in M1.

`fxp4-bench`: CUDA events, 300 runs each, 1080p yuv16, random pictures. Two versions:
- **v1** computes each weight per output sample, with a float intermediate, over every source row;
- **v2** computes the weights once per box (one small kernel), with a 16-bit intermediate (the
  value × 16), over the needed rows only, each thread keeping its column's weights over 16 rows.
  v2 and v1 differ by at most 1 code (the 16-bit intermediate; 0.9 % of the samples); each matches
  its own CPU reference.

### Resample of one box, v2 (horizontal + vertical pass, Y, Cb, Cr, ms)

| Size | bilinear | triangle (widened) | Catmull-Rom (widened) | Lanczos-3 (widened) |
|---|---|---|---|---|
| 1.0 | 1.062 | 1.059 | 1.164 | 1.328 |
| 0.75 | 0.745 | 0.741 | 0.896 | 0.951 |
| 0.5 | 0.430 | 0.424 | 0.520 | 0.775 |
| 0.333 | 0.313 | 0.314 | 0.494 | 0.793 |
| 0.25 | 0.219 | 0.220 | 0.504 | 0.868 |
| 1.5 | 0.993 | 0.981 | 1.031 | 1.148 |

v2 with border, soft and shadow, resample + compose (the background alone 0.139 ms): corner PiP
1/3 0.668, half PiP 0.734, split 2 × 1/2 1.374, quad 4 × 1/2 2.569, 8 × 1/4 4.405, full 1.0
1.549 ms.

### Resample of one box, v1 (ms)

Y, Cb, Cr (an opaque source):

| Size | bilinear | triangle (widened) | Catmull-Rom (widened) | Lanczos-3 (widened) |
|---|---|---|---|---|
| 1.0 | 1.617 | 1.616 | 1.758 | 3.487 |
| 0.75 | 1.175 | 1.187 | 1.397 | 2.997 |
| 0.5 | 0.679 | 0.710 | 0.902 | 2.432 |
| 0.333 | 0.448 | 0.511 | 0.730 | 2.174 |
| 0.25 | 0.287 | 0.354 | 0.593 | 1.936 |
| 1.5 | 1.613 | 1.613 | 1.764 | 3.506 |

With the key plane (a `linear` box), Catmull-Rom: 2.49 / 1.99 / 1.33 / 1.07 / 0.88 / 2.49 ms
for the same sizes.

### Compose with boxes, v1 (ms)

The background alone through the compose kernel: 0.143.

| Layout | Plain: resample + compose | Border + soft + shadow: compose | Total |
|---|---|---|---|
| corner PiP 1/3 | 0.724 + 0.146 | 0.176 | 0.897 |
| half PiP | 0.892 + 0.152 | 0.205 | 1.116 |
| split 2 × 1/2 | 1.795 + 0.192 | 0.317 | 2.100 |
| quad 4 × 1/2 | 3.579 + 0.260 | 0.497 | 4.070 |
| 8 × 1/4 | 4.712 + 0.245 | 0.433 | 5.145 |
| full 1.0 (a move) | 1.767 + 0.211 | 0.380 | 2.172 |

Lanczos-3 totals: 2.33 / 2.64 / 5.10 / 10.06 / 15.79 / 3.90 ms for the same layouts.

### Exactness

Catmull-Rom resample and the decorated compose of 2 boxes (sizes 0.41 and 0.77, a crop of the
second, sub-pixel positions, 6 px border, 1.5 px soft, a 12 px shadow with 16 px blur) against the
CPU reference over a whole 1080p frame: **0 samples differ**.
