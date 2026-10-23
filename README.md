# FlowXer

DMF **Vision Mixer** microservice for the [EBU Dynamic Media Facility](https://tech.ebu.ch/dmf/ra) Media eXchange Layer ([dmf-mxl/mxl](https://github.com/dmf-mxl/mxl)).

The mixer is controlled over HTTP. OpenAPI lives at `/docs` on the **GUI origin** (port **9620**). Media stays **uncompressed** on the MXL domain:

| Essence | MXL media type |
|---------|----------------|
| Video | `video/v210` (10-bit 4:2:2) |
| Key (fill + key) | `video/v210a` |
| Audio | `audio/float32`, 48 kHz |

Two images: **`flowxer-vision-mixer`** holds the Python control plane (FastAPI: API, state, NMOS, tally, media library) and the C++/CUDA **media engine** (`engine/`), which the control plane starts as a child process, restarts with a backoff and drives over a control API on 127.0.0.1. **`flowxer-gui`** (React, nginx) is the operator console and serves the operator-screen widgets. An NVIDIA GPU is required.

```mermaid
flowchart LR
    subgraph In["MXL inputs"]
      Live["live inputs\nv210 + float32"]
      Key["key input\nv210a"]
    end
    subgraph Mixer["flowxer-vision-mixer"]
      CP["control plane\nAPI · NMOS · tally · library"]
      subgraph Engine["media engine (GPU)"]
        Players["clip players\nNVDEC, stills"]
        MEs["ME 4 … ME 1\nPGM · PVW · DSK"]
        Mosaic["preview mosaic\nNVENC"]
      end
    end
    subgraph Out["MXL outputs per ME"]
      PGM["PGM video + audio"]
      PVW["PVW video"]
    end
    GUI["flowxer-gui\nconsole · widgets"]
    MTX["MediaMTX\nWHEP"]

    Live --> MEs
    Key --> MEs
    Players --> MEs
    CP -- control API --> Engine
    MEs --> PGM
    MEs --> PVW
    MEs --> Mosaic --> MTX --> GUI
    GUI -- /api --> CP
```

## What it does

- **MEs:** up to 4 (`FLOWXER_PANELS`), every rendering ME renders all the time on the TAI grid: PGM video + PGM audio and PVW video per ME, written to MXL. An ME can take the Program of a higher ME (re-entry, the same output frame).
- **Sources** in slot order: live inputs (an MXL video + audio pair, routed over IS-05), clip players, Black.
- **Clip players** play clips from the media library (decoded on the GPU) and stills (frame memory): loop, play on cut, cue, play, pause, stop. The colour bars ship as a library still.
- **Transitions:** Cut, and per ME the next transition for Auto: Mix or Wipe (23 SMPTE wipe codes with soft edge, border width and colour, reverse and flip-flop), at the ME's rate in frames, or a Stinger over its own length; a T-bar; Fade to Black of the ME's whole output (the DSKs and the sound too; the buses and the tally stay). **Audio follows video** with an equal-power crossfade along the transition.
- **Audio** (P5, `docs/engine-p5.md`): per ME a PGM mix of strips, one per live input, clip player, re-entered ME and the stinger, each with a fader (−90 = off … +10 dB), MUTE and AFV (audio follows video: heard while its source is visible on the ME's Program, as background or through a visible luma key or pip, nested MEs included; off: always in the mix, e.g. a commentary or a music bed), and a master per ME. A live input has a delay (0 … `FLOWXER_AUDIO_MAX_DELAY_MS`, default 1000 ms; audio later than its video) and a channel map for every ME. Every change ramps over one frame; a Cut crossfades over its frame (equal power). Meters per source (peak, RMS) and per ME PGM (peak, true peak, RMS, EBU R 128 loudness M/S/I per channel pair, overs) every 100 ms. With the defaults the sound is P4's. CPU on the engine's audio thread: about 0.04 cores at 2 channels, 0.14 at 16 (4 MEs, 8 inputs; `docs/engine-p5.md` "M4 as built"). No EQ, dynamics, limiter, aux buses or audio preview.
- **Stingers** play on the GPU with alpha and sound, over the upstream keys and under the DSKs: the background switches at the stinger's cut point, its sound crossfades over the 4 frames around it, and the stinger's own sound goes into PGM ([Stinger convention](#stinger-convention)).
- **Keyers** on every rendering ME: upstream keyers (`FLOWXER_KEYERS_PER_ME`, 0–8, default 2) under the transition and DSKs (`FLOWXER_DSKS_PER_ME`, 0–4, default 1) over it, each linear (its own v210a fill + key input, e.g. mxl-browser-source in `v210a` key mode or a chroma-key function) or luma (a self key of a source the ME may take): on, off, auto. Keyers routed to the same flow share one reader. Keys have no preview: Preview shows only its source.
- **DME boxes** (P4, `docs/engine-p4.md`): an upstream keyer can put its key in a box (`dme`: position, size, crop, soft edge, outer border with width, soft, colour and opacity, a black rectangular drop shadow), and the keyer mode `pip` shows any source the ME may take (an input, a clip player, `me <n>` with n > m) opaque in a box. The boxes are the upstream keyers: an ME has as many as `FLOWXER_KEYERS_PER_ME` (GPU budget at 1080p50 with 4 MEs at full load: at most 4 per ME, a linear box counting as 1.5 and each key flow with linear boxes 1.5 more; `docs/engine-p4.md` "M4 as built" and "Linear boxes unpacked once"). Scaling is Catmull-Rom (anti-aliased when shrinking), sub-pixel positions glide; a linear box is scaled premultiplied (no fringes). DSKs have no DME. Each box has up to 8 **keyframes** (position, size in log space, crop, border, shadow; each with its time in frames and an ease: linear, ease, ease-in, ease-out, hold): runs to a keyframe and back, loops (wrap, pingpong), stop; with **FLY** the keyer's Auto flies the box on (keyframe 0 to the last) and off instead of fading it. **Presets:** corner PiPs, split, quad, full.
- **Keyer ties** (P4): an ME's next transition can take its upstream keyers along (BKGD, KEY 1 … K): Cut cuts them with the background, Auto and the T-bar fade a keyer with the mix or wipe or fly its box along its keyframes squeezed into the transition, and with a stinger they cut at its cut point. BKGD off is key only (Cut cuts the tied keyers, Auto runs each one's own Auto).
- **Frame grab** (P4): any live input, clip player, or ME's Program or Preview as it is in the next frame becomes a still in the library (`Grab <source> <date time>`, the production raster, no conversion and no alpha), optionally loaded straight into a clip player: the frame memory.
- **Previews:** the engine encodes every source and every ME's Preview once into one mosaic (NVENC), and each ME's Program as a sharper stream of its own (1280×720p25 at 4 Mbit/s by default, `FLOWXER_PREVIEW_PGM*`); the console crops its pictures out of the mosaic's WebRTC stream and plays each Program stream whole. **Cost** of the Program streams (A16): about +1.4 % of the GPU's encoder per ME at 1280×720p25 (measured with the same NVENC settings; 960×540: about +1 %, measured in the engine; the 1080p25 mosaic: 3–4 %), about 20 MiB GPU memory per ME (2 × 1.4 MB frames plus its NVENC session), the area-average downscale well under 0.1 ms per grain, about 1–2 % of a core per stream for packetising and RTSP, and MediaMTX relaying each stream per viewer; per viewer +4 Mbit/s per ME (a console with 3 MEs: about 18 instead of 6 Mbit/s). No audio preview.
- **NMOS** IS-04/IS-05 node (BCP-007-03), **tally** over TSL UMD 5.0 (to receivers, and the raw tally for the platform's tally calculator), Prometheus metrics.
- Runs in **Docker** (`vision-mixer` + `gui`) or on Kubernetes with the MXL domains mounted.

## Operator GUI

The GUI is a **separate React service** (Vite + TypeScript). Every control on the console is an HTTP call to the mixer API; there is no GUI-only mixer path.

```
┌─ File  Settings  Tally  Help ─────────────── [CPU% RAM% format] ─┐
│  [ME 1] [ME 2]                                                   │
│  PREVIEW                       PROGRAM                           │
│  [ME 1 Key 1 ON|AUTO|25 f|⚙] … [DSK 1 …]  Stinger ⚙ SNAP AUDIO  │
│                                                                  │
│  [Cam 1 ⚙] [Cam 2 ⚙] [Clip 1 ⚙] …   source tiles (16:9 or 9:16)  │
│   left of picture = PVW · right = PGM   MIX WIPE STING  25 fr    │
│   clip players: [item ▾] · 00:03.12 −00:06.37 · ⏮ ▶ ⏸ ■ LOOP AUTO │
│                                         [wipe pattern icons]     │
│                                         CUT     AUTO     FTB     │
│  AUDIO (open): [Cam 1][Cam 2]…[Clip 1][ME 2][Stinger] [ME 1 PGM] │
│   meter · fader · MUTE · AFV · ● on air      M S I · RESET       │
└──────────────────────────────────────────────────────────────────┘
```

- **Pictures:** every source tile and the Preview monitor are crops of the engine's preview mosaic; the Program monitor (and a higher ME's tile) plays that ME's own Program stream. The page opens **one WHEP session per stream** (the mosaic and each ME's Program; `GET /api/v1/preview` gives the streams, `tiles` in the mosaic and `streams` of their own) and reconnects each by itself when it drops. Each mosaic picture is a box of its tile's aspect ratio with `overflow: hidden`, holding a `<video>` on the mosaic's stream that a CSS `transform: scale(…) translate(…)` stretches and moves so only the tile shows, **5 px inside the tile's edges** (the browser's scaling would otherwise show a sliver of the neighbouring tile). That works in every browser (no `object-view-box`). Black has no tile; off-air the monitors say why there is no picture. An engine without Program streams (`FLOWXER_PREVIEW_PGM=false`, or no NVENC sessions left) has the Programs as mosaic tiles again; then the PROGRAM title says why (`from the mosaic: …`, `GET /preview` → `streams_error`), and the box editor allows for the tile's 5 px inset.
- **Layout:** PREVIEW and PROGRAM take about 40 % of the window's height (32 % each, one above the other, under 900 px wide); a window wider than the two 16:9 pictures centres them as a pair. The deck (source tiles, transition) gets the rest; what does not fit scrolls.
- **Higher MEs** (re-entry) are tiles after the sources: their Program picture, left half to Preview, right half to Program of the active ME (as the bus widget's ME buttons; red or green while the active ME has it on Program or Preview).
- **Clip players** pick the library clip or still they play in their tile's header (an empty player is black), show the position and remaining time over the picture, and have the transport under it: ⏮ cue the first frame, ▶ play, ⏸ pause, ■ stop (back to the first frame), **LOOP**, **AUTO** (play on cut: start when taken to Program). A still shows **FRAME MEM**. **END CUT** / **END AUTO** / **END STING** marks a clip player whose end takes the ME's Preview ([docs/clip-end.md](docs/clip-end.md); set in its ⚙: *At the end*).
- **Keyers** of the active ME (its upstream keyers, then its DSKs): ON/OFF cuts it, **AUTO** fades it over its rate (in frames; kept with the keyer), `· NO KEY` (amber, also in the keys widget) when a linear key's input has no signal; ⚙ sets the key: linear, luma of a source with clip, soft and invert, or (upstream) PiP of a source, and the gain; an upstream keyer's box: on or off (always on for a PiP), x, y and size in percent, crop in source pixels, border, shadow and soft edge; FLY (Auto flies the box), the keyframe list (STORE the box as it is, frames and ease per keyframe, GO, SET, move up and down, delete), RUN ◀ ▶ (once, wrap or pingpong), STOP and the presets. Changes to a box on air go to Program at once (ON AIR).
- **Box editor:** the active ME's Program picture shows the outline of each of its boxes (dashed while off air, ON AIR while on air): drag a box to move it, a corner handle to scale it (the opposite corner stays). The changes go to the mixer while dragging.
- **File** takes the mixer on-air or off-air and opens the **clip and still library** and the stinger library. While a file goes up its bar shows the bytes sent (within each 8 MiB chunk too), then each conversion under way has a bar with the item's name and Cancel; both stay at the top of the library while it scrolls, and a converting item's card has its own bar.
- **Settings → Console layout…** (`PUT /workspace`): video format (off-air), source tiles 16:9 or 9:16 (display only, also on air), the number of sources, MEs, stinger slots, stinger players and keyers. What the production sets through the environment is greyed out.
- The gear on a **source** (`PATCH /inputs/{id}`): name, kind (`mxl_live`, `clip_player`, `black`), flows of a live input, the clip or still of a clip player with loop and play on cut, and the auto-stinger. A live input's **Audio** section (`PATCH /audio/inputs/{id}`): its delay (±1 ms, ±1 frame) and channel map (per PGM channel the input channel or silent; 1:1, Mono), for every ME.
- **Transition section** (per ME): the type (MIX, WIPE, STING) and the rate in frames (`PATCH /panels/{id}/transition`); the next transition's elements **BKGD**, **KEY 1** … (lit when tied; a keyer on air has a red underline; the last one lit stays) and **GRAB** (ME Program to the library); for WIPE the pattern picker (icons, the SMPTE code as a tooltip), reverse, flip-flop (the next wipe goes back), soft edge and border (0–100 % of the pattern's travel) and the border colour; for STING the slot. **CUT**, **AUTO** (`/mixer/auto`: the ME's transition) and **FTB** (lit while the output is black) map to `/mixer/cut`, `/mixer/auto`, `/mixer/fade-to-black`; the **T-bar** is a real lever (ping-pong): drag the handle from the end it rests at to the other end to make the transition; it stays there, and the next transition goes back the other way; back to its start ends it without the swap. An Auto runs the handle to the other end at the ME's rate; a Cut of a held transition counts as done. The mixer keeps the lever's end, so every console, the T-bar widget and a hardware panel agree. While dragging, the handle follows only the pointer (from where it was grabbed; a click on the track does nothing), and after reaching the end it is latched until released (no flicker at the end of travel).
- **AUDIO** (in the keyer row) opens and closes the active ME's audio strip under the deck (remembered in the browser; P5, `docs/engine-p5.md`): one column per strip (live inputs, clip players, the Programs of higher MEs, the stinger) with its meter (up to 2 bars: with more channels the loudest odd and even channels; a 1 s peak hold and a red over mark), a fader (+10 … −∞; double click: 0 dB; the values go to the mixer while it moves), **MUTE**, **AFV** (audio follows video; off: always in the mix), a red dot while the strip is heard in PGM and a delayed input's delay; then the master: the PGM meter (the true peak as a line, OVER with a count), the loudness of channels 1–2 (M, S, I in LUFS), **RESET** (integrated from now on) and the master fader. Meters poll at 10 Hz only while it is open. No audio preview.
- **SNAP** (in the keyer row) opens the snapshots ([docs/snapshots.md](docs/snapshots.md)): the active ME's, then those of all MEs, each with its number and name (double click renames; START marks the start-up snapshot), **PGM** and **AUD** (its recall's defaults: Program/on-air and audio), **RECALL** and **⋯** (recall once with other options, rename, store the current look over it, recall when the mixer starts, delete). **STORE** stores the active ME, or all MEs, into the lowest free number.
- **Frame grab:** right click on a source tile or on the Preview or Program monitor: *Grab still*, or *Grab to <clip player>* (the frame memory); a toast names the new still.
- **Tally → Receivers…** adds TSL UMD 5.0 listeners (Companion, VSM, BFE, Riedel HI, custom).
- The **status chip** is a CPU / RAM / format pill; click it for the mixer state, NMOS, load and issues.

```bash
cd gui && npm install && npm run dev   # proxies /api, /docs, /openapi.json, /widgets to the mixer on loopback :9610
```

### Widgets

Operator screens (the platform's production designer) frame single controls of FlowXer. flowxer-gui serves them:

| Widget | Query | Minimum size | Shows |
|---|---|---|---|
| `bus` | `me` (required, 1..number of MEs), `transition` (default true) | 480×150 | that ME's Program and Preview rows (the inputs and the Programs of the higher MEs) with Cut and Auto and, unless `transition=false`, its transition type (MIX / WIPE / STING) and keyer ties (BG, K1 …); tally colours |
| `keys` | `me` (required) | 240×150 | that ME's upstream keyers and DSKs with On/Off and Auto |
| `dme` | `me`, `keyer` (required: upstream keyer 1..8) | 320×120 | that keyer's box: On/Off, Auto (a fly with FLY), Run back and forth, Stop and the presets; editing stays in the console |
| `audio` | `me` (required), `faders` (default true) | 480×240 | that ME's audio strips (meter, MUTE, AFV and, unless `faders=false`, the fader) and its master meter with the loudness (M, S, I) and RESET; meters at 10 Hz |
| `tbar` | `me` (optional, default 1), `buttons` (default true) | 120×240 | that ME's T-bar lever, the same as the console's (it stays at the end a transition took it to) and, unless `buttons=false`, Cut and Auto |
| `multiview` | `me` (optional, default 1: whose tally), `pvw` (default false) | 480×270 | a control-room wall over WebRTC: each ME's Program (its own stream), with `pvw=true` each ME's Preview, then the live inputs and clip players (mosaic tiles), each with its label; red border while it is on air on ME `me` through any path (the console's `on_air` when the mixer has it, else Program), green while it is on the Preview of any ME (every ME's Preview picture is green; red wins). The grid follows the mixer (sources and MEs added or removed) and fills the frame with the largest 16:9 pictures that fit |
| `snapshots` | `me` (optional), `all` (default true) | 240×120 | a recall button per snapshot of that ME and, unless `all=false`, of all MEs (without `me`: those only), with its PGM / AUD defaults; the last recall is lit |

- `GET /widgets` lists them with a JSON schema of their parameters (from the mixer's `GET /api/v1/widgets`); it answers an `Origin` that `WIDGET_FRAME_ANCESTORS` names (or `*`) with `Access-Control-Allow-Origin`.
- `GET /widget/bus?me=<n>[&transition=false][&theme=dark|light|transparent]` (and `/widget/keys?me=<n>`, `/widget/dme?me=<n>&keyer=<k>`, `/widget/audio?me=<n>[&faders=false]`, `/widget/snapshots[?me=<n>][&all=false]`, `/widget/multiview[?me=<n>][&pvw=true]`, `/widget/tbar[?me=<n>][&buttons=false]`) is the page without the console around it, on the GUI's own origin (its `/api`). A bad parameter answers 400, an unknown widget 404. Only these routes carry `Content-Security-Policy: frame-ancestors <WIDGET_FRAME_ANCESTORS>`; none carries `X-Frame-Options`. The page posts `{type: "widget-ready"}` and `{type: "widget-size", w, h}` to its parent.
- `WIDGET_FRAME_ANCESTORS` is set on the **gui** container: a CSP source list, default `'self'` (an invalid value stops nginx with exit code 78).

## API

Compose and the platform publish **one** HTTP port: the operator GUI on **9620**. nginx there proxies `/api`, `/docs`, `/redoc` and `/openapi.json` to the mixer, which listens on **127.0.0.1:9610** (on the Docker network in bridge Compose), not on a public interface.

| | |
|--|--|
| Operator GUI | http://localhost:9620 |
| Swagger UI | http://localhost:9620/docs |
| Mixer API | http://localhost:9620/api/v1/… |
| Mixer on the host (loopback) | http://127.0.0.1:9610 |

If `FLOWXER_API_TOKEN` is set, calls to **9610** need `Authorization: Bearer …` or `X-FlowXer-Token`. Calls through **9620** do not: nginx (and the Vite dev proxy) inject the token. `/api/v1/health` stays unauthenticated on both.

| Console action | API |
|----------------|-----|
| Poll layout, buses, resources, preview map, clip player state | `GET /console` |
| File → mixer on-air / off-air | `POST /mixer/start`, `POST /mixer/stop` |
| Settings → Console layout… | `PUT /workspace` |
| Source left click (PVW) / right click (PGM) | `POST /mixer/preview`, `POST /mixer/take` |
| Source ⚙, clip player item, loop, play on cut, at the end | `PATCH /inputs/{id}` (`library_item_id`, `loop`, `play_on_cut`, `on_end`: `off`, `cut`, `auto`, `stinger`, `stinger_slot_id`) |
| Clip player transport | `POST /players/{id}/cue` (`{"frame": n}`), `/play`, `/pause`, `/stop` |
| Keyers | `GET /keyers`; `PATCH /keyers/{id}` (`enabled`, `rate_ms`, `mode`: `linear`/`luma`/`pip`, `source_id`, `clip`, `soft`, `gain`, `invert`, `dme`: a partial box `{enabled, x, y, size, soft, crop {left, right, top, bottom} (whole source pixels; left + right < the raster's width, top + bottom < its height; left and right on 4:2:2 pairs), border {width, soft, color, opacity}, shadow {dx, dy, blur, opacity}}` merged into the keyer's, `fly`; `pip` and `dme` upstream only; a pip is always a box); `PUT /keyers/{id}/dme` replaces the whole box; ids `me-<n>-key-<k>`, `dsk-<k>` (ME 1), `me-<n>-dsk-<k>`. While a run moves a box, `GET /keyers` and `/console` show it where the engine has it and `run` (`running`, `fly`, `loop`, `keyframe`, `step`) |
| Keyer AUTO | `POST /keyers/{id}/auto` (`duration_ms`, default the keyer's rate); with `fly` (and 2 keyframes) the box flies on from keyframe 0 to the last, or off back to 0 |
| Keyframes | `PUT /keyers/{id}/keyframes` (up to 8: `[{x, y, size, soft, crop, border {width, soft, opacity}, shadow, frames, ease}]`; missing box fields take the keyer's box); `POST /keyers/{id}/run` (`to`, default the last keyframe; `reverse`: to 0; `loop`: `off`, `wrap`, `pingpong`; `frames: 0` jumps), `POST /keyers/{id}/stop` |
| Presets | `GET /dme/presets`; `POST /keyers/{id}/preset` (`name`: `corner-tl`, `corner-tr`, `corner-bl`, `corner-br`, `split`, `quad`, `full`): the box and keyframes (0 off screen, 1 on) of that keyer (split: the ME's keyers 1 and 2, quad: 1 to 4); on air the box flies there over the keyer's rate |
| Transition type, rate, wipe pattern and edge, ties | `PATCH /panels/{id}/transition` (`type`, `rate_frames`, `wipe`: `pattern`, `reverse`, `alternate`, `soft`, `border`, `border_color`; `stinger_slot_id`; `ties`: the ME's upstream keyer ids that Cut, Auto and the T-bar take along; `background`: false is key only and needs a tie); `GET /console` `panels[].transition` |
| Audio settings | `GET /audio` (per live input `delay_ms`, `channel_map` and its flow's `channels`; per rendering panel `master_db` and its `strips` in slot order with `fader_db`, `mute`, `afv`; the PGM's `channels`, `max_delay_ms`); also in `GET /console` `audio` |
| Audio of a live input | `PATCH /audio/inputs/{id}` (`delay_ms`: 0 … `FLOWXER_AUDIO_MAX_DELAY_MS`, whole 48 kHz samples; `channel_map`: per PGM channel the input channel, 1-based, 0 silent, `[]` 1:1), for every ME |
| Audio of an ME | `PATCH /audio/panels/{id}` (`master_db`); `PATCH /audio/panels/{id}/strips/{strip}` (`fader_db` −90 … +10, `mute`, `afv`; `strip`: an input or clip player id, a higher panel id (re-entry) or `stinger`, which has no `afv`); `POST /audio/panels/{id}/loudness/reset` (integrated loudness from now on) |
| Audio meters (10 Hz) | `GET /audio/meters`: the last 100 ms block; `sources` (live inputs and clip players by id, `stinger-<k>`: `peak_dbfs`, `rms_dbfs` per channel), `panels` (`peak_dbfs`, `true_peak_dbtp`, `rms_dbfs` per channel, `loudness` per pair: `momentary`, `short_term`, `integrated` LUFS; `overs`; `strips`: `gain_db`, `heard`). Values below −120 dB are −120 |
| SNAP (snapshots) | `GET /snapshots` (also `GET /console` `snapshots`); `POST /snapshots` (`panel_id`: `me-<n>` or `all`, `number` 1–99, default the lowest free; `name`; `options`: `program`, `audio`): store the current state; `GET`, `PATCH` (`name`, `options`), `DELETE /snapshots/{number}`; `POST /snapshots/{number}/recall` (`program`, `audio` override its defaults; answers what it `skipped`); `PUT /snapshots/startup` (`number` or null). See [docs/snapshots.md](docs/snapshots.md) |
| T-bar lever | `POST /mixer/position` (`panel_id` and either `position` 0–1: off 0 starts the ME's transition and holds it, 1 completes it, 0 ends it without the swap; or `lever` 0 top … 1 bottom: where a physical lever is, counted from the end it rests at, see below); AUTO runs the rest; a stinger runs as an Auto. The lever rests at `tbar_end` of the panel (`top` or `bottom`, saved); each transition the T-bar or an Auto completes (or a Cut of a held one) moves it to the other end, so the next transition goes back the other way (ping-pong). With `lever`, nothing starts within 2 % of the resting end, and a held transition snaps within 2 % of either end (back without the swap, or complete). When another console's Auto or Cut moves the resting end: a lever request with `from` (`top`/`bottom`, the end its drag began at; the GUI sends it) from an end that is no longer the resting one is ignored, so a drag cannot take the shot back; a lever without `from` (a hardware panel) counts only once it has come within 2 % of the resting end (pickup): at start, after the engine restarts or a state is loaded, and after each change of the end; until then it is ignored, also while a console holds a transition. One hardware lever per ME without `from`, or give each its own `source` (each picks up on its own; at most 64 per ME are kept, the oldest picks up again). An ignored value answers 200 with `status: "ignored"` and `reason` (`stale_from` or `pickup`). With a stinger or BKGD off the lever fires one Auto, then waits for the next drag (or its pickup at the other end). `GET /console` → `tbar` per ME: `end`, `position` (of a held transition), `lever` |
| CUT / AUTO / FTB | `POST /mixer/cut`, `/mixer/auto` (`panel_id`, `frames`), `/mixer/fade-to-black`. Kept for API clients: `/mixer/fade` (a mix), `/mixer/wipe` (arms Wipe), `/mixer/take` (`transition`: `cut`, `mix`, `wipe`, `stinger`; `frames` or `duration_ms`) |
| Stinger chip / ⚙ | `POST /stinger/play`, `PATCH /stinger-slots/{id}` |
| GRAB | `POST /library/grab` (`source`: an input or clip player id, a panel id for its Program, `<panel id>:pvw` for its Preview; `player`: a clip player to load the still into; `name`, default `Grab <source> <YYYY-MM-DD HH:MM:SS>`): the new still (kind `still`, source `grab`, tag `grab`), ready at once |
| File → libraries | `GET /library`, `POST /uploads`, `GET /jobs` |
| Tally → Receivers… | `PUT /tally/receivers`, `POST /tally/refresh` |
| Pictures | `GET /preview`: the mosaic's WHEP URL (or the port of the built-in MediaMTX) and the tile map (`source:<input id>`, `panel:<panel id>:pgm\|pvw`) |
| Widgets | `GET /widgets` (gui) = `GET /api/v1/widgets` |
| NMOS (IS-04/IS-05) | Node API on **3252**, see [docs/nmos.md](docs/nmos.md) |

API only: `GET /health`, `/config`, `/engine` (the engine's status: GPU time per stage, output counters, arrival lag per input and the recommended latency, clip players, keyers; refreshed once a second), `/mixer`, `/domain`, `/domain/flows`, `/metrics`, `/livez`, `/readyz`; `GET /config/export`, `POST /config/import`; `POST`/`DELETE /inputs`; `GET /storage/clips`, `/storage/stingers`; the library's item, upload and job routes (see [Media library](#media-library)).

```bash
curl http://localhost:9620/api/v1/health
curl -X POST http://localhost:9620/api/v1/mixer/start -H 'content-type: application/json' -d '{"program_input_id":"cam-1"}'

# A clip from the library on clip player 1, looping, started by the Cut that takes it to Program
curl -X PATCH http://localhost:9620/api/v1/inputs/clip-1 -H 'content-type: application/json' \
  -d '{"library_item_id":"<item id>","loop":true,"play_on_cut":true}'
curl -X POST http://localhost:9620/api/v1/mixer/preview -H 'content-type: application/json' -d '{"input_id":"clip-1","panel_id":"me-1"}'
curl -X POST http://localhost:9620/api/v1/mixer/cut -H 'content-type: application/json' -d '{"panel_id":"me-1"}'
curl -X POST http://localhost:9620/api/v1/players/clip-1/pause

# Re-entry: ME 1 takes ME 2's Program
curl -X POST http://localhost:9620/api/v1/mixer/take -H 'content-type: application/json' \
  -d '{"input_id":"me-2","panel_id":"me-1","transition":"cut"}'

# DSK 1: rate 25 frames at 50p, then fade it on
curl -X PATCH http://localhost:9620/api/v1/keyers/dsk-1 -H 'content-type: application/json' -d '{"rate_ms":500}'
curl -X POST http://localhost:9620/api/v1/keyers/dsk-1/auto

# TSL 5.0 tally/UMD to Riedel HI (same shape for Companion, VSM, BFE)
curl -X PUT http://localhost:9620/api/v1/tally/receivers -H 'content-type: application/json' \
  -d '{"receivers":[{"id":"hi-1","kind":"hi","label":"Riedel HI","host":"10.0.0.40","port":8900,"transport":"udp","enabled":true,"screen":0,"index_offset":0}]}'
```

## Docker

Compose pulls the images published from `main` to GHCR (`latest`, or set `FLOWXER_IMAGE_TAG`). The mixer needs an NVIDIA GPU and the NVIDIA container toolkit (Compose reserves one GPU).

```bash
mkdir -p storage/clips storage/library storage/import
# optional: drop clips and pictures into storage/import for watched ingest
docker compose pull
docker compose up
```

Images: `ghcr.io/firesh0ot/flowxer-vision-mixer` and `ghcr.io/firesh0ot/flowxer-gui`, tagged `{version}`, `latest` and `git-<sha>`. Both run as **uid/gid 1000**; host mounts (`/Volumes/mxl`, `./storage`) must be writable by that user. To run a source tree, `docker build` the two Dockerfiles yourself (`Dockerfile`, `gui/Dockerfile`).

- **gui** on **9620**: console, widgets, mixer API (`/api/v1`) and OpenAPI. Upstream `FLOWXER_MIXER_URL` (Compose `http://vision-mixer:9610`; host network and Kubernetes `http://127.0.0.1:9610`).
- **vision-mixer** on **127.0.0.1:9610**, and the preview mosaic of the engine's built-in MediaMTX: WHEP 8989, HLS 8988, ICE 8289 (UDP and TCP). `FLOWXER_HOST` defaults to `127.0.0.1`; bridge Compose sets `0.0.0.0` so the GUI container can reach it.
- A tmpfs MXL domain at `/mxl-domain` (the deprecated single-domain alias `FLOWXER_MXL_DOMAIN`, for local demos) and `./storage` for the library.

The mixer image is CUDA 12.8 on Ubuntu 24.04 with [MXL](https://github.com/dmf-mxl/mxl) `release/v1.1` at `218ddaa0a08c12ffe75fc475ae65aa3d9eef16d7` (image label `io.dmf.mxl.revision`, `GET /api/v1/health` `mxl_revision`), Ubuntu's FFmpeg (the engine's NVDEC and NVENC, the library's conversions) and MediaMTX. The engine's own README is [engine/README.md](engine/README.md); `flowxer-engine --selftest` checks the GPU kernels against the CPU reference.

## Running on an MXL platform

On an MXL node, FlowXer is one NMOS media function: GUI **9620**, mixer **9610** (loopback), Node API **3252**. `FLOWXER_API_TOKEN` is **required**. The pod needs one GPU (nvidia runtime class, a time-sliced share is enough), `NVIDIA_DRIVER_CAPABILITIES=compute,video,utility` (the image sets it), the MXL root, `/config` for the state and a persistent volume for the library (`FLOWXER_STORAGE_ROOT`).

```bash
kubectl apply -f deploy/kubernetes/flowxer.yaml               # host network
kubectl apply -f deploy/kubernetes/servicemonitor.yaml        # optional, Prometheus Operator CRDs
```

`deploy/kubernetes/flowxer-pod-network.yaml` is the same function on the pod network with the platform's env names (`MXL_*`, `NMOS_*`, `SHUTDOWN_TIMEOUT_S`, `PREVIEW_*`): the node announces the pod IP, probes go to `/livez` and `/readyz`, the preview mosaic goes to the platform's shared MediaMTX. Host Compose: `docker compose -f docker-compose.host.yml up` with `/Volumes/mxl` mounted and `FLOWXER_API_TOKEN` set.

### Ports

| Port | Use |
|---|---|
| 9610 | Mixer HTTP (`/api/v1`, `/metrics`, `/livez`, `/readyz`), bind `127.0.0.1` |
| 9620 | Operator GUI and widgets (proxies `/api` to the mixer) |
| 3252 | NMOS Node + Connection API (not behind the API token); 3253 reserved |
| 9630 | The media engine's control API, `127.0.0.1` only (`FLOWXER_ENGINE_PORT`) |
| 8989, 8988, 8289 udp+tcp | The built-in MediaMTX: WHEP, HLS, ICE (not used with `PREVIEW_PUBLISH_URL`) |
| 8654, 9897 | The built-in MediaMTX: RTSP ingest and API, `127.0.0.1` only |

### Configuration

Settings come from the environment (or a `.env` file). Where a platform name exists it wins over the `FLOWXER_` name; both work. An invalid value stops the process with exit code 78.

| Variable | Default | Notes |
|---|---|---|
| `FLOWXER_HOST`, `FLOWXER_PORT` | `127.0.0.1`, `9610` | Mixer bind. Bridge Compose uses `0.0.0.0` |
| `FLOWXER_MIXER_URL`, `FLOWXER_GUI_PORT` | `http://127.0.0.1:9610`, `9620` | gui container: nginx upstream and port |
| `WIDGET_FRAME_ANCESTORS` | `'self'` | gui container: who may frame the widgets, see [Widgets](#widgets) |
| `MXL_DOMAIN_SCAN_PATH` / `FLOWXER_MXL_ROOT` | `/Volumes/mxl` | Scan for `domain_def.json` |
| `MXL_OUTPUT_DOMAIN_DIR` / `FLOWXER_MXL_OUTPUT_DOMAIN_DIR` | `<root>/flowxer-<seed-short>` | Output domain of the MEs; never `mirror-*`. An existing `domain_def.json` with another id is logged as an error and kept |
| `MXL_OUTPUT_DOMAIN_ID` / `FLOWXER_MXL_OUTPUT_DOMAIN_ID` | UUIDv5(seed) | |
| `MXL_HISTORY_DURATION` / `FLOWXER_MXL_HISTORY_DURATION_NS` | `200000000` | ns; written to `options.json` when the domain is created |
| `MXL_CLEANUP_ON_EXIT` / `FLOWXER_MXL_CLEANUP_ON_EXIT` | `false` | Remove the own output domain on SIGTERM (only when its id matches; never the root or a mirror) |
| `FLOWXER_NMOS_ENABLE` | `true` | `false` keeps REST-only behaviour |
| `NMOS_SEED` / `FLOWXER_NMOS_SEED` | `{hostname}-flowxer` | Stable UUIDv5 ids |
| `NMOS_LABEL` / `FLOWXER_NMOS_LABEL`, `NMOS_TAGS` / `FLOWXER_NMOS_TAGS` | `FLOWXER_TITLE`, `{}` | Node and device label and tags |
| `NMOS_PORT` / `FLOWXER_NMOS_PORT` | `3252` | |
| `NMOS_HOST_ADDRESS` / `FLOWXER_NMOS_HOST_IP` | first non-loopback | Node `href` / `api.endpoints` (an IP address) |
| `NMOS_REGISTRY_ADDRESS`, `NMOS_REGISTRY_PORT` | empty | Registration API; or the full URL in `FLOWXER_NMOS_REGISTRY_URL` |
| `NMOS_DNS_SD` / `FLOWXER_NMOS_DNS_SD` | `false` | Not implemented; `true` only logs a warning |
| `FLOWXER_STATE_DIR` | `/config` | Saved state, see below |
| `FLOWXER_FORMAT`, `FLOWXER_LIVE_INPUTS`, `FLOWXER_INPUT_LABELS`, `FLOWXER_CLIP_PLAYERS`, `FLOWXER_PANELS` | empty | The production's structure, see below |
| `FLOWXER_RENDER_MES` | all MEs | The first n MEs render (PGM, PVW, senders and flows) |
| `FLOWXER_PVW_OUTPUTS` | `true` | PVW video flows; `false` renders PVW for the previews only (saves PCIe) |
| `FLOWXER_PREVIEW_PGM` | `true` | Each ME's Program as a WebRTC stream of its own (`<prefix>/me<n>-pgm`), sharper than a mosaic tile; the mosaic then has no Program tiles. The engine needs **1 + MEs NVENC sessions** (the mosaic and each Program; cost below): it opens them all at start, and when the GPU cannot give them (e.g. a GeForce card's session limit, or other encoders on it) the Programs stay mosaic tiles (`GET /preview` → `streams_error` says why; the console falls back by itself). `false`: the mosaic only (1 session) |
| `FLOWXER_PREVIEW_PGM_WIDTH`, `FLOWXER_PREVIEW_PGM_FPS`, `FLOWXER_PREVIEW_PGM_KBPS` | `1280`, `25`, `4000` | Its width (the height keeps the raster's aspect: 1280×720 at 16:9; at most the raster's width), frame rate and bit rate (CBR) |
| `FLOWXER_AUDIO_CHANNELS` | `2` | PGM audio channels of every ME (a flow that exists keeps its own); each live input maps into them 1:1 or by its channel map |
| `FLOWXER_AUDIO_MAX_DELAY_MS` | `1000` | The longest audio delay of a live input (0–5000); the engine's delay lines hold this much (stereo: about 0.44 MB per live input at 1 s, 16 channels 3.5 MB) |
| `FLOWXER_LATENCY` | `4` | Grains (1–10): output grain *i* is made from input grain *i − L*. It must cover how late the inputs arrive: `GET /api/v1/engine` gives each input's arrival lag and `recommended_latency_grains` |
| `FLOWXER_PROGRAM_AUTOSTART` | `false` | Start Program at process start (after the state is restored) |
| `FLOWXER_TALLY_TSL` | empty | Raw tally for the platform's tally calculator, see [Raw tally export](#raw-tally-export-flowxer_tally_tsl) |
| `PREVIEW_PUBLISH_URL`, `PREVIEW_PATH_PREFIX`, `PREVIEW_WHEP_URL`, `PREVIEW_HLS_URL` | empty, `flowxer` | The platform's preview contract: publish the mosaic (`<prefix>/mosaic`) to a shared MediaMTX over RTSP; the console plays it from `PREVIEW_WHEP_URL`. Empty: the built-in MediaMTX |
| `MEDIAMTX_PUBLIC_IP` | empty | Built-in MediaMTX: extra ICE host candidate for browsers |
| `FLOWXER_ENGINE_PORT`, `FLOWXER_ENGINE_GPU`, `FLOWXER_ENGINE_BIN` | `9630`, `0`, `flowxer-engine` | The engine's control port (loopback), CUDA device in the container, binary |
| `FLOWXER_STORAGE_ROOT`, `FLOWXER_LIBRARY_DIR`, `FLOWXER_IMPORT_DIR` | `./storage` (image `/storage`), `<root>/library`, empty | Media library, see below |
| `FLOWXER_CONVERT_CONCURRENCY`, `FLOWXER_UPLOAD_LIMIT_GB` | `1`, `20` | |
| `SHUTDOWN_TIMEOUT_S` / `FLOWXER_SHUTDOWN_TIMEOUT_S` | `10` | Open requests get half; the rest is for stopping media and deregistering |
| `FLOWXER_API_TOKEN` | empty | **Required on the platform** |
| `FLOWXER_SIMULATE` | `false` | The control plane without the engine (tests, the AMWA testing tool): state, NMOS and tally work, no media |

### The media engine

The engine reads the live inputs from MXL (one DMA upload per grain), plays the clip players, renders every ME on the GPU (ME 4 → ME 1, so re-entry is the same output frame) with its transitions and keyers in one pass per ME, packs PGM and PVW to v210 and writes them with the PGM audio to MXL at a fixed latency on the TAI grid: when it is late it skips to the current grain and counts it; it never slides. It encodes the preview mosaic once with NVENC. [engine/README.md](engine/README.md) has the design, its configuration, its API and the lab measurements (P1: 4 MEs, 8 inputs and the mosaic at 3.4 ms GPU render time per grain and 0.13 CPU cores on an A16).

FlowXer writes the engine's configuration (`FLOWXER_ENGINE_*`) itself, starts it with the GPU of the container, and after every start pushes the routes, clip players, keyers, buses and the audio settings. An engine that exits is started again after 1, 2, 5, then every 10 s; `GET /api/v1/health` (`engine`) and `GET /api/v1/engine` show its state. A crashed engine's MediaMTX is reaped by tini (PID 1).

GPU memory: about 0.6 GiB for 1 ME up to 0.8 GiB for 4 MEs with 8 inputs and the mosaic, about 50 MB per clip player and 170 MB per loaded clip, about 350 MB per stinger player with a stinger loaded, about 80 MB per distinct routed key flow (lab: 867 MiB with 2 MEs, 4 inputs, a clip and a still, 1 key input and the mosaic). Key flows also take PCIe: next to 8 inputs at 1080p50, 8 distinct key flows are the edge of a Gen4 x4 or Gen3 x8 link; under full load (wipes and stingers on every ME) a grain can come late now and then. FlowXer sets no limit and gives no warning (measured: `docs/engine-p3.md`, M4 as built).

### Saved state, export and import

The mixer saves its configuration to `FLOWXER_STATE_DIR/state.json` after every successful change through the API and after every IS-05 activation, and loads it on start: inputs, console layout, mixer panels, downstream keyers, stinger slots, tally receivers, the receiver connections, the audio (`audio`: delays and channel maps, masters and strips; a state without it loads with the defaults) and the snapshots (`snapshots`: the start-up snapshot and every snapshot with its contents; a state without it loads with none). A start keeps ME 1's saved Program and Preview unless the request names others; a start-up snapshot ([docs/snapshots.md](docs/snapshots.md)) wins over both. Audio changes are saved at most once a second (a moving fader sends up to 20). Mount `/config` to keep it across restarts. A file that cannot be read is logged and ignored (the mixer starts with defaults).

`GET /api/v1/config/export` returns the same document; `POST /api/v1/config/import` restores it (409 while the mixer is on-air, 422 when it is invalid). It holds no secrets: the API token only comes from the environment.

### Production structure from the environment

The platform's production designer sets the mixer's structure through the environment, so the inputs and the NMOS labels are known before the pod starts. A variable that is set wins over the saved state at every start; unset (or empty), the saved state and the GUI decide.

| Variables | Set | Example |
|---|---|---|
| `FLOWXER_FORMAT` | `format_id`, raster and rate | `1080p50` |
| `FLOWXER_LIVE_INPUTS`, `FLOWXER_INPUT_LABELS`, `FLOWXER_CLIP_PLAYERS` | The input list: `logical_source_count` and each input's id, kind and label | `4`; `["Cam 1","Cam 2","Cam 3","Cam 4"]` or `Cam 1,Cam 2,Cam 3,Cam 4`; `2` |
| `FLOWXER_PANELS` | `mixer_panel_count` (MEs, 1–4) | `2` |
| `FLOWXER_KEYERS_PER_ME`, `FLOWXER_DSKS_PER_ME` | `keyers_per_me` (0–8, default 2) and `dsks_per_me` (0–4, default 1) of every rendering ME; set, the keyer labels are fixed | `2`, `1` |
| `FLOWXER_STINGER_PLAYERS` | `stinger_players` (0–4, default 1, at most the rendering MEs): stingers that can play at once | `1` |

With the input list set, the inputs are in this order (the slot order, the TSL INDEX): `cam-1`..`cam-N` (`mxl_live`, labelled from `FLOWXER_INPUT_LABELS`, the rest `Camera n`), `clip-1`..`clip-C` (clip players, `Clip n`), then `black` (Black). Live inputs and clip players together are at most 23 (24 sources with Black). More labels than live inputs, a duplicate or empty label, or an unknown format stop the process with exit code 78. `FLOWXER_CLIP_PLAYERS` replaces `FLOWXER_TEST_SOURCES`, which is no longer read; saved states and API clients with test, file or replay inputs get clip players.

- **Kept from the saved state:** receiver connections (IS-05), and per input the essences, group hint, library item, loop, play on cut and auto-stinger when its id and kind stay the same; keyers, stingers, tally and the other workspace fields. Routes of an input the environment removed are dropped; Program or Preview on it starts empty.
- **API and GUI:** what the environment sets cannot be changed (409, the message names the variable): `PUT /workspace` with another `format_id`, `logical_source_count` or `mixer_panel_count`; `POST` and `DELETE /inputs`; `PATCH /inputs/{id}` with another `label` or `kind`. The current values pass, so a client may send whole documents. `GET /console` lists these fields in `pinned`; the GUI greys them out.
- **Export and import:** an imported document gets the environment's structure, as at a start; everything else in it is imported.
- **NMOS labels** follow the structure: receivers `<input label> Video` and `<input label> Audio` for each live input, and one v210a receiver per keyer of each rendering ME: `ME <n> Key <k>` (upstream), `DSK <k> Key` (ME 1, P2's id kept), `ME <n> DSK <k> Key`; per rendering ME the senders `ME <n> PGM Video`, `ME <n> PGM Audio` and `ME <n> PVW Video`, each with its own flow. ME 1 PGM keeps FlowXer's flow ids across versions.
- `FLOWXER_PROGRAM_AUTOSTART=true` starts Program once the state is restored: ME 1 Program on the first live input (else the first input), Preview on the next one. A start that fails is logged and shown in `GET /mixer`, and tried again after 2, 5, 10, then every 30 s until Program runs or an operator starts or stops it. An input routed to an MXL domain that does not exist yet (a fabrics mirror after a node reboot) is black and silent and reads its flow once the domain appears.

```bash
FLOWXER_FORMAT=1080p50
FLOWXER_LIVE_INPUTS=4
FLOWXER_INPUT_LABELS='["Camera 1","Camera 2","Camera 3","Camera 4"]'
FLOWXER_CLIP_PLAYERS=2
FLOWXER_PANELS=2
FLOWXER_PROGRAM_AUTOSTART=true
```

### Exit codes and probes

| Code | Meaning |
|---|---|
| 1 | Start-up failed (see the log) |
| 75 | The mixer or NMOS port cannot be bound |
| 78 | Invalid configuration (the message names the setting) or an unusable output domain |
| 143 | Stopped by SIGTERM: media stopped, node deregistered, own domain removed with `MXL_CLEANUP_ON_EXIT` |

`/readyz` is 200 only when the MXL root is readable, the output domain is writable and, with a registry configured, the node is registered (heartbeat within 12 s). `/livez` is 503 when the control plane is stuck: an IS-05 activation or a Program start or stop has not finished for 60 s (Kubernetes then restarts the pod); `flowxer_control_plane_busy_seconds` is the age of the oldest running one.

### Metrics

`GET /metrics` (and `/api/v1/metrics`, not behind the API token) is Prometheus text. For dashboards:

| What | Metrics |
|---|---|
| On air, Program | `flowxer_on_air`, `flowxer_program_stalled`, `flowxer_frames_rendered_total`, `flowxer_frames_dropped_total` (grains skipped to stay on the TAI grid), `flowxer_transitions_total{type}` (`cut`, `mix`, `wipe`, `stinger`), `flowxer_program_input`, `flowxer_preview_input` |
| Engine output | `flowxer_engine_up`, `flowxer_engine_restarts_total`, `flowxer_engine_output_fps`, `flowxer_engine_output_grains_rendered_total`, `_committed_total`, `_skipped_total`, `_late_total`, `flowxer_engine_output_write_failed_total`, `flowxer_engine_output_lag_grains{stat}`, `flowxer_engine_output_commit_ms{stat}` |
| Latency | `flowxer_latency_grains`, `flowxer_recommended_latency_grains` (covers the latest arrival in the last minute of the inputs and key flows that deliver; an input's audio only while some ME hears it) |
| Inputs (FlowXer ids) | `flowxer_input_arrival_ms{input,essence,stat}`, `flowxer_input_late_total{input}`, `flowxer_input_missing_total{input,essence}`, `flowxer_input_state{input,essence,state}` |
| Inputs (engine names `in<n>`, `key<n>`) | `flowxer_engine_input_running`, `_grains_total`, `_late_total`, `_missing_total`, `_resyncs_total`, `_reopens_total`, `flowxer_engine_input_upload_ms{stat}` (also of each key flow), `flowxer_engine_input_audio_blocks_total`, `_audio_missing_total`, `_audio_waits_total` |
| GPU time | `flowxer_engine_gpu_stage_ms{stage,stat}` with `stage` = `upload_per_input`, `unpack`, `me` (and `me="<n>"`, per ME), `pack`, `mosaic`, `render`, `download`, `total`; `flowxer_engine_dme_ms{me,stat}` (the resample of each ME's boxes; their box layers are in the ME's time); `flowxer_engine_gpu_utilization_percent`, `flowxer_engine_gpu_device_used_mb`, `flowxer_engine_gpu_engine_allocated_mb` |
| Clip players | `flowxer_engine_player_playing{player}`, `flowxer_engine_player_frames_decoded_total`, `flowxer_engine_player_frames_late_total`, `flowxer_engine_player_decode_ms{stat}` |
| Stingers | `flowxer_engine_stingers_played_total`, `flowxer_engine_stinger_wait_grains_total` (grains a take waited for its first frame), `flowxer_engine_stinger_cuts_total` (takes that became a cut after 50 grains), `flowxer_stinger_fallbacks_total{reason}` (takes the control plane made a cut: `not_ready`, still converting; `busy`, no stinger player free within 0.5 s), per player `flowxer_engine_stinger_player_state{player,state}` (1 for `empty`, `cued`, `playing`, `paused` or `ended`), `flowxer_engine_stinger_frames_decoded_total`, `_frames_late_total`, `flowxer_engine_stinger_decode_ms{stat}` |
| Keyers | `flowxer_engine_keyer_on_air{keyer,me,stage,mode}` (on or fading), `flowxer_engine_keyer_key_running{…}` (its key flow runs; 0 for a linear key is NO KEY) |
| Audio (P5) | `flowxer_engine_audio_level_dbfs{source,channel,stat}` (`source` `in<k>`, `cp<k>`, `st<k>`; `stat` `peak`, `rms`; the last second, before the faders), `flowxer_engine_pgm_audio_level_dbfs{me,channel,stat}` (`peak`, `true_peak`, `rms`), `flowxer_engine_pgm_loudness_lufs{me,pair,window}` (`momentary`, `short_term`, `integrated`), `flowxer_engine_pgm_audio_overs_total{me}`, `flowxer_engine_audio_strip_heard{me,strip}`, `flowxer_engine_audio_ms{stat}` (fx-audio per grain), `flowxer_engine_audio_wait_ms{stat}`, `flowxer_engine_audio_jobs_dropped_total`; values below −120 dB are −120 |
| Frame grabs | `flowxer_engine_grabs_total{source}` (`input`, `player`, `pgm`, `pvw`), `flowxer_engine_grab_failed_total`, `flowxer_engine_grab_ms{stat}` (from the request to the file) |
| Preview mosaic | `flowxer_engine_mosaic_frames_total`, `_dropped_total`, `flowxer_engine_mosaic_encode_ms`, `flowxer_engine_mosaic_connected` (publishing), `flowxer_engine_mosaic_bytes_total`, `flowxer_engine_gpu_encoder_sessions`, `flowxer_engine_gpu_encoder_percent`; own MediaMTX: `flowxer_engine_preview_mediamtx_running`, `_restarts_total` |
| Library | `flowxer_library_jobs{state=queued\|running}`, `flowxer_library_conversions_total{result}`, `flowxer_library_last_conversion_seconds`, `flowxer_library_nvenc` |
| Control plane | `flowxer_control_plane_busy_seconds`, `flowxer_nmos_registry_up`, `flowxer_nmos_activations_total{result}`, `flowxer_tally_export_*`, `flowxer_engine_cpu_percent{thread}`, `flowxer_process_cpu_percent`, `flowxer_process_memory_bytes` |

Gone with the GStreamer mixer: `flowxer_webrtc_peers`. The preview's viewers are the MediaMTX path's readers (MediaMTX API `/v3/paths/list`, or its own metrics). The Grafana dashboard `deploy/grafana/flowxer.json` shows the engine's fps, skipped and late grains, GPU time (render, download, upload, per ME), GPU memory and NVENC sessions, stingers (played, waits, cuts, player state), keyers (on air per ME, linear keys without a key signal) and audio (PGM loudness and true peak per ME, overs, silent sources, the audio thread and the strips heard).

### Upgrading from FlowXer 14

The P2 image replaces 14.x in place with the same `/config` (the saved state), the same NMOS seed and the same output domain. Lab, from 14.21.46 with 4 live inputs routed over IS-05, 2 test sources, 2 MEs and a multiviewer reading ME 1 PGM: ME 1 PGM keeps its flow ids, the IS-05 activations of the live inputs are restored, their tally INDEX stays 0..N−1 and Black keeps its number; the multiviewer had no signal while the images were swapped and played again 3.2 s after the new one started.

- **Environment:** `FLOWXER_TEST_SOURCES` is no longer read: set `FLOWXER_CLIP_PLAYERS` instead (the designer's `testSources` becomes `clipPlayers`). Set a GPU (`nvidia.com/gpu`, `NVIDIA_DRIVER_CAPABILITIES=compute,video,utility`) and drop `FLOWXER_GPU`, `FLOWXER_GST_MODE`, `FLOWXER_MONITOR_FPS` and `FLOWXER_WEBRTC_*`. `WIDGET_FRAME_ANCESTORS` goes to the gui container.
- **What an operator loses:** test sources become clip players at the same TSL INDEX, empty (black) until a clip or the colour bars are put on them; the Replay input (the last INDEX) is gone, its role is a clip player's; the HTML overlay is gone, so DSK 1 stays on but keys nothing until a v210a key (e.g. mxl-browser-source in `v210a` mode) is routed to its receiver `DSK 1 Key`; 14.x showed the overlay on Preview too, P2 keys Program only (keys have no preview); the per-source WebRTC and JPEG previews are replaced by the mosaic; clips of the library are converted again (NVENC, about 35 s per minute of clip) and black until then.
- **New on the tally:** an ME's re-entry source, INDEX 1000 + n.
- **Known in 14.21.46:** started with its live inputs unrouted (autostart, or a start before the first IS-05 activation) its MXL sources crash the process (exit 139, a general protection fault in libstdc++ in an `mxlsrc` thread). With the routes in the saved state it starts. P2 has no such path.
- **Rollback** (not tested): 14.x does not know clip players and ignores a state that P2 saved (it starts from its environment and defaults). Keep the 14.x state (`GET /api/v1/config/export`, or a copy of `/config`) before the upgrade to roll back with it.

### Route a camera to input 1, take it to Program, route Program out

Live inputs have NMOS receivers; an IS-05 activation opens the engine's reader for that flow, a deactivation closes it. Replace the UUIDs with values from `GET http://<host>:3252/x-nmos/node/v1.3/receivers` and `…/senders`.

```bash
# The receivers of Camera 1 (video + audio).
curl -sS http://127.0.0.1:3252/x-nmos/node/v1.3/receivers

# Route an mxl-decklink sender onto Camera 1 video (the platform does this over IS-05).
# Accepted even if the flow is not on disk yet (state waiting).
curl -sS -X PATCH "http://127.0.0.1:3252/x-nmos/connection/v1.2/single/receivers/<RECEIVER_UUID>/staged" \
  -H 'content-type: application/json' \
  -d '{"sender_id":"<DECKLINK_SENDER_UUID>","master_enable":true,"activation":{"mode":"activate_immediate"},"transport_params":[{"mxl_domain_id":"<DOMAIN_UUID>","mxl_flow_id":"<FLOW_UUID>"}]}'

# Take Camera 1 to Program.
curl -sS -X POST http://127.0.0.1:9620/api/v1/mixer/take -H 'content-type: application/json' \
  -d '{"input_id":"cam-1","panel_id":"me-1","transition":"cut"}'

# Route ME 1 PGM to an mxl-decklink output: set that receiver's IS-05 transport params to the
# mxl_domain_id / mxl_flow_id of FlowXer's "ME 1 PGM Video" sender.
curl -sS http://127.0.0.1:3252/x-nmos/connection/v1.2/single/senders
```

AMWA NMOS Testing Tool (non-interactive, `amwa/nmos-testing` image):

```bash
bash scripts/nmos-testing.sh http://127.0.0.1:3252
NMOS_TESTING_SUITES=IS-04-01,IS-05-01,IS-05-02,BCP-007-03-01 bash scripts/nmos-testing.sh http://127.0.0.1:3252
```

CI runs it on every PR (`continue-on-error`, JUnit XML as an artifact). Grafana: `deploy/grafana/flowxer.json`.

## Local development

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
pytest
FLOWXER_SIMULATE=true FLOWXER_STORAGE_ROOT=./storage FLOWXER_MXL_ROOT=./data/mxl-domain \
  uvicorn flowxer.app:create_app --factory --port 9610
cd gui && npm run dev        # the console on 9620, /api proxied to the mixer
```

Simulate mode runs the control plane without the engine (no pictures, nothing on MXL). The engine needs a GPU: build the mixer image, or the engine alone with CMake (`engine/README.md`; `-DFXENG_BUILD_ENGINE=OFF` builds only its unit tests, which need no GPU).

## Media library

Clips, stills and stingers share one ingest path: **upload → background conversion → mezzanine → play**. Heavy work happens at ingest. A clip player (`library_item_id` on its input) plays a clip from its mezzanine, decoded ahead on the GPU (NVDEC), or shows a still uploaded once to the GPU; the colour bars are a still that every new library starts with.

Layout under `FLOWXER_LIBRARY_DIR` (default `storage/library/`):

```
<id>/
  item.json
  original… or sequence/  # the uploaded file, never changed
  mezz-<format>.mkv       # clip, and a stinger's colour: HEVC 4:4:4 10-bit
  mezz-<format>-alpha.mkv # stinger: its alpha as the luma of lossless HEVC 4:2:0 10-bit (none: opaque)
  mezz-<format>.f32       # clip or stinger: its sound, float32 48 kHz interleaved, stereo (or mono); a silent stinger has none
  still-<format>.v210     # still: one v210 picture at the raster
  thumb.jpg
  convert.log
```

Upload via the operator GUI (**File → Clip and still library…** or **Stinger library…**) or HTTP (`POST /uploads` chunked, or `POST /uploads/sequence` for a TGA or PNG folder with an optional WAV). Options include fit/fill, sequence framerate, and cut frame. Changing the mixer format (off-air) re-queues conversion for every item.

- **Chunked upload:** `POST /uploads` with the file size (413 above `FLOWXER_UPLOAD_LIMIT_GB`, 507 when the library volume lacks the space), then `PUT /uploads/{id}/chunks/{n}` — every chunk exactly `chunk_size` bytes (8 MiB), the last one the rest (otherwise 413/422) — and `POST /uploads/{id}/complete`, which fails with 422 while a chunk is missing. Chunks go straight into place on disk and completing is a rename, so it answers at once; a repeated complete returns the same item. An upload idle for an hour is dropped, and leftovers are removed at start. A sequence ZIP (TGA or PNG frames, one optional WAV next to them) is checked when the upload completes and unpacked by its conversion job. The GUI's nginx passes `/api/v1/uploads` through unbuffered and without a body size limit; the mixer enforces the limits.
- **`POST /uploads/sequence`** (multipart: `name`, `sequence_fps`, `cut_frame`, `fit`, `files`): the frames are written to disk part by part, at most `FLOWXER_UPLOAD_LIMIT_GB` and 10 000 files. Frame names keep their padding, case (`.TGA`) and first number.
- **Legacy storage:** files in `storage/clips` and `storage/stingers/<id>/` are imported in the background after start and referenced in place (not copied; their conversions wait behind uploads). Deleting an imported item leaves the legacy file alone and it is not imported again.
- **Import dir** (`FLOWXER_IMPORT_DIR`): a file whose size held still for one scan (2 s) moves to `.processing/` and then into the library (a picture such as `.png`, `.jpg` or `.tif` as a still, a ZIP as a stinger, anything else as a clip); a file that cannot be imported moves to `.failed/`. A read-only import dir is copied from, and each file (name, size, mtime) is imported once — also when it failed.
- **Clip mezzanine:** the picture at the raster and rate as 10-bit 4:2:2, each chroma sample repeated to 4:4:4 (NVDEC decodes HEVC 4:4:4 but no 4:2:2; the engine takes the mean of each pair and gets the 4:2:2 picture back), HEVC Main 4:4:4 10, no B-frames and a key frame every second (a cue decodes at most one second). NVENC encodes it with constant QP 8; when NVENC cannot be opened (tried once, logged) x265 with CRF 8. On the lab's clips at 1080p50: NVENC 56–60 dB PSNR against the 4:2:2 source and 200–400 MB per minute, x265 56–58 dB and 150–320 MB; with heavy film grain 2.4 GB (NVENC) and 1.5 GB (x265) per minute at 48–52 dB (ProRes 422 HQ: 1.0–2.9 GB). The sound is padded or cut to exactly the clip's frames inside ffmpeg and stored next to it as raw stereo (or mono with `map_channels: 1`) float. A source with more channels is downmixed when its layout is known, else its first two channels are used.
- **Still:** its first picture at the raster (fit, fill or centre) as one limited-range BT.709 10-bit 4:2:2 v210 picture.
- **Stinger mezzanine** (NVDEC decodes no alpha): its colour as the clip mezzanine, its alpha as the luma of HEVC 4:2:0 10-bit (chroma 512, no range conversion), lossless with NVENC (x265 lossless without it), and its sound as a clip's: a sequence's WAV (aligned to frame 0) or a video's first audio stream; without one the stinger is silent. Sources: TGA or PNG sequences (8 or 16 bits, converted from R'G'B' with the BT.709 matrix) and video with alpha: ProRes 4444 (XQ), QuickTime Animation, PNG or RGBA in MOV, FFV1, VP9 with alpha in WebM (decoded with libvpx). A video without alpha (also HEVC or H.264 with Apple's alpha layer, whose alpha FFmpeg does not read) is an opaque stinger. Bars around a stinger that does not fill the raster are transparent. On the lab (1080p50): a graphic stinger takes about 3 MB per minute (alpha 0.2 MB), a full-frame photo stinger about 400 MB (alpha 7 MB); a stinger converted before (ProRes 4444) is converted again at the next start (the original stays).
- **Conversion:** ffmpeg runs under `nice -n 10` and `ionice -c3`, with a timeout that grows with the input length, and with at most the container's CPU limit less two cores (cgroup `cpu.max`; 8 threads without a limit; shared by `FLOWXER_CONVERT_CONCURRENCY` jobs): with NVENC for decoding and scaling (half each), with x265 for its workers (decoding and scaling then on one thread each); numpy's OpenBLAS (the sound's loop crossfade) runs on one thread (`OPENBLAS_NUM_THREADS=1` in the image). A conversion that fills the limit gets the whole container throttled, the engine with it. On the lab (A16, Xeon Gold 6136, 6-core limit, on air): NVENC about 90 frames/s, a minute of 1080p50 in about 35 s, beside the engine's mosaic encoder; x265 7–10 frames/s, a minute in 5–7 minutes. Cancelling a job (or deleting its item) kills its ffmpeg. A reconversion keeps the current mezzanine playable until the new one replaces it. A restart re-queues conversions it interrupted, and converts again a ready item whose files are missing, such as a clip with the ProRes mezzanine of earlier versions (that file is removed then; the original stays). A conversion job reports its progress (`GET /jobs`, `progress` 0–1) from ffmpeg's `-progress` output: a clip's picture from 10 % to 80 %, its sound 80 %, the thumbnail 90 %; a stinger's colour from 20 % (to 50 % with alpha, else 75 %), its alpha to 75 %.
- **Mixer state:** inputs and stinger slots that use the library are saved with `library_item_id` only; the mezzanine is looked up when the state (or `POST /config/import`) is loaded and again when a conversion ends. A clip assigned while it converts is black until then and is loaded (cued) when its conversion ends.

A stinger that is still converting never blocks the mixer: a stinger take falls back to a hard cut and logs a warning. When every stinger player is playing (or has just played), a take waits up to 0.5 s for one to be free, then plays; only if none is free by then is it a cut (`flowxer_stinger_fallbacks_total{reason="busy"}`).

## Stinger convention

Stingers play on the GPU: a stinger player (`FLOWXER_STINGER_PLAYERS`, default 1) decodes its colour and alpha in lockstep (NVDEC) and the ME keys it over its upstream keys, under its DSKs. A stinger taken at grain s shows its frame f in grain s + f; the background switches from the old Program to the new one at grain s + cut frame; after its last frame the buses swap (Program has shown the new source since the cut). The background sound crossfades (equal power) over the frames cut − 2 … cut + 1, and the stinger's sound is added to the ME's PGM audio, to the sample. Tally: both sources are red (LH) for the whole stinger, as for a mix.

What plays a stinger:

- a **Cut** to a source with an **auto-stinger** (`stinger_slot_id` on the source), and a take of such a source to Program;
- a **Cut** with **Wipe armed** (`POST /mixer/wipe`, FlowXer 14's meaning): the stinger slot by direction. The armed Wipe is used up by that Cut; a Cut that plays Preview's auto-stinger leaves it armed for the next Cut, as in FlowXer 14;
- **Auto** (or the T-bar, which does not scrub a stinger) when the ME's transition type is **STING**: its stinger slot, or the slot by direction;
- `POST /stinger/play` and `POST /mixer/take` with `transition: "stinger"` (and optionally `stinger_id`).

The slot by direction: going to a clip player uses an `in` slot, anything else an `out` slot (a `shared` slot serves both). Stinger player k holds the stinger that ME k's next Cut or Auto plays (Preview's auto-stinger, an armed Wipe's slot, the STING type's slot), so that take starts at the next grain. Another stinger (a direct take, a second ME) is loaded on demand: it starts when its first frame is decoded (a few grains; the engine counts the wait and cuts after 1 s).

Each stinger slot can use a **library item**, a legacy **TGA sequence** or a **video file**, and has a **cut frame**: the moment Program switches while the sting covers the picture (`cut_frame`, with `cut_ms` stored alongside). Legacy sequences under `storage/stingers/<id>/` are imported into the library at start:

```
frame_00000.tga
frame_00001.tga
...
stinger.json   # { "kind": "sequence", "frame_count", "cut_frame", "cut_ms", "pattern": "frame_%05d.tga" }
```

Video stingers live in the same tree (`kind: "video"` plus `media_path`). TGA or PNG folders and ZIP uploads are validated (natural sort, gap detection, Zip-Slip / bomb limits, alpha warning). `python scripts/generate_stinger.py --dest storage/stingers/replay-wipe` generates the bundled wipe.

## Tally and UMD (TSL 5.0)

FlowXer is a TSL UMD Protocol 5.0 **sender**. Each configured receiver gets one packet per bus or label change:

- **UDP** (default, TSL port **8900**): raw packet
- **TCP**: DLE `0xFE` / STX `0x02` wrapper with DLE stuffing

| TSL field | FlowXer |
|-----------|---------|
| SCREEN | per-receiver `screen` (default 0) |
| INDEX | source `slot` + `index_offset` |
| TEXT | source label (ASCII UMD) |
| RH tally | Program = red: visible on some ME's Program through any path (below) |
| LH tally | Preview = green |
| Text tally | Program red, Preview green, both amber |

Presets: Bitfocus Companion, Lawo VSM, BFE Commander, Riedel HI, or Custom.

**Red follows every path:** a source is red while it is visible on an ME's Program: as its Program (both sources while a mix runs), as the source of a luma key or a pip that is on (or fading, or flying off), and through every ME it shows (re-entry as Program or in a key), recursively: ME 1 with a pip of ME 2, which has a pip of Camera 3, lights Camera 3. Hidden layers do not count: a keyer at gain 0, a box at size 0, off screen or cropped away, and anything under an opaque pip that covers the whole raster (gain 1, no soft edge), the background included. The console's source tiles show the same per ME (`GET /console` → `on_air`: per ME id the visible sources and MEs); audio follows the same rules (AFV, `docs/engine-p5.md` "Keys and boxes"). The receivers get a packet on each change and again when a mix or a key's fade or fly ends (so the outgoing source goes off without an operator action). The raw tally export below stays per ME (the platform's calculator follows the re-entry INDEXes) and **unchanged** (a box off screen still lights its source there).

### Raw tally export (`FLOWXER_TALLY_TSL`)

For the platform's tally calculator, which works out from this raw tally what is on air further down. It is independent of the receivers above. `FLOWXER_TALLY_TSL=udp://host:port` (for example `udp://mxl-tally.mxl-platform.svc.cluster.local:8910`) or `tcp://host:port` (`8911`); unset, nothing is sent. The mixer process sends the packets itself, so their source address is the pod IP.

| TSL field | FlowXer |
|-----------|---------|
| SCREEN | ME: 1..4 in panel order, each tallied from its own Program and Preview |
| INDEX | The source's number (below); the Program of a higher ME that this ME can take (re-entry): 1000 + n |
| LH tally | Red: the source is on the ME's Program, or it is the outgoing or incoming source of a mix (Fade, Fade to Black, take with mix) while it runs |
| RH tally | Green: the source is on the ME's Preview |
| Text tally | Off |
| Brightness | 3 |
| TEXT | Source label, UTF-16LE (FLAGS bit 0) |

- **INDEX:** at start the sources are numbered from 0 in slot order: with the production structure from the environment `cam-1`..`cam-N`, `clip-1`..`clip-C`, `black`. An input added later (`POST /inputs`, only without that structure) gets the next number. A number stays with its input while the process runs; after a restart the numbering starts again from the slots.
- **Keys** light LH only (keys have no preview): a luma key's or a pip's source while the key is on air, fading off or flying off (a box fully off screen still lights it); each linear keyer's key input as INDEX `2000 + 100·n + k` (upstream keyer k of ME n) or `3000 + 100·n + k` (DSK k of ME n; ME 1 DSK 1 = 3101), TEXT its receiver label, red while the keyer is on air. Fade to Black lights nothing new.
- **When:** each update holds every source of every ME, the ones that are off included. It goes out on each change (also when a mix starts and ends) and every second. While Program is stopped every lamp is off; the last update at shutdown says so.
- **Transport:** a packet holds at most 2048 bytes; a larger update is split. Over TCP each packet is wrapped in DLE/STX … DLE/ETX with DLE stuffing. The host name is resolved when the export connects and again after a failure. After a failure the export tries again after 1, 2, 5, then every 10 s and logs at most once a minute. A thread of its own takes the state and sends.
- **Metrics** (`/metrics`, only while the export is set): `flowxer_tally_export_packets_total`, `flowxer_tally_export_send_errors_total` and `flowxer_tally_export_last_success_timestamp_seconds`.

## License

Apache-2.0 for the FlowXer source (same family as MXL). See `LICENSE` and the copyright appendix, plus `NOTICE` for third-party attribution.

The mixer image bundles third-party binaries that keep their own licences: MXL (Apache-2.0), MediaMTX (MIT), Ubuntu's FFmpeg packages (LGPL, built with GPL parts such as libx264 and libx265, which the media library runs as the separate `ffmpeg` program) and the NVIDIA CUDA runtime (NVIDIA's licence). Shipping the image does **not** turn FlowXer into GPL. Keep `LICENSE` and `NOTICE` with any binary or image distribution.

The operator GUI (`gui/`) is also Apache-2.0; React and Vite are MIT.

## Public / staging access

The HTTP control plane can start, stop, and take sources on-air. Do not put
an unauthenticated mixer on a public address.

1. Set `FLOWXER_API_TOKEN` in `.env` (see `.env.example`).
2. Open the console at `http://<host>:9620` and docs at `http://<host>:9620/docs`.
   API calls are `http://<host>:9620/api/v1/…`. Compose binds the mixer to
   `127.0.0.1:9610` only; nginx injects the token so the browser does not send it.
3. Health stays open at `/api/v1/health`. Direct calls to loopback **9610** still
   need `Authorization: Bearer …` (or `X-FlowXer-Token`) when the token is set.

## Branches and releases

FlowXer uses three long-lived branches. Version numbers are **(merges to main).(promotions to stage).(pushes to dev)** and live in `VERSION`.

```
dev  →  stage  →  main
code     test       container
```

| Branch | What you do | Automation |
|--------|-------------|------------|
| **dev** | Write code. Open PRs into `dev`. | Push increments the **patch** (code) counter. Tests run on the PR (`ci.yml`). |
| **stage** | Merge `dev` → `stage` when a slice is ready to verify. | Push increments the **minor** (stage) counter, runs pytest + typecheck, **AMWA NMOS testing** (`continue-on-error`), **builds containers without publishing**, and starts a **Cursor cloud agent** if `CURSOR_API_KEY` is set. Then merges `stage` back into `dev` so `VERSION` stays aligned. |
| **main** | **Manually** merge `stage` → `main` when you want a release. | Push increments the **major** (main) counter, tags `vX.Y.Z`, publishes `ghcr.io/<owner>/flowxer-vision-mixer` and `flowxer-gui`, then merges `main` → `stage` → `dev`. |

Example: `1.4.12` means 1 production release, 4 stage promotions, 12 coding pushes since the counters started.

Before bumping, each version job **reconciles** `VERSION` to the component-wise max of `origin/main`, `origin/stage`, and `origin/dev`, then increments the counter for that branch. That keeps the triple monotonic even if a branch was behind.

Do not merge `dev` straight to `main`. Stage is the test gate; main is the container release.

### Branch protection (required)

This repository has no GitHub branch protection yet. Configure it under **Settings → Rules → Rulesets** (or **Settings → Branches**) so the workflow cannot be skipped:

| Branch | Rules |
|--------|--------|
| **main** | Require a pull request. Require the `CI / Pytest` check. Do not allow force pushes or deletions. Restrict who can push to admins / the merge queue. |
| **stage** | Same as `main`. PRs should come from `dev`. |
| **dev** | Require a pull request. Require `CI / Pytest`. Do not allow force pushes or deletions. |

Without these rules, a direct push to `main` still publishes GHCR images.

### Cursor environment on stage

Cloud Agent setup lives in `.cursor/environment.json` (install script + mixer/GUI terminals). Commit that file; do not rely on a personal dashboard environment.

Two options for the **stage test agent** (pick one; both are valid):

1. **GitHub Actions secret `CURSOR_API_KEY`** — Settings → Secrets and variables → Actions. `stage.yml` calls `https://api.cursor.com/v1/agents` with `startingRef: stage`. Create the key at [cursor.com/dashboard](https://cursor.com/dashboard) → Integrations / Cloud Agents API.
2. **Cursor Automation** — [cursor.com/automations](https://cursor.com/automations), trigger **Push to branch: `stage`**. Prompt is in `.cursor/automations/stage-test.md`.

Rebuilding a Cursor *environment snapshot* on every stage push is the wrong lever (that snapshot is for agent VM setup). The automation/agent **uses** that environment to run the tests.
