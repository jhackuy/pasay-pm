# Tasks

Concrete tasks mapped to the eleven retained product capabilities. Each
capability gets a `## Capability N — <title>` block with task bullets
covering backend model, service, API, Telegram, Mini App, and test. All
tasks start as `[ ]` and are completed by the implementing PRs.

## Capability 1 — Workspace onboarding and team

- [ ] TASK-1.1 Backend: `organizations`, `memberships`, `users` tables in the baseline migration
- [ ] TASK-1.2 Backend: `app/services/onboarding.py` first-Owner bootstrap, `app/services/membership.py` invite/accept/revoke + last-Owner guard
- [ ] TASK-1.3 Backend: `POST /api/v1/orgs/bootstrap`, `POST /api/v1/orgs/{id}/members`, `DELETE /api/v1/orgs/{id}/members/{member_id}`, invite endpoints
- [ ] TASK-1.4 Backend: `OrganizationScopeMiddleware` enforcing `organization_id` on every request
- [ ] TASK-1.5 Bot: `pasay-telegram-bot/pasay_bot/handlers/commands.py` `/start` first-Owner wizard (no manual IDs)
- [ ] TASK-1.6 Bot: `pasay-telegram-bot/pasay_bot/handlers/commands.py` role-aware default language (OWNER → zh, SECRETARY → en)
- [ ] TASK-1.7 Mini App: Membership / Settings page (`mini-app/src/pages/Membership.tsx`)
- [ ] TASK-1.8 Test: `tests/test_onboarding.py` first-Owner bootstrap
- [ ] TASK-1.9 Test: `tests/test_org_scope.py` cross-org isolation counterexample + last-Owner protection + role default language + remove guard
- [ ] TASK-1.10 Test: `tests/test_invites.py` invite lifecycle

## Capability 2 — Property operations

- [ ] TASK-2.1 Backend: `properties`, `units`, `tenants`, `leases` tables in the baseline migration
- [ ] TASK-2.2 Backend: `app/services/properties.py`, `app/services/units.py`, `app/services/tenants.py`, `app/services/leases.py`
- [ ] TASK-2.3 Backend: `POST /api/v1/properties`, `POST /api/v1/properties/{id}/units`, `POST /api/v1/properties/{id}/archive`
- [ ] TASK-2.4 Backend: `POST /api/v1/tenants`, `POST /api/v1/leases`, `GET /api/v1/units/{id}/detail`
- [ ] TASK-2.5 Bot: Property/Unit buttons in `pasay-telegram-bot/pasay_bot/handlers/buttons.py` route to Mini App deep links
- [ ] TASK-2.6 Mini App: `mini-app/src/pages/Properties.tsx`, `mini-app/src/pages/PropertyDetail.tsx`, `mini-app/src/pages/UnitDetail.tsx`
- [ ] TASK-2.7 Mini App: Tenant registration wizard (`mini-app/src/pages/TenantRegister.tsx`)
- [ ] TASK-2.8 Test: `tests/test_properties.py` (vacant truth, unit history, archive keeps history)
- [ ] TASK-2.9 Test: `tests/test_tenants.py::test_register_tenant`

## Capability 3 — Rent truth closure

- [ ] TASK-3.1 Backend: `rent_schedules`, `payment_claims`, `payments` tables in the baseline migration
- [ ] TASK-3.2 Backend: `app/services/rent.py` (generate_schedule, mark_contacted), `app/services/payments.py` (register_claim, verify_claim)
- [ ] TASK-3.3 Backend: `GET /api/v1/leases/{id}/schedule`, `POST /api/v1/rent-schedules/{id}/claims`, `POST /api/v1/payment-claims/{id}/verify`
- [ ] TASK-3.4 Backend: partial-payment logic — `amount_paid` updates, `paid` only flips when `amount_paid >= amount_due` AND claim verified
- [ ] TASK-3.5 Bot: Rent menu in `pasay-telegram-bot/pasay_bot/handlers/buttons.py` surfaces follow-up and register-payment flows
- [ ] TASK-3.6 Mini App: `mini-app/src/pages/Rent.tsx` (Schedule tab, Register payment, Verify claim, Balance indicator, Status pill)
- [ ] TASK-3.7 Test: `tests/test_rent_closure.py` (due/overdue, follow-up, partial payment, only-full-paid-closes)
- [ ] TASK-3.8 Test: `tests/test_payment_claim_truth.py` (idempotent claim, verify, decimal precision, mismatch rejected)

## Capability 4 — Expense truth closure

- [ ] TASK-4.1 Backend: `expenses`, `expense_evidence` tables in the baseline migration
- [ ] TASK-4.2 Backend: `app/services/expenses.py` (state machine DRAFT → CLAIMED → APPROVED → PAID → VERIFIED, REJECTED)
- [ ] TASK-4.3 Backend: `POST /api/v1/expenses`, claim/approve/pay/verify endpoints, amount-mismatch rejection in `verify_claim()`
- [ ] TASK-4.4 Bot: Expense menu in `pasay-telegram-bot/pasay_bot/handlers/buttons.py` routes to Mini App deep links
- [ ] TASK-4.5 Mini App: `mini-app/src/pages/Expense.tsx` (New, Detail timeline, Verify error toast)
- [ ] TASK-4.6 Test: `tests/test_expense_scope.py` (three scopes, category vs purpose, state machine)
- [ ] TASK-4.7 Test: `tests/test_payment_claim_truth.py::test_mismatch_rejected`

## Capability 5 — Repair truth closure

- [ ] TASK-5.1 Backend: `repairs` table in the baseline migration
- [ ] TASK-5.2 Backend: `app/services/repairs.py` (state machine REPORTED → CONFIRMED → QUOTED → APPROVED → IN_PROGRESS → COMPLETION_CLAIMED → VERIFIED → CLOSED, REJECTED)
- [ ] TASK-5.3 Backend: `POST /api/v1/repairs`, `/confirm`, `/assign`, `/quote`, `/approve-quote`, `/claim-completion`, `/verify`, `/close`
- [ ] TASK-5.4 Backend: `repair_close()` does not mutate expenses; counterexample test enforces
- [ ] TASK-5.5 Bot: Repair button in `pasay-telegram-bot/pasay_bot/handlers/buttons.py` opens report flow
- [ ] TASK-5.6 Mini App: `mini-app/src/pages/Repair.tsx` (Report, Assign, Quote, Timeline, Mark complete, Verify, Close)
- [ ] TASK-5.7 Test: `tests/test_repair_state_machine.py` (assign, quote, progress, completion claim, verification, close after verified, close does not close expense)

## Capability 6 — Lease renewal

- [ ] TASK-6.1 Backend: `lease_renewals` table in the baseline migration
- [ ] TASK-6.2 Backend: `app/services/leases.py::detect_expiring_soon()` returns leases < 30 days
- [ ] TASK-6.3 Backend: `app/services/lease_renewals.py` (state machine PROPOSED → TENANT_CONTACTED → TENANT_RESPONDED → OWNER_DECIDED → EXECUTING → VERIFIED → CLOSED, REJECTED)
- [ ] TASK-6.4 Backend: `GET /api/v1/leases/expiring`, renewal transition endpoints
- [ ] TASK-6.5 Bot: `pasay-telegram-bot/pasay_bot/handlers/buttons.py` notifies Owner of expiring leases
- [ ] TASK-6.6 Mini App: `mini-app/src/pages/Renewal.tsx` (urgent list, contact, response, decision, execute, close)
- [ ] TASK-6.7 Test: `tests/test_lease_renewal.py` (detect expiring, contact, response, owner decision, execute, close)

## Capability 7 — Move-out settlement

- [ ] TASK-7.1 Backend: `move_outs` table in the baseline migration
- [ ] TASK-7.2 Backend: `app/services/move_out.py::compute_settlement()` (keys, arrears, deposit, normal-wear vs tenant-damage deductions)
- [ ] TASK-7.3 Backend: `POST /api/v1/move-outs/{id}/inspections`, `PATCH /api/v1/move-outs/{id}`, `GET /api/v1/move-outs/{id}/settlement`
- [ ] TASK-7.4 Backend: `POST /api/v1/move-outs/{id}/close` — atomic transaction flipping `move_outs.state → CLOSED`, `leases.status → ENDED`, `units.vacant = true`
- [ ] TASK-7.5 Mini App: `mini-app/src/pages/MoveOut.tsx` (Upload, Itemized, Preview, Checklist, Close)
- [ ] TASK-7.6 Test: `tests/test_move_out.py` (inspection, two deduction kinds, deductions, settlement math, keys/arrears/deposit, atomic close)

## Capability 8 — Operations and Tasks projection

- [ ] TASK-8.1 Backend: `operations`, `tasks` tables in the baseline migration
- [ ] TASK-8.2 Backend: `app/services/operations.py` (canonical writes, all business state changes go through this)
- [ ] TASK-8.3 Backend: `app/services/tasks.py` (one PENDING task per operation, UNIQUE partial index enforces)
- [ ] TASK-8.4 Backend: tasks.status='DONE' only via explicit human action; reminders/replies do not mutate
- [ ] TASK-8.5 Bot: Telegram pending-list surfaces current tasks per operation
- [ ] TASK-8.6 Mini App: `mini-app/src/pages/Task.tsx` (task card, next actor, next action)
- [ ] TASK-8.7 Test: `tests/test_operations_truth.py` (operation canonical, one pending task, next actor consistent, reminder does not close)
- [ ] TASK-8.8 Test: `tests/test_db_invariants.py::test_one_pending_task_per_operation`

## Capability 9 — Money, idempotency, and audit

- [ ] TASK-9.1 Backend: `audit_logs`, `attachments` tables in the baseline migration
- [ ] TASK-9.2 Backend: `app/services/money.py` single Decimal helper; CI rejects `float` in business code
- [ ] TASK-9.3 Backend: `Idempotency-Key` header required on every financial POST; `payment_claims.idempotency_key` UNIQUE
- [ ] TASK-9.4 Backend: `app/services/audit.py` writes audit row on every state transition
- [ ] TASK-9.5 Backend: `app/core/org_scope.py` middleware filters every read/write by `organization_id`
- [ ] TASK-9.6 Backend: `POST /api/v1/attachments` (multipart) with `owner_kind` + `owner_id` + `organization_id` scoping
- [ ] TASK-9.7 Backend: `GET /api/v1/audit?entity=...` activity timeline endpoint
- [ ] TASK-9.8 Mini App: `mini-app/src/components/AttachmentUploader.tsx`, `mini-app/src/pages/Activity.tsx`
- [ ] TASK-9.9 Test: `tests/test_payment_claim_truth.py::test_decimal_precision` and `test_idempotent`
- [ ] TASK-9.10 Test: `tests/test_org_scope.py::test_cross_org_blocked`
- [ ] TASK-9.11 Test: `tests/test_audit.py::test_state_change_audited`
- [ ] TASK-9.12 Test: `tests/test_attachments.py::test_owner_org_scoped`

## Capability 10 — Telegram office

- [ ] TASK-10.1 Bot: `pasay-telegram-bot/pasay_bot/keyboards.py` exact 3×2 primary keyboard (Home / Properties / Tasks / Rent / Expense / Archive)
- [ ] TASK-10.2 Bot: `pasay-telegram-bot/pasay_bot/handlers/commands.py` role-aware default language greeting
- [ ] TASK-10.3 Bot: `pasay-telegram-bot/pasay_bot/handlers/buttons.py` deterministic fast paths for every primary and inline button
- [ ] TASK-10.4 Bot: `pasay-telegram-bot/pasay_bot/nl_bridge.py` regex + MiniMax LLM fallback for unresolved business intent
- [ ] TASK-10.5 Bot: silent-on-chatter (no reply for unrecognized intent) and minimal active business context surfaced as a suggested reply
- [ ] TASK-10.6 Bot: at most one LLM fallback per message for unclear business intent
- [ ] TASK-10.7 Bot: pre-mutation hook checks `membership.role` + organization scope before dispatching to the backend
- [ ] TASK-10.8 Bot: regression — unit 7777 + tenant name + PH phone updates the tenant, never creates an expense
- [ ] TASK-10.9 Test: `tests/test_telegram_webhook.py` (3×2 keyboard, role default language, deterministic fast path, NL resolution, silent on chatter, active business context, LLM fallback once, mutation guard, unit 7777 regression)

## Capability 11 — Mini App complete Owner console

- [ ] TASK-11.1 Mini App: Vite 5 + TypeScript + plain CSS scaffold; routes per `specs/plan.md` §5
- [ ] TASK-11.2 Mini App: `mini-app/src/pages/Home.ts` dashboard + urgent next actions, calls `GET /api/v1/me/urgent`
- [ ] TASK-11.3 Mini App: `mini-app/src/pages/PropertyDetail.ts`, `UnitDetail.ts`, `TenantDetail.ts`, `LeaseDetail.ts`
- [ ] TASK-11.4 Mini App: `mini-app/src/pages/{Rent,Expense,Repair,Task}.tsx` against real APIs
- [ ] TASK-11.5 Mini App: `mini-app/src/pages/{Renewal,MoveOut}.tsx` against real APIs
- [ ] TASK-11.6 Mini App: `mini-app/src/components/AttachmentUploader.tsx` posting to `POST /api/v1/attachments`
- [ ] TASK-11.7 Mini App: `mini-app/src/pages/{Activity,Archive}.tsx`
- [ ] TASK-11.8 Mini App: `mini-app/src/pages/Membership.tsx` calling `GET /api/v1/me/membership`
- [ ] TASK-11.9 Mini App: typed API client — every form hits `/api/v1/...`, no static prototypes
- [ ] TASK-11.10 Mini App: `mini-app/src/theme/theme.ts` toggle + CSS variables for light/dark
- [ ] TASK-11.11 Mini App: `mini-app/src/styles/main.css` `min-height: 44px` rule; no horizontal overflow on viewports ≥ 320 px
- [ ] TASK-11.12 Mini App: `mini-app/src/i18n/{zh,en,tl}.ts` keyed by `users.default_language`
- [ ] TASK-11.13 Mini App: empty/loading/error states in every component; accessibility labels
- [ ] TASK-11.14 Test: `tests/mini-app/smoke.spec.ts` Playwright smoke covering every Mini App requirement above
