# Product Coverage Matrix — PASAY Rent Clean Rewrite

> **Hard acceptance contract.** Every retained capability from the legacy PASAY
> implementation is mapped below to the new code. Marking a row out of scope
> requires explicit evidence that it was obsolete or only historical governance.
> The PR will be rejected unless `Unimplemented / missing: 0 (must be 0)`.

## Mapping schema

| # | Old product rule / field / behavior / regression | New domain module | New API endpoint(s) | Telegram or Mini App surface | Test |

---

## 1. Workspace onboarding and team

| # | Old product rule / field / behavior / regression | New domain module | New API endpoint(s) | Telegram or Mini App surface | Test |
|---|---|---|---|---|---|
| 1 | `app/models/membership.py::Organization` (create org) | `app/services/workspaces.py::create_organization` | `POST /api/v1/workspaces` | Mini App `#/workspaces/new` | `tests/api/test_workspaces.py::test_create_organization` |
| 2 | First-Owner bootstrap without manual IDs (`app/services/onboarding.py::bootstrap_owner`) | `app/services/workspaces.py::bootstrap_owner` | `POST /api/v1/workspaces/bootstrap` | Telegram `/start` | `tests/api/test_workspaces.py::test_bootstrap_owner_no_manual_ids` |
| 3 | Multiple Secretaries per org (`app/models/membership.py::Membership` with `role='SECRETARY'`) | `app/services/workspaces.py::add_secretary` | `POST /api/v1/workspaces/{org_id}/secretaries` | Mini App `#/settings/members` | `tests/api/test_workspaces.py::test_multiple_secretaries` |
| 4 | Invite lifecycle PENDING/ACCEPTED/CANCELLED/EXPIRED (`app/models/membership.py::SecretaryInvite`) | `app/services/workspaces.py::invite_lifecycle` | `POST /api/v1/workspaces/{org_id}/invites`, `POST /api/v1/invites/{invite_id}/accept\|cancel` | Telegram deep-link accept | `tests/api/test_invites.py::test_invite_lifecycle` |
| 5 | Remove/leave guards (`app/services/membership.py::remove_membership`) | `app/services/workspaces.py::remove_member` | `DELETE /api/v1/workspaces/{org_id}/members/{member_id}` | Mini App `#/settings/members` remove button | `tests/api/test_workspaces.py::test_remove_member` |
| 6 | Last-Owner protection (`app/services/membership.py` raises if last ACTIVE OWNER) | `app/services/workspaces.py::ensure_owner_protected` (enforced in `remove_member` and `leave_workspace`) | `DELETE /api/v1/workspaces/{org_id}/members/{member_id}` (409 on last OWNER) | Mini App disable remove button + error toast | `tests/api/test_workspaces.py::test_last_owner_protected` |
| 7 | Role-aware permissions OWNER vs SECRETARY (`app/api/deps.py::require_roles`) | `app/services/workspaces.py::require_role_aware` (re-exported from `app/core/permissions.py`) | every endpoint under `/api/v1/` honours `require_role` | Telegram menus hide admin-only buttons | `tests/api/test_permissions.py::test_role_aware_permissions` |
| 8 | Role-aware default language Owner=zh-CN, Secretary=en-US (`pasay-telegram-bot/pasay_bot/roles.py`) | `app/services/workspaces.py::default_language_for_role` | embedded in `/api/v1/users/me` response | Telegram keyboards localized, Mini App picks `i18n` bundle | `tests/telegram/test_keyboards.py::test_default_language_per_role` |

## 2. Property operations

| # | Old product rule / field / behavior / regression | New domain module | New API endpoint(s) | Telegram or Mini App surface | Test |
|---|---|---|---|---|---|
| 1 | `app/models/property.py::Property` CRUD | `app/services/properties.py::PropertyService.upsert\|archive` | `POST /api/v1/properties`, `GET /api/v1/properties` | Mini App `#/properties`, `#/properties/new` | `tests/api/test_properties.py::test_property_crud` |
| 2 | `app/models/property.py::Unit` CRUD with status vacant/occupied/maintenance | `app/services/properties.py::UnitService` | `POST /api/v1/properties/{property_id}/units` | Mini App `#/properties/{id}/units` | `tests/api/test_units.py::test_unit_status` |
| 3 | One-property/one-channel authority (`app/models/property_channel.py::UnitChannelBinding` unique on `(unit_id, purpose)`) | `app/services/properties.py::ChannelBindingService` | `POST /api/v1/properties/{property_id}/units/{unit_id}/channels` | Telegram archive/business_group binding | `tests/api/test_property_channel.py::test_one_property_one_channel` |
| 4 | Vacant/occupied truth drives notifications (`app/services/property_channel.py` + unit status) | `app/services/properties.py::UnitService.set_status` emits `unit_lifecycle_events` row | `PATCH /api/v1/units/{unit_id}` `status` field | Mini App status badge | `tests/api/test_units.py::test_vacant_occupied_truth` |
| 5 | Unit detail with photos/documents and history (`app/api/routers/evidence.py`, `app/models/attachment.py`) | `app/services/properties.py::UnitService.detail` joins `attachments`, `unit_lifecycle_events` | `GET /api/v1/units/{unit_id}` | Mini App `#/properties/{property_id}/units/{unit_id}` | `tests/api/test_units.py::test_unit_detail_with_history` |
| 6 | Register tenant + create lease from property (`app/services/leases.py::create_lease_with_tenant`) | `app/services/leases.py::LeaseService.create_with_tenant` | `POST /api/v1/leases` with embedded tenant | Mini App `#/properties/{id}/register-tenant` | `tests/api/test_leases.py::test_register_tenant_create_lease` |
| 7 | Archive property without destroying history (`Property.archived_at`) | `app/services/properties.py::PropertyService.archive` | `POST /api/v1/properties/{property_id}/archive` | Mini App `#/properties/{id}` archive button + confirm | `tests/api/test_properties.py::test_archive_preserves_history` |
| 8 | `UnitLifecycleEvent` stream (`app/models/property.py::UnitLifecycleEvent`) | `app/services/properties.py::UnitLifecycleService.record` | `POST /api/v1/units/{unit_id}/events` | (audit only) | `tests/api/test_units.py::test_lifecycle_event_stream` |

## 3. Rent

| # | Old product rule / field / behavior / regression | New domain module | New API endpoint(s) | Telegram or Mini App surface | Test |
|---|---|---|---|---|---|
| 1 | Due/overdue schedule from `Lease.monthly_rent` (`app/services/rent_payment_truth.py::due_for_lease`) | `app/services/rent.py::RentScheduleService.upcoming` | `GET /api/v1/rent/schedule?lease_id=&from=&to=` | Telegram `Rent` button → overdue list | `tests/api/test_rent.py::test_due_overdue_schedule` |
| 2 | Contact/follow-up state on lease (`Lease.contact_status`, NL bridge `pasay-telegram-bot/pasay_bot/handlers/conversation.py`) | `app/services/rent.py::RentContactService.set_contact_status` | `PATCH /api/v1/leases/{lease_id}/contact` | Telegram inline keyboard `Tenant replied` / `Wrong number` | `tests/api/test_rent.py::test_contact_followup_state` |
| 3 | `RentPaymentClaim` create with idempotency (`app/models/rent_payment_claim.py`, partial unique `uq_rent_payment_claims_idempotency_key`) | `app/services/rent.py::RentClaimService.create` | `POST /api/v1/rent/claims` | Telegram `Record rent` conversation + Mini App `#/finance/claim-rent` | `tests/api/test_rent.py::test_rent_claim_idempotency` |
| 4 | Evidence upload for a claim (`app/api/routers/evidence.py`) | `app/services/evidence.py::AttachmentService.upload_for_owner('rent_claim', id)` | `POST /api/v1/rent/claims/{claim_id}/evidence` | Telegram send photo in conversation, Mini App camera button | `tests/api/test_rent.py::test_evidence_upload_for_claim` |
| 5 | Verification / rejection transitions PENDING→VERIFIED/FAILED (`app/services/rent_claims.py::verify_rent_claim`) | `app/services/rent.py::RentClaimService.verify\|reject` | `POST /api/v1/rent/claims/{claim_id}/verify`, `.../reject` | Telegram inline `Verify` / `Reject` buttons | `tests/api/test_rent.py::test_verify_reject_claim` |
| 6 | Partial payment balance + continued collection (`app/services/rent_payment_truth.py::balance`) | `app/services/rent.py::RentBalanceService.balance` | `GET /api/v1/leases/{lease_id}/rent-balance` | Mini App `#/finance/balance` shows partial | `tests/api/test_rent.py::test_partial_rent_continues_collection` |
| 7 | Only fully verified balance becomes Paid (`tests/test_rent_closure_m2.py::test_full_verified_only_paid`) | `app/services/rent.py::RentBalanceService.is_paid` | computed in `/api/v1/leases/{lease_id}/rent-balance` | Mini App status pill `Paid` only when verified sum ≥ due | `tests/api/test_rent.py::test_full_verified_only_paid` |
| 8 | Rent activity history + Operation closure (`app/services/rent_payment_truth.py::close_rent_operation`) | `app/services/rent.py::RentService.close_operation` | `POST /api/v1/rent/operations/{operation_id}/close` | Mini App timeline + audit | `tests/api/test_rent.py::test_rent_operation_closure` |

## 4. Expense

| # | Old product rule / field / behavior / regression | New domain module | New API endpoint(s) | Telegram or Mini App surface | Test |
|---|---|---|---|---|---|
| 1 | Purpose field property/building/unit (`Expense.purpose`) | `app/services/expenses.py::ExpenseService.upsert` validates `purpose ∈ {property,building,unit,other}` | `POST /api/v1/expenses` | Mini App `#/finance/expenses/new` purpose dropdown | `tests/api/test_expenses.py::test_expense_purpose_validated` |
| 2 | Category ≠ purpose (independent fields) | `app/services/expenses.py::ExpenseService.upsert` allows any `category` independent of `purpose` | `POST /api/v1/expenses` | Mini App two separate fields | `tests/api/test_expenses.py::test_category_purpose_independent` |
| 3 | `ExpensePaymentClaim` separate from `Expense` (`app/models/expense_claim.py`) | `app/services/expenses.py::ExpenseClaimService` | `POST /api/v1/expenses/{expense_id}/claims` | Telegram `Record payment` conversation | `tests/api/test_expenses.py::test_claim_separate_from_expense` |
| 4 | Evidence upload for claim (`app/api/routers/evidence.py`) | `app/services/evidence.py::AttachmentService.upload_for_owner('expense_claim', id)` | `POST /api/v1/expenses/claims/{claim_id}/evidence` | Telegram photo in conversation | `tests/api/test_expenses.py::test_evidence_for_expense_claim` |
| 5 | Verification separate from approval (`app/services/expense_claims.py::verify_expense_claim`) | `app/services/expenses.py::ExpenseClaimService.verify` | `POST /api/v1/expenses/claims/{claim_id}/verify` | Mini App `Verify payment` button | `tests/api/test_expenses.py::test_verification_separate` |
| 6 | Amount mismatch rejection (`Expense.amount` vs `ExpensePaymentClaim.claimed_amount`) | `app/services/expenses.py::ExpenseClaimService.validate_amount` | returns 422 if mismatch exceeds tolerance | Mini App inline error toast | `tests/api/test_expenses.py::test_amount_mismatch_rejected` |
| 7 | Approval ≠ payment (status transitions tracked independently) | `app/services/expenses.py::ExpenseService.approve` only flips `status='approved'`; no money movement | `POST /api/v1/expenses/{expense_id}/approve` | Mini App status pill changes | `tests/api/test_expenses.py::test_approval_not_payment` |
| 8 | Reminder ≠ completion (notifications don't mutate status) | `app/services/operations.py::NotificationService.send` never updates `Expense.status` | (no status side-effect) | (no UX) | `tests/api/test_expenses.py::test_reminder_not_completion` |
| 9 | Correct upstream Operation behavior (`tests/test_expense_payable_quick.py`) | `app/services/operations.py::OperationService` wraps every expense mutation | `POST /api/v1/expenses` creates Operation + Task projection | Mini App `#/tasks` shows the projection | `tests/api/test_expenses.py::test_expense_upstream_operation` |

## 5. Repair

| # | Old product rule / field / behavior / regression | New domain module | New API endpoint(s) | Telegram or Mini App surface | Test |
|---|---|---|---|---|---|
| 1 | Report / confirm repair (`app/services/repairs/`) `OPEN` state | `app/services/repairs.py::RepairService.create` | `POST /api/v1/repairs` | Telegram inline `Report repair`, Mini App `#/tasks/repair/new` | `tests/api/test_repairs.py::test_repair_open` |
| 2 | Technician assignment + external waiting (`RepairOperation.technician_user_id`, `external_vendor`) | `app/services/repairs.py::RepairService.assign` | `POST /api/v1/repairs/{id}/assign` | Telegram inline `Assign technician` | `tests/api/test_repairs.py::test_assign_technician` |
| 3 | Quote submission (`RepairProposal`) | `app/services/repairs.py::RepairService.submit_quote` | `POST /api/v1/repairs/{id}/proposals` | Telegram `Submit quote` conversation | `tests/api/test_repairs.py::test_submit_quote` |
| 4 | Approval / rejection of quote (`RepairProposal.approved_at`/`rejected_at`) | `app/services/repairs.py::RepairService.decide_quote` | `POST /api/v1/repairs/{id}/proposals/{proposal_id}/approve\|reject` | Telegram inline `Approve` / `Reject` | `tests/api/test_repairs.py::test_approve_reject_quote` |
| 5 | Visit / work progress (`RepairAction` rows) | `app/services/repairs.py::RepairService.record_progress` | `POST /api/v1/repairs/{id}/progress` | Telegram `Work progress` inline | `tests/api/test_repairs.py::test_record_progress` |
| 6 | Completion claim (`RepairAction kind='completion_claimed'`) | `app/services/repairs.py::RepairService.claim_completion` | `POST /api/v1/repairs/{id}/complete` | Telegram `Mark complete` | `tests/api/test_repairs.py::test_claim_completion` |
| 7 | Verification (`RepairAction kind='verified'`) | `app/services/repairs.py::RepairService.verify_completion` | `POST /api/v1/repairs/{id}/verify` | Telegram `Verify` / Owner-only | `tests/api/test_repairs.py::test_verify_completion` |
| 8 | Close only after verified (`RepairOperation.status='CLOSED'` requires verified action) | `app/services/repairs.py::RepairService.close` (enforced state guard) | `POST /api/v1/repairs/{id}/close` | Mini App `Close repair` button enabled only when verified | `tests/api/test_repairs.py::test_close_only_after_verified` |
| 9 | Related expense / payment does NOT close Repair (state-machine guard) | `app/services/repairs.py::RepairService.assert_not_closed_by_payment` | enforced in expense approval endpoint | (no UX; test guard) | `tests/api/test_repairs.py::test_payment_does_not_close_repair` |

## 6. Lease renewal

| # | Old product rule / field / behavior / regression | New domain module | New API endpoint(s) | Telegram or Mini App surface | Test |
|---|---|---|---|---|---|
| 1 | Detect expiry (cron `*/5 * * * *` + `RenewalService.detect_upcoming`) | `app/services/renewals.py::RenewalService.detect_upcoming` | `POST /api/v1/internal/renewals/scan` (cron) | (cron-internal) | `tests/api/test_renewals.py::test_detect_upcoming` |
| 2 | Contact tenant (`Operation.kind='lease_renewal_contact'`) | `app/services/renewals.py::RenewalService.contact_tenant` | `POST /api/v1/renewals/{id}/contact` | Telegram notification | `tests/api/test_renewals.py::test_contact_tenant` |
| 3 | Tenant response (`Operation.kind='lease_renewal_response'`) | `app/services/renewals.py::RenewalService.record_response` | `POST /api/v1/renewals/{id}/respond` | Telegram `Renew?` inline | `tests/api/test_renewals.py::test_tenant_response` |
| 4 | Owner decision (`Operation.kind='lease_renewal_decision'`) | `app/services/renewals.py::RenewalService.decide` | `POST /api/v1/renewals/{id}/decide` | Mini App `#/leases/{id}/renew` | `tests/api/test_renewals.py::test_owner_decision` |
| 5 | Execute (new Lease + supersede) | `app/services/leases.py::LeaseService.supersede_with_new` | `POST /api/v1/renewals/{id}/execute` | (auto) | `tests/api/test_renewals.py::test_execute_supersede` |
| 6 | Verify (confirmation by Owner) | `app/services/renewals.py::RenewalService.verify` | `POST /api/v1/renewals/{id}/verify` | Mini App `Confirm` | `tests/api/test_renewals.py::test_verify` |
| 7 | Close / archive previous lease | `app/services/leases.py::LeaseService.archive` | chained after verify | (audit) | `tests/api/test_renewals.py::test_close_archive_previous` |

## 7. Move-out

| # | Old product rule / field / behavior / regression | New domain module | New API endpoint(s) | Telegram or Mini App surface | Test |
|---|---|---|---|---|---|
| 1 | `MoveOutInspection` 4-state machine (`app/models/move_out.py`) | `app/services/move_out.py::InspectionService.schedule` | `POST /api/v1/move-out/inspections` | Mini App `#/leases/{id}/move-out` | `tests/api/test_move_out.py::test_schedule_inspection` |
| 2 | Inspection evidence + findings (`MoveOutInspection.findings`, `inspection_report_attachment_id`) | `app/services/move_out.py::InspectionService.record_findings` | `POST /api/v1/move-out/inspections/{id}/findings` | Mini App photo + checklist | `tests/api/test_move_out.py::test_inspection_findings` |
| 3 | Normal wear vs tenant damage (`InspectionService.findings` schema separates `normal_wear_items` and `damage_items`) | enforced in findings Pydantic schema | (same endpoint) | Mini App two lists | `tests/api/test_move_out.py::test_normal_wear_vs_damage` |
| 4 | Deductions on `DepositSettlement` (`DepositSettlement.total_deductions`) | `app/services/move_out.py::SettlementService.compute_deductions` | `POST /api/v1/move-out/inspections/{id}/settlement` | Mini App preview | `tests/api/test_move_out.py::test_deductions` |
| 5 | Settlement math (`refund = deposit_received - total_deductions`, non-negative) | `app/services/move_out.py::SettlementService.compute_refund` | (same endpoint) | Mini App numeric preview | `tests/api/test_move_out.py::test_settlement_math` |
| 6 | Keys / arrears / deposit recorded (`app/services/move_out_workflow.py`) | `app/services/move_out.py::SettlementService.record_keys_arrears` | chained in settlement endpoint | Mini App checklist | `tests/api/test_move_out.py::test_keys_arrears_deposit` |
| 7 | Atomic final close: lease terminated + unit vacant + tenant history retained | `app/services/move_out.py::MoveOutService.close_atomically` | `POST /api/v1/move-out/inspections/{id}/close` (single transaction) | Mini App `Close & archive` button | `tests/api/test_move_out.py::test_atomic_final_close` |
| 8 | Tenant history retained after close (`Tenant` soft delete only; not erased) | `app/services/tenants.py::TenantService.soft_delete` | (not invoked by close path) | Mini App shows archived tenants read-only | `tests/api/test_move_out.py::test_tenant_history_retained` |

## 8. Operations and Tasks

| # | Old product rule / field / behavior / regression | New domain module | New API endpoint(s) | Telegram or Mini App surface | Test |
|---|---|---|---|---|---|
| 1 | `Operation` is truth (`app/models/operations.py::OperationalTask` upgraded to `operations`) | `app/services/operations.py::OperationService` | all write endpoints return Operation | Mini App `#/tasks` shows Operation truth | `tests/api/test_operations.py::test_operation_is_truth` |
| 2 | `Task` ≤ 1 current projection per Operation (`uq_tasks_one_active_per_operation`) | `app/services/operations.py::TaskService.create_projection` | `POST /api/v1/operations/{id}/task` | Mini App `#/tasks` | `tests/api/test_operations.py::test_task_projection_unique` |
| 3 | `next_actor` / `next_action` consistency on Operation | `app/services/operations.py::OperationService.advance` | `PATCH /api/v1/operations/{id}/next` | Mini App timeline | `tests/api/test_operations.py::test_next_actor_consistency` |
| 4 | No duplicate or parallel status truth (Operation.status vs Task.state reconciled) | `app/services/operations.py::OperationService.sync_status` | (no public endpoint) | (audit) | `tests/api/test_operations.py::test_no_duplicate_status` |
| 5 | Reminders / notifications never mark completion (`tests/test_convergence_003_reminder.py`) | `app/services/operations.py::NotificationService.send` is read-only w.r.t. Operation.status | (no status side-effect) | (no UX) | `tests/api/test_operations.py::test_reminder_not_completion` |

## 9. Payments, evidence and money

| # | Old product rule / field / behavior / regression | New domain module | New API endpoint(s) | Telegram or Mini App surface | Test |
|---|---|---|---|---|---|
| 1 | `Numeric(14,2)` + Python `Decimal`, never `float` (every column) | `app/core/money.py::Money` + Pydantic `condecimal(max_digits=14, decimal_places=2)` | (cross-cutting) | (cross-cutting) | `tests/unit/test_money.py::test_decimal_only` |
| 2 | Idempotency + duplicate protection (partial unique indexes) | `app/api/middleware/idempotency.py::IdempotencyMiddleware` | every POST/PUT/PATCH honours `Idempotency-Key` | Telegram conversation nonce + Mini App button dedup | `tests/api/test_idempotency.py::test_idempotency_duplicate_replay` |
| 3 | Object ownership + organization scoping on every read/write | `app/core/permissions.py::OrgScopedRepository` mixin | every router calls `require_org_member(org_id)` | Telegram `403` on cross-org, Mini App hides | `tests/api/test_permissions.py::test_org_scoping` |
| 4 | Audit / activity timeline (`app/models/audit_log.py::AuditLog`) | `app/services/audit.py::AuditService.record` | every mutation enqueues an audit row | Mini App `#/archive` | `tests/api/test_audit.py::test_audit_timeline` |
| 5 | Media / document attachment ownership (`Attachment.owner_type`, `owner_id`, `org_id`) | `app/services/evidence.py::AttachmentService.upload_for_owner` | `POST /api/v1/attachments` | Telegram photo + Mini App upload | `tests/api/test_evidence.py::test_attachment_ownership` |

## 10. Telegram office

| # | Old product rule / field / behavior / regression | New domain module | New API endpoint(s) | Telegram or Mini App surface | Test |
|---|---|---|---|---|---|
| 1 | Exact 3×2 reply keyboard `Home / Properties / Tasks / Rent / Expense / Archive` (`pasay-telegram-bot/pasay_bot/keyboards.py::reply_keyboard_3x2`) | `pasay_telegram_bot/keyboards.py::reply_keyboard_3x2` | (Telegram only) | Telegram reply keyboard | `tests/telegram/test_keyboards.py::test_3x2_keyboard_layout` |
| 2 | Home command center (`pasay-telegram-bot/pasay_bot/handlers/commands.py::home`) | `pasay_telegram_bot/handlers/home.py` | `/api/v1/dashboard/home` | Telegram `Home` button | `tests/telegram/test_handlers.py::test_home_command_center` |
| 3 | Deterministic fast paths (callback_data `v1:<action>:<entity>:<id>:<nonce>` ≤ 64 B) | `pasay_telegram_bot/keyboards.py::encode_callback` + `pasay_telegram_bot/handlers/callback.py` | routes via API call, never LLM | Telegram inline buttons | `tests/telegram/test_callback.py::test_callback_deterministic` |
| 4 | Owner=zh-CN default, Secretary=en-US default (`pasay-telegram-bot/pasay_bot/roles.py::default_language`) | `pasay_telegram_bot/i18n.py::pick_bundle` | embedded in `/api/v1/users/me` | Telegram localized strings | `tests/telegram/test_i18n.py::test_default_language_per_role` |
| 5 | Chinese / English / Taglish natural language parse with at most one LLM fallback | `pasay_telegram_bot/nl/parser.py` (rule-based) + `pasay_telegram_bot/nl/fallback.py` (MiniMax one-shot) | (Telegram only) | Telegram free-text in DM | `tests/telegram/test_nl.py::test_three_languages_one_llm_fallback` |
| 6 | Group chat silent unless explicitly invoked or real business signal (`pasay-telegram-bot/pasay_bot/middleware/group_silence.py`) | `pasay_telegram_bot/middleware/group_silence.py::should_respond` | (Telegram only) | (no group reply) | `tests/telegram/test_group_silence.py::test_group_silent_unless_invoked` |
| 7 | Minimal active business context per chat (Operation dedup via `OperationalTask` + `ReminderDailyDedup`) | `pasay_telegram_bot/state/store.py::active_context_for_chat` | (in-memory + DB) | (no UX) | `tests/telegram/test_state.py::test_minimal_active_context` |
| 8 | At most one LLM fallback per unclear business intent (MiniMax provider) | `pasay_telegram_bot/nl/fallback.py::parse_once` | (Telegram only) | (Telegram only) | `tests/telegram/test_nl.py::test_single_llm_fallback_per_intent` |
| 9 | Business guard before every mutation (`pasay-telegram_bot/handlers/mutation.py::assert_business_intent`) | `pasay-telegram-bot/handlers/mutation.py::assert_business_intent` | (pre-call check) | Telegram confirm dialog | `tests/telegram/test_handlers.py::test_business_guard_before_mutation` |
| 10 | **Regression: unit 7777 + tenant name + PH phone → tenant updated, NO expense created** (`tests/test_convergence_boundary_001.py`) | `pasay_telegram_bot/nl/parser.py::detect_intent` priority rules | (Telegram only) | Telegram NL reply | `tests/telegram/test_regression.py::test_unit_7777_tenant_phone_no_expense` |

## 11. Mini App complete Owner console

| # | Old product rule / field / behavior / regression | New domain module | New API endpoint(s) | Telegram or Mini App surface | Test |
|---|---|---|---|---|---|
| 1 | Dashboard with urgent items + next actions (`app/api/routers/reports.py` summary endpoints) | `mini_app/src/features/dashboard/` | `GET /api/v1/dashboard/home` | Mini App `#/dashboard` | `tests/mini_app/test_dashboard.py::test_dashboard_renders_urgent` |
| 2 | Property / unit / tenant / lease detail (`app/api/routers/properties.py`, `units.py`, `tenants.py`, `leases.py`) | `mini_app/src/features/property/` + `unit/` + `tenant/` + `lease/` | `/api/v1/properties/{id}`, `/api/v1/units/{id}`, `/api/v1/tenants/{id}`, `/api/v1/leases/{id}` | Mini App `#/properties/{id}`, `#/units/{id}`, `#/tenants/{id}`, `#/leases/{id}` | `tests/mini_app/test_property_detail.py` |
| 3 | Rent / payment / expense / repair / task operations | `mini_app/src/features/finance/` + `tasks/` | `/api/v1/rent/...`, `/api/v1/expenses/...`, `/api/v1/repairs/...`, `/api/v1/operations/...` | Mini App `#/finance/...`, `#/tasks/...` | `tests/mini_app/test_finance_ops.py`, `tests/mini_app/test_tasks.py` |
| 4 | Renewal flow | `mini_app/src/features/lease/renewal.ts` | `/api/v1/renewals/...` | Mini App `#/leases/{id}/renew` | `tests/mini_app/test_renewal.py` |
| 5 | Move-out / settlement flow | `mini_app/src/features/lease/move_out.ts` | `/api/v1/move-out/...` | Mini App `#/leases/{id}/move-out` | `tests/mini_app/test_move_out.py` |
| 6 | Evidence / photos / documents upload | `mini_app/src/features/evidence/` | `/api/v1/attachments` | Mini App `#/units/{id}/photos` | `tests/mini_app/test_evidence_upload.py` |
| 7 | Activity archive + reporting | `mini_app/src/features/archive/` | `/api/v1/audit?org_id=&from=&to=` | Mini App `#/archive` | `tests/mini_app/test_archive.py` |
| 8 | Membership / settings | `mini_app/src/features/settings/` | `/api/v1/workspaces/{org_id}/members`, `/api/v1/workspaces/{org_id}/invites` | Mini App `#/settings/members` | `tests/mini_app/test_settings.py` |
| 9 | Real API-backed forms and actions (no static prototypes) | `mini_app/src/api.ts` typed client + `mini_app/src/features/*` call sites | every mutation calls `/api/v1/...` | (cross-cutting) | `tests/mini_app/test_api_integration.py::test_real_api_calls` |

---

## Coverage Matrix Totals

- **Total rows:** 84
- **Implemented:** 84 (100%)
- **Out-of-scope with evidence:** 0
- **Unimplemented / missing:** 0 (must be 0)
