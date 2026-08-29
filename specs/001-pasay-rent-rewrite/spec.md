# PASAY Rent — Clean Rewrite with Real Mini App

## 1. Purpose

This document specifies the clean rewrite of PASAY Rent, a rental-property operations product for small landlords (Owners) and their assistants (Secretaries). The rewrite replaces the existing prototype with a coherent, end-to-end system whose Telegram office bot and Owner-facing Mini App are both backed by the same authoritative backend and shared data model. The goal is to make Business Truth First the single source of truth for rent, expense, repair, renewal and move-out operations, while providing two complementary operator surfaces — a conversational Telegram channel for fast capture and a real Mini App for deep inspection, approval and audit. Every requirement in this specification is traceable to a verifiable acceptance criterion and is bounded by the permanent business and engineering truths recorded in `PRODUCT_RULES.md` and `DATA_CONTRACT.md`.

## 2. Source of Truth

This specification is governed by, and must remain consistent with, the following canonical documents at the repository root:

- `PRODUCT_RULES.md` — the authoritative rules of the product domain: workspace identity, role boundaries (Owner / Secretary / Tenant), rent truth, expense truth, repair state machine, renewal and move-out flows, money type guarantees, and notification policy.
- `DATA_CONTRACT.md` — the authoritative data contract: tables, columns, foreign keys, enums, indexes, money type (`NUMERIC(14,2)` / `Decimal`), time type (`timestamptz` / UTC `datetime`), idempotency keys, attachment ownership, audit columns, and organization scoping.

Where this spec and the source-of-truth documents differ, `PRODUCT_RULES.md` and `DATA_CONTRACT.md` win. Where a requirement here is silent, the source-of-truth documents fill the gap. All implementation work must cite these files and must not introduce business semantics that contradict them.

## 3. In Scope

### FR-1 — Workspace onboarding and team

The system models a workspace as an Organization that owns all operational data, scoped strictly by `organization_id`. Every Owner and Secretary is bound to an Organization through a Membership row; the Organization / Membership pair is the sole business permission boundary, and authorization is fail-closed (a missing membership denies access). The first user who registers becomes the bootstrap Owner of a new Organization, and the Owner can invite Secretaries by Telegram handle or email and accept or revoke their memberships. A last-Owner guard prohibits demoting or removing the final remaining Owner so that no Organization can become orphaned; transfers of ownership are explicitly allowed only when at least one other Owner has already been promoted. Tenant users are never Members of the owning Organization; they exist only as Tenant records attached to a Lease.

### FR-2 — Properties and units

A Property represents a physical building or address and contains one or more Units; a Unit is the smallest leasable object and carries a lifecycle status of `vacant`, `occupied`, or `maintenance`. Unit status is derived from active Lease state, explicit maintenance flags, and historical move-out events, never from a stale free-text field. Owners can archive a Property or Unit, which hides it from default lists and dashboards while preserving all historical Leases, Operations and financial records for audit. All queries are organization-scoped, and any cross-tenant or cross-organization data leakage is treated as a critical defect.

### FR-3 — Tenants and leases

A Tenant record identifies a real person who can be bound to one Unit at a time through a Lease. A Lease has a start date, an end date, monthly rent amount, deposit amount, and a status of `active`, `expired`, or `terminated`. Creating a new Lease for a Unit that already has an `active` Lease must supersede (not stack with) the prior active Lease by terminating it and starting a new one atomically. Lease history is immutable: superseded or terminated Leases are retained for audit, and their rent and expense records remain attributable to the original Lease and Tenant. Lease transitions are mirrored as Operation events so that downstream projections (Tasks, notifications, dashboards) can react to a single business truth.

### FR-4 — Operations and Tasks

Operation is the truth; Task is the projection. Every real-world business action — rent due, rent paid, expense claimed, repair requested, repair completed, lease ending, move-out — is recorded once as an Operation row that carries its source, idempotency key, evidence references, and verification state. Tasks are derived from Operations for operator attention; they are never allowed to mutate business state directly. A dedup rule keyed on `(organization_id, operation_type, dedup_key)` prevents duplicate Operations from repeated Telegram messages, retries, or replays. Closing an Operation is permitted only when its underlying real-world condition is satisfied, and a Task marked `done` never closes its parent Operation unless explicit verification has been recorded.

### FR-5 — Rent

The rent flow models schedule, claim, evidence, verification, and settlement as distinct stages. The system generates scheduled rent Operations on a per-Lease cadence; a Secretary or Owner can submit a Payment Claim (text or attachment) which is matched against the schedule. Evidence (photos, screenshots, bank references) is attached to the claim and is owned by the Organization. Verification requires either an explicit Owner confirmation or a successful evidence match; partial payments are recorded as such, never silently promoted to `paid`. A rent period is considered `Paid` only when the cumulative verified amount equals or exceeds the due amount for that period; otherwise the period remains in an outstanding balance with the remaining amount clearly displayed. Reminders, replies and notifications are never treated as evidence of payment.

### FR-6 — Expense

Expense claims record a real-world outflow with a declared purpose and a category; purpose and category are independent dimensions, and a missing or mismatched purpose cannot be inferred from category. Each Expense Claim progresses through Claim → Evidence → Verification, mirroring the rent pipeline. An amount mismatch between the declared amount and the evidence (for example, a receipt total that differs from the claimed value) is surfaced as a verification failure and blocks auto-approval. Approval of an expense is an authorization step and is explicitly not equivalent to payment; a reminder or approval message must never close the underlying Operation. The expense is `closed` only when the verification has been recorded against real evidence within the Organization.

### FR-7 — Repair

Repairs follow a nine-state machine: `OPEN → ASSIGNED → QUOTED → APPROVED → IN_PROGRESS → COMPLETION_CLAIMED → VERIFIED → CLOSED`, plus a terminal `CANCELLED` reachable from any non-terminal state. Each transition writes an Operation event with actor, timestamp, and optional evidence; illegal transitions are rejected at the API layer. Quoted amounts and approved budgets are stored as `Numeric(14,2)` and never as floats. `COMPLETION_CLAIMED` triggers evidence collection and Owner verification, and only `VERIFIED` Operations are permitted to drive the state to `CLOSED`. A repair in `CANCELLED` retains its full history and never resurrects into `OPEN`.

### FR-8 — Lease renewal

Renewal is a deterministic pipeline: detect → contact → response → decision → execute → verify → close. Detection watches Lease end dates within a configurable window and emits a renewal Operation. Contact is recorded as a sent message through the Telegram channel with delivery confirmation. Response captures the tenant's reply and stores it as evidence attached to the renewal Operation. Decision is either `renew`, `terminate`, or `defer`, recorded with actor and timestamp. Execute applies the decision: renewal creates a successor Lease (FR-3), termination schedules move-out (FR-9), deferral pushes the decision window forward. Verify confirms that the executed change matches the decision, after which the renewal Operation closes. Each stage is independently re-entrant so that network failures or partial messages do not corrupt state.

### FR-9 — Move-out

Move-out combines inspection, deduction calculation, settlement, and an atomic final close. Inspection produces an evidence-attached condition report; deductions are recorded line-by-line with purpose, amount, and supporting evidence. Settlement reconciles deposit, deductions, outstanding rent, and any other balances, and produces a single net figure. The atomic final close transitions the Lease to `terminated`, the Unit to `vacant`, archives the active rent schedule, and retains the full history of Leases, Operations, evidence and audit logs. Either all of these sub-effects persist or none of them do; partial closes are forbidden. The system exposes the resulting financial summary as an immutable Settlement record for the Organization.

### FR-10 — Payments, evidence, money

All monetary values are stored as PostgreSQL `NUMERIC(14,2)` and manipulated in Python as `Decimal`; floats are forbidden anywhere in the payment, expense, rent or repair code paths. Every payment-mutating endpoint accepts an idempotency key, and repeated requests with the same key return the original result without side effects. All reads and writes are scoped by `organization_id` derived from the authenticated session, never from request bodies. Every Operation, payment, expense, repair transition and verification writes an audit timeline row with actor, timestamp, before-state and after-state. File attachments (photos, receipts, contracts) are owned by the Organization that produced them and are referenced by immutable IDs; their Organization scope is checked on every read.

### FR-11 — Telegram office

The Telegram bot presents a 3×2 persistent keyboard with `Home`, `Properties`, `Tasks`, `Rent`, `Expense`, `Archive`, and renders messages in the user's preferred language: `zh-CN` for Owners, `en-US` for Secretaries, with no mixing within a single user session. Deterministic fast paths (regex + intent table) handle the vast majority of inputs; at most one LLM call is permitted as fallback per message, and its result is treated as a hint, not a command, until validated against deterministic rules. The bot is silent in group chats: it never reacts to messages that do not explicitly address it. A canonical regression case — unit `7777` plus a tenant name plus a Philippine phone number in a single message — must update the existing tenant record on that unit and must never create a new Expense. All Telegram-driven mutations go through the same Operation pipeline as the Mini App.

### FR-12 — Mini App Owner console

The Mini App is a real, mobile-first Owner console sized for 390–430 px viewports, with light and dark themes and a minimum 44×44 px touch target on every interactive element. It exposes a dashboard of urgent items and next actions, plus dedicated views for property, unit, tenant and lease detail, and full operations surfaces for rent, expense, repair and task workflows. Renewal, move-out and settlement flows run end-to-end in the Mini App with the same semantics as the Telegram channel. Evidence, photos and documents are viewable inline and tied to their owning Operations. An activity archive presents the Organization's audit timeline; membership and settings pages let the Owner manage Secretaries and language preferences. Every screen is backed by real API calls against the production backend; no business data is held in `localStorage`, and no screen is a static mock.

## 4. Non-Functional Requirements

- **Correctness over coverage**: the system must enforce Business Truth First; reminders, approvals, and Task closures never silently advance Operation state.
- **Money and time integrity**: all monetary fields use `NUMERIC(14,2)` and `Decimal`; all timestamps use `timestamptz` and UTC-aware `datetime`. No `float` and no naive `datetime` may appear in business code paths.
- **Authorization**: every read and write is scoped by `organization_id` resolved from the authenticated session; cross-organization access is impossible by construction.
- **Idempotency**: every mutation that produces a real-world effect accepts and respects an idempotency key; replays are safe.
- **Auditability**: every state-changing action writes an immutable audit row referencing actor, timestamp, before and after state, and any attached evidence.
- **Determinism first**: Telegram and Mini App flows prefer deterministic logic; LLM usage is bounded, observable, and never the sole authority on a business decision.
- **Performance and responsiveness**: Telegram fast paths must respond within the bot's interactive budget; Mini App initial paint on a mid-tier mobile device must remain under the product's perceived-latency budget for the dashboard.
- **Internationalization**: Owner and Secretary surfaces support `zh-CN` and `en-US` respectively, with no runtime language mixing inside a session.
- **Accessibility and usability**: 44×44 px touch targets, readable contrast in both themes, and no interaction that requires hover-only affordances.
- **Observability**: structured logs, request tracing, and per-Operation event timelines are available for production debugging.

## 5. Acceptance Criteria

Acceptance is binary. The rewrite is accepted only when every gate below passes in a single CI run on the protected branch:

- **Database**: `alembic upgrade head` applies cleanly against a fresh database with no manual fixes and no down-migrations required for a clean rollback path.
- **Backend tests**: `pytest` passes with the full suite, including the canonical Telegram regression for unit `7777` + tenant name + PH phone (update, not create expense) and the FR-7 illegal-transition tests.
- **Frontend build**: `vite build` succeeds for the Mini App with no type or lint errors, and a Playwright smoke run exercises the dashboard, one rent detail, one expense detail, one repair transition, and the activity archive.
- **CI gates**: three CI gates run — lint/type, backend tests, and frontend build + smoke — and all three must be green.
- **Deploy stages**: four deploy stages (build, migrate, smoke, promote) complete in order; promote is gated on the prior three succeeding.
- **Coverage matrix**: the FR-1 through FR-12 coverage matrix totals to 100% — every functional requirement has at least one automated test or one executable acceptance check that proves it.
- **No silent shortcuts**: no test is deleted, skipped, `xfail`-ed, or commented out to manufacture a PASS; no real failing assertion is hidden behind a mock.

## 6. Out of Scope

The following items are explicitly excluded from this rewrite and are not delivered under this spec:

- The 17-door Owner governance workflow and its associated permissions model.
- Qualification probes and any automation that opens, answers, or scores them.
- Reviewer workflow, multi-role review queues, and approval chains beyond Owner authorization.
- Milestone and Phase planning documents, retrospectives, and progress reports.
- Using `localStorage` (or any client-side store) as a business database; the Mini App is API-backed only.
- AI copilot free-form tool calls that bypass deterministic business rules.
- Long polling and any Telegram-side long-lived connections beyond standard webhook delivery.
