# PASay -> Cloudflare Capability Mapping

Decisions made by reading the current Pasay codebase. **No code is being
migrated in this task**; this document only describes which Cloudflare
products are appropriate for each Pasay component, and which are explicitly
NOT a fit today.

## Current Pasay surface (read-only audit)

| Component | Path | Runtime | Why this matters for CF |
| --- | --- | --- | --- |
| Pasay backend API | `app/main.py` | FastAPI 0.133 / uvicorn 0.41 / Python 3.11 | Stateful, SQLAlchemy + psycopg2 + alembic, 60 endpoints, Bearer API key auth, decimal money math, attachments to disk |
| Telegram bot | `pasay-telegram-bot/` | python-telegram-bot 21+, long polling, httpx to Pasay API, local SQLite for state/idempotency | Long-running, persistent SQLite for nonce/idempotency, callback_data <=64B, RBAC |
| Mini App | (not in repo yet) | n/a | No frontend code yet; `ux/` only contains design docs |
| PostgreSQL | docker compose / native launchd | PostgreSQL 16 | Local container / native; target is Neon (managed Postgres) |
| Files | `uploads/` | local disk volume | Receipts + NAS backup |

## Mapping per Cloudflare product

### Cloudflare Workers  -  GOOD FIT (future, after migration plan)

- **Smoke Worker only today**: `cf-smoke/` — already deployed to a temporary
  preview account. Verifies the Wrangler -> Cloudflare -> public HTTPS chain.
- **Future fit**: edge API gateway in front of the FastAPI backend. Workers
  can do JWT verification, rate limiting, request shaping, GeoIP routing, and
  fan-out to Neon over Hyperdrive. **Not done in this task**; would require a
  separate migration plan and would NOT replace FastAPI as the runtime.

### Cloudflare Pages  -  FUTURE FIT (Mini App)

- Once a Mini App exists, Pages (or Workers with static assets) is the
  obvious host for the static frontend bundle. No Mini App code exists yet
  in this repo, so nothing to deploy today.

### Cloudflare R2  -  GOOD FIT (attachments)

- `uploads/` (receipts, attachments) is currently a local disk volume backed
  by a NAS rsync backup. R2 is a clean replacement for the persistent layer,
  keeping S3-compatible API semantics and zero egress. Migration is
  non-trivial (URLs, signed download, lifecycle) and is **not done here**.

### Cloudflare KV  -  NOT FIT today

- Pasay does not currently have any read-mostly, eventually-consistent
  configuration that fits KV semantics. Adding KV without a concrete consumer
  would be "use CF for the sake of CF" and is explicitly avoided.

### Cloudflare Queues  -  NOT FIT today

- Telegram bot already does its own polling and reconciliation; the backend
  uses synchronous API writes plus an in-process scheduler. There is no
  detached producer/consumer shape today. Defer until a concrete producer
  appears (e.g. an event-driven webhook ingestion pipeline).

### Cloudflare Cron Triggers  -  NOT FIT today

- Pasay already runs its scheduled jobs on the bot host via JobQueue. Moving
  them to Cron Triggers means duplicating state and risking duplicate runs.
  Defer.

### Cloudflare D1  -  EXPLICITLY NOT FIT

- PostgreSQL (target: Neon) stays the system of record. D1 (SQLite at edge)
  is **not** a substitute for the audit-loggable, transactional money math
  that Pasay relies on. The brief is explicit: do not migrate Postgres to
  D1.

### Cloudflare Hyperdrive  -  FUTURE FIT (Neon connection)

- Once the backend is on Neon, Hyperdrive can sit between Workers and Neon
  to keep connection count sane and reduce latency. Today the FastAPI
  runtime connects to Postgres directly, so this is not active yet.

## Explicit DO-NOT-MIGRATE list (for this round)

- **Pasay backend (FastAPI)** — stays as its own runtime. It is not a
  good fit for Workers: psycopg2, decimal-heavy money math, alembic
  migrations, 60 endpoints, file uploads to disk. Workers would force a
  rewrite to async + edge-compatible drivers and would lose the audit /
  idempotency semantics baked into the current code.
- **Telegram long-polling bot** — stays on its current host (long-polling
  vs webhook; persistent SQLite for nonce/idempotency). If Telegram webhooks
  are wanted later, a *new* Cloudflare Worker can receive them and proxy
  to the existing Pasay backend; the bot itself does not move.
- **PostgreSQL** — target is Neon (managed Postgres). **Not D1**.
- **Mini App** — does not exist yet; no deploy target.

## Reverse-proxy / API gateway decision

- Cloudflare Worker in front of FastAPI is **not enabled** in this task. It
  would be a future "CF-002" style migration with its own design brief.
- Today, if a Mini App or external client needs to hit Pasay, they talk to
  the FastAPI backend directly over HTTPS with a Bearer API key. The edge
  layer adds nothing yet.
