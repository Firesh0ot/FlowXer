# Stage test — Cursor Automation

Create this once at https://cursor.com/automations (or with `/automate`).

## Trigger

**Push to branch** → `stage` on `Firesh0ot/FlowXer`.

Optional extra trigger: **Workflow run completed** for `stage.yml` (file name `stage.yml`) when you want the agent to triage CI failures.

## Prompt

```
You are verifying FlowXer after a promotion to stage. Do not change production code unless tests fail and the fix is obvious.

1. pip install -e '.[dev]' and run pytest -q.
2. In gui/, npm ci && npx tsc --noEmit.
3. If the operator GUI can be started, exercise Cut / Fade / Wipe / stinger settings.
4. Comment on the GitHub commit or open a short report: pass/fail, version from the VERSION file, and anything that broke.

Do not merge to main.
```

## Tools

Enable **Comment on pull request** if promotions go through a PR. Computer use is useful for the GUI.

## GitHub Actions fallback

`.github/workflows/stage.yml` always runs pytest and a Docker build. If repository secret `CURSOR_API_KEY` is set, that workflow also launches a cloud agent via `https://api.cursor.com/v1/agents`.
