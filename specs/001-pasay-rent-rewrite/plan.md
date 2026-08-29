# PASAY Rent — Technical Plan (001-pasay-rent-rewrite)

## 1. Architecture overview

```
Telegram ──► Cloudflare Worker (TS, /telegram/webhook)
                │ X-Telegram-Bot-Api-Secret-Token
                ▼
            Cloudflare Queue: pasay-events (DLQ: pasay-events-dlq)
                │
                ▼
            Cloudflare Container (PasayContainer singleton "pasay-singleton", official @cloudflare/containers)
                │ X-Pasay-Ingest-Token
                ▼
            FastAPI app (uvicorn :8000) inside container
                │ SQLAlchemy 2.x + Alembic
                ▼
            Neon PostgreSQL 16 (Numeric(14,2), timestamptz)
```

**Note:** The Mini App calls the **same FastAPI** via `Authorization: Bearer <token>` against the same `/api/v1/*` paths. There is exactly one business backend; the Telegram bot and the Mini App are two ingress surfaces into the same domain.

Frozen topology (per AGENTS.md §4): `Telegram → Cloudflare Worker → Cloudflare Queue → Cloudflare Container → Neon PostgreSQL 16`. Operation is Truth, Task is Projection.

## 2. Repository layout

- `app/` — FastAPI application package
  - `app/api/` — HTTP routers (v1)
  - `app/core/` — config, security, principal, deps
  - `app/db/` — SQLAlchemy session, engine, base
  - `app/models/` — ORM models (org, property, tenant, lease, operation, task, rent, expense, repair, renewal, move_out, attachment)
  - `app/schemas/` — Pydantic v2 request/response models
  - `app/services/` — domain services (see §3)
  - `app/telegram/` — PTB application, handlers, keyboards, callback codec, NL routing, LLM fallback
  - `app/mini_app/` — Mini App specific auth + DTO adapters (reuses services)
  - `app/workers/` — outbox dispatcher, renewal scheduler, reminder dispatcher
  - `app/main.py` — FastAPI factory + lifespan (PTB init once, NO polling)
- `alembic/` — migrations
  - `alembic/versions/` — sequential migrations, Numeric(14,2) + timestamptz
  - `alembic/env.py`
  - `alembic.ini`
- `pasay_telegram_bot/` — standalone PTB entrypoint (used only for local dev / replay)
- `mini_app/` — Vite + TS Mini App frontend
  - `mini_app/src/` — pages, components, api client
  - `mini_app/tests/` — Playwright smoke
- `cloudflare-worker/` — Worker source (TS)
  - `cloudflare-worker/src/index.ts` — /telegram/webhook, secret-token check
  - `cloudflare-worker/src/queue.ts` — Queue producer
  - `cloudflare-worker/wrangler.toml` — bindings + Container config
- `tests/` — pytest suites
  - `tests/unit/`
  - `tests/integration/`
  - `tests/telegram/`
  - `tests/mini_app/` — Playwright
- `.github/workflows/` — CI gates (see §9)

## 3. Domain modules

Each service file owns one bounded slice. All services enforce OrgScopedRepository at the row level.

- `app/services/workspaces.py` — Organization / Membership / Invite creation + acceptance + the **last-Owner guard** (cannot demote or remove the final Owner of an org).
- `app/services/properties.py` — Property / Unit CRUD, lifecycle states (active/archived), archive cascades (leases closed, tenants detached, no destructive delete).
- `app/services/tenants.py` — Tenant entity + contact channels (telegram_user_id, phone, email) + contact status (unreachable / reachable / opted_out).
- `app/services/leases.py` — create / supersede / renew / terminate; lease state is derived from Operation truth, never from Task completion.
- `app/services/operations.py` — **Operation is Truth, Task is Projection.** Dedup by `(org_id, kind, natural_key)`, idempotent inserts, outbox row written in same transaction, projection of Task rows downstream.
- `app/services/rent.py` — schedule generation (monthly/weekly/custom), payment claim, verification (verified vs claimed), running balance per lease — all `Decimal` arithmetic.
- `app/services/expenses.py` — Expense (real spend) + ExpenseClaim (tenant reimbursement) + Evidence attachments + Verification state.
- `app/services/repairs.py` — 9-state machine (`reported → triaged → quoted → approved → scheduled → in_progress → verified → closed` / `cancelled`) + RepairProposal + RepairAction audit.
- `app/services/renewals.py` — detect window → contact tenant → capture response → owner decision → execute (supersede lease) → verify (Operation CLOSED). Every step is an Operation.
- `app/services/move_out.py` — Inspection (checklist + findings) → Settlement (Decimal math, refund vs owing) → atomic close of lease + property unit in one DB transaction.
- `app/services/evidence.py` — Attachment ownership scoping (org member only), content-addressed storage key, audit trail of who viewed/downloaded.
- `app/services/telegram_ingress.py` — webhook security (`X-Telegram-Bot-Api-Secret-Token` re-check + `X-Pasay-Ingest-Token`), idempotency (by `update_id` + partial unique index), update routing to PTB handlers.

## 4. Permission model

All authorization is **fail-closed** at the dependency layer. No endpoint runs without a resolved Principal.

- `get_current_principal()` — FastAPI dependency. Resolves the caller from:
  - `Authorization: Bearer <jwt>` for Mini App (HS256, signed with shared secret, claims: `sub`, `org_id`, `role`, `exp`).
  - `X-Pasay-Ingest-Token` for the Worker/Container hop (internal only).
  - PTB update handler derives Principal from `update.effective_user` ↔ Membership lookup.
  Returns a `Principal` value object: `(user_id, org_id, role, scopes)`.
- `require_role(*roles)` — guards a route/handler to one or more roles within the resolved org.
- `require_org_member(org_id)` — guards cross-org access; rejects when `principal.org_id != path.org_id` with 403.
- `OrgScopedRepository` — SQLAlchemy mixin applied to every domain repository. Injects `WHERE org_id = :principal_org_id` into every query; the org_id is **never** taken from request body or path without re-validation against the Principal.

Owner/Secretary/Tenant role semantics are enforced by `require_role`. Permission boundary is **Organization / Membership**, full stop.

## 5. Money + Time

- **Money** — DB column type `NUMERIC(14, 2)` (no `FLOAT4`, no `FLOAT8`). Python uses `decimal.Decimal` end-to-end. JSON serialization via Pydantic v2 with `condecimal(max_digits=14, decimal_places=2)`. Arithmetic uses `Decimal` context; `float()` conversion is forbidden by lint rule.
- **Time** — DB column type `timestamptz` (timezone-aware). Python uses `datetime.datetime` with `timezone.utc`. All "now" values come from a single `app.core.clock.utcnow()` helper to keep tests deterministic.
- Alembic migrations enforce both at the column level; downgrade is not used in normal flow.

## 6. Idempotency

- HTTP header `Idempotency-Key: <uuid>` (optional on safe methods, **required** on every POST/PATCH that mutates state).
- FastAPI middleware stores `(principal.org_id, route, idempotency_key) → response_hash` in a small `idempotency` table with a **partial unique index** `(org_id, route, key) WHERE key IS NOT NULL`. Replays return the cached response.
- Per-table safety net: every domain table that accepts an external event has a partial unique index on `(org_id, external_kind, external_id)` (e.g. Telegram `update_id`, Mini App `client_event_id`, worker `delivery_id`).
- Result: webhook retries, queue redeliveries, and Mini App double-taps cannot double-write Operations or duplicate Rent claims.

## 7. Telegram webhook pipeline

1. Telegram calls `POST https://<worker-host>/telegram/webhook` with header `X-Telegram-Bot-Api-Secret-Token`.
2. **Worker (TS)** — verifies the secret token, drops anything else with 401, wraps the `Update` into a typed message, and `env.QUEUE_PASAY_EVENTS.send(...)` to `pasay-events`.
3. **Queue** — at-least-once delivery with `pasay-events-dlq` bound for poison messages after retries.
4. **Container** — official `@cloudflare/containers` `PasayContainer` (class `pasay-singleton`, `instanceType: "standard"`, `maxInstances: 1`). The singleton ensures exactly one PTB Application instance per container.
5. **Worker → Container** — Worker pulls the message and POSTs to `http://<container>/internal/ingest` with header `X-Pasay-Ingest-Token`.
6. **FastAPI `telegram_ingress`** — `/internal/ingest` route re-checks `X-Pasay-Ingest-Token`, dedups by `update_id` (partial unique index), resolves Principal, then hands the `Update` to `app.telegram.application.process_update(update)`.
7. PTB `Application` is initialized **once** at FastAPI lifespan startup. **No `run_polling()`** anywhere in the container.

## 8. Mini App API contract

- Auth: `Authorization: Bearer <jwt>` (HS256, org + role + exp claims).
- Same `/api/v1/*` paths used by every other client — Mini App is **not** a separate API.
- Idempotency: same `Idempotency-Key` header, same replay semantics.
- Errors: RFC 7807 problem+json shape; 401 (no/invalid token), 403 (role/org mismatch), 409 (idempotency conflict), 422 (validation), 5xx with correlation id.
- The Mini App never talks to Telegram directly; auth identity is anchored to a Membership row, not a Telegram user id.

## 9. CI

Exactly three gates. Nothing else.

1. **`pytest -q`** — runs after `alembic upgrade head` against a clean Postgres 16 service container in the same job. Includes unit + integration + telegram suites.
2. **`alembic upgrade head`** — on a clean Postgres 16 (fresh schema). Verifies every migration applies from scratch with no manual fixups.
3. **`vite build` + Playwright smoke** — builds the Mini App and runs the headless Playwright smoke that hits `/health` and one happy-path page.

No governance lint, no prose test, no architecture-drift detector, no custom rule engine. The constitution (AGENTS.md) is reviewed by humans, not enforced by CI scripts.

## 10. Deploy

Manual / scripted deploy, four ordered stages. No click-ops.

1. **DB** — `alembic upgrade head` against the Neon production branch (run from CI with a Neon-scoped token).
2. **Edge + Container** — `wrangler deploy` for the Worker; the Container binding (`PASAY_CONTAINER`, class `pasay-singleton`) and secrets (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_WEBHOOK_SECRET`, `PASAY_INGEST_TOKEN`, `NEON_DATABASE_URL`, `JWT_SECRET`) are taken from the existing Wrangler secrets store — no new secrets introduced this milestone.
3. **Health probe** — `curl https://<host>/health` returns `{ "status": "ok", "architecture_frozen": true }`.
4. **Telegram smoke** — a synthetic Update (sent via PTB test util) is delivered to `/telegram/webhook`; Worker returns 200, queue accepts, container processes, and the synthetic Operation lands in Neon within the SLA window.

## 11. Test strategy

- **Unit** (fast, deterministic) — state transitions (repairs 9-state machine, lease lifecycle), `Decimal` money arithmetic (rent schedule, settlement, balance), idempotency middleware (replay returns cached), callback codec encode/decode round-trip, principal/role guards.
- **Integration** (per-domain end-to-end against a clean Postgres 16) — workspaces invite acceptance, property archive cascade, rent schedule + claim + verify + balance, expense claim with evidence, renewal detect→execute→verify, move-out inspection→settlement→atomic close, evidence ownership scoping.
- **Telegram** — keyboard rendering, callback dispatch, deterministic fast-path handlers, NL regression corpus (fixed inputs → expected handler), LLM fallback path (stub provider asserts "fallback engaged" without calling the network).
- **Mini App** — Playwright smoke against a locally served build + FastAPI test app: login, list leases, submit a rent claim; asserts the same `/api/v1` contract.
- **Forbidden** — governance/prose tests, "architecture should be X" assertions, snapshot tests of constitution text.

## 12. Risks + mitigations

- **Cloud resource missing (Queue, Container, Worker route)** — `wrangler deploy` fails fast and the Container singleton cannot start. Mitigation: `/health` returns `architecture_frozen=false` with a specific missing-binding field; the deploy script aborts stage 4.
- **Old migration breaks against current schema** — a stale DB will reject `alembic upgrade head`. Mitigation: drop-and-recreate schema is permitted in non-production; production runs only additive migrations, and any corrective migration is shipped as a new versioned file (no rewriting of shipped history).
- **LLM provider down** — risk is contained because every Telegram handler has a deterministic fast path; the LLM is a fallback only. Mitigation: feature flag `PASAY_LLM_ENABLED` defaults to `false`; the deterministic handler set covers all production-required flows.
- **Telegram 409 Conflict (multiple bot instances polling)** — eliminated by construction: there is exactly one container instance (`maxInstances: 1`, singleton), no `run_polling()`, and the webhook handler runs only in the FastAPI lifespan. A 409 would indicate the deployment violated the singleton invariant and is treated as a P0.
