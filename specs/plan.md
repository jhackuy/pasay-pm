# Plan

## 1. Topology

The PASAY rewrite preserves the architecture topology:

```
Telegram → Cloudflare Worker → Cloudflare Queue → Cloudflare Container → Neon PostgreSQL 16
```

- **Telegram** is the chat surface used by Owner and Secretary. It receives
  user updates through the Cloudflare Worker and sends messages back through
  the Telegram Bot API.
- **Cloudflare Worker** is the single ingress for Telegram webhooks. It
  authenticates, normalizes, and enqueues a single message per business event
  into the Cloudflare Queue. There is no duplicate dispatcher.
- **Cloudflare Queue** decouples the Worker from the Container, so that webhook
  acknowledgements are immediate and processing is durable.
- **Cloudflare Container** runs the Python/FastAPI backend process. It pulls
  messages from the queue, executes business logic against Neon PostgreSQL 16,
  and sends outbound Telegram messages via the Telegram Bot API.
- **Neon PostgreSQL 16** is the single source of business truth. The schema is
  defined by exactly one baseline Alembic migration; no legacy data is migrated.

The Mini App is a TypeScript Vite single-page application served from a static
host and consumed by Telegram's `WebApp` API. It speaks REST to the Cloudflare
Worker; the Worker forwards Mini App requests to the Container.

## 2. Backend Stack

The Container runs the Python backend with the following stack:

- **Python 3.11** — runtime.
- **FastAPI** — HTTP framework.
- **SQLAlchemy 2.x** — ORM and query layer.
- **Alembic** — schema migrations. One baseline migration
  (`alembic/versions/001_baseline.py`) defines the entire fresh schema.
- **Pydantic v2** — request/response validation, with strict
  `Decimal`/`datetime` typing at every API boundary.
- **PostgreSQL 16** (Neon) — database.
- **psycopg2-binary** — driver.

The backend layout follows the layout that the rest of the project already
uses (`app/models/`, `app/services/`, `app/api/`, `app/core/`, `alembic/`,
`tests/`). Money math runs through `app/services/money.py`. Permission checks
run through `app/core/org_scope.py`. Audit writes run through
`app/services/audit.py`. The Telegram bot is a sibling package
`pasay-telegram-bot/`, not a sub-app.

## 3. Telegram Bot Stack

- **python-telegram-bot v21+** — Telegram Bot API client.
- Located in `pasay-telegram-bot/` (sibling to `app/`, not inside it).
- Modules:
  - `pasay_bot/keyboards.py` — 3×2 primary keyboard + inline keyboards.
  - `pasay_bot/handlers/commands.py` — `/start`, `/help`, language, and
    bootstrap.
  - `pasay_bot/handlers/buttons.py` — deterministic fast paths for every
    primary and inline button.
  - `pasay_bot/handlers/text.py` — natural-language text input.
  - `pasay_bot/nl_bridge.py` — regex-first intent resolution with a single
    MiniMax LLM fallback for unresolved business intent.
  - `pasay_bot/api_client.py` — typed HTTP client to the backend.
- Default language: `zh` for OWNER, `en` for SECRETARY.
- Behavior: silent on chatter, no LLM in the hot path, mutation guard
  before every API call.

## 4. Cloudflare Worker Stack

- **TypeScript** — source language.
- **Node 22** — runtime.
- **`@cloudflare/containers` 0.3.x** — official API for the Container binding.
- **`wrangler` 3.x** — deploy CLI.
- Modules:
  - `src/index.ts` — webhook ingress, Mini App request forwarding, queue
    producer.
  - `src/auth.ts` — Telegram webhook secret check, Mini App init-data check.
  - `src/queue.ts` — single-message-per-event producer.
  - `wrangler.toml` — bindings for the Container, the Queue, and secrets.
- The Worker must not duplicate events. One Telegram update produces at most
  one queue message.

## 5. Mini App Stack

- **TypeScript** — source language.
- **Vite 5.x** — build tool and dev server.
- **plain CSS** with CSS variables for the light/dark theme. No CSS-in-JS, no
  utility framework. Touch targets `min-height: 44px`. No horizontal
  overflow on any viewport ≥ 320 px.
- React 18 as the view layer (typed, function components, hooks).
- **Playwright** — smoke tests under `mini-app/tests/smoke.spec.ts`.
- Routes:
  - `/` (Home / dashboard)
  - `/properties/:id` (Property detail)
  - `/units/:id` (Unit detail)
  - `/leases/:id` (Lease detail)
  - `/rent` (Rent schedules)
  - `/expense` (Expense management)
  - `/repair` (Repair management)
  - `/renewal` (Lease renewal)
  - `/move-out` (Move-out settlement)
  - `/activity` (Audit timeline)
  - `/archive` (Archived items)
  - `/membership` (Membership and settings)
- i18n: `mini-app/src/i18n/{zh,en,tl}.ts`, keyed by `users.default_language`.

## 6. CI Gates

CI is required to pass before any PR is mergeable. Gates are:

1. **Backend pytest (retained/behavior tests)** — every test under
   `tests/test_*.py` mapped to a capability row in `COVERAGE_MATRIX.md`. No
   skip, no xfail. CI must surface every failure.
2. **Fresh PostgreSQL + alembic upgrade head** — CI spins up an empty
   PostgreSQL 16 (or compatible) database and runs `alembic upgrade head`.
   The migration must produce the exact schema in `DATA_CONTRACT.md`.
3. **Backend + container build** — the FastAPI app boots, the Container
   image builds, and the Container starts against a fresh database.
4. **Mini App build + Playwright smoke** — Vite production build succeeds;
   Playwright smoke against the dev server (or built bundle) exercises
   light/dark theme, 44×44 touch targets, no horizontal overflow, and the
   primary routes.

CI must run independently of agent self-report. The agent's claim of success
is never sufficient on its own.

## 7. Deployment

Deployment order is fixed:

1. `alembic upgrade head` against the target Neon database. Schema must match
   `DATA_CONTRACT.md` exactly.
2. `wrangler deploy` — pushes the Worker with the Container binding.
3. `GET /health` on the Container — must return 200.
4. Telegram smoke — set the webhook, send `/start`, and confirm the 3×2
   primary keyboard is delivered with the role-correct default language.
5. Mini App smoke — open the Mini App in a test Telegram session and confirm
   Home loads from the real API.

If any step fails, deployment halts and the failure is surfaced to the Owner.

## 8. Data Layer Strategy

The data layer is a clean baseline. There is no migration of legacy data and
no compatibility layer.

- **One fresh baseline migration**: `alembic/versions/001_baseline.py` is the
  only schema migration in the rewrite. It creates every table described in
  `DATA_CONTRACT.md`. Any later schema change is a new migration that
  revisions from `001`.
- **No old-data migration**: rows from any pre-rewrite database are not
  copied. The Owner Addendum explicitly accepts data loss.
- **No compatibility layer**: there is no API that maps old endpoints to new
  ones, no field that is aliased to a legacy name, and no fallback path that
  reads from a non-canonical source.

The baseline migration is the schema contract. CI verifies this by upgrading
an empty database and comparing the resulting schema to `DATA_CONTRACT.md`.
