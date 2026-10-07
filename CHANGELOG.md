# Changelog

Versions are set by the release workflows (`VERSION`). Details and reasons are
in `docs/platform-integration-plan.md` §8.

## Unreleased

### Added

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

- Program keeps running when an input delivers nothing (an MXL flow that is
  missing, in another domain than the route says, or a frozen mirror). The
  input's GUI monitor never got a first frame, so the pipeline never reached
  PLAYING and Program stopped after a few frames while the mixer said
  `running` (small platform: 2–6 frames in 10 h). The pipeline is now set up as
  the live pipeline it is: the GUI monitor taps and the Program/sound sinks do
  not wait for a first buffer (`async=false`), and the compositor and the
  audiomixer always mix on time (`force-live=true`) instead of waiting for a
  pad without data. Such an input shows black and silence.
- A route whose domain does not hold the flow (IS-05 and REST default a
  missing domain to the own output domain) reads the flow from the domain
  below the MXL root that has it, a local domain before a fabrics mirror.
- The status reports a Program without new frames for 3 s in `error`
  ("Program renders no frames …", the state stays `running` = on air), the log
  says it once, and `flowxer_program_stalled` is 1.
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
