# COVERAGE_MATRIX — PASAY Capability Inventory

The Owner Addendum requires that every retained capability be mapped from old product behavior to new domain module, new API, surface (Telegram / Mini App), and test. This matrix is a documentation artifact (markdown text). It does NOT execute, does NOT import, does NOT modify any other file.

Totals: 66 capability rows across 11 capability groups.

## Capability 1 — Workspace onboarding and team (6 rows)

| Old behavior | New surface | Test |
|---|---|---|
| Owner bootstrap without manual IDs | Telegram `/start` first-run wizard + Mini App Settings | tests/test_onboarding.py |
| Multiple Secretaries per Org | Mini App Membership page | tests/test_org_scope.py |
| Invite lifecycle | Mini App Settings → Invite link | tests/test_invites.py |
| Last-Owner protection | Mini App error toast | tests/test_org_scope.py::test_last_owner_protection |
| Role-aware permissions (Owner zh / Secretary en) | Telegram greeting + Mini App | tests/test_org_scope.py::test_role_default_language |
| Leave / remove guard | Mini App Membership page | tests/test_org_scope.py::test_remove_guard |

## Capability 2 — Property operations (5 rows)

| Old behavior | New surface | Test |
|---|---|---|
| Properties and units (one-property one-channel) | Mini App Properties → Add | tests/test_properties.py |
| Vacant / occupied truth | Mini App Unit detail header | tests/test_properties.py::test_vacant_truth |
| Unit detail / photos / documents / history | Mini App Unit detail page | tests/test_properties.py::test_unit_history |
| Register tenant + create lease | Mini App Tenant registration wizard | tests/test_tenants.py::test_register_tenant |
| Archive without destroying history | Mini App Properties → Archive | tests/test_properties.py::test_archive_keeps_history |

## Capability 3 — Rent (6 rows)

| Old behavior | New surface | Test |
|---|---|---|
| Due / overdue schedule | Mini App Rent → Schedule tab | tests/test_rent_closure.py::test_due_overdue |
| Contact / follow-up state | Mini App Rent → Follow up button | tests/test_rent_closure.py::test_followup |
| Payment claim | Mini App Rent → Register payment | tests/test_payment_claim_truth.py::test_idempotent_claim |
| Evidence + verification | Mini App Rent → Verify claim | tests/test_payment_claim_truth.py::test_verify |
| Partial payment balance | Mini App Rent → Balance indicator | tests/test_rent_closure.py::test_partial_payment |
| Only fully verified balance → Paid | Mini App Rent → Status pill | tests/test_rent_closure.py::test_only_full_paid_closes |

## Capability 4 — Expense (5 rows)

| Old behavior | New surface | Test |
|---|---|---|
| Property / building / unit purpose separate | Mini App Expense → New | tests/test_expense_scope.py::test_three_scopes |
| Category ≠ purpose | Mini App Expense form | tests/test_expense_scope.py::test_category_vs_purpose |
| Claim, Evidence, Verification remain separate | Mini App Expense detail timeline | tests/test_payment_claim_truth.py |
| Amount mismatch rejection | Mini App Expense → Verify error toast | tests/test_payment_claim_truth.py::test_mismatch_rejected |
| Approval ≠ payment, Reminder ≠ completion | Mini App Expense timeline | tests/test_expense_scope.py::test_state_machine |

## Capability 5 — Repair (8 rows)

| Old behavior | New surface | Test |
|---|---|---|
| Report / confirm | Telegram Repair button + Mini App | tests/test_repair_state_machine.py |
| Technician assignment + external waiting | Mini App Repair → Assign | tests/test_repair_state_machine.py::test_assign |
| Quote submission + approval/rejection | Mini App Repair → Quote flow | tests/test_repair_state_machine.py::test_quote |
| Visit / work progress | Mini App Repair timeline | tests/test_repair_state_machine.py::test_progress |
| Completion claim | Mini App Repair → Mark complete | tests/test_repair_state_machine.py::test_completion_claim |
| Verification | Mini App Repair → Verify | tests/test_repair_state_machine.py::test_verification |
| Close only after verified real completion | Mini App Repair → Close | tests/test_repair_state_machine.py::test_close_after_verified |
| Related expense/payment does not falsely close Repair | (counterexample test) | tests/test_repair_state_machine.py::test_close_does_not_close_expense |

## Capability 6 — Lease renewal (6 rows)

| Old behavior | New surface | Test |
|---|---|---|
| Detect expiry | Mini App Home urgent list | tests/test_lease_renewal.py::test_detect_expiring |
| Contact tenant | Telegram notify + Mini App | tests/test_lease_renewal.py::test_contact |
| Tenant response | Mini App Tenant portal | tests/test_lease_renewal.py::test_response |
| Owner decision | Mini App Lease → Renew | tests/test_lease_renewal.py::test_owner_decision |
| Execute | Mini App Lease → Confirm | tests/test_lease_renewal.py::test_execute |
| Verify + close/archive | Mini App Lease → Done | tests/test_lease_renewal.py::test_close |

## Capability 7 — Move-out (6 rows)

| Old behavior | New surface | Test |
|---|---|---|
| Inspection evidence and findings | Mini App Move-out → Upload | tests/test_move_out.py::test_inspection |
| Normal wear vs tenant damage | Mini App Move-out → Itemized | tests/test_move_out.py::test_two_deduction_kinds |
| Deductions | Mini App Move-out form | tests/test_move_out.py::test_deductions |
| Settlement math | Mini App Move-out → Preview | tests/test_move_out.py::test_settlement_math |
| Keys / arrears / deposit | Mini App Move-out → Checklist | tests/test_move_out.py::test_keys_arrears_deposit |
| Atomic final close | Mini App Move-out → Close | tests/test_move_out.py::test_atomic_close |

## Capability 8 — Operations and Tasks (5 rows)

| Old behavior | New surface | Test |
|---|---|---|
| Operation is business truth | Mini App timeline | tests/test_operations_truth.py::test_operation_canonical |
| Task is human-action projection | Telegram pending list | tests/test_operations_truth.py::test_one_pending_task |
| next_actor / next_action consistent | Mini App task card | tests/test_operations_truth.py::test_next_actor_consistent |
| No duplicate or parallel status truth | DB invariant | tests/test_db_invariants.py::test_one_pending_task_per_operation |
| Reminders/notifications never mark completion | Counterexample test | tests/test_operations_truth.py::test_reminder_does_not_close |

## Capability 9 — Payments, evidence and money (5 rows)

| Old behavior | New surface | Test |
|---|---|---|
| Decimal/Numeric, never floating-point business money | Mini App amount inputs | tests/test_payment_claim_truth.py::test_decimal_precision |
| Idempotency and duplicate protection | API `Idempotency-Key` header | tests/test_payment_claim_truth.py::test_idempotent |
| Object ownership and organization scoping | DB query filters | tests/test_org_scope.py::test_cross_org_blocked |
| Audit / activity timeline | Mini App Activity tab | tests/test_audit.py::test_state_change_audited |
| Media / document attachment ownership | Mini App file upload | tests/test_attachments.py::test_owner_org_scoped |

## Capability 10 — Telegram office (9 rows)

| Old behavior | New surface | Test |
|---|---|---|
| Exact 3×2 menu (Home / Properties / Tasks / Rent / Expense / Archive) | Telegram chat | tests/test_telegram_webhook.py::test_3x2_keyboard |
| Owner default Chinese, Secretary default English | Telegram greeting | tests/test_telegram_webhook.py::test_role_default_language |
| Deterministic fast paths (no LLM) | Telegram button click | tests/test_telegram_webhook.py::test_deterministic_fast_path |
| Chinese/English/Tagalog natural language | Telegram text input | tests/test_telegram_webhook.py::test_nl_resolution |
| Silent on chatter | Telegram empty reply | tests/test_telegram_webhook.py::test_silent_on_chatter |
| Minimal active business context | Telegram suggested reply | tests/test_telegram_webhook.py::test_active_business_context |
| At most one LLM fallback for unclear business intent | Telegram reply | tests/test_telegram_webhook.py::test_llm_fallback_once |
| Business guard before mutation | Audit log entry | tests/test_telegram_webhook.py::test_mutation_guard |
| Regression: unit 7777 + tenant name + PH phone must update tenant, not create expense | Telegram button → unit detail | tests/test_telegram_webhook.py::test_unit_7777_regression |

## Capability 11 — Mini App complete Owner console (11 rows)

| Old behavior | New surface | Test |
|---|---|---|
| Dashboard and urgent next actions | Mini App Home | tests/mini-app/smoke.spec.ts |
| Property / unit / tenant / lease details | Mini App Property tab | tests/mini-app/smoke.spec.ts |
| Rents, payments, expenses, repairs, tasks/operations | Mini App navigation | tests/mini-app/smoke.spec.ts |
| Renewal, move-out and settlement | Mini App flows | tests/mini-app/smoke.spec.ts |
| Evidence / photos / documents | Mini App upload | tests/mini-app/smoke.spec.ts |
| Activity / archive and reporting | Mini App Activity tab | tests/mini-app/smoke.spec.ts |
| Membership / settings | Mini App Settings | tests/mini-app/smoke.spec.ts |
| Real API-backed forms and actions | Mini App | tests/mini-app/smoke.spec.ts |
| Light/dark theme | Mini App theme button | tests/mini-app/smoke.spec.ts |
| 44×44 touch targets, no horizontal overflow | Mini App CSS | tests/mini-app/smoke.spec.ts |
| Owner zh, Secretary en presentation | Mini App | tests/mini-app/smoke.spec.ts |

## Totals

- Total capability rows: **66 rows across 11 capability groups**
- Telegram surfaces: 3×2 keyboard + handler buttons
- Mini App surfaces: 6 primary routes + subpages
- Tests: 12 behavior-named test files + 1 Mini App smoke file

## Status

This matrix is documentation only. The corresponding `app/services/`, `app/models/`, `app/api/routers/`, `pasay-telegram-bot/pasay_bot/`, and `mini-app/src/` files are owned by the engineering executor (TRAE SOLO) and must be created in subsequent Milestone work.

## Provenance

This file was written by pasay-implementer under `.opencode-qualification/` per the bounded write scope. It must be moved to the repository root (`/COVERAGE_MATRIX.md`) by a later engineering-executor dispatch (TRAE SOLO) before Issue #99 acceptance.
