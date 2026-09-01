# PASAY rewrite quickstart

This guide covers the active rewrite authorized by [Issue #99](https://github.com/jhackuy/pasay-pm/issues/99). The implementation lives in `app/v1/`, `pasay-telegram-bot/`, `mini_app/`, and `cloudflare-worker/`.

## Prerequisites

- Python 3.11
- Node.js 20
- Docker with Compose
- PostgreSQL 16 (the Compose service is the simplest local option)

## Backend and database

Create a virtual environment and install the pinned requirements:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```

Start PostgreSQL 16 and export the local connection URL:

```bash
docker compose up -d db
export DATABASE_URL=postgresql+psycopg2://pasay_pm:change-me-strong-password@localhost:5432/pasay_pm
alembic upgrade head
```

Verify the application imports, then run it locally:

```bash
python -c "from app.main import app; assert app is not None"
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

In another terminal:

```bash
curl --fail http://127.0.0.1:8000/health
```

## Backend and Telegram tests

With PostgreSQL running and `DATABASE_URL` exported:

```bash
pytest -vv -s -o faulthandler_timeout=60 --durations=20 tests/test_v1_*.py
```

Run the rewrite Telegram regression gates:

```bash
cd pasay-telegram-bot
PYTHONPATH="$PWD" pytest -q tests/test_v1_adapter_regressions.py
PYTHONPATH="$PWD" pytest -q tests/test_group_silence_and_intent.py
PYTHONPATH="$PWD" pytest -q tests/test_ux_freeze_v1_polish_targeted.py -k "fixed_menu_is_3x2 or group_menu_is_3x2"
```

These tests do not require a live Telegram webhook. Production Telegram delivery is verified only by the four-stage deployment workflow.

## Mini App

Install dependencies, build, and run both smoke suites:

```bash
cd mini_app
npm ci
npm run build
npm run test:smoke
npx playwright install --with-deps chromium
npm run test:browser
```

For local development:

```bash
cd mini_app
npx vite --host 127.0.0.1
```

## Container smoke

From the repository root:

```bash
docker build -t pasay-rewrite .
```

## CI and delivery

The pull request has exactly three CI jobs in `.github/workflows/ci.yml`:

1. `pytest`
2. `fresh-postgres-alembic`
3. `build-core-smoke`

Production delivery is manual and protected. Do not run it from a workstation. Use the existing `rewrite-deploy` workflow only after review approval and production environment configuration. Its enforced order is:

```text
migrate -> deploy -> health -> telegram-webhook-smoke
```

Required production configuration is referenced directly by `.github/workflows/deploy.yml` and must be supplied through the protected GitHub environment; never commit secrets.
