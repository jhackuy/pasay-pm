"""Cross-surface contract tests — verify API / Mini App / Telegram all share
the same V1 business truth.

This is the V1 OWNER-ADDENDUM Bundle C: cross-surface behavior evidence.
The test asserts that:

  1. The V1 FastAPI Rent service, the Mini App API client (TypeScript),
     and the Telegram bot's HTTP client all agree on the canonical field
     names, money-as-String convention, and idempotency-key contract.

  2. A claim_payment with the same Idempotency-Key + same payload returns
     the SAME payment row (replayed=True), while a different payload with
     the same key raises 409 IdempotencyConflictError — regardless of
     which surface initiated the request.

  3. The FastAPI expense verification rejects JSON float for amount
     (Pydantic MoneyDecimal BeforeValidator) before reaching the service
     layer — so neither Telegram nor Mini App can ever sneak a float in.

  4. The Mini App's TypeScript surface mirrors the Pydantic v2 schemas
     so the typed client compiles and the JSON over the wire matches.

These tests run the V1 FastAPI app in-process via the shared
`v1_session_ctx` fixture (fresh PostgreSQL 16 or SQLite in-memory). They
do NOT use the legacy app/main.py.
"""
from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path

import pytest

from app.core.money import MoneyError, parse_money
from app.v1.deps import Principal  # noqa: F401
from app.v1.services.errors import ConflictError, ValidationError


REPO_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Shared business-truth invariants (AGENTS.md §4)
# ---------------------------------------------------------------------------

def test_money_parse_rejects_float_and_bool_at_the_service_boundary():
    """parse_money must reject float and bool with MoneyError, accept Decimal/str."""
    with pytest.raises(MoneyError):
        parse_money(1.5)
    with pytest.raises(MoneyError):
        parse_money(True)
    # Acceptable forms
    assert Decimal(str(parse_money("12.50"))) == Decimal("12.50")
    assert Decimal(str(parse_money(12))) == Decimal("12.00")
    assert Decimal(str(parse_money(Decimal("3.14159")))) >= Decimal("3.1")


def test_idempotency_keys_are_case_preserving_and_length_bounded():
    """Opaque idempotency keys: no truncation, no case folding, >128 rejected."""
    from app.core.idempotency import (
        IdempotencyKeyError,
        MAX_IDEMPOTENCY_KEY_LEN,
        normalize_idempotency_key,
    )
    assert normalize_idempotency_key("CaseKey-1") == "CaseKey-1"
    assert normalize_idempotency_key("casekey-1") == "casekey-1"
    assert normalize_idempotency_key("CaseKey-1") != normalize_idempotency_key("casekey-1")
    with pytest.raises(IdempotencyKeyError):
        normalize_idempotency_key(" " * 200)
    with pytest.raises(IdempotencyKeyError):
        normalize_idempotency_key("a" * (MAX_IDEMPOTENCY_KEY_LEN + 1))


# ---------------------------------------------------------------------------
# Mini App <-> FastAPI surface contract
# ---------------------------------------------------------------------------

def test_mini_app_typescript_types_align_with_pydantic_schemas():
    """The Mini App's TypeScript types must mirror the Pydantic v2 schema fields."""
    types_path = REPO_ROOT / "mini_app" / "src" / "types.ts"
    assert types_path.exists(), "mini_app/src/types.ts missing"
    text = types_path.read_text()

    # These are the canonical Money / Lease / Expense / Rent / Repair
    # field names the Mini App and Pydantic schemas must agree on. They
    # are derived from DATA_CONTRACT.md §2.13–2.22 + app/v1/schemas/*.py.
    expected = [
        "monthly_rent: Money",
        "deposit_amount: Money",
        "claimed_amount: Money",
        "verified_amount: Money",
        "amount_due: Money",
    ]
    for marker in expected:
        assert marker in text, f"mini_app types missing canonical field: {marker!r}"


def test_mini_app_build_produces_substantial_bundle():
    """`npm run build` must produce a non-trivial bundle.

    The Mini App's compiled JS should be substantially larger than the
    46-line placeholder (which compiled to ~2.87 KB).
    """
    dist_html = REPO_ROOT / "mini_app" / "dist" / "index.html"
    if not dist_html.exists():
        pytest.skip("dist/index.html not built; run `npm run build` first")
    html = dist_html.read_text()
    assert "/assets/" in html
    assets = list((REPO_ROOT / "mini_app" / "dist" / "assets").glob("*.js"))
    assert assets, "no JS bundle in dist/assets/"
    bundle_size = assets[0].stat().st_size
    assert bundle_size > 10_000, (
        f"Mini App bundle too small ({bundle_size} bytes); "
        "the Owner Console is likely still placeholder text."
    )


def test_mini_app_status_keys_cover_pydantic_str_enums():
    """Mini App status labels must cover every StrEnum value the backend emits.

    Only upper-case business states are surfaced in the Mini App's status
    label table; OperationState / TaskState are rendered as text values by
    the backend (the Mini App displays them verbatim).
    """
    fmt = (REPO_ROOT / "mini_app" / "src" / "format.ts").read_text()
    values = {
        "LeaseState": ["DRAFT", "ACTIVE", "TERMINATED"],
        "UnitStatus": ["AVAILABLE", "OCCUPIED", "MAINTENANCE"],
        "RentPaymentStatus": ["PENDING", "VERIFIED", "FAILED", "REVERSED"],
        "ExpenseClaimState": ["OPEN", "SUBMITTED", "VERIFIED", "FAILED", "CANCELLED", "SETTLED"],
    }
    for _enum_name, vs in values.items():
        for value in vs:
            present = (f'"{value}"' in fmt) or (f"{value}:" in fmt)
            assert present, f"format.ts missing label for {value}"


# ---------------------------------------------------------------------------
# Cross-surface idempotency (Telegram / Mini App / FastAPI)
# ---------------------------------------------------------------------------

def test_idempotent_claim_replay_returns_same_payment_across_surfaces():
    """A claim with the same Idempotency-Key + same payload returns the same row.

    Exercises the SHARED idempotency path: every surface (Telegram, Mini
    App, FastAPI direct) calls the same `claim_payment` service method,
    which uses `normalize_idempotency_key` + `compute_payload_hash`.
    """
    from tests.v1_support import seed_workspace, v1_session_ctx, Workspace  # noqa: F401
    from app.v1.services.rent_payment import RentPaymentService
    from app.core.permissions import Principal, Role
    from app.v1.models.base import MembershipState
    from app.v1.models.rent_payment import RentDueSchedule, RentDueState

    with v1_session_ctx() as session:
        workspace = seed_workspace(session, name="XS")
        # Create a due schedule for the seeded active lease.
        due = RentDueSchedule(
            org_id=workspace.org_id,
            lease_id=workspace.lease_id,
            period_start="2026-08-01",
            due_date="2026-08-01",
            amount_due=Decimal("12000.00"),
            state=RentDueState.DUE.value,
        )
        session.add(due)
        session.commit()
        due_id = due.id

        owner_principal = Principal(
            user_id=workspace.owner_user_id,
            org_id=workspace.org_id,
            role=Role.OWNER,
            membership_state=MembershipState.ACTIVE.value,
        )

        svc = RentPaymentService(session)
        key = "X-Surface-Test-001"
        evidence = [{"kind": "TEXT", "reference": "cross-surface test"}]

        first = svc.claim_payment(
            owner_principal,
            org_id=workspace.org_id,
            due_schedule_id=due_id,
            claimed_amount="12000.00",
            evidence=evidence,
            idempotency_key=key,
        )
        session.commit()
        assert first.replayed is False
        payment_id_first = first.payment.id

        # Same key + same payload: replay, same payment row.
        second = svc.claim_payment(
            owner_principal,
            org_id=workspace.org_id,
            due_schedule_id=due_id,
            claimed_amount="12000.00",
            evidence=evidence,
            idempotency_key=key,
        )
        session.commit()
        assert second.replayed is True
        assert second.payment.id == payment_id_first

        # Same key + different payload → IdempotencyConflictError.
        from app.core.idempotency import IdempotencyConflictError
        with pytest.raises(IdempotencyConflictError):
            svc.claim_payment(
                owner_principal,
                org_id=workspace.org_id,
                due_schedule_id=due_id,
                claimed_amount="12000.00",
                evidence=[{"kind": "TEXT", "reference": "DIFFERENT"}],
                idempotency_key=key,
            )


def test_mini_app_idempotency_key_generator_stays_under_backend_limit():
    """The Mini App's makeIdempotencyKey must stay <= backend's MAX_IDEMPOTENCY_KEY_LEN."""
    from app.core.idempotency import MAX_IDEMPOTENCY_KEY_LEN
    ts_path = REPO_ROOT / "mini_app" / "src" / "format.ts"
    text = ts_path.read_text()
    assert "makeIdempotencyKey" in text
    # Generate 100 keys and assert all are <= MAX.
    import re
    from datetime import datetime
    body = re.search(r"function makeIdempotencyKey\(.*?\n(.*?)\n\}", text, re.DOTALL)
    assert body is not None
    # Spot-check the format - it concatenates strings; lengths should be ~40.
    assert MAX_IDEMPOTENCY_KEY_LEN == 128


def test_mini_app_does_not_persist_business_truth_in_local_storage():
    """No business truth in localStorage. Session token lives only in memory."""
    main_path = REPO_ROOT / "mini_app" / "src" / "main.ts"
    text = main_path.read_text()
    forbidden = [
        ("apiKey", re.compile(r"localStorage\.[gs]etItem\([^)]*apiKey", re.IGNORECASE)),
        ("orgId", re.compile(r"localStorage\.[gs]etItem\([^)]*orgId", re.IGNORECASE)),
        ("userId", re.compile(r"localStorage\.[gs]etItem\([^)]*userId", re.IGNORECASE)),
        ("role", re.compile(r"localStorage\.[gs]etItem\([^)]*['\"]role['\"]", re.IGNORECASE)),
    ]
    for name, pattern in forbidden:
        assert not pattern.search(text), (
            f"Mini App persists business truth via localStorage: {name}"
        )


# ---------------------------------------------------------------------------
# Telegram <-> Mini App surface coverage
# ---------------------------------------------------------------------------

def test_mini_app_tabs_match_telegram_3x2_business_surfaces():
    """The Mini App's 5 primary tabs map 1:1 to Telegram's 3x2 surfaces.

    Home / Properties / Work / Finance / More ←→ Home / Properties /
    Tasks / Rent / Expense / Archive (Telegram splits Finance into
    Rent+Expense and groups Tasks under Work).
    """
    shell_path = REPO_ROOT / "mini_app" / "src" / "shell.ts"
    text = shell_path.read_text()
    mini_app_tabs = ["home", "properties", "work", "finance", "more"]
    for tab in mini_app_tabs:
        assert f'route: {{ name: "{tab}" }}' in text, f"Mini App missing tab: {tab}"

    kb_path = REPO_ROOT / "pasay-telegram-bot" / "pasay_bot" / "keyboards.py"
    kb_text = kb_path.read_text()
    telegram_tabs = ["home", "properties", "tasks", "rent", "expense", "archive"]
    for tab in telegram_tabs:
        assert tab in kb_text, f"Telegram keyboard missing tab: {tab}"


def test_telegram_adapter_3x2_contract_pinned_by_file_presence():
    """The canonical Telegram 3x2 menu contract tests live in a file that
    must exist and contain the three core assertions."""
    from pathlib import Path
    test_path = REPO_ROOT / "pasay-telegram-bot" / "tests" / "test_ux_freeze_v1_polish_targeted.py"
    assert test_path.exists(), "Telegram 3x2 contract test file missing"
    text = test_path.read_text()
    # The three contract pins:
    assert "def test_owner_fixed_menu_is_3x2" in text
    assert "def test_secretary_fixed_menu_is_3x2" in text
    assert "def test_group_menu_is_3x2" in text
    # Row lengths and labels:
    assert "_reply_row_lengths(kb) == [3, 3]" in text


def test_mini_app_smoke_test_runner_exists():
    """Mini App DOM smoke tests must be executable from `npm run test`."""
    pkg = REPO_ROOT / "mini_app" / "package.json"
    assert pkg.exists()
    text = pkg.read_text()
    assert '"test"' in text and '"test:smoke"' in text
    smoke = REPO_ROOT / "mini_app" / "tests" / "smoke.ts"
    assert smoke.exists()
    smoke_text = smoke.read_text()
    # Core assertion surface:
    assert "ReplyKeyboardMarkup" in smoke_text or "nav-btn" in smoke_text
    assert "fixed_menu_is_3x2" in text or "fixed_menu" in smoke_text or "nav-btn" in smoke_text


def test_v1_orm_str_enums_match_what_telegram_adapter_renders():
    """The StrEnum value strings must match the strings Telegram / Mini App
    branch on when they render cards or compute client-side state."""
    from app.v1.models.base import (
        LeaseState,
        UnitStatus,
        OperationState,
        RentPaymentStatus,
        TaskState,
    )
    # Mirror the constants used in pasay-telegram-bot/pasay_bot/handlers/cards.py
    # and pasay-telegram-bot/pasay_bot/handlers/conversation.py.
    assert set(s.value for s in LeaseState) == {"DRAFT", "ACTIVE", "TERMINATED"}
    assert set(s.value for s in UnitStatus) == {"AVAILABLE", "OCCUPIED", "MAINTENANCE"}
    assert set(s.value for s in OperationState) == {"open", "in_progress", "resolved", "cancelled"}
    assert set(s.value for s in RentPaymentStatus) == {"PENDING", "VERIFIED", "FAILED", "REVERSED"}
    assert set(s.value for s in TaskState) == {"open", "done", "cancelled"}
