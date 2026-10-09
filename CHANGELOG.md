# Changelog

Versions are set by the release workflows (`VERSION`). Details and reasons are
in `docs/platform-integration-plan.md` §8.

## Unreleased

### Added

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
