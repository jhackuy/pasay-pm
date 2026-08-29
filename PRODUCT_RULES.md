# PRODUCT_RULES — PASAY

These rules are derived from verified old facts and the Owner Addendum. They are non-negotiable.

## Money

- Database column: `NUMERIC(14,2)` for every monetary amount.
- Python type: `decimal.Decimal` everywhere in business code. **`float` is forbidden** for money. Tests and CI must enforce this.
- All money math runs through a single helper service (`app/services/money.py`) that always operates on `Decimal`. No inline arithmetic.

## Time

- Database column: `timestamptz` (timezone-aware). Naive datetimes are rejected at the API boundary and at the Alembic constraint level.
- Python type: `datetime` with `tzinfo=timezone.utc`. `datetime.utcnow()` is forbidden; use `datetime.now(timezone.utc)`.

## Permissions

- The only business permission boundary is `Organization` and `Membership(role)`. A request without a valid `Organization` membership is rejected (fail-closed).
- Roles: `OWNER`, `SECRETARY`. `OWNER` can be exactly one per organization (last-Owner protection).
- Every read/write filters by `organization_id`. Cross-org access is rejected and audited.

## Business truth

- `Operation` is the truth. `Task` is at most one current human-action projection of an Operation.
- A Task's status cannot mutate business truth. Reminders, replies, notifications ≠ completion.
- `Operation CLOSED` is permitted only when the real-world problem is resolved. Quote ≠ Expense. Approval ≠ Payment. Payment Claim ≠ Verified Payment. Partial Rent ≠ Paid. Reject Quote ≠ Repair Closed.
- Repair closes only after completion is verified (verified visit/work evidence + Owner or Secretary confirmation).
- Expense closes only when the underlying Operation is verified-closed AND (if any) the payment has been verified against evidence.

## Identity

- A Telegram user is bound to exactly one `Membership` per `Organization` at a time.
- Phone numbers use E.164 with a country code (default +63 for Philippine tenants per the unit-7777 regression case).

## Audit

- Every state transition writes an `audit_logs` entry with actor, before-state, after-state, evidence_refs.
- Evidence (photos, documents) is stored under `attachments` with `owner_organization_id` and `attachment_kind` (PHOTO / DOCUMENT / RECEIPT / INSPECTION_NOTE).
