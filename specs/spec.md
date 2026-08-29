# Spec

## 1. Goal

PASAY Rent is rewritten as a clean, newly designed real Mini App on top of a single
canonical backend. The rewrite preserves the architecture topology
`Telegram → Cloudflare Worker → Cloudflare Queue → Cloudflare Container → Neon PostgreSQL 16`
but discards all legacy implementation, schema, and process machinery.

The deliverable is one backend, one Telegram bot, one Cloudflare Worker, and one
Mini App that together implement the eleven retained product capabilities from the
Owner Addendum, with a fresh baseline data model and no compatibility layer.

## 2. Users

- **Owner** — default language `zh` (Chinese). Owns one Organization, creates the
  workspace, can perform every business action, has the final decision on
  approvals, renewals, and move-out settlements.
- **Secretary** — default language `en` (English). Operational actor for daily
  rent follow-up, expense entry, repair reporting, and tenant contact. Cannot
  unilaterally override Owner decisions.
- **Tenant** — read-only visibility through the API and the Mini App public
  surfaces. No business-mutating role in the workspace.

## 3. Inputs

- **Telegram 3×2 primary keyboard** — `Home / Properties / Tasks` on row 1 and
  `Rent / Expense / Archive` on row 2, plus contextual inline keyboards for
  every operation.
- **Mini App forms** — typed forms in the Mini App for every business action
  (workspace bootstrap, property/unit setup, tenant registration, lease, rent,
  expense, repair, renewal, move-out, attachments, membership).
- **REST API** — `POST /api/v1/...` for mutations, `GET /api/v1/...` for reads,
  with `Idempotency-Key` headers on financial mutations, multipart
  `POST /api/v1/attachments` for evidence, and `GET /api/v1/audit?entity=...`
  for the activity timeline.

## 4. Behaviors

### 4.1 Workspace onboarding and team

The first user to run `/start` in the Telegram bot becomes the Owner of a new
Organization without any manual ID provisioning. The Owner can invite
Secretaries via invite links, remove members, and the system enforces
last-Owner protection. Every user has a `default_language` (zh for OWNER, en
for SECRETARY) that drives both Telegram greetings and Mini App presentation.

### 4.2 Property operations

The Owner or Secretary creates Properties and Units. Each Unit has a
`vacant` flag that is updated by lease transitions. The Owner can archive a
Property or Unit (soft delete via `archived_at`) while keeping its history
intact. Units carry photos, documents, and a per-unit history view.

### 4.3 Rent truth closure

A lease produces a deterministic `rent_schedules` table. A payment claim
(`payment_claims`) does not flip `paid` to true on its own. The `paid` flag
flips only when `amount_paid >= amount_due` AND a `payments` row exists with
verified evidence. Partial payments update `amount_paid` but keep `paid=false`.
The Operation `RENT` only moves to `CLOSED` after this verified truth.

### 4.4 Expense truth closure

Expenses carry separate `property_id`, `building`, `unit_id`, `category`, and
`purpose` fields. Expense state machine: `DRAFT → CLAIMED → APPROVED → PAID →
VERIFIED`. Approval is not Payment. Verification requires evidence
(`attachments` linked via `expense_evidence`) and a verifier user. A Quote
that is rejected does not close the underlying Repair.

### 4.5 Repair truth closure

Repair state machine: `REPORTED → CONFIRMED → QUOTED → APPROVED → IN_PROGRESS
→ COMPLETION_CLAIMED → VERIFIED → CLOSED` (with `REJECTED` as terminal
alternative). Closing a Repair requires `completion_verified_at` set and a
verifier (Owner or Secretary). Related Expense or Payment activity does not
falsely close the Repair.

### 4.6 Lease renewal

Expiring leases (<30 days to `end_date`) surface on the Owner Home as urgent.
The renewal flow walks `PROPOSED → TENANT_CONTACTED → TENANT_RESPONDED →
OWNER_DECIDED → EXECUTING → VERIFIED → CLOSED`, producing a new `leases` row
on execution and archiving the prior lease without losing history.

### 4.7 Move-out settlement

Move-out carries `inspection_date`, `deductions_normal_wear`,
`deductions_tenant_damage`, and a computed `deposit_refund` from
`app/services/move_out.py::compute_settlement()`. The atomic final close
flips `move_outs.state → CLOSED`, sets `leases.status → ENDED`, and sets
`units.vacant = true` in a single database transaction.

### 4.8 Operations and Tasks projection

`operations` is the canonical truth. `tasks` is a current human-action
projection of an operation, with a UNIQUE partial index on
`(operation_id) WHERE status = 'PENDING'` so that there is at most one open
Task per Operation. Reminders, replies, and notifications do not mark
Tasks DONE; only an explicit human action through the API or Mini App can
flip a Task to DONE, and DONE only ever projects the Operation forward
without mutating the Operation's business truth on its own.

### 4.9 Money, idempotency, and audit

All monetary amounts use `NUMERIC(14,2)` in the database and `Decimal` in
Python. A single `app/services/money.py` helper performs every money
calculation. `float` is forbidden. `payment_claims.idempotency_key` is UNIQUE
and required. Every state transition writes an `audit_logs` row with actor,
before-state, after-state, and `evidence_refs`. Every read/write filters by
`organization_id`; cross-org access is rejected and audited.

### 4.10 Telegram office

The Telegram bot exposes the 3×2 primary keyboard, deterministic fast paths
on every button (no LLM in the hot path), Chinese/English/Tagalog natural
language understanding via regex + a single MiniMax LLM fallback for
unresolved business intent, role-aware default language, silent-on-chatter
behavior, and minimal active business context surfaced as a suggested
reply. Every mutation is guarded by membership-role and organization-scope
checks before it is dispatched to the backend.

### 4.11 Mini App console

The Mini App is a real, API-backed Owner console with: dashboard and urgent
next actions, property/unit/tenant/lease detail, rent/expense/repair
management, renewal and move-out flows, evidence upload, activity and
archive views, membership and settings, light/dark theme, 44×44 touch
targets, no horizontal overflow, and presentation language driven by
`users.default_language`. Every form hits `/api/v1/...` with a typed client;
nothing is a static prototype.

## 5. Non-Goals

- **Legacy compatibility.** No backfill, no compatibility shim, no old data
  migration. The single baseline migration defines the only schema.
- **Qualification or reviewer machinery.** No `.ai-control/`,
  `AI_WORKFLOW_RULES.md`, `GITHUB_DEV_WORKFLOW.md`, OpenSpec, Superpowers,
  Qoder, or any complex custom orchestrator.
- **Second task database.** Tasks are projections of Operations. There is no
  standalone task system.
- **Polling.** The Mini App and Telegram bot read state via API and webhook
  push only.
- **Duplicate dispatcher.** A single Cloudflare Worker pushes a single message
  per business event into the Cloudflare Queue; the container processes once.
- **localStorage as business truth.** The Mini App may cache for UX, but
  business truth lives in the backend only.
- **Float money.** No business code uses `float` for money. CI enforces.
- **Naive datetimes.** No business code uses `datetime.utcnow()`. CI enforces.
- **Cross-organization reads.** Cross-org access is rejected and audited.

## 6. Acceptance

The rewrite is accepted when:

1. The single baseline Alembic migration `alembic/versions/001_baseline.py`
   creates the entire schema described in `DATA_CONTRACT.md`, and a fresh
   PostgreSQL 16 database upgraded from empty passes all retained/behavior
   tests.
2. The Telegram bot exposes the 3×2 primary keyboard, has deterministic fast
   paths for every button, and resolves unit-7777-style regressions to a
   tenant update rather than to a new expense.
3. The Mini App boots, hits real APIs for every form action, supports
   light/dark theme, 44×44 touch targets, no horizontal overflow, and the
   Owner/ Secretary language presentation.
4. `COVERAGE_MATRIX.md` enumerates 66 capability rows across 11 capability
   groups, each row mapped to a domain module, an API, a surface, and a
   test, with all listed tests passing in CI.
5. The CI gates pass: backend pytest (retained/behavior tests), fresh
   PostgreSQL + `alembic upgrade head`, backend + container build, Mini App
   build, and Mini App Playwright smoke.
6. `PRODUCT_RULES.md` invariants (Decimal-only money, `timestamptz`, Org/
   Membership permission boundary, Operation-as-truth) are enforced in code
   and verified by counterexample tests.
