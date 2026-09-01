# CI workflow change for the Playwright browser smoke

This directory contains the build-core-smoke job edit that the
pasay-opencode-bot GitHub App could not push because it lacks
the `workflows` permission.

## File

`.github/workflows/ci.yml` — replace the existing `build-core-smoke` job
with the version in `ci.yml.patch`.

## Why it is not in the pushed commit

The push error was:

> refusing to allow a GitHub App to create or update workflow
> `.github/workflows/ci.yml` without `workflows` permission

The implementation commit (`8cf3d6d`) was pushed with the workflow edit
removed from staging, and the working tree still carries the change.

## How the Owner can apply it

Three options:

1. **Commit it locally with a workflows-authorized identity:**

   ```bash
   git add .github/workflows/ci.yml
   git commit -m "ci(rewrite): run Mini App Playwright browser smoke"
   git push origin opencode/issue99-20260829042355
   ```

2. **Or grant `workflows: write` to the OpenCode App token.** The
   `actions/create-github-app-token` step in
   `.github/workflows/opencode.yml` should be augmented with
   `permission-workflows: write`. That makes this kind of push work
   for every subsequent commit.

3. **Or apply the diff manually** (lines from `ci.yml.patch`).

## What the patch does

After `npm run build` succeeds inside `build-core-smoke`:

1. Installs Playwright Chromium (`npx --yes playwright@1.48.2 install --with-deps chromium`).
2. Runs `npm run test:browser`, which boots a Python harness
   (`mini_app/tests/serve_app.py`) on a free port, then runs the
   Playwright browser smoke (`mini_app/tests/browser_smoke.mjs`) that
   drives an Owner core flow through the real bundle against a real
   FastAPI V1 instance.

CI job count remains exactly three:
- `pytest`
- `fresh-postgres-alembic`
- `build-core-smoke`

The new browser smoke is added as a step inside `build-core-smoke`,
not as a separate job.

## Verification locally before applying

```bash
cd mini_app
npm run build
npm test    # runs both test:smoke (JSDOM, 8/8) and test:browser (Playwright, 9/9)
```
