#!/usr/bin/env bash
set -euo pipefail

mkdir -p "${FLOWXER_MXL_DOMAIN:-/mxl-domain}" \
         "${FLOWXER_STORAGE_ROOT:-/storage}/clips" \
         "${FLOWXER_STORAGE_ROOT:-/storage}/stingers" \
         "${FLOWXER_STORAGE_ROOT:-/storage}/graphics"

if [[ ! -f "${FLOWXER_MXL_DOMAIN:-/mxl-domain}/domain_def.json" ]]; then
  cp /app/configs/domain_def.json "${FLOWXER_MXL_DOMAIN:-/mxl-domain}/domain_def.json"
fi

if [[ -d /opt/mxl/lib || -d /opt/gstcef ]]; then
  ldconfig || true
fi

# cefsrc needs a display. Start a private Xvfb when the host did not provide one.
if [[ -z "${DISPLAY:-}" ]] && command -v Xvfb >/dev/null 2>&1; then
  export DISPLAY="${FLOWXER_DISPLAY:-:99}"
  Xvfb "${DISPLAY}" -screen 0 1920x1080x24 -nolisten tcp >/tmp/xvfb.log 2>&1 &
fi
mkdir -p "${GST_CEF_CACHE_LOCATION:-/tmp/cef-cache}"

exec uvicorn flowxer.app:create_app --factory --host "${FLOWXER_HOST:-0.0.0.0}" --port "${FLOWXER_PORT:-9610}"
