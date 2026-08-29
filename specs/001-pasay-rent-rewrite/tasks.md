# Tasks — 001-pasay-rent-rewrite

> Spec Kit flat numbered checklist. Each task is binary and independently verifiable.
> Status legend: `[ ]` pending · `[x]` done · `[~]` in-progress · `[!]` blocked.

---

## Phase 0 — Repo reset

- [ ] **T-001** Quarantine legacy governance docs into `_archive/`
  - deliverable: `_archive/governance/AGENTS.md`, `_archive/governance/project_rules.md`, `_archive/governance/SOLO_HANDOFF.md`, `_archive/governance/CURRENT_ARCHITECTURE.md`, `_archive/governance/pasay-governance.md`
  - acceptance: `ls _archive/governance/{AGENTS.md,project_rules.md,SOLO_HANDOFF.md,CURRENT_ARCHITECTURE.md,pasay-governance.md} 2>/dev/null | wc -l == 5`
  - status: pending

- [ ] **T-002** Delete legacy AI control files at repo root
  - deliverable: deleted `.ai-control/`, `AI_WORKFLOW_RULES.md`, `GITHUB_DEV_WORKFLOW.md`, `WINDOWS_RUNTIME_*`, `V122_*`, `PHASE*_*`, `REPAIR_*`, `*_REPORT.md` at repo root
  - acceptance: `find . -maxdepth 2 -type f \( -name "AI_WORKFLOW_RULES.md" -o -name "GITHUB_DEV_WORKFLOW.md" -o -name "PHASE*.md" -o -name "V122_*.md" \) -not -path "./.git/*" -not -path "./_archive/*" -print | wc -l == 0`
  - status: pending

- [ ] **T-003** Preserve evidence artifacts under `_archive/evidence/`
  - deliverable: `_archive/evidence/` containing previous screenshots, test reports, fixture bundles
  - acceptance: `find _archive/evidence -type f | wc -l >= 10` and prior `mtime` preserved on copied files
  - status: pending

- [ ] **T-004** Bootstrap Spec Kit directory structure
  - deliverable: `specs/001-pasay-rent-rewrite/`
  - acceptance: `mkdir -p specs/001-pasay-rent-rewrite && ls -d specs/001-pasay-rent-rewrite returns 0`
  - status: pending

---

## Phase 1 — Spec Kit + Docs

- [ ] **T-005** Write `spec.md` (user stories + acceptance criteria)
  - deliverable: `specs/001-pasay-rent-rewrite/spec.md`
  - acceptance: `markdownlint specs/001-pasay-rent-rewrite/spec.md passes` and file contains sections "User Stories", "Acceptance Criteria", "Out of Scope"
  - status: pending

- [ ] **T-006** Write `plan.md` (architecture + milestones)
  - deliverable: `specs/001-pasay-rent-rewrite/plan.md`
  - acceptance: `plan.md` references phases 0–15 and names FastAPI, SQLAlchemy 2, Alembic, Cloudflare Worker, PTB, Vite+TS as the stack
  - status: pending

- [ ] **T-007** Write `PRODUCT_COVERAGE_MATRIX.md`
  - deliverable: `specs/001-pasay-rent-rewrite/PRODUCT_COVERAGE_MATRIX.md`
  - acceptance: matrix has one row per feature and columns `spec | test | impl | deploy`; every row has all four cells populated (no `—`)
  - status: pending

- [ ] **T-008** Write `PRODUCT_RULES.md` (Permanent Business Truths)
  - deliverable: `specs/001-pasay-rent-rewrite/PRODUCT_RULES.md`
  - acceptance: `PRODUCT_RULES.md` codifies: Operation is truth / Task is projection, `NUMERIC(14,2)` + `Decimal`, `timestamptz` + tz-aware UTC, Org/Membership is the only permission boundary, Fail-closed
  - status: pending

- [ ] **T-009** Write `DATA_CONTRACT.md` (schema contract)
  - deliverable: `specs/001-pasay-rent-rewrite/DATA_CONTRACT.md`
  - acceptance: every aggregate listed has `fields`, `types`, `FKs`; all money fields typed `NUMERIC(14,2)`; all timestamps typed `timestamptz`
  - status: pending

- [ ] **T-010** Write `quickstart.md` (dev onboarding commands)
  - deliverable: `specs/001-pasay-rent-rewrite/quickstart.md`
  - acceptance: `quickstart.md` documents `make seed`, `make test`, `make telegram-local`, `make mini-app-dev`, `make deploy-worker`
  - status: pending

---

## Phase 2 — Backend foundation

- [ ] **T-011** `pyproject.toml` with pinned dependency set
  - deliverable: `pyproject.toml`, `uv.lock`
  - acceptance: `uv sync succeeds` and `python -c "import fastapi, sqlalchemy, alembic, telegram, pydantic, asyncpg"` returns exit 0
  - status: pending

- [ ] **T-012** `app/settings.py` (Pydantic Settings)
  - deliverable: `app/settings.py`
  - acceptance: `python -c "from app.settings import Settings; s = Settings(); assert s.DATABASE_URL"` returns 0; missing `DATABASE_URL` raises ValidationError
  - status: pending

- [ ] **T-013** `app/core/money.py` (Decimal utilities, no float)
  - deliverable: `app/core/money.py`
  - acceptance: `pytest tests/unit/core/test_money.py -q passes` and `money.add(1.1, 2.2) == Decimal("3.3")` (not `3.3000000000000003`)
  - status: pending

- [ ] **T-014** `app/core/time.py` (timezone-aware UTC helpers)
  - deliverable: `app/core/time.py`
  - acceptance: `pytest tests/unit/core/test_time.py -q passes` and `now_utc()` returns `datetime` with `tzinfo == timezone.utc`
  - status: pending

- [ ] **T-015** `app/db/session.py` (async SQLAlchemy engine + session)
  - deliverable: `app/db/session.py`
  - acceptance: `python -c "from app.db.session import engine; import asyncio; asyncio.run(engine.connect())"` returns 0 against a live Postgres 16
  - status: pending

- [ ] **T-016** `app/db/base.py` (DeclarativeBase + TimestampMixin)
  - deliverable: `app/db/base.py`
  - acceptance: `python -c "from app.db.base import Base, TimestampMixin; class M(Base, TimestampMixin): __tablename__='m'; id = Column(Integer, primary_key=True)"` returns 0; all models import `Base` from this single module
  - status: pending

- [ ] **T-017** Alembic baseline against empty Postgres 16
  - deliverable: `alembic.ini`, `alembic/env.py`, `alembic/versions/0001_baseline.py`
  - acceptance: `alembic upgrade head succeeds on empty Postgres 16` and `alembic downgrade base succeeds` (round-trip clean)
  - status: pending

---

## Phase 3 — Domain models (one file per aggregate)

- [ ] **T-018** Organization + Membership aggregate
  - deliverable: `app/models/organization.py`
  - acceptance: `Organization`, `Membership` registered with `Base`; `Membership.org_id` is `ON DELETE CASCADE`; cross-org FK query raises in test
  - status: pending

- [ ] **T-019** Property + Unit aggregate
  - deliverable: `app/models/property.py`
  - acceptance: `Property`, `Unit` present; `unit_code` is `UNIQUE (org_id, unit_code)`; `Unit.property_id` FK to Property
  - status: pending

- [ ] **T-020** Tenant aggregate
  - deliverable: `app/models/tenant.py`
  - acceptance: `Tenant.phone` stored as E.164 string, `Tenant.org_id` enforced, integration test confirms cross-org read returns 404
  - status: pending

- [ ] **T-021** Lease + RentSchedule aggregate
  - deliverable: `app/models/lease.py`
  - acceptance: `Lease.tenant_id`, `Lease.unit_id` FKs present; `RentSchedule.amount_due NUMERIC(14,2)`, `period_start`, `period_end timestamptz`
  - status: pending

- [ ] **T-022** Operation + Task aggregate (truth vs projection)
  - deliverable: `app/models/operation.py`, `app/models/task.py`
  - acceptance: `Operation.kind` enum (REMINDER, EXPENSE, PAYMENT, …), `Operation.status` enum (OPEN, CLAIMED, VERIFIED, CLOSED, REJECTED); `Task.operation_id` FK; Task has no path that mutates Operation (lint rule + test)
  - status: pending

- [ ] **T-023** Receipt + Verification aggregate
  - deliverable: `app/models/receipt.py`
  - acceptance: `Receipt.payload JSONB`; `Verification.operation_id` FK; `verified_amount NUMERIC(14,2)`, `verified_at timestamptz`, `verified_by` FK to Membership
  - status: pending

- [ ] **T-024** Idempotency-Key aggregate
  - deliverable: `app/models/idempotency.py`
  - acceptance: `IdempotencyKey` UNIQUE on `(org_id, key, route)`; `expires_at` defaults to 24h; TTL purge job exists
  - status: pending

---

## Phase 4 — Domain services (one per domain, state transitions)

- [ ] **T-025** `OperationService` (state machine: open → claim → verify → close/reject)
  - deliverable: `app/services/operation_service.py`
  - acceptance: `pytest tests/unit/services/test_operation_service.py -q passes`; `close()` requires real-world predicate to be true (test: predicate=False raises `IncompleteOperationError`)
  - status: pending

- [ ] **T-026** `TaskService` (projection only, never mutates Operation)
  - deliverable: `app/services/task_service.py`
  - acceptance: `pytest tests/unit/services/test_task_service.py -q passes`; fuzz test that calls every TaskService method confirms Operation table untouched
  - status: pending

- [ ] **T-027** `RentService` (schedule generation + due-date logic)
  - deliverable: `app/services/rent_service.py`
  - acceptance: `RentService.generate_schedule(lease)` returns deterministic `list[Period]` for fixed inputs; partial payment is **not** accepted as `Paid` (test asserts status stays `PARTIAL`)
  - status: pending

- [ ] **T-028** `PaymentVerificationService` (claim ≠ verified)
  - deliverable: `app/services/payment_verification_service.py`
  - acceptance: `verify_claim(claim)` requires Receipt evidence; `verify_claim` on a `claim` without `Receipt` raises `MissingEvidenceError`
  - status: pending

- [ ] **T-029** `NotificationService` (writes Task, never alters Operation)
  - deliverable: `app/services/notification_service.py`
  - acceptance: `NotificationService.send_reminder(op)` writes a Task row and returns its id; `op.status` unchanged before/after (assertion in unit test)
  - status: pending

---

## Phase 5 — API routers (grouped by domain, thin handlers)

- [ ] **T-030** Auth router (login / refresh / me)
  - deliverable: `app/api/auth.py`
  - acceptance: `pytest tests/integration/api/test_auth.py -q passes`; `POST /auth/login` with valid creds returns 200 + JWT; missing creds return 401
  - status: pending

- [ ] **T-031** Tenant + Lease routers
  - deliverable: `app/api/tenants.py`, `app/api/leases.py`
  - acceptance: `POST /tenants` creates tenant scoped to caller's org; cross-org fetch via valid JWT returns 404 (no leakage)
  - status: pending

- [ ] **T-032** Operation router (typed endpoints per kind)
  - deliverable: `app/api/operations.py`
  - acceptance: `POST /operations/{kind}/open`, `/claim`, `/verify`, `/close` present; bad `kind` returns 400; unauthorized role returns 403
  - status: pending

- [ ] **T-033** Reporting router (dashboard aggregates)
  - deliverable: `app/api/reports.py`
  - acceptance: `GET /reports/dashboard` returns Decimal sums only (JSON serialisation test: no `float` field present in response)
  - status: pending

---

## Phase 6 — Auth + Idempotency middleware + guards

- [ ] **T-034** JWT auth dependency (`app/api/deps.py`)
  - deliverable: `app/api/deps.py`
  - acceptance: `pytest tests/integration/test_auth_dependency.py -q passes`; missing `Authorization` header returns 401; expired token returns 401
  - status: pending

- [ ] **T-035** Permission guard (role-based, Owner/Secretary/Tenant)
  - deliverable: `app/auth/permissions.py`
  - acceptance: `require_role("owner")` returns 403 when caller is a Secretary without grant; passes for Owner (test)
  - status: pending

- [ ] **T-036** Org-scope guard (multi-tenant isolation, Fail-closed)
  - deliverable: `app/auth/org_scope.py`
  - acceptance: cross-org query raises `NotFound` (not `403`, to avoid existence leak); integration test asserts this behavior
  - status: pending

- [ ] **T-037** Idempotency middleware (`Idempotency-Key` header)
  - deliverable: `app/middleware/idempotency.py`
  - acceptance: same `Idempotency-Key` within 24h replays original response byte-for-byte; colliding body returns 409; missing key on POST returns 400
  - status: pending

---

## Phase 7 — Telegram ingress (webhook + PTB)

- [ ] **T-038** Telegram webhook route
  - deliverable: `app/api/telegram_webhook.py`
  - acceptance: `POST /webhook/telegram` with valid `X-Telegram-Bot-Api-Secret-Header` returns 200; PTB dispatcher invoked
  - status: pending

- [ ] **T-039** Webhook secret verification + replay protection
  - deliverable: `app/api/telegram_webhook.py` (verify function), `app/middleware/telegram_dedupe.py`
  - acceptance: `Telegram webhook rejects missing secret with 403`; same `update_id` posted twice is acknowledged once (200) and once no-op (200, no double effect)
  - status: pending

- [ ] **T-040** PTB application factory
  - deliverable: `app/telegram/app.py`
  - acceptance: `python -c "from app.telegram.app import build_application; build_application()"` returns 0; handlers wired to a single dispatcher
  - status: pending

- [ ] **T-041** Dispatcher (handler registration + NL fallback)
  - deliverable: `app/telegram/dispatcher.py`
  - acceptance: `dispatcher.add_handler` covers menu buttons + NL fallback; unknown text routed to NL bridge (test asserts fallback path)
  - status: pending

---

## Phase 8 — Telegram keyboards + handlers + cards + NL bridge

- [ ] **T-042** 3×2 reply keyboard (main menu)
  - deliverable: `app/telegram/keyboards/reply.py`
  - acceptance: `build_main_keyboard()` returns `ReplyKeyboardMarkup` with exactly 2 rows × 3 buttons labeled `Tenant`, `Lease`, `Operation`, `Payment`, `Status`, `Cancel`
  - status: pending

- [ ] **T-043** Inline keyboards per state
  - deliverable: `app/telegram/keyboards/inline.py`
  - acceptance: payment-claim inline keyboard shows `[Confirm] [Edit] [Cancel]`; `callback_data` contains opaque `op_id` (no PII)
  - status: pending

- [ ] **T-044** Card renderer (human-readable Markdown)
  - deliverable: `app/telegram/cards.py`
  - acceptance: `render_operation_card(op)` returns Markdown with `Decimal` formatted to 2dp and tz-aware dates rendered in user's locale; sample snapshot test passes
  - status: pending

- [ ] **T-045** State handlers (rent reminder, payment claim, verification reply)
  - deliverable: `app/telegram/handlers/rent.py`, `app/telegram/handlers/payment.py`, `app/telegram/handlers/verification.py`
  - acceptance: `unit 7777 + tenant name + PH phone NL → tenant updated, no expense created` (integration test asserts no Operation row inserted)
  - status: pending

- [ ] **T-046** NL bridge (single LLM fallback, function-calling)
  - deliverable: `app/telegram/nl_bridge.py`
  - acceptance: golden set `tests/nl/golden.jsonl` (≥15 cases) achieves ≥90% correct dispatch; non-dispatchable input returns safe "I didn't understand" card (no Operation side-effect)
  - status: pending

---

## Phase 9 — Cloudflare Worker (simplified)

- [ ] **T-047** Worker skeleton (TypeScript)
  - deliverable: `cloudflare/worker/src/index.ts`, `cloudflare/worker/wrangler.toml`
  - acceptance: `pnpm --filter worker build succeeds` and `wrangler deploy --dry-run --outdir=dist succeeds`
  - status: pending

- [ ] **T-048** Same envelope contract (Worker → Container → Neon)
  - deliverable: `cloudflare/worker/src/envelope.ts`
  - acceptance: `Envelope` type matches backend's `app/contracts/envelope.py` field-for-field; contract test passes both sides
  - status: pending

- [ ] **T-049** Cron trigger every 5 minutes (queue drain)
  - deliverable: `cloudflare/worker/wrangler.toml` (`[triggers] crons = ["*/5 * * * *"]`)
  - acceptance: `wrangler triggers --cron "*/5 * * * *" deploys`; cron schedule visible in Cloudflare dashboard after deploy
  - status: pending

---

## Phase 10 — Mini App scaffold (Vite + TS)

- [ ] **T-050** Vite + TypeScript scaffold
  - deliverable: `mini-app/package.json`, `mini-app/vite.config.ts`, `mini-app/tsconfig.json`, `mini-app/index.html`
  - acceptance: `vite build produces dist/ with index.html`; `tsc --noEmit` returns 0
  - status: pending

- [ ] **T-051** Typed API client (matches `DATA_CONTRACT.md`)
  - deliverable: `mini-app/src/api/client.ts`
  - acceptance: client exports typed `auth.*`, `tenants.*`, `operations.*`, `reports.*`; types generated from `DATA_CONTRACT.md`; `tsc --noEmit` returns 0
  - status: pending

- [ ] **T-052** Hash router (Telegram-friendly, no History API)
  - deliverable: `mini-app/src/router.ts`
  - acceptance: routes `#/`, `#/tenants`, `#/operations/:id`, `#/reports` resolve correctly; static analysis asserts no `history.pushState` / `window.location.assign` calls in app code
  - status: pending

- [ ] **T-053** i18n (en, zh, es, ru minimum)
  - deliverable: `mini-app/src/i18n/{en,zh,es,ru}.json`
  - acceptance: `t("dashboard.title")` returns translation in each of `en`, `zh`, `es`, `ru`; missing-key test fails fast
  - status: pending

- [ ] **T-054** Theme system (light/dark, Telegram colorScheme)
  - deliverable: `mini-app/src/theme/`
  - acceptance: theme variable flips on `Telegram.WebApp.colorScheme` change; `prefers-color-scheme` fallback works when not in Telegram (unit test)
  - status: pending

- [ ] **T-055** Mini App features (dashboard, tenants, operations, payments)
  - deliverable: `mini-app/src/features/{dashboard,tenants,operations,payments}/`
  - acceptance: `Playwright login → dashboard renders without console error`; all four feature pages mount without runtime exceptions
  - status: pending

---

## Phase 11 — Seeds (deterministic demo fixtures)

- [ ] **T-056** Org + Memberships seed
  - deliverable: `app/seeds/orgs.py`
  - acceptance: `python -m app.seeds.orgs` runs idempotently (re-run yields same rows); deterministic UUIDs from a fixed seed
  - status: pending

- [ ] **T-057** Properties + Tenants + Leases seed
  - deliverable: `app/seeds/properties.py`
  - acceptance: seeds create 3 properties, 10 tenants, 10 leases with rent schedules for 12 months; deterministic IDs
  - status: pending

- [ ] **T-058** Operations + Receipts seed (full lifecycle fixtures)
  - deliverable: `app/seeds/operations.py`
  - acceptance: seeds create at least one of each lifecycle: REMINDER (OPEN), EXPENSE (OPEN), PAYMENT_CLAIM (CLAIMED), PAYMENT (VERIFIED), PAYMENT (CLOSED); one rejected example included
  - status: pending

---

## Phase 12 — Tests

- [ ] **T-059** Unit tests (money, time, services, state transitions)
  - deliverable: `tests/unit/`
  - acceptance: `pytest tests/unit -q passes` and `coverage report --include="app/core/*,app/services/*" shows ≥ 90%`
  - status: pending

- [ ] **T-060** Integration tests (API + auth + idempotency)
  - deliverable: `tests/integration/`
  - acceptance: `pytest tests/integration -q passes` against an ephemeral Neon branch (or testcontainers Postgres 16); CI artifact uploaded
  - status: pending

- [ ] **T-061** Telegram handler tests (PTB test utilities)
  - deliverable: `tests/telegram/`
  - acceptance: `pytest tests/telegram -q passes`; ≥95% handler line coverage; NL fallback path tested
  - status: pending

- [ ] **T-062** Mini App smoke tests (Playwright)
  - deliverable: `tests/mini-app/`
  - acceptance: `Playwright login → dashboard renders without console error`; screenshot attached to test report
  - status: pending

- [ ] **T-063** NL bridge golden set
  - deliverable: `tests/nl/golden.jsonl`
  - acceptance: `pytest tests/nl -q passes` with ≥90% of the 15+ golden utterances dispatched to the correct handler; regression report printed
  - status: pending

---

## Phase 13 — CI (3 gates)

- [ ] **T-064** Lint + type-check gate
  - deliverable: `.github/workflows/lint.yml`
  - acceptance: `ruff check .` and `mypy --strict app` both exit 0 in CI; workflow fails on first error
  - status: pending

- [ ] **T-065** Pytest + coverage gate
  - deliverable: `.github/workflows/test.yml`
  - acceptance: `pytest -q passes` and coverage gate at ≥80% enforced; CI red below threshold
  - status: pending

- [ ] **T-066** Mini App build + Playwright e2e gate
  - deliverable: `.github/workflows/mini-app.yml`
  - acceptance: `pnpm build` and `playwright test` both exit 0; build artifact (`dist/`) uploaded
  - status: pending

---

## Phase 14 — Deploy (4 stages)

- [ ] **T-067** Provision Neon Postgres 16 (database + roles)
  - deliverable: `scripts/neon_provision.py`
  - acceptance: script creates database, applies Alembic baseline, prints `DATABASE_URL`; idempotent re-run detected and skipped
  - status: pending

- [ ] **T-068** Register Telegram webhook (with secret header)
  - deliverable: `scripts/telegram_set_webhook.py`
  - acceptance: `setWebhook` succeeds; `getWebhookInfo` returns expected URL + `has_custom_certificate=false` + secret set
  - status: pending

- [ ] **T-069** Deploy Cloudflare Worker
  - deliverable: `cloudflare/worker/wrangler.toml` (production env)
  - acceptance: `wrangler deploy --env production` returns success; `curl https://<worker>.workers.dev/healthz` returns 200
  - status: pending

- [ ] **T-070** Deploy Mini App to Cloudflare Pages
  - deliverable: `cloudflare/mini-app/wrangler.toml`
  - acceptance: `wrangler pages deploy ./mini-app/dist --project-name=pasay-mini-app` returns success; mini app URL returns 200 with `index.html`
  - status: pending

---

## Phase 15 — PR

- [ ] **T-071** PR description with PRODUCT_COVERAGE_MATRIX totals
  - deliverable: `.github/pull_request_template.md` + populated PR body
  - acceptance: PR body contains `PRODUCT_COVERAGE_MATRIX` totals row; every feature row shows `✅` in `spec | test | impl | deploy`; no `❌` entries remain
  - status: pending

- [ ] **T-072** Screenshot artifact placeholders in PR
  - deliverable: PR comments / `docs/screenshots/` directory committed under `_archive/` evidence
  - acceptance: PR includes placeholders for: `dashboard.png`, `tenant-create.png`, `payment-claim.png`, `payment-verified.png`, `mini-app-dark.png`; CI uploads them as artifacts
  - status: pending

---

## Totals

- Phase 0: 4 tasks
- Phase 1: 6 tasks
- Phase 2: 7 tasks
- Phase 3: 7 tasks
- Phase 4: 5 tasks
- Phase 5: 4 tasks
- Phase 6: 4 tasks
- Phase 7: 4 tasks
- Phase 8: 5 tasks
- Phase 9: 3 tasks
- Phase 10: 6 tasks
- Phase 11: 3 tasks
- Phase 12: 5 tasks
- Phase 13: 3 tasks
- Phase 14: 4 tasks
- Phase 15: 2 tasks

**Total: 72 tasks across 16 phases.**
