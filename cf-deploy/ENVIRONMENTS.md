# PASay Cloudflare / Deployment Environments

Three environments are reserved:

- **development** — local; uses `.env` on the dev box. No Cloudflare account
  needed for FastAPI / bot. Wrangler dev (`wrangler dev`) is local-only.
- **staging** — a Cloudflare preview account bound to the staging Worker
  name. Used for integration tests against a Neon-staging Postgres (future).
- **production** — the real Cloudflare account, Neon-prod Postgres, real
  Pasay API key, real Telegram bot tokens. **Only Owner can bind this.**

## Where each secret lives (no real values in this repo)

| Secret / var | development | staging | production |
| --- | --- | --- | --- |
| `PASSAY_TG_BOT_TOKEN` | `.env` (gitignored) / `pasay-telegram-bot/.env` | GitHub Actions secret `PASSAY_TG_BOT_TOKEN_STAGING` | GitHub Actions secret `PASSAY_TG_BOT_TOKEN_PROD` + Wrangler secret |
| `PASSAY_API_KEY` | `.env` | GitHub secret | Wrangler secret + GitHub secret |
| `PASSAY_ADMIN_API_KEY` | `.env` | GitHub secret | Wrangler secret + GitHub secret |
| `DATABASE_URL` | docker compose / launchd local | Neon-staging connection string (Wrangler secret + GH secret) | Neon-prod connection string (Wrangler secret + GH secret) |
| `CLOUDFLARE_API_TOKEN` | n/a | Owner-bound; not in repo | Owner-bound; not in repo |
| `CLOUDFLARE_ACCOUNT_ID` | n/a | Owner-bound; not in repo | Owner-bound; not in repo |

## Storage layers

- **Wrangler `vars`** — non-secret configuration only (e.g. `ENV_NAME=staging`,
  `LOG_LEVEL`). Bound via `wrangler.toml` `[env.<name>] vars = { ... }` block.
- **Wrangler `secrets`** — actual secret values, uploaded via
  `wrangler secret put <NAME>` by Owner. Never in `wrangler.toml`, never in
  GitHub, never in commit history.
- **GitHub Actions secrets** — used by the CI workflows to authenticate to
  Cloudflare and to push runtime secrets to staging Workers. Set via
  `Settings > Secrets and variables > Actions` in the GitHub repo.
- **Neon env vars** — Neon connection strings live in the Neon dashboard;
  Workers / GitHub read them via secret references, not as inline values.

## Wrangler env file naming

For each environment, a `.dev.vars.<env>` file holds secrets *only* for local
Wrangler dev runs. These are gitignored (already excluded by the existing
`.gitignore` rule for `.env*` with `.env.example` exception).

## Files in this directory

- `secrets.example.json` — shape only; never holds a real value.
- `wrangler.example.toml` — example shape for a future non-smoke Worker; no
  real account / route / secret bindings.
