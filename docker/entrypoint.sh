#!/usr/bin/env bash
set -euo pipefail

mkdir -p "${FLOWXER_MXL_DOMAIN:-/mxl-domain}" \
         "${FLOWXER_STORAGE_ROOT:-/storage}/clips" \
         "${FLOWXER_STORAGE_ROOT:-/storage}/stingers" \
         "${FLOWXER_STORAGE_ROOT:-/storage}/graphics"

if [[ ! -f "${FLOWXER_MXL_DOMAIN:-/mxl-domain}/domain_def.json" ]]; then
  cp /app/configs/domain_def.json "${FLOWXER_MXL_DOMAIN:-/mxl-domain}/domain_def.json"
fi

if [[ -d /opt/mxl/lib ]]; then
  ldconfig /opt/mxl/lib || true
fi

exec uvicorn flowxer.app:create_app --factory --host "${FLOWXER_HOST:-0.0.0.0}" --port "${FLOWXER_PORT:-9610}"
