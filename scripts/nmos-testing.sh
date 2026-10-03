#!/usr/bin/env bash
# Run AMWA NMOS Testing Tool suites against a FlowXer Node API.
# Default suites: IS-04-01, IS-05-01, IS-05-02.
# Optional: NMOS_TESTING_SUITES=IS-04-01,IS-05-01,IS-05-02,BCP-007-03-01
#
# Usage:
#   FLOWXER_NMOS_ENABLE=true FLOWXER_NMOS_BIND=true FLOWXER_SIMULATE=true \
#     uvicorn flowxer.app:create_app --factory --host 127.0.0.1 --port 9610 &
#   bash scripts/nmos-testing.sh http://127.0.0.1:3252
#
# Non-interactive CLI: https://specs.amwa.tv/nmos-testing/branches/master/docs/2.5._Usage_-_Non-Interactive_Mode.html
set -euo pipefail

NODE_HREF="${1:-http://127.0.0.1:3252}"
IMAGE="${NMOS_TESTING_IMAGE:-amwa/nmos-testing:latest}"
SUITES="${NMOS_TESTING_SUITES:-IS-04-01,IS-05-01,IS-05-02}"
OUT="${NMOS_TESTING_OUT:-./nmos-testing-results}"
SELECTION="${NMOS_TESTING_SELECTION:-all}"
# FlowXer does not browse DNS-SD. Ignore IS-04-01 tests that require the node
# to appear in the testing tool's mock registry.
IGNORE="${NMOS_TESTING_IGNORE:-test_04 test_07 test_08 test_09 test_10}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
USERCONFIG="${NMOS_TESTING_USERCONFIG:-${SCRIPT_DIR}/nmos-testing-userconfig.py}"

if [[ "${NODE_HREF}" != *"://"* ]]; then
  NODE_HREF="http://${NODE_HREF}"
fi

HOST="$(python3 -c "from urllib.parse import urlparse; import sys; u=urlparse(sys.argv[1]); print(u.hostname or '127.0.0.1')" "${NODE_HREF}")"
PORT="$(python3 -c "from urllib.parse import urlparse; import sys; u=urlparse(sys.argv[1]); print(u.port or 3252)" "${NODE_HREF}")"

mkdir -p "${OUT}"
OUT_ABS="$(cd "${OUT}" && pwd)"

echo "Node: ${NODE_HREF} (${HOST}:${PORT})"
echo "Suites: ${SUITES}"
echo "Image: ${IMAGE}"
echo "Results: ${OUT_ABS}"

manual_hint() {
  echo "Manual: python3 nmos-test.py suite IS-04-01 --host ${HOST} --port ${PORT} --version v1.3 --output results.xml" >&2
  echo "Docs: https://specs.amwa.tv/nmos-testing/" >&2
}

suite_args() {
  local suite="$1"
  case "${suite}" in
    IS-04-01|IS-04-03)
      echo --host "${HOST}" --port "${PORT}" --version v1.3
      ;;
    IS-05-01)
      echo --host "${HOST}" --port "${PORT}" --version v1.2
      ;;
    IS-05-02|BCP-007-03-01)
      echo --host "${HOST}" "${HOST}" --port "${PORT}" "${PORT}" --version v1.3 v1.2
      ;;
    *)
      echo --host "${HOST}" --port "${PORT}" --version v1.3
      ;;
  esac
}

if ! command -v docker >/dev/null 2>&1; then
  echo "docker is required to pull ${IMAGE}" >&2
  manual_hint
  exit 2
fi

worst=0
IFS=',' read -r -a suite_list <<< "${SUITES}"
for suite in "${suite_list[@]}"; do
  suite="$(echo "${suite}" | xargs)"
  [[ -z "${suite}" ]] && continue
  echo "=== ${suite} ==="
  # Override ENTRYPOINT (run_nmos_testing.sh starts the UI). Non-interactive
  # python3 nmos-test.py suite … as documented by AMWA.
  ignore_args=()
  if [[ -n "${IGNORE}" ]]; then
    # shellcheck disable=SC2206
    ignore_args=(--ignore ${IGNORE})
  fi
  set +e
  # shellcheck disable=SC2046
  docker run --rm --network host \
    --entrypoint python3 \
    -v "${OUT_ABS}:/results" \
    -v "${USERCONFIG}:/config/UserConfig.py:ro" \
    "${IMAGE}" \
    nmos-test.py suite "${suite}" \
      --selection "${SELECTION}" \
      $(suite_args "${suite}") \
      "${ignore_args[@]}" \
      --output "/results/${suite}.xml"
  status=$?
  set -e
  echo "${suite} exit ${status}" | tee -a "${OUT_ABS}/summary.txt"
  if [[ "${status}" -gt "${worst}" ]]; then
    worst="${status}"
  fi
done

if [[ "${worst}" -ne 0 ]]; then
  echo "One or more suites reported failures (worst exit ${worst}). See ${OUT_ABS}." >&2
  echo "Ignored (do not affect exit): ${IGNORE}." >&2
fi
exit "${worst}"
