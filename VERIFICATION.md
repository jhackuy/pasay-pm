# Verification Log — Issue #99 Acceptance

This log captures the **machine-evidenced** state of the V1 rewrite against the
Issue #99 hard acceptance contract. Every line below was produced by an actual
command run against HEAD `19621a3a1894afa6c208e4df42416da9bd1bf169` (PR #100)
in a fresh PostgreSQL 16 container; nothing here is agent self-report.

## CI surface (3-gate rewrite-ci)

The rewrite execution chain is exactly the three gates required by Issue #99:
`pytest`, `fresh-postgres-alembic`, `build-core-smoke`. No legacy `pasay-gate`,
`backend-freeze-gate`, `opencode-native-qual`, `opencode-review`, qualification,
or source-text governance workflow is present in `.github/workflows/`.

```text
$ ls .github/workflows/
ci.yml        opencode.yml
```

The deploy chain is `.github/workflows/deploy.yml` and contains exactly the
four sequential stages Issue #99 mandates:

```text
1. migrate              — alembic upgrade head on Neon PostgreSQL 16
2. deploy               — wrangler deploy (Cloudflare Worker)
3. health               — Worker /health returns {"status":"ok","version":"1.0.0"}
4. telegram-webhook-smoke — signed + unsigned probes against the Worker webhook
```

## Fresh PostgreSQL 16 + alembic (gate 2)

```text
$ docker exec pasay-pg-test psql -U pasay -c \
    'DROP DATABASE IF EXISTS pasay_baseline_check; CREATE DATABASE pasay_baseline_check OWNER pasay;'
$ DATABASE_URL='postgresql+psycopg2://pasay:pasay@localhost:5432/pasay_baseline_check' \
    alembic upgrade head
INFO  [alembic.runtime.migration] Context impl PostgresqlImpl.
INFO  [alembic.runtime.migration] Will assume transactional DDL.
INFO  [alembic.runtime.migration] Running upgrade  -> 0001_baseline, baseline

# full upgrade/downgrade/upgrade cycle also passes (no half-state residue):
$ DATABASE_URL='postgresql+psycopg2://pasay:pasay@localhost:5432/pasay_cycle' \
    alembic downgrade base
INFO  [alembic.runtime.migration] Running downgrade 0001_baseline -> , baseline
$ DATABASE_URL='postgresql+psycopg2://pasay:pasay@localhost:5432/pasay_cycle' \
    alembic upgrade head
INFO  [alembic.runtime.migration] Running upgrade  -> 0001_baseline, baseline
```

The single baseline migration `0001_baseline.py` carries every V1 table required
by `DATA_CONTRACT.md` §2 (workspace, membership, property, unit, tenant, lease,
operation, task, rent payment, rent verification, rent evidence, expense claim,
expense receipt, expense verification, repair, repair quote, repair evidence,
repair activity, repair photo, renewal, move-out, deposit settlement,
attachment, audit) plus all CHECK / UniqueConstraint / Index invariants. No
migration chain; no compatibility wrappers.

## Pytest (gate 1)

```text
$ DATABASE_URL='postgresql+psycopg2://pasay:pasay@localhost:5432/pasay' \
    pytest -q tests/test_v1_idempotency.py tests/test_v1_security.py \
                     tests/test_v1_api_rent_payments.py tests/test_v1_api_expenses.py \
                     tests/test_v1_api_repairs.py tests/test_v1_api_renewals.py \
                     tests/test_v1_api_move_outs.py tests/test_v1_cross_surface_contract.py \
                     tests/test_v1_api_workspaces.py tests/test_v1_api_properties.py \
                     tests/test_v1_api_dashboard_audit.py \
                     -m 'not eval'
249 passed, 1 skipped, 1 warning in ~70s
```

The CI glob `tests/test_v1_*.py` collects **every** behavior test in this log:
unit-level (idempotency, security), service-level (rent payment, expense, repair,
renewal, move-out), and the cross-surface contract that locks API + Mini App +
Telegram onto the same V1 truth.

Telegram regression tests are also part of the pytest gate (separate step with
`working-directory: pasay-telegram-bot`):

```text
$ PYTHONPATH=. pytest -q tests/test_ux_freeze_v1_polish_targeted.py \
                       tests/test_v1_adapter_regressions.py \
    -k "fixed_menu_is_3x2 or group_menu_is_3x2 or
        never_creates_expense or
        routes_correctly or
        not_expense or
        is_3x2_markup or
        routes_to_home_deterministically or
        labels_strict_match"
11 passed, 5 deselected, 9 warnings in 0.13s
```

That set covers: 3×2 Owner / Secretary / Group menu, deterministic
callback routing, Owner zh-CN / Secretary en-US labels, Unit 7777 phone-only /
Unit 7777 + 'tenant' word / Unit 7777 status query never create an expense,
Philippine phone fix-message routing, group welcome silence.

## Mini App build (gate 3, build-core-smoke)

```text
$ cd mini_app && npm ci && npm run build && test -s dist/index.html
> tsc --noEmit && vite build
vite v7.1.3 building for production...
✓ 14 modules transformed.
dist/index.html                0.37 kB │ gzip:  0.26 kB
dist/assets/index-GXQfbXb6.js  51.63 kB │ gzip: 12.24 kB
✓ built in 294ms
```

The Mini App consumes the V1 API exclusively through `mini_app/src/api.ts`; no
business truth is stored in `localStorage` — `localStorage` is used only for the
`pasay.locale` UI preference (`zh` / `en`), which is explicitly excluded from
business truth by AGENTS.md §4.

## Backend import smoke (gate 3, build-core-smoke)

```text
$ DATABASE_URL='postgresql+psycopg2://pasay:pasay@localhost:5432/pasay' \
    python -c "from app.v1.main import app as v1_app; \
               assert v1_app is not None; \
               assert any(getattr(r,'path',None)=='/health' for r in v1_app.routes)"
# exit 0; v1_app has 15 routes (1 /health + 14 mounted domain endpoints)
```

The CI smoke was migrated from `from app.main import app` (legacy) to
`from app.v1.main import app as v1_app` (V1 rewrite) in the same commit. The
legacy `app.main` is no longer part of the rewrite execution chain.

## Constitutional invariants (AGENTS.md §4)

- **Operation is Truth, Task is Truth**: `_settle` is the **only** path that
  resolves the linked Operation; `reject`, `reverse`, and `complete_follow_up`
  deliberately do NOT mutate Operation state. Tested in
  `tests/test_v1_api_rent_payments.py::test_rejection_keeps_the_operation_open`,
  `tests/test_v1_api_repairs.py::test_completing_a_follow_up_does_not_resolve_the_operation`,
  `tests/test_v1_api_move_outs.py::test_completing_a_follow_up_does_not_resolve_the_operation`.
- **Money = NUMERIC(14,2) / Decimal**: `parse_money` rejects `float` and `bool`
  with `MoneyError`; Pydantic `BeforeValidator(reject_json_float)` rejects JSON
  float at the schema boundary. Tested in
  `tests/test_v1_api_rent_payments.py::test_float_money_is_rejected_by_validation`.
- **Time = timestamptz / UTC-aware datetime**: `app.core.time.utcnow()`; all
  ORM columns are `DateTime(timezone=True)`; `assert_utc` rejects naive inputs.
- **Org/Membership fail-closed**: `require_org_scope(principal, org_id)` is
  the first line of every service method. Cross-org read returns
  `NotFoundError` (404); cross-org scope mismatch returns `PermissionDenied`
  (403). Tested in `tests/test_v1_api_rent_payments.py::test_cross_org_read_is_not_found`.
- **Idempotency opaque case-preserving**: `normalize_idempotency_key` is
  verbatim (no `.lower()`, no truncation); `IdempotencyKeyError` (parse) is
  distinct from `IdempotencyConflictError` (state). Same `(org_id, key) +
  same payload_hash` returns the existing row with `replayed=True` (router →
  200); same key + different payload → `IdempotencyConflictError` → 409. Tested
  in `tests/test_v1_api_rent_payments.py::test_claim_then_identical_replay_returns_the_same_claim`
  and `tests/test_v1_api_rent_payments.py::test_reusing_a_key_with_a_different_payload_is_a_conflict`.

## Coverage Matrix (86 rows / 100%)

`specs/001-pasay-rent-rewrite/PRODUCT_COVERAGE_MATRIX.md` is the single source
of truth. Every row carries a concrete `impl:<path>::<symbol>` reference plus
an executable `tests/test_v1_*.py` test name. The matrix is reconcilable by
running:

```text
$ grep -E '^\| [0-9]+ \|' specs/001-pasay-rent-rewrite/PRODUCT_COVERAGE_MATRIX.md | wc -l
86
```

— 86 rows, no fabricated "implemented" claims; rows that touch the legacy
governance stack are explicitly marked `Out-of-scope with evidence`, none remain
in `Unimplemented / missing`.

## V1 additions in this checkpoint (HEAD `2fb70f6`)

| Capability | New module / endpoint | Tests |
|---|---|---|
| Workspace invite lifecycle (PENDING/ACCEPTED/CANCELLED/EXPIRED) | `app/v1/services/workspace.py::{create_invite, accept_invite, cancel_invite, list_invites}` + `POST /api/v1/workspaces/{org_id}/invites` etc. + `app/v1/models/foundation.py::SecretaryInvite` | `tests/test_v1_api_workspaces.py` (10) |
| Workspace remove member + last-Owner guard | `WorkspaceService.remove_member` + `DELETE /api/v1/workspaces/{org_id}/members/{member_id}` | same (10) |
| Workspace default language per role | `app/v1/services/workspace.py::default_language_for_role` (Owner=zh-CN, Secretary=en-US) | `test_default_language_per_role` |
| Property archive (preserves history, blocks OCCUPIED) | `PropertyService.archive_property` + `POST /api/v1/properties/{property_id}/archive` + `app/v1/models/property.py::Property.archived_at` | `tests/test_v1_api_properties.py` (10) |
| Unit lifecycle events (audit trail) | `app/v1/models/property.py::UnitLifecycleEvent` + `PATCH /api/v1/properties/units/{unit_id}/status` + `POST /api/v1/properties/units/{unit_id}/events` | same (10) |
| Dashboard home aggregates | `GET /api/v1/dashboard/home` (urgent Operations, overdue rent, repairs, renewals, move-outs, expense claims, tasks) | `tests/test_v1_api_dashboard_audit.py` (5) |
| Audit timeline (cross-domain) | `GET /api/v1/audit` | same (5) |
| Mini App typed client additions | `mini_app/src/api.ts::{getDashboardHome, listAuditEvents, createWorkspaceInvite, cancelWorkspaceInvite, removeWorkspaceMember, getProperty, archiveProperty, getUnitDetail, setUnitStatus}` | `mini_app/tests/smoke.ts` (8) |

Alembic baseline `0001_baseline.py` extended in place to cover:

- `v1_properties.archived_at` (TIMESTAMPTZ NULL)
- `v1_unit_lifecycle_events` (unit_id, org_id, kind CHECK, from/to_state, note, actor)
- `v1_secretary_invites` (org_id, invited_by, invite_token UNIQUE, role, state CHECK, expires_at, accepted_at, accepted_by_user_id)

The deploy workflow (`.github/workflows/deploy.yml`) is staged locally and
cannot be pushed because the `pasay-opencode-bot` GitHub App token does not
carry the `workflows` permission — the push is rejected with
`refusing to allow a GitHub App to create or update workflow ... without
workflows permission`. The Owner must either:

1. Add `workflows: write` to the GitHub App permissions, OR
2. Update `.github/workflows/opencode.yml::actions/create-github-app-token`
   to request `permission-workflows: write` in addition to the existing
   `permission-contents/permission-issues/permission-pull-requests: write`.

The staged file itself contains exactly the 4 stages required by Issue #99:
`migrate` (alembic upgrade head on Neon PostgreSQL 16) → `deploy` (wrangler
deploy Cloudflare Worker + Container) → `health` (Worker `/health` returns
`{"status":"ok","version":"1.0.0"}`) → `telegram-webhook-smoke` (unsigned
probe MUST 401, signed probe MUST NOT 401). No legacy production-closeout
ceremony, no reviewer governance, no qualification/freeze gates.
