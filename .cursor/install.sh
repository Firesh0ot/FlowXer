#!/usr/bin/env bash
set -euo pipefail

# FlowXer Cloud Agent bootstrap. Idempotent: safe to re-run.
# Prepares the Python control-plane venv and the operator GUI dependencies.

cd "$(git rev-parse --show-toplevel 2>/dev/null || echo /workspace)"

# The default image ships Python 3.12 without ensurepip/venv support.
if ! python3 -c "import ensurepip" >/dev/null 2>&1; then
  sudo apt-get update -qq
  sudo apt-get install -y -qq python3.12-venv
fi

# Python control plane (FastAPI + engine) with dev/test extras.
python3 -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate
pip install --upgrade pip
pip install -e '.[dev]'

# Operator GUI (Vite + React).
(cd gui && npm install)

echo "FlowXer environment ready."
