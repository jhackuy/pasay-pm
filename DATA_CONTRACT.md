# DATA_CONTRACT

> Canonical data shape contract for Pasay. This file is the source of truth for
> the database schema, the on-the-wire API envelope, and the API/Telegram
> versioning rules. All migrations, ORM models, Pydantic schemas, and API
> handlers MUST conform to this contract.

---

## 1. Conventions

### 1.1 Money
- Database: `NUMERIC(14, 2)` (14 total digits, 2 fractional).
- Python: `decimal.Decimal` via Pydantic `condecimal(max_digits=14, decimal_places=2)`.
- JSON over the wire: **string** (never a JSON number) to preserve precision.
- **Float is forbidden for any monetary value anywhere in the system.**

### 1.2 Time
- Database: `TIMESTAMPTZ` (timezone-aware, stored as UTC).
- Python: `datetime.datetime` with `tzinfo=timezone.utc`.
- JSON over the wire: ISO-8601 string (e.g. `"2026-08-29T07:15:42Z"`).
- Naive `datetime` is forbidden in storage and serialization.

### 1.3 Identifiers
- Every primary key is `BIGSERIAL` (PostgreSQL sequence-backed 64-bit integer).
- **UUIDs are NOT used as primary keys** in any domain table.
- Internal opaque identifiers (request_id, idempotency_key, etc.) are strings.

### 1.4 Audit columns
Every domain entity carries the following audit columns:

| Column      | Type            | Notes                                          |
|-------------|-----------------|------------------------------------------------|
| `created_at`| `TIMESTAMPTZ`   | `NOT NULL`, defaults to server `now()`.        |
| `updated_at`| `TIMESTAMPTZ`   | `NOT NULL`, defaults to server `now()`.        |
| `created_by`| `BIGINT NULL`   | FK → `users.id`.                              |
| `updated_by`| `BIGINT NULL`   | FK → `users.id`.                              |

`updated_at` MUST be maintained by an `AuditMixin` on every UPDATE.

### 1.5 Soft delete
Entities that may be archived instead of hard-deleted carry:

- `deleted_at TIMESTAMPTZ NULL`

Applied to: `Property`, `Unit`, `Tenant`. (For all other entities, hard delete
is permitted only when no business record references them.)

### 1.6 Mixins
- Every domain entity inherits `AuditMixin` (see §1.4).
- Every domain entity that belongs to an Organization also inherits
  `OrgScopedMixin` (every query MUST be filtered by `org_id`; the API layer is
  fail-closed on org-scope violations).

---

## 2. Entity table

> Notation: `BIGSERIAL PK` = `BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY`
> unless stated otherwise. `FK` = foreign key constraint with appropriate
> `ON DELETE` behavior per business rules. Partial unique indexes use the
> `WHERE <predicate>` form.

### 2.1 `users`
- `id BIGSERIAL PK`
- `display_name TEXT NOT NULL`
- `telegram_user_id BIGINT NULL UNIQUE`
- `default_language VARCHAR(8) NOT NULL CHECK (default_language IN ('zh-CN','en-US','tl-PH'))`
- `is_active BOOLEAN NOT NULL DEFAULT TRUE`
- Audit columns (`created_at`, `updated_at`).
- Primary index: `users_pkey` on `id`.
- Unique index: `uq_users_telegram_user_id` on `telegram_user_id`
  (nullable, so uniqueness applies only when present).

### 2.2 `principals`
- `id BIGSERIAL PK`
- `principal_type VARCHAR(16) NOT NULL CHECK (principal_type IN ('HUMAN','SERVICE','AI_AGENT','SYSTEM'))`
- `display_name TEXT NOT NULL`
- `user_id BIGINT NULL REFERENCES users(id)` — only set when `principal_type='HUMAN'`.
- `is_active BOOLEAN NOT NULL DEFAULT TRUE`
- Audit columns.
- Partial unique index: `uq_principals_human_user` ON `(user_id)`
  `WHERE principal_type='HUMAN' AND user_id IS NOT NULL`.

### 2.3 `api_credentials`
- `id BIGSERIAL PK`
- `principal_id BIGINT NOT NULL REFERENCES principals(id)`
- `purpose VARCHAR(32) NOT NULL`
- `key_hash CHAR(64) NOT NULL UNIQUE` — HMAC-SHA256 hex digest of the secret.
- `state VARCHAR(16) NOT NULL DEFAULT 'ACTIVE' CHECK (state IN ('ACTIVE','REVOKED'))`
- `revoked_at TIMESTAMPTZ NULL`
- `created_at TIMESTAMPTZ NOT NULL DEFAULT now()`
- `supersedes_id BIGINT NULL REFERENCES api_credentials(id)`
- Table CHECK: `(state = 'ACTIVE') = (revoked_at IS NULL)`.
- Unique: `key_hash`. Partial unique behavior on rotation is enforced via
  `supersedes_id` lineage, not via the hash.

### 2.4 `organizations`
- `id BIGSERIAL PK`
- `name TEXT NOT NULL`
- `default_currency CHAR(3) NOT NULL DEFAULT 'PHP'`
- Audit columns.

### 2.5 `memberships`
- `id BIGSERIAL PK`
- `org_id BIGINT NOT NULL REFERENCES organizations(id)`
- `user_id BIGINT NOT NULL REFERENCES users(id)`
- `role VARCHAR(16) NOT NULL CHECK (role IN ('OWNER','SECRETARY'))`
- `state VARCHAR(16) NOT NULL DEFAULT 'ACTIVE' CHECK (state IN ('ACTIVE','REMOVED'))`
- `removed_at TIMESTAMPTZ NULL`
- `invited_by_membership_id BIGINT NULL REFERENCES memberships(id)`
- `removed_by_membership_id BIGINT NULL REFERENCES memberships(id)`
- Audit columns.
- Partial unique index: `uq_memberships_active_user_org` ON `(org_id, user_id)`
  `WHERE state = 'ACTIVE'`.
- Table CHECK: `(state = 'ACTIVE') = (removed_at IS NULL)`.

### 2.6 `secretary_invites`
- `id BIGSERIAL PK`
- `org_id BIGINT NOT NULL REFERENCES organizations(id)`
- `telegram_user_id BIGINT NOT NULL`
- `role VARCHAR(16) NOT NULL DEFAULT 'SECRETARY' CHECK (role IN ('SECRETARY'))`
- `state VARCHAR(16) NOT NULL DEFAULT 'PENDING' CHECK (state IN ('PENDING','ACCEPTED','CANCELLED','EXPIRED'))`
- `expires_at TIMESTAMPTZ NOT NULL`
- `created_membership_id BIGINT NULL REFERENCES memberships(id)`
- `created_at TIMESTAMPTZ NOT NULL DEFAULT now()`
- Table CHECK: `expires_at > created_at`.

### 2.7 `properties`
- `id BIGSERIAL PK`
- `org_id BIGINT NOT NULL REFERENCES organizations(id)`
- `name TEXT NOT NULL`
- `address TEXT NOT NULL`
- `monthly_rent_target NUMERIC(14,2) NULL`
- `archived_at TIMESTAMPTZ NULL`
- Audit columns + soft delete (`deleted_at TIMESTAMPTZ NULL`).

### 2.8 `units`
- `id BIGSERIAL PK`
- `property_id BIGINT NOT NULL REFERENCES properties(id)`
- `org_id BIGINT NOT NULL REFERENCES organizations(id)` (denormalized for OrgScopedMixin).
- `unit_number TEXT NOT NULL`
- `status VARCHAR(16) NOT NULL DEFAULT 'vacant' CHECK (status IN ('vacant','occupied','maintenance'))`
- `monthly_rent NUMERIC(14,2) NULL`
- `current_lease_id BIGINT NULL`
- Audit columns + soft delete (`deleted_at TIMESTAMPTZ NULL`).
- Partial unique index: `uq_units_active_property_unit_number`
  ON `(property_id, unit_number)` `WHERE deleted_at IS NULL`.

### 2.9 `unit_lifecycle_events`
- `id BIGSERIAL PK`
- `unit_id BIGINT NOT NULL REFERENCES units(id)`
- `event_type VARCHAR(32) NOT NULL`
- `payload JSONB NOT NULL DEFAULT '{}'`
- `occurred_at TIMESTAMPTZ NOT NULL DEFAULT now()`
- `actor_user_id BIGINT NULL REFERENCES users(id)`
- Index: `(unit_id, occurred_at DESC)`.

### 2.10 `unit_channel_bindings`
- `id BIGSERIAL PK`
- `unit_id BIGINT NOT NULL REFERENCES units(id)`
- `org_id BIGINT NOT NULL REFERENCES organizations(id)`
- `chat_id BIGINT NOT NULL`
- `purpose VARCHAR(16) NOT NULL CHECK (purpose IN ('archive','business_group'))`
- `state VARCHAR(16) NOT NULL DEFAULT 'ACTIVE' CHECK (state IN ('ACTIVE','REVOKED'))`
- Audit columns.
- Partial unique index: `uq_unit_binding_active_unit_purpose`
  ON `(unit_id, purpose)` `WHERE state = 'ACTIVE'`.

### 2.11 `tenants`
- `id BIGSERIAL PK`
- `org_id BIGINT NOT NULL REFERENCES organizations(id)`
- `display_name TEXT NOT NULL`
- `phone_e164 VARCHAR(32) NULL`
- `contact_status VARCHAR(16) NOT NULL DEFAULT 'UNKNOWN' CHECK (contact_status IN ('UNKNOWN','UNVERIFIED','VERIFIED','WRONG_NUMBER','UNREACHABLE','CHANGED'))`
- `telegram_user_id BIGINT NULL`
- Audit columns + soft delete (`deleted_at TIMESTAMPTZ NULL`).

### 2.12 `leases`
- `id BIGSERIAL PK`
- `org_id BIGINT NOT NULL REFERENCES organizations(id)`
- `unit_id BIGINT NOT NULL REFERENCES units(id)`
- `tenant_id BIGINT NOT NULL REFERENCES tenants(id)`
- `start_date DATE NOT NULL`
- `end_date DATE NOT NULL`
- `monthly_rent NUMERIC(14,2) NOT NULL`
- `deposit NUMERIC(14,2) NOT NULL DEFAULT 0`
- `status VARCHAR(16) NOT NULL DEFAULT 'active' CHECK (status IN ('active','expired','terminated'))`
- `superseded_by_lease_id BIGINT NULL`
- Audit columns.
- Composite foreign key: `(superseded_by_lease_id, unit_id, tenant_id)`
  REFERENCES `leases(id, unit_id, tenant_id)` — guarantees a successor
  belongs to the same unit and tenant.
- Table CHECK: `end_date > start_date`.
- Partial unique index: `uq_leases_superseded_by_one_predecessor`
  ON `(superseded_by_lease_id)` `WHERE superseded_by_lease_id IS NOT NULL`
  (a lease can be superseded by at most one successor).

### 2.13 `operations`
- `id BIGSERIAL PK`
- `org_id BIGINT NOT NULL REFERENCES organizations(id)`
- `kind VARCHAR(32) NOT NULL`
- `subject_type VARCHAR(32) NOT NULL`
- `subject_id BIGINT NOT NULL`
- `state VARCHAR(16) NOT NULL CHECK (state IN ('open','in_progress','resolved','cancelled'))`
- `next_actor_user_id BIGINT NULL REFERENCES users(id)`
- `next_action VARCHAR(64) NULL`
- `due_at TIMESTAMPTZ NULL`
- `closed_at TIMESTAMPTZ NULL`
- Audit columns.
- Composite index: `(org_id, subject_type, subject_id)`.

### 2.14 `tasks`
- `id BIGSERIAL PK`
- `operation_id BIGINT NOT NULL REFERENCES operations(id)`
- `assignee_user_id BIGINT NULL REFERENCES users(id)`
- `state VARCHAR(16) NOT NULL CHECK (state IN ('open','done','cancelled'))`
- `due_at TIMESTAMPTZ NULL`
- `done_at TIMESTAMPTZ NULL`
- Audit columns.
- **Partial unique index: `uq_tasks_one_active_per_operation`
  ON `(operation_id)` `WHERE state = 'open'`** — at most one active
  (open) task per operation at any time.

### 2.15 `recurring_rules`
- `id BIGSERIAL PK`
- `org_id BIGINT NOT NULL REFERENCES organizations(id)`
- `operation_kind VARCHAR(32) NOT NULL`
- `cron_expr VARCHAR(64) NOT NULL`
- `subject_template JSONB NOT NULL`
- `is_active BOOLEAN NOT NULL DEFAULT TRUE`
- Audit columns.

### 2.16 `notification_outbox`
- `id BIGSERIAL PK`
- `org_id BIGINT NOT NULL REFERENCES organizations(id)`
- `operation_id BIGINT NULL REFERENCES operations(id)`
- `channel VARCHAR(16) NOT NULL CHECK (channel IN ('telegram','mini_app','email'))`
- `recipient_user_id BIGINT NULL REFERENCES users(id)`
- `payload JSONB NOT NULL`
- `state VARCHAR(16) NOT NULL DEFAULT 'pending' CHECK (state IN ('pending','sent','failed','cancelled'))`
- `idempotency_key VARCHAR(64) NOT NULL`
- Audit columns.
- Partial unique index: `uq_notification_outbox_dedupe` ON `(idempotency_key)`.

### 2.17 `reminder_daily_dedup`
- `id BIGSERIAL PK`
- `key VARCHAR(128) NOT NULL`
- `sent_on DATE NOT NULL`
- Audit columns.
- Partial unique index: `uq_reminder_daily_dedup_key` ON `(key, sent_on)`.

### 2.18 `incomes`
- `id BIGSERIAL PK`
- `org_id BIGINT NOT NULL REFERENCES organizations(id)`
- `lease_id BIGINT NOT NULL REFERENCES leases(id)`
- `amount NUMERIC(14,2) NOT NULL`
- `received_at TIMESTAMPTZ NOT NULL`
- `category VARCHAR(32) NOT NULL DEFAULT 'rent'`
- `description TEXT NULL`
- `status VARCHAR(16) NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','confirmed','reversed'))`
- `idempotency_key VARCHAR(64) NULL`
- Audit columns.
- Partial unique index: `uq_incomes_idempotency_key`
  ON `(org_id, idempotency_key)` `WHERE idempotency_key IS NOT NULL`.

### 2.19 `expenses`
- `id BIGSERIAL PK`
- `org_id BIGINT NOT NULL REFERENCES organizations(id)`
- `property_id BIGINT NULL REFERENCES properties(id)`
- `unit_id BIGINT NULL REFERENCES units(id)`
- `purpose VARCHAR(32) NOT NULL CHECK (purpose IN ('property','building','unit','other'))`
- `category VARCHAR(32) NOT NULL`
- `amount NUMERIC(14,2) NOT NULL`
- `incurred_at TIMESTAMPTZ NOT NULL`
- `description TEXT NULL`
- `status VARCHAR(32) NOT NULL DEFAULT 'reported' CHECK (status IN ('reported','approved','payment_claimed','partially_paid','paid','rejected','cancelled'))`
- `idempotency_key VARCHAR(64) NULL`
- Audit columns.
- Partial unique index: `uq_expenses_idempotency_key`
  ON `(org_id, idempotency_key)` `WHERE idempotency_key IS NOT NULL`.

### 2.20 `rent_payment_claims`
- `id BIGSERIAL PK`
- `org_id BIGINT NOT NULL REFERENCES organizations(id)`
- `lease_id BIGINT NOT NULL REFERENCES leases(id)`
- `claimed_amount NUMERIC(14,2) NOT NULL`
- `verified_amount NUMERIC(14,2) NULL`
- `claimed_by_user_id BIGINT NOT NULL REFERENCES users(id)`
- `claimed_at TIMESTAMPTZ NOT NULL`
- `status VARCHAR(16) NOT NULL DEFAULT 'PENDING' CHECK (status IN ('PENDING','VERIFIED','FAILED','REVERSED'))`
- `evidence_attachment_id BIGINT NULL`
- `idempotency_key VARCHAR(64) NOT NULL`
- `verified_at TIMESTAMPTZ NULL`
- `verified_by_user_id BIGINT NULL REFERENCES users(id)`
- `failure_reason TEXT NULL`
- Audit columns.
- Partial unique index: `uq_rent_payment_claims_idempotency_key`
  ON `(org_id, idempotency_key)`.

### 2.21 `expense_payment_claims`
- Same shape as `rent_payment_claims` (see §2.20), with `expense_id BIGINT NOT NULL REFERENCES expenses(id)` in place of `lease_id`.
- Partial unique index: `uq_expense_payment_claims_idempotency_key`
  ON `(org_id, idempotency_key)`.

### 2.22 `deposit_settlements`
- `id BIGSERIAL PK`
- `org_id BIGINT NOT NULL REFERENCES organizations(id)`
- `lease_id BIGINT NOT NULL REFERENCES leases(id)`
- `move_out_inspection_id BIGINT NULL`
- `deposit_received NUMERIC(14,2) NOT NULL DEFAULT 0`
- `total_deductions NUMERIC(14,2) NOT NULL DEFAULT 0`
- `refund_amount NUMERIC(14,2) NOT NULL DEFAULT 0`
- `status VARCHAR(16) NOT NULL DEFAULT 'DRAFT' CHECK (status IN ('DRAFT','CONFIRMED','RECONCILED'))`
- Audit columns.
- Table CHECK: `deposit_received >= 0 AND total_deductions >= 0 AND refund_amount >= 0`.
- Composite foreign key: `(move_out_inspection_id, lease_id)`
  REFERENCES `move_out_inspections(id, lease_id)`.

### 2.23 `move_out_inspections`
- `id BIGSERIAL PK`
- `org_id BIGINT NOT NULL REFERENCES organizations(id)`
- `lease_id BIGINT NOT NULL REFERENCES leases(id)`
- `scheduled_at TIMESTAMPTZ NOT NULL`
- `inspected_at TIMESTAMPTZ NULL`
- `findings JSONB NOT NULL DEFAULT '{}'`
- `status VARCHAR(16) NOT NULL DEFAULT 'SCHEDULED' CHECK (status IN ('SCHEDULED','INSPECTED','CONFIRMED','CANCELLED'))`
- `inspection_report_attachment_id BIGINT NULL`
- Audit columns.
- Partial unique index: `uq_move_out_inspections_active_per_lease`
  ON `(lease_id)` `WHERE status IN ('SCHEDULED','INSPECTED','CONFIRMED')`
  (a lease can have at most one active inspection record).

### 2.24 `repair_operations`
- `id BIGSERIAL PK`
- `org_id BIGINT NOT NULL REFERENCES organizations(id)`
- `property_id BIGINT NULL REFERENCES properties(id)`
- `unit_id BIGINT NULL REFERENCES units(id)`
- `title TEXT NOT NULL`
- `description TEXT NOT NULL`
- `status VARCHAR(32) NOT NULL DEFAULT 'OPEN' CHECK (status IN ('OPEN','ASSIGNED','QUOTED','APPROVED','IN_PROGRESS','COMPLETION_CLAIMED','VERIFIED','CLOSED','CANCELLED'))`
- `technician_user_id BIGINT NULL REFERENCES users(id)`
- `external_vendor TEXT NULL`
- Audit columns.

### 2.25 `repair_proposals`
- `id BIGSERIAL PK`
- `repair_operation_id BIGINT NOT NULL REFERENCES repair_operations(id)`
- `amount NUMERIC(14,2) NOT NULL`
- `vendor TEXT NOT NULL`
- `scope TEXT NOT NULL`
- `submitted_at TIMESTAMPTZ NOT NULL DEFAULT now()`
- `approved_at TIMESTAMPTZ NULL`
- `rejected_at TIMESTAMPTZ NULL`

### 2.26 `repair_actions`
- `id BIGSERIAL PK`
- `repair_operation_id BIGINT NOT NULL REFERENCES repair_operations(id)`
- `kind VARCHAR(32) NOT NULL`
- `payload JSONB NOT NULL DEFAULT '{}'`
- `actor_user_id BIGINT NULL REFERENCES users(id)`
- `occurred_at TIMESTAMPTZ NOT NULL DEFAULT now()`
- Partial unique index: `uq_repair_actions_active_dedupe`
  ON `(repair_operation_id, kind)`
  `WHERE kind IN ('quote_submitted','completion_claimed','verified','closed')`
  (these terminal-style actions can fire at most once per repair operation).

### 2.27 `attachments`
- `id BIGSERIAL PK`
- `org_id BIGINT NOT NULL REFERENCES organizations(id)`
- `owner_type VARCHAR(32) NOT NULL`
- `owner_id BIGINT NOT NULL`
- `storage_path TEXT NOT NULL`
- `mime_type VARCHAR(64) NOT NULL`
- `size_bytes BIGINT NOT NULL`
- `uploaded_by_user_id BIGINT NOT NULL REFERENCES users(id)`
- Audit columns.
- Composite index: `(owner_type, owner_id)`.

### 2.28 `audit_log`
- `id BIGSERIAL PK`
- `org_id BIGINT NOT NULL REFERENCES organizations(id)`
- `actor_principal_id BIGINT NULL REFERENCES principals(id)`
- `actor_user_id BIGINT NULL REFERENCES users(id)`
- `action VARCHAR(64) NOT NULL`
- `subject_type VARCHAR(32) NOT NULL`
- `subject_id BIGINT NOT NULL`
- `payload JSONB NOT NULL DEFAULT '{}'`
- `occurred_at TIMESTAMPTZ NOT NULL DEFAULT now()`
- Composite index: `(org_id, occurred_at DESC)`.

### 2.29 `telegram_webhook_updates`
- `id BIGSERIAL PK`
- `update_id BIGINT NOT NULL UNIQUE`
- `chat_id BIGINT NULL`
- `payload JSONB NOT NULL`
- `state VARCHAR(16) NOT NULL CHECK (state IN ('pending','claimed','done','failed','retryable'))`
- `attempts INT NOT NULL DEFAULT 0`
- `last_error_type VARCHAR(64) NULL`
- `created_at TIMESTAMPTZ NOT NULL DEFAULT now()`
- `updated_at TIMESTAMPTZ NOT NULL DEFAULT now()`
- Composite index: `(state, updated_at)`.

### 2.30 `scheduled_jobs`
- `id BIGSERIAL PK`
- `name VARCHAR(64) NOT NULL`
- `bucket_iso VARCHAR(32) NOT NULL` (e.g. `"2026-08-29T07:00:00Z"`).
- `state VARCHAR(16) NOT NULL CHECK (state IN ('claimed','done','failed'))`
- `payload JSONB NOT NULL`
- Audit columns.
- Partial unique index: `uq_scheduled_jobs_bucket` ON `(name, bucket_iso)`.

---

## 3. API response envelope

All HTTP API responses use a single canonical envelope.

### 3.1 Success

```json
{
  "data": <payload>,
  "request_id": "<opaque>"
}
```

For 204 No Content responses, the body is empty and `request_id` is returned
in the `X-Request-Id` response header.

### 3.2 Error

```json
{
  "error": {
    "code": "<snake_case>",
    "message": "<human-readable>",
    "details": <object | null>
  },
  "request_id": "<opaque>"
}
```

- `code`: machine-readable, snake_case, stable per error class
  (e.g. `idempotency_replay`, `state_machine_violation`, `validation_error`).
- `message`: human-readable, localized per `users.default_language`.
- `details`: optional structured context (validation field errors, conflicting
  resource ids, etc.) or `null`.

### 3.3 Status codes

| Code | Meaning                                                                 |
|------|-------------------------------------------------------------------------|
| 200  | OK — general success with body.                                         |
| 201  | Created — resource successfully created.                                |
| 204  | No Content — success, no body.                                          |
| 400  | Bad Request — malformed request.                                        |
| 401  | Unauthenticated — credentials missing or invalid.                       |
| 403  | Unauthorized — authenticated but not allowed for this org/resource.     |
| 404  | Not Found — resource does not exist or is org-scoped out.               |
| 409  | Conflict — idempotency replay or state-machine violation.              |
| 422  | Validation — semantic validation failure (types, ranges, references).   |
| 429  | Rate Limited — quota exceeded.                                          |
| 5xx  | Server Error — unexpected failure; safe to retry with backoff.          |

---

## 4. Versioning

### 4.1 API
- All HTTP routes are mounted under the prefix `/api/v1`.
- Any breaking change to request shape, response shape, status code, or
  error envelope semantics requires a new major prefix (e.g. `/api/v2`).
- Non-breaking additions (new optional fields, new endpoints) may ship under
  the existing `/api/v1` prefix.

### 4.2 Telegram callback
- Every Telegram callback payload carries a `v1:` prefix
  (e.g. `v1:unit.bind:42`).
- Unknown or malformed versions are **ignored** — the worker MUST NOT crash,
  retry, or send a user-visible error for an unparseable callback version.
- Future versions introduce a new prefix (e.g. `v2:`); legacy `v1:` handlers
  continue to be honored until a documented deprecation window closes.
