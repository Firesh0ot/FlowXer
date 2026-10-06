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
