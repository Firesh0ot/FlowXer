# Agent notes

## Cursor Cloud specific instructions

This repo’s Cloud Agent environment is defined in `.cursor/environment.json`.

- Install is `bash .cursor/install.sh` (Python venv + `pip install -e '.[dev]'` + `gui` `npm ci`).
- `mixer-api` starts the FastAPI app in **simulate** mode on port **9610** (`FLOWXER_SIMULATE=true`). Do not expect GStreamer or a real MXL domain.
- `gui` is the operator deck on port **9620** and proxies `/api` to the mixer.
- After install: `source .venv/bin/activate && pytest -q`. In `gui/`: `npx tsc --noEmit`.
- Prefer the committed environment file over a personal dashboard environment when starting new agents. Start the agent from a revision that contains `.cursor/environment.json` (`dev` / `stage`, or `main` after the next release).

Do not bump `VERSION` by hand on `dev` / `stage` / `main` — GitHub Actions does that on push. Use `python3 scripts/bump_version.py reconcile-refs` only when repairing drift.
