#!/usr/bin/env bash
set -euo pipefail

mkdir -p "${FLOWXER_STORAGE_ROOT:-/storage}/clips" \
         "${FLOWXER_STORAGE_ROOT:-/storage}/stingers"

# flowxer binds FLOWXER_HOST:FLOWXER_PORT and NMOS_PORT itself: exit 75 when a port is
# taken, 78 on invalid settings, 143 after SIGTERM. It starts the media engine
# (flowxer-engine) as its child when Program goes on air.
exec flowxer
