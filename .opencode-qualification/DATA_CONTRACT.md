# DATA_CONTRACT — PASAY (fresh baseline)

All tables described here are created by the single baseline Alembic migration `alembic/versions/001_baseline.py`. No legacy data is migrated.

## Conventions

- PK: `id BIGSERIAL PRIMARY KEY`.
- FK: `*_id BIGINT NOT NULL REFERENCES parent(id)`.
- Timestamps: `created_at timestamptz NOT NULL DEFAULT now()`, `updated_at timestamptz NOT NULL DEFAULT now()`.
- Soft-delete: `archived_at timestamptz NULL` (history preserved).
- Money: `NUMERIC(14,2)` (no float).
- Org-scope: every business table has `organization_id BIGINT NOT NULL`.

## Tables (text descriptions)

### organizations
Identity, name, timestamps.

### memberships
User ↔ Organization binding with role (`OWNER` / `SECRETARY`).

### users
Telegram id (unique nullable), display name, E.164 phone (nullable), default language `zh`/`en`/`tl` (default `en`).

### properties
Org-scoped property record, address, optional Telegram channel id (unique when present).

### units
Property-scoped unit, label (unique per property), vacant flag, history preserved via `archived_at`.

### tenants
Org-scoped tenant record, optionally bound to a unit, E.164 phone, full name.

### leases
Unit + tenant + start/end dates + monthly rent + deposit + status (`ACTIVE`/`RENEWING`/`ENDED`/`MOVED_OUT`).

### operations
Org-scoped business-truth row with kind, subject_id, next_actor_role, next_action, state (`OPEN`/`WAITING`/`VERIFIED`/`CLOSED`/`REJECTED`).

### tasks
At-most-one current PENDING human-action projection of an Operation. UNIQUE partial index on `(operation_id) WHERE status='PENDING'`.

### rent_schedules
Per-lease due rows: due_date, amount_due, amount_paid, paid flag.

### payment_claims
Org-scoped claims against rent_schedules: amount_claimed, claim_method (`GCASH`/`BANK`/`CASH`/`OTHER`), idempotency_key UNIQUE.

### payments
Verified payment rows linked to a payment_claim with verifier_user_id and optional evidence attachment.

### expenses
Org-scoped expense with separate `property_id`, `building`, `unit_id` (three distinct scope fields), `category` (≠ purpose), `purpose`, `amount`, status state machine `DRAFT→CLAIMED→APPROVED→PAID→VERIFIED` plus `REJECTED`.

### expense_evidence
Expense ↔ Attachment binding.

### repairs
Org-scoped repair with state machine `REPORTED→CONFIRMED→QUOTED→APPROVED→IN_PROGRESS→COMPLETION_CLAIMED→VERIFIED→CLOSED` plus `REJECTED`. Quote amount + optional quote attachment. `completion_verified_at` set on verification.

### lease_renewals
Per-lease renewal flow: proposed_end_date, proposed_rent, state machine `PROPOSED→TENANT_CONTACTED→TENANT_RESPONDED→OWNER_DECIDED→EXECUTING→VERIFIED→CLOSED` plus `REJECTED`.

### move_outs
Per-lease move-out with inspection_date, deductions_normal_wear, deductions_tenant_damage, deposit_refund, state `INSPECTING→SETTLING→CLOSED` plus `REJECTED`. Atomic close sets lease `ENDED` and unit `vacant=true` in a single transaction.

### attachments
Polymorphic owner reference via `owner_kind` (`REPAIR`/`EXPENSE`/`PAYMENT_CLAIM`/`UNIT`/`PROPERTY`/`TENANT`/`MOVE_OUT`) + `owner_id`. `kind` (`PHOTO`/`DOCUMENT`/`RECEIPT`/`INSPECTION_NOTE`). storage_url, mime_type, uploaded_by_user_id.

### audit_logs
Every state transition writes a row with actor_user_id, entity_kind, entity_id, action, before_state JSONB, after_state JSONB, evidence_refs JSONB, created_at timestamptz.

## Notes

- Every monetary column is `NUMERIC(14,2)`. Float is forbidden at every layer.
- Every timestamp is `timestamptz`. Naive datetimes are rejected at the API boundary.
- Every business table is org-scoped via `organization_id`.
- DB-level uniqueness constraints back behavior invariants: idempotency_key on payment_claims, partial UNIQUE on tasks per operation, unique channel_id on properties, etc.

## Provenance

This file was written by pasay-implementer under `.opencode-qualification/` per the bounded write scope. It must be moved to the repository root (`/DATA_CONTRACT.md`) by a later engineering-executor dispatch (TRAE SOLO) before Issue #99 §1 acceptance.
