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
if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install -e '.[dev]'

# Operator GUI (Vite + React). Use the lockfile so Cloud Agent installs stay deterministic.
(cd gui && npm ci)

echo "FlowXer environment ready."
