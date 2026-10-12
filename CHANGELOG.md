# Changelog

Versions are set by the release workflows (`VERSION`). Details and reasons are
in `docs/platform-integration-plan.md` §8.

## Unreleased

### Added: the boxes' GPU time (P4 M4)

- `flowxer_engine_dme_ms{me,stat}` and `/status` `gpu_ms.dme`: per ME the resample of its boxes
  (their box layers are part of the ME's time). Grafana shows it next to the ME times.
- Measured with boxes on top of P3's worst case (`docs/engine-p4.md`, "M4 as built"): a half-size
  box adds about 0.31–0.33 ms on the lab A16 (≈ 0.14 ms estimated on an RTX A4000).
- Recommended at 1080p50 with 4 MEs at full load: at most 4 boxes per ME, up to 8 per ME on one or
  two MEs; a linear box costs as much as 4 pips. The 2 h soak with flies, ties, stingers and 849
  grabs: 0 skipped, 0 late, GPU memory flat, 0 failed grabs.

### Added: keyer ties and frame grab (P4 M3)

- Keyer ties: `PATCH /panels/{id}/transition` takes `ties` (the ME's upstream keyers) and
  `background` (BKGD). Cut, Auto and the T-bar take the tied keyers along: a fading keyer fades
  with the mix or wipe, a flying keyer flies its keyframes squeezed into the transition, with a
  stinger they cut at its cut point. BKGD off is key only: Cut cuts the tied keyers, Auto runs
  each one's own Auto. A tied keyer's tally follows it (on air while it goes off).
- Frame grab: `POST /library/grab {source, player?, name?}` grabs a live input, a clip player, or
  an ME's Program or Preview (`<panel>:pvw`) as it is in the next grain: a library still (source
  `grab`, tag `grab`) named `Grab <source> <date time>`, ready at once in the production raster
  with no conversion; `player` loads it into a clip player (the frame memory). Its lossless
  original (FFV1 in Matroska) and thumbnail follow in the background. The engine copies the
  picture with the grain's downloads (no stall) and writes it in its own thread.
- GUI: BKGD, KEY 1 … and GRAB in the transition section; right click on a source tile or the
  Preview or Program monitor grabs (also into a clip player); the `bus` widget has the tie buttons.
- Metrics: `flowxer_engine_grabs_total{source}`, `flowxer_engine_grab_failed_total`,
  `flowxer_engine_grab_ms`.

### Fixed: P3 trial on small

- `recommended_latency_grains` counts only what delivers: each input's video and audio while
  they run and the key flows in use while they run. Unrouted inputs and keys, key flows without
  grains, clip players and stinger players never count. An input's audio whose head stood still
  for a second is `no_signal` (like video) and adds no lag (a left flow's growing distance held
  the peak).
- The arrival and audio lag statistics (and so `recommended_latency_grains`) skip a source's
  start-up window and 2 s after it, at every (re)route: on small one 546 ms audio peak while the
  routes were applied at an Up held the recommendation at 28 grains although the steady state
  needs about 3.
- Arrival and audio lag samples later than 10 s are ignored: they are no arrivals but grains the
  clock cannot place (index 0 of a mirror flow that was just created showed 1.79e12 ms on small).
- The audio lag statistics also skip 2 s after an input's audio head starts to move again after
  standing still: on small (P4 trial) a mirror flow written only after the route's settle window
  caught up with one 541 ms peak and held the recommendation at 28 grains again.
- `recommended_latency_grains` covers the latest arrival of the last minute (`recent` in each
  arrival and audio lag statistic; `peak` stays the maximum since the route): with
  dev-14.21.55 small still showed one 525 ms audio peak of the mirrored cameras after an Up
  (recommendation 27) although the head never stood still. A transient now ages out after a
  minute; lateness that repeats within a minute still counts.
- GUI: a linear keyer without a key signal shows NO KEY in amber, in the keys widget too, so
  ON does not look like a key on air.

### Added: DME keyframes, presets, box editor and the dme widget (P4 M2)

- Keyframes: up to 8 per upstream keyer (`PUT /keyers/{id}/keyframes`): position, size (in log
  space), crop, border and shadow, each with its time in frames and an ease (linear, ease,
  ease-in, ease-out, hold). `POST /keyers/{id}/run` runs the box from where it is to a keyframe,
  back (`reverse`), in a loop (`wrap`, `pingpong`) or jumps (`frames: 0`); `/stop` holds it. The
  engine computes the box of every grain from the grain index: exact and reproducible.
- `fly`: the keyer's Auto flies the box on (keyframe 0 to the last) and off; an Auto during a fly
  turns back; cut on and off jump. The raw tally keeps a keyer on air while it fades or flies off.
- Presets (`POST /keyers/{id}/preset`, `GET /dme/presets`): four corner PiPs, split (keyers 1
  and 2), quad (keyers 1 to 4), full; on air the box flies there.
- `PUT /keyers/{id}/dme` replaces a box (PATCH merges); a pip keyer is always a box.
- GUI: the box editor over the active ME's Program (drag to move, corner handles to scale, ON AIR
  while on air), the keyframe list with RUN, STOP, loops and the presets in the keyer panel.
- Widget `dme` (`/widget/dme?me=<n>&keyer=<k>`): On/Off, Auto, Run back and forth, Stop, presets.
- Box buffers are allocated when a box is first enabled, not by the render thread.

### Added: DME boxes (P4 M1)

- An upstream keyer can put its key in a box: `dme` with position, size, crop, soft edge, an
  outer border (width, soft, colour, opacity) and a black rectangular shadow (offset, blur,
  opacity). The keyer mode `pip` shows any source the ME may take (an input, a clip player, a
  higher ME) opaque in a box. `PATCH /keyers/{id}` takes `mode: "pip"` and a partial `dme`;
  the GUI's keyer panel has the box fields. DSKs have no DME.
- Scaling is Catmull-Rom (anti-aliased when shrinking) with sub-pixel positions; chroma on its
  own 4:2:2 grid; a linear box is scaled premultiplied. GPU and CPU reference agree bit for bit.
- Tally: a pip's source lights LH while the keyer is on air, like a luma key's.
- Cost on the lab A16: about 0.29 ms per half-size 1080p box (resample and box layer).

### Added: metrics of P3 (M4)

- Stingers: `flowxer_engine_stingers_played_total`, `_stinger_wait_grains_total`, `_stinger_cuts_total`;
  per player `flowxer_engine_stinger_player_state{player,state}`, `_stinger_frames_decoded_total`,
  `_stinger_frames_late_total`, `_stinger_decode_ms`. The engine's `/status` gains `stingers.played`.
  Control plane: `flowxer_stinger_fallbacks_total{reason}` (`not_ready`, `busy`): takes it made a cut.
- A stinger take that finds every stinger player busy waits up to 0.5 s for one, then plays (back-to-back
  Autos both play); only then is it a cut.
- Keyers: `flowxer_engine_keyer_on_air{keyer,me,stage,mode}` and `flowxer_engine_keyer_key_running`.
- `flowxer_engine_input_upload_ms{input,stat}` for every input and key flow.
- `flowxer_transitions_total` counts `wipe` too.
- Grafana (`deploy/grafana/flowxer.json`): download, upload and per-ME GPU time, GPU memory and
  NVENC sessions, stingers, keyers on air and linear keys without a key signal.

### Changed: stingers play on the GPU (P3 M3)

- Stingers play again, on the GPU with alpha and sound, over the upstream keys and under the
  DSKs: an auto-stinger, an armed Wipe with a Cut, Auto with the transition type STING,
  `/stinger/play` and a take with `transition: "stinger"`. The background switches at the cut
  frame, its sound crossfades over 4 frames there, and the stinger's sound goes into PGM. Both
  sources are red on the tally. A stinger that still converts is a cut; with every stinger player busy
  a take waits up to 0.5 s for one (M4). The armed Wipe stays armed when a Cut plays Preview's auto-stinger (as in
  FlowXer 14).
- `FLOWXER_STINGER_PLAYERS` (0–4, default 1, at most the rendering MEs): stingers that can play
  at once; pinned like the panels (workspace `stinger_players`).
- Library: TGA or PNG sequences with one optional WAV (the sound), and video with alpha (ProRes
  4444, QuickTime Animation, PNG/RGBA in MOV, FFV1, VP9 with alpha). The stinger mezzanine is
  the clip mezzanine (colour), a lossless HEVC 4:2:0 alpha stream and the sound; stingers
  converted before (ProRes 4444) are converted again at the next start, their originals stay.
  Library items gain `has_sound`.

### Removed

- `FLOWXER_RAM_CLIP_MAX_S` and `FLOWXER_RAM_BUDGET_MB`: only the P2 stinger conversion used
  them (stingers now decode ahead on the GPU like clips). They are no longer read; a platform
  that sets them can drop them.

### Changed: the engine replaces the GStreamer mixer (P2)

FlowXer's media plane is its C++/CUDA engine (`engine/`), a child process of the
control plane. One image, one code path; an NVIDIA GPU is required
(`NVIDIA_DRIVER_CAPABILITIES=compute,video,utility`).

- Every ME renders all the time: PGM video + audio and PVW video per ME, written
  to MXL; `FLOWXER_RENDER_MES` (the first n MEs, default all panels),
  `FLOWXER_PVW_OUTPUTS` (default true), `FLOWXER_LATENCY` (grains, default 4).
- NMOS: senders `{ME} PGM Video`, `{ME} PGM Audio`, `{ME} PVW Video` per
  rendering ME, each with its own flow (ME n no longer advertises ME 1's flow);
  ME 1 PGM keeps its flow ids. Receiver `DSK 1 Key` (v210a). IS-05 opens and
  closes the engine's readers at run time.
- Sources in slot order (TSL INDEX): live inputs, clip players
  (`FLOWXER_CLIP_PLAYERS`, replaces `FLOWXER_TEST_SOURCES`), Black. Test, file
  and replay inputs become clip players (saved states and clients migrate).
  Re-entry: an ME takes the Program of a higher ME (`me-<n>`, INDEX 1000 + n).
- `GET /api/v1/engine` (timing, arrival lag per input, recommended latency),
  `GET /api/v1/preview` (the mosaic stream and its tile map),
  `POST /players/{id}/{action}`, `POST /keyers/{id}/auto`.
- Clip players: a library clip plays from its mezzanine, decoded on the GPU
  (NVDEC), with its sound in audio-follows-video; a still is uploaded once and
  held (frame memory). Loop and play on cut (it starts when the player goes to
  Program) on the input (`PATCH /inputs/{id}`); transport cue, play, pause,
  stop. An empty player is black. The colour bars ship as a library still.
- DSK 1 on ME 1: a linear key of the v210a flow routed to `DSK 1 Key`
  (e.g. mxl-browser-source in `v210a` key mode; straight fill) over ME 1 PGM
  only (keys have no preview: PVW shows only its source); on, off, auto. Its Auto rate is kept with the keyer (`PATCH /keyers/{id}`
  `rate_ms`; `POST /keyers/{id}/auto` without a duration uses it).
- GUI: every picture of the console (sources, each ME's Preview and Program) is
  a crop of the engine's one preview mosaic: one WHEP session per page, each
  picture a box of its tile's aspect ratio with the stream's video scaled and
  moved inside it (CSS transform; every browser, no `object-view-box`). The
  session connects again by itself, at once when no video came for a second:
  about 2.4 s after a MediaMTX restart (the engine checks its built-in
  MediaMTX every 200 ms and its publisher retries within 0.5 s). Clip players get their library
  clip or still in the tile's header, the position and remaining time over the
  picture and a transport bar (cue, play, pause, stop, loop, play on cut; a
  still is a frame memory); DSK 1 gets Auto and its rate; the clip library
  holds stills.
  The per-source WebRTC and JPEG previews are gone.
- Operator-screen widget `bus` (Program and Preview rows with Cut and Auto of
  one ME), served by flowxer-gui: `GET /widgets` (from `GET /api/v1/widgets`)
  and `GET /widget/bus?me=<n>[&theme=dark|light|transparent]`, framed only by
  the origins in `WIDGET_FRAME_ANCESTORS` (gui container, default `'self'`),
  with `widget-ready` and `widget-size` messages.
- Library: a clip's mezzanine is HEVC 4:4:4 10-bit (a key frame every second,
  no B-frames; NVDEC decodes no 4:2:2 HEVC) next to a raw float32 sound file,
  instead of ProRes 422 HQ. NVENC encodes it with constant QP 8 (about 90
  frames/s on air: a minute of clip in about 35 s); without NVENC x265 with
  CRF 8 (7–10 frames/s), logged once. The 4:2:2 picture goes in with each
  chroma sample repeated, so the engine gets the 4:2:2 picture back. At least
  56 dB PSNR, about 200–400 MB per minute at 1080p50 (x265: 150–320 MB), up
  to 2.4 GB with heavy grain (x265: 1.5 GB). Stills
  (uploads, and pictures in the import dir) become one v210 picture. A clip
  converted before is converted again at the next start (the original stays,
  the ProRes mezzanine goes). A conversion uses the container's CPU limit less
  two cores (8 threads without a limit): for decoding and scaling with NVENC,
  for x265's workers otherwise (decoding and scaling then on one thread), and
  numpy's OpenBLAS runs on one thread (`OPENBLAS_NUM_THREADS=1`): a conversion
  that filled the limit got the container throttled and the engine skipped
  grains.
- Metrics for the engine, the inputs (arrival lag, late and missing grains
  per FlowXer input), GPU time per stage and per ME, the clip players
  (`flowxer_engine_player_frames_decoded_total`, `_decode_ms`), the mosaic and
  its MediaMTX (`flowxer_engine_preview_mediamtx_running`, `_restarts_total`)
  and the library (`flowxer_library_jobs`, `flowxer_library_conversions_total`,
  `flowxer_library_last_conversion_seconds`, `flowxer_library_nvenc`); the list
  is in the README (Metrics). `flowxer_webrtc_peers` is gone; the Grafana
  dashboard shows the engine's fps, skipped and late grains and GPU time
  instead.
- Upgrade from 14.x with the same `/config`: ME 1 PGM keeps its flow ids, the
  live inputs keep their IS-05 activations and tally INDEX; test sources become
  empty clip players, the Replay input goes, DSK 1 needs a v210a key (README,
  Upgrading from FlowXer 14).
- Removed: GStreamer, CEF, aiortc; `FLOWXER_GPU`, `FLOWXER_GST_MODE`,
  `FLOWXER_MONITOR_FPS`, `FLOWXER_WEBRTC_*`, `FLOWXER_OVERLAY_URL`; the routes
  `/overlay`, `/replay/*`, `/stinger/tick`, `/preview/jpeg/*`,
  `/webrtc/whep/*`; the `/graphics` sample page. Stingers come back in P3: a
  stinger take, an auto-stinger and an armed Wipe cut.

### Added

- `FLOWXER_TALLY_TSL=udp://host:port` (or `tcp://`): the raw tally of every ME
  for the platform's tally calculator, TSL UMD 5.0 from the mixer process.
  SCREEN is the ME, INDEX the input (slot order at start, stable while the
  process runs), LH red on Program and for both sources of a running mix or
  stinger, RH green on Preview, the label in UTF-16. Sent on each change and
  every second; unset, nothing is sent. `/metrics` counts its packets and
  failed sends and gives the time of the last good send.
- Production structure from the environment (plan §3.10): `FLOWXER_FORMAT`,
  `FLOWXER_LIVE_INPUTS`, `FLOWXER_INPUT_LABELS`, `FLOWXER_TEST_SOURCES` and
  `FLOWXER_PANELS` win over the saved state at every start; routes, keyers,
  stingers and tally stay. The API refuses to change them (409), `GET /console`
  lists them in `pinned` and the GUI greys them out.
  `FLOWXER_PROGRAM_AUTOSTART` starts Program at process start. Unset, nothing
  changes.
- Optional GPU media path, `FLOWXER_GPU=auto|on|off` (default `off`): on an
  NVIDIA GPU each source is uploaded once, unpacked from v210 by a shader,
  composited by `glvideomixerelement` (cut, mix, stingers, keyer) and Program
  is packed to v210 and downloaded once; the GUI monitor pictures are scaled
  on the GPU. `auto` takes it when the GPU passes a test frame at start and
  logs why not otherwise, `on` exits with 78 without it, `off` is the CPU
  path unchanged. `flowxer_info{media_path}` and `media_path` /
  `media_path_reason` in `GET /api/v1/mixer` name the path. The image adds
  `gstreamer1.0-gl` and the glvnd EGL vendor file for `libEGL_nvidia`.
- The GUI monitors (JPEG and WebRTC) show the pipeline's pictures: each
  source and, for the main panel, the mixed Program (`FLOWXER_MONITOR_FPS`,
  default 10). They were generated cards.
- Fade and Fade to Black dissolve picture and sound (they were cuts);
  `POST /mixer/take` with `transition: mix` uses its `duration_ms`.
- CI job `Pytest (GStreamer)`: the media pipeline on the GStreamer of the
  image.
- Platform env names (`MXL_*`, `NMOS_*`, `SHUTDOWN_TIMEOUT_S`) next to the
  `FLOWXER_` names; the platform name wins.
- Saved state in `FLOWXER_STATE_DIR` (default `/config`), restored on start,
  receiver connections included. `GET /api/v1/config/export` and
  `POST /api/v1/config/import`.
- `NMOS_LABEL` and `NMOS_TAGS` on the node and the device.
- Exit codes 75 (port taken), 78 (invalid configuration) and 143 (SIGTERM).
- SIGTERM deregisters the node and, with `MXL_CLEANUP_ON_EXIT`, removes the
  own output domain.
- `deploy/kubernetes/flowxer-pod-network.yaml`; OCI labels `source` and
  `revision`.
- Media library for clips and stingers: chunked upload (or a TGA folder,
  ZIP or the watched `FLOWXER_IMPORT_DIR`), background conversion with
  ffmpeg to a ProRes mezzanine, `/library`, `/uploads` and `/jobs` in the
  API and File → Clip / Stinger library… in the GUI. Inputs and stinger
  slots take a `library_item_id`. Legacy `storage/clips` and
  `storage/stingers` are imported in the background and referenced in place.
  Settings `FLOWXER_LIBRARY_DIR`, `FLOWXER_IMPORT_DIR`,
  `FLOWXER_CONVERT_CONCURRENCY`, `FLOWXER_UPLOAD_LIMIT_GB`. Review fixes
  before the release: a restart or a config import keeps library inputs and
  slots (they are saved by item id; the whole state was dropped); a stinger
  that is still converting hard-cuts instead of recursing into a 500; the
  sound is conformed by ffmpeg, not in the mixer process (about 92 MB per
  stereo minute); the GUI proxy passes upload chunks (nginx answered 413);
  chunk sizes, the upload limit and the free disk space are enforced and
  completing an upload is a rename; a file in the import dir that fails is
  moved to `.failed/` instead of being retried every 4 s; the legacy import
  no longer copies everything before the API starts; ffmpeg runs at low
  CPU and I/O priority and is killed on cancel. Found on the lab: GStreamer
  read the mezzanine's little-endian float sound as big-endian (silence) and
  cannot play more than two PCM channels from MOV, so clip sound is now stereo
  (or mono) big-endian float and stinger mezzanines carry no sound; the queue
  in front of a file input's video (`FLOWXER_PREROLL_FRAMES`, which did
  nothing) could take the sound pad and stop the input, and is gone.

### Changed

- Test sources, Black and a Replay or file input without a clip are made
  once and repeated live (`imagefreeze`); on the GPU path they are uploaded
  once. Each of them drew every 1080p50 frame on the CPU (about half a core
  on the platform) and uploaded it (12–19 % of the GL thread). The colour bars
  no longer carry the running time. Their GUI monitor picture is taken once
  a second.
- GPU path: the HTML keyer's BGRA frames go up as RGBA bytes and its shader
  swaps red and blue (no `glcolorconvert` pass: 7–9 % of the GL thread).
- WebRTC previews send `FLOWXER_MONITOR_FPS` frames per second (the rate
  the monitor pictures change) instead of aiortc's 30; every peer encoded
  each picture three times.
- `flowxer_frames_dropped_total` counts the Program frames the mixer skipped
  because it was late (it was always 0).
- A stinger (Wipe, auto-stinger, stinger take) on ME 2..4 switches that ME at
  once without playing the media. It used to play over ME 1 and switch ME 1's
  rendered Program at its cut (ME 1's panel and tally kept the old source),
  and it ended a stinger running on ME 1.
- The compositor works in AYUV instead of BGRA (no RGB round trip for
  Program).
- `flowxer_frames_rendered_total` counts Program frames at the video output.
- The registry client registers once and then heartbeats, instead of posting
  every resource every 5 s.
- `/readyz` is 503 until the node is registered (with a registry configured).
- Program flow ids include the NMOS seed, so they change once for existing
  deployments.
- The image starts `flowxer` instead of the uvicorn CLI.

### Fixed (8.15.31 on the lab)

- Routing an input over IS-05 whose kind was changed on air (a test source made
  `mxl_live` while Program runs) no longer stops its source and hangs the API.
  The running pipeline still has the earlier source (the new kind applies at
  the next start); the retarget took that source to NULL, failed on the missing
  `video-flow-id`/`audio-flow-id` property and left it stopped, the
  input-selectors then held the other inputs' streaming threads, and the next
  route blocked: REST, NMOS and GUI stopped answering while Program ran on.
  Only an `mxlsrc` is retargeted now, it always returns to PLAYING, and the
  log says that the route applies at the next start.

### Fixed (platform)

- Program fell behind real time on a busy node (platform vmix, 13.20.43, GPU
  path): the mixer made 48.6–48.8 frames/s, its timestamps ran on without
  gaps, and `mxlsink` wrote each frame at the MXL index of its timestamp, so
  Program fell 1.4 grains per second further behind TAI (5–8 s after a few
  minutes; the multiviewer froze). A stall had the same effect for good
  (lab: 1.4–2.6 s behind after 10 WebRTC previews connected). The Program sink
  now measures each frame's lateness (`qos=true`); a frame more than two
  frames late makes the compositor skip to real time plus three frames, so
  Program keeps to the TAI timeline with gaps and the compositor waits for
  its inputs again. Skipped frames are counted in
  `flowxer_frames_dropped_total` and logged once per burst. Program audio
  was not late and is unchanged. GStreamer's own QoS (with or without
  `max-lateness`) was tried first: after a 1 s stall it kept the compositor
  at the edge of real time and Program at 1 to 33 frames/s.
- Program did not start after a node reboot (platform vmix): the fabrics
  agent had not recreated its mirror domains yet, `mxlsrc` failed on the
  missing domain directory, the pipeline did not reach PLAYING and
  `FLOWXER_PROGRAM_AUTOSTART` gave up after one try (state `error`, 0 fps,
  until an operator started Program). An input routed to a domain that does
  not exist now waits in the own output domain (black and silence) and reads
  its flow once the domain appears (checked every 2 s); Program runs
  throughout. The autostart tries again after 2, 5, 10, then every 30 s until
  Program runs or an operator starts or stops it, and logs each attempt.
- After a start that failed, the next start reported an old error
  ("asrc_cam-1: GStreamer error: state change failed and some element failed
  to post a proper error message …"): the pipeline that did not start kept its
  bus watch, and the next pipeline's main loop delivered its errors (and
  restarted its failed source). It is now taken to NULL and its watch
  removed; the start error names the failed element.
- The mixer stopped with exit 132 every 10–25 minutes on the platform's vmix
  (8 inputs, GPU path, keyer on): SIGILL in `libcef.so`, thread `MemoryInfra`.
  CEF reads the malloc totals with glibc's legacy `mallinfo()`, whose `int`
  fields wrap once the process holds 2 GiB or more from malloc, and Chromium's
  memory metrics (a memory dump at random times, every 30 min on average)
  check them and abort. With that layout the mixer holds 1.6–1.8 GiB from
  malloc, mostly CPU copies of GStreamer GL textures that are never touched,
  so RSS stays far lower. The image now preloads a `mallinfo()` that caps the
  values instead (`docker/mallinfo-shim.c`, `LD_PRELOAD`).
- Program audio stayed silent after a start (platform: every start), and now
  and then Program video stopped after 2 frames. Right after a start the
  audiomixer and the compositor can start their output over at 0 (audio
  `[0, 0.01)` then `[0, 0.02)`); mxlsink cannot write behind what it wrote,
  failed without a message, and the error stopped that essence for good. A
  Program buffer that goes back in time is now dropped before mxlsink
  (`flowxer_program_buffers_dropped_total{essence}`, a log line).
- An IS-05 route could hang the node: GET /mixer, the NMOS API and stop timed
  out until the pod was restarted. A retargeted source's allocation query
  waited in its queue, whose thread waited in an input-selector whose active
  input had stopped; the next route of that source then blocked for good under
  the NMOS lock. A source restart now flushes the source's branch first and
  waits at most 10 s (stop at most 15 s), then logs and reports an error
  instead of holding the lock. Unrouting gives mxlsrc the unrouted flow id
  (`00000000-…`) instead of an empty one. Each route is logged.
- An input froze on its last picture when its mxlsrc failed: mxlsrc stops for
  good on a grain marked invalid, which an ST 2110 gateway writes for an
  incomplete frame. A failed MXL source is started again after 1 s (longer
  when it fails again at once), `flowxer_input_restarts_total{input,essence}`
  counts it and `GET /mixer` `error` names it.
- `/livez` fails (503) when an IS-05 activation, Program start or stop or a
  source restart has not finished for 60 s, so Kubernetes restarts a hung pod.
  `flowxer_control_plane_busy_seconds` shows the oldest running one.
- Program keeps running when an input delivers nothing (an MXL flow that is
  missing, in another domain than the route says, or a frozen mirror). The
  input's GUI monitor never got a first frame, so the pipeline never reached
  PLAYING and Program stopped after a few frames while the mixer said
  `running` (small platform: 2–6 frames in 10 h). The GUI monitor taps no
  longer wait for a first buffer (`async=false`), and an `mxl_live` essence
  without a route reads a flow id that never exists (nil UUID) instead of
  `UNBOUND`, which made `mxlsrc` fail at start and held the pipeline the same
  way. Such an input shows black and silence.
- A route whose domain does not hold the flow (IS-05 and REST default a
  missing domain to the own output domain) reads the flow from the domain
  below the MXL root that has it, a local domain before a fabrics mirror.
- The status reports a Program without new frames for 3 s in `error`
  ("Program renders no frames …", the state stays `running` = on air), the log
  says it once, and `flowxer_program_stalled` is 1.
- The source ⚙ in the GUI sends the flows and the group hint only when they
  changed. Saving the auto-stinger of a live input without a route was 422,
  and saving a routed one set its domain back to the output domain.
- `GET /api/v1/preview/jpeg/{stream_id}` answers 404 with the accepted forms
  (`source:<input id>`, `panel:<panel id>:pgm|pvw`) for any other name or an
  input or panel that does not exist. `panel:program` was a 500
  (`not enough values to unpack`), `program` a NO SIGNAL picture. A bare input
  id still works; a WebRTC monitor of an unknown name shows NO SIGNAL.
- The NMOS node answers paths with a doubled slash. The device's IS-05 control
  href ends in `/`, and a controller that appends `/single/...` to it asked for
  `/x-nmos/connection/v1.2//single/receivers/<id>/active` and got 404 for every
  receiver; nmos-cpp nodes accept that form.

### Fixed (CI)

- The merge-back after a release (`main` → `stage` → `dev`) failed when the
  merge conflicted in the version files: `reconcile-refs` read `VERSION` with
  the conflict markers in it. It now resolves conflicts that only differ in the
  version line in `VERSION`, `pyproject.toml` and `src/flowxer/__init__.py`
  before it reconciles; any other conflict there still stops the merge-back.

### Fixed (lab run on real hardware)

- The mixer silently fell back to simulate when the pipeline did not parse
  (`zero-size-is-unconfigured` on GStreamer 1.24); a failed pipeline is now
  `state=error` and `/mixer/start` returns 409.
- `timeoverlay` missing from the image; TGA stinger caps without a frame rate.
- The NMOS refresh loop restarted live inputs four times a second.
- An invalid `PATCH /inputs/{id}` returned 500 instead of 422.
- A stinger played only once (at pipeline start); every playback now decodes
  it again and its frames drive the cut.
- An MXL audio flow with more channels than Program (16 on the test player)
  was downmixed into Program; Program now gets its first channels.
