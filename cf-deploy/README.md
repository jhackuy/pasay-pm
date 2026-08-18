# PASay Cloudflare Deployment Foundation

Scope: PASAY-CLOUDFLARE-ENV-001.

This directory holds the **deployment-only** templates and docs for the Pasay
AI edge layer. **No production Pasay business code lives here**; this is
strictly the infrastructure seam between the current FastAPI / Telegram Bot /
PostgreSQL stack and Cloudflare.

```
cf-deploy/
  README.md                  # this file (topology + readiness notes)
  CAPABILITY_MAPPING.md      # which component maps to which CF product
  ENVIRONMENTS.md            # dev/staging/prod env & secret classification
  secrets.example.json       # shape only — NEVER real values
  wrangler.example.toml      # example future prod-side wrangler config
.github/workflows/
  cf-smoke-deploy.yml        # CI: deploy smoke Worker on push to main
```

## Target topology (intended, not yet active)

```
Users / Telegram / Mini App
            |
       Cloudflare Edge
         /        \
  Mini App       API routing
  Pages/Worker      |
                   v
            Pasay Backend
              (FastAPI)
                   |
                   v
            Neon PostgreSQL
```

See `CAPABILITY_MAPPING.md` for which component maps to which Cloudflare
product and which components are intentionally NOT on Cloudflare yet.

## Current state (after PASAY-CLOUDFLARE-ENV-001)

- Smoke Worker: `cf-smoke/` — deployed to a Cloudflare TEMPORARY preview
  account for chain verification only. See the smoke report (Owner action:
  claim the temporary account within 60 minutes, or ignore and let it expire).
- No production Worker / Pages / R2 / KV is active.
- No existing Pasay routing was modified.
- FastAPI backend, Telegram bot, and PostgreSQL are untouched.
