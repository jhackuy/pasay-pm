"""V1.3 Slice 2 — Entry B rent-payment matcher (partial payment).

The core (text parsing + confidence grading) is pure and runs without a
database; the API-level test at the bottom exercises the real endpoint and
therefore needs the PostgreSQL test DB like the rest of the backend suite.
SLICE2-RENT-005 adds partial-amount matching, cumulative balance math,
overpayment protection and invalid-amount rejection.
"""
from datetime import date
from decimal import Decimal

from app.models.financial import Income, IncomeStatus
from app.models.lease import Lease, LeaseStatus
from app.models.property import Property, Unit
from app.models.tenant import Tenant
from app.services.operations.rent_math import (
    confirmed_paid_by_period,
    lease_periods,
    period_remaining,
)
from app.services.payment_match import (
    LeaseCtx,
    MatchConfidence,
    MatchKind,
    _unit_matches,
    match_from_leases,
    parse_hints,
)

TODAY = date(2026, 8, 12)


def _make_ctx(
    unit_no="1608",
    rent="70000.00",
    tenant="John Dela Cruz",
    start="2026-01-01",
    end="2026-12-31",
    due_day=5,
    property_name="Bayshore",
    lease_id=1,
):
    lease = Lease(
        id=lease_id, unit_id=lease_id, tenant_id=lease_id,
        start_date=date.fromisoformat(start), end_date=date.fromisoformat(end),
        monthly_rent=Decimal(rent), deposit=Decimal("0.00"),
        status=LeaseStatus.active, due_day=due_day,
    )
    unit = Unit(
        id=lease_id, property_id=lease_id, unit_number=unit_no,
        monthly_rent=Decimal(rent), status="occupied",
    )
    prop = Property(
        id=lease_id, name=property_name, address="x", city="Pasay", total_units=1,
    )
    ten = Tenant(id=lease_id, full_name=tenant, is_active=True)
    return lease, unit, prop, ten


def _ctx(incomes=None, **kw):
    lease, unit, prop, ten = _make_ctx(**kw)
    incomes = incomes or []
    periods = lease_periods(lease)
    confirmed = [i for i in incomes if i.status == IncomeStatus.confirmed]
    ctx = LeaseCtx(
        lease=lease, unit=unit, property=prop, tenant=ten,
        periods=periods,
        paid_by_period=confirmed_paid_by_period(periods, confirmed),
        incomes=incomes,
    )
    for income in [i for i in incomes if i.status == IncomeStatus.pending]:
        month = income.description.split()[-1] if "rent " in (income.description or "") else income.received_date.strftime("%Y-%m")
        if month in {m for m, _ in periods}:
            ctx.pending_periods.add(month)
    return ctx


def _inc(period, status, amount="70000.00", received="2026-08-10", income_id=1):
    return Income(
        id=income_id, lease_id=1, amount=Decimal(amount),
        received_date=date.fromisoformat(received), status=status,
        description=f"rent {period}",
    )


def _paid_jan_jul(amount="70000.00"):
    return [
        _inc(f"2026-{m:02d}", IncomeStatus.confirmed, amount=amount,
             received=f"2026-{m:02d}-10", income_id=m)
        for m in range(1, 8)
    ]


# --- text parsing -----------------------------------------------------------

def test_parse_unit_phrase():
    hints = parse_hints("1608租金收到了", ["1608", "DEV-BAY-1708"], TODAY)
    assert hints.unit_hints == ["1608"]
    assert hints.amounts == []
    assert hints.received_date == TODAY


def test_unit_match_rejects_digit_suffix_overmatch():
    """SLICE2-RENT-003FIX: '608' must not over-match '1608' / 'DEV-BAY-1608'
    and '20' must not match '1020', while '1608' still resolves the prefixed
    dev style and exact ids/punctuation still match."""
    assert not _unit_matches("608", "1608")
    assert not _unit_matches("608", "DEV-BAY-1608")
    assert not _unit_matches("8", "1608")
    assert not _unit_matches("20", "1020")
    assert _unit_matches("1608", "DEV-BAY-1608")
    assert _unit_matches("1608.", "DEV-BAY-1608")
    assert _unit_matches("DEV-BAY-1608", "DEV-BAY-1608")
    assert _unit_matches("20", "20")


def test_parse_unit_hint_rejects_digit_suffix_overmatch():
    """The unit hint parser must not resolve '608租金收到了' to unit 1608 /
    DEV-BAY-1608, and must still resolve '1608' against the prefixed style."""
    assert parse_hints("608租金收到了", ["1608", "DEV-BAY-1608"], TODAY).unit_hints == []
    assert parse_hints("1608租金收到了", ["DEV-BAY-1608"], TODAY).unit_hints == ["1608"]
    assert parse_hints("20租金收到了", ["1020", "20"], TODAY).unit_hints == ["20"]


def test_english_sentence_with_trailing_period():
    """SLICE2-RENT-002 Secretary examples end with '.', which must not hide
    the unit hint: 'Received rent for 1608.' and '1608 rent received.' both
    resolve to the unique open bill (HIGH)."""
    for text in ("Received rent for 1608.", "1608 rent received."):
        hints = parse_hints(text, ["1608", "1708"], TODAY)
        assert hints.unit_hints == ["1608"]
        result = match_from_leases([_ctx(_paid_jan_jul())], text, today=TODAY)
        best = result.best
        assert best is not None
        assert best.kind == MatchKind.OPEN
        assert best.confidence == MatchConfidence.HIGH
        assert best.period == "2026-08"


def test_parse_tenant_amount_phrase():
    hints = parse_hints("John的70000到了", ["1608", "1708"], TODAY)
    assert hints.unit_hints == []
    assert hints.amounts == [Decimal("70000")]
    assert hints.received_date == TODAY


def test_parse_yesterday_phrase():
    hints = parse_hints("昨天收到1608房租", ["1608"], TODAY)
    assert hints.unit_hints == ["1608"]
    assert hints.received_date == date(2026, 8, 11)


def test_parse_period_hint():
    hints = parse_hints("8月租金收到了", [], TODAY)
    assert hints.period_hint == "2026-08"


def test_parse_full_date_wins_over_period():
    hints = parse_hints("8月10日收到1608租金", ["1608"], TODAY)
    assert hints.received_date == date(2026, 8, 10)
    assert hints.period_hint is None


def test_parse_amount_skips_unit_number():
    hints = parse_hints("1608租金收到了", ["1608"], TODAY)
    assert hints.amounts == []


# --- matching ---------------------------------------------------------------

def test_high_unique_exact_match():
    """1608租金收到了 / 唯一未结清 + 金额一致 + 无重复 -> HIGH."""
    result = match_from_leases([_ctx(_paid_jan_jul())], "1608租金收到了", today=TODAY)
    best = result.best
    assert best.kind == MatchKind.OPEN
    assert best.confidence == MatchConfidence.HIGH
    assert best.period == "2026-08"
    assert best.amount == Decimal("70000.00")
    assert best.open_count == 1
    assert best.remaining_balance == Decimal("0.00")
    assert best.unit_number == "1608"


def test_tenant_and_amount_phrase_high():
    ctx_1608 = _ctx(_paid_jan_jul(), lease_id=1)
    ctx_1708 = _ctx(
        [_inc("2026-07", IncomeStatus.confirmed, amount="65000.00", received="2026-07-10", income_id=20)],
        unit_no="1708", rent="65000.00", tenant="Ana Cruz", lease_id=2,
    )
    result = match_from_leases([ctx_1608, ctx_1708], "John的70000到了", today=TODAY)
    assert result.best.kind == MatchKind.OPEN
    assert result.best.confidence == MatchConfidence.HIGH
    assert result.best.unit_number == "1608"
    assert result.best.period == "2026-08"


def test_yesterday_date_hint_high():
    result = match_from_leases([_ctx(_paid_jan_jul())], "昨天收到1608房租", today=TODAY)
    assert result.received_date == date(2026, 8, 11)
    assert result.best.confidence == MatchConfidence.HIGH


def test_duplicate_recognized():
    incomes = _paid_jan_jul() + [
        _inc("2026-08", IncomeStatus.confirmed, received="2026-08-10", income_id=99)
    ]
    result = match_from_leases([_ctx(incomes)], "1608租金收到了", today=TODAY)
    assert result.candidates
    best = result.best
    assert best.kind == MatchKind.DUPLICATE
    assert best.income_id == 99
    assert best.income_status == "confirmed"
    # Several paid months exist -> the most recent one is the best duplicate;
    # confidence is MEDIUM because the month is not stated explicitly.
    assert best.confidence == MatchConfidence.MEDIUM


def test_pending_income_returns_pending_kind():
    incomes = _paid_jan_jul() + [
        _inc("2026-08", IncomeStatus.pending, received="2026-08-11", income_id=77)
    ]
    result = match_from_leases([_ctx(incomes)], "1608租金收到了", today=TODAY)
    assert result.best.kind == MatchKind.PENDING
    assert result.best.income_id == 77


def test_two_open_months_ambiguous_low():
    incomes = _paid_jan_jul()[:-1] + [
        _inc("2026-06", IncomeStatus.confirmed, received="2026-06-10", income_id=6),
        _inc("2026-07", IncomeStatus.reversed, received="2026-07-10", income_id=7),
    ]
    result = match_from_leases([_ctx(incomes)], "1608租金收到了", today=TODAY)
    assert len(result.candidates) == 2
    assert result.best.confidence == MatchConfidence.LOW
    assert result.best.open_count == 2
    assert result.best.remaining_balance == Decimal("70000.00")


def test_partial_amount_opens_partial_candidate():
    """SLICE2-RENT-005: a below-rent amount is a valid partial payment, not a
    mismatch. Candidate carries due / paid / remaining-after for the bot."""
    result = match_from_leases([_ctx(_paid_jan_jul())], "1608的60000到了", today=TODAY)
    best = result.best
    assert best is not None
    assert best.kind == MatchKind.OPEN
    assert best.amount == Decimal("60000.00")
    assert best.due_amount == Decimal("70000.00")
    assert best.paid_amount == Decimal("0.00")
    assert best.remaining_balance == Decimal("10000.00")
    assert best.confidence == MatchConfidence.HIGH


def test_partial_cumulative_accumulates_and_settles():
    """40k -> 30k -> 30k on a 100k bill: paid_amount grows and the final
    payment leaves remaining 0 (paid off)."""
    ctx = _ctx(_paid_jan_jul(amount="100000.00"), rent="100000.00")
    partials = [
        ("1608 付了 40000", Decimal("40000.00"), Decimal("0.00"), Decimal("60000.00")),
        ("1608 又付了 30000", Decimal("30000.00"), Decimal("40000.00"), Decimal("30000.00")),
        ("1608 又付了 30000", Decimal("30000.00"), Decimal("70000.00"), Decimal("0.00")),
    ]
    for text, want_amount, want_paid, want_remaining in partials:
        result = match_from_leases([ctx], text, today=TODAY)
        best = result.best
        assert best.kind == MatchKind.OPEN
        assert best.amount == want_amount
        assert best.paid_amount == want_paid
        assert best.remaining_balance == want_remaining
        assert best.due_amount == Decimal("100000.00")
        ctx.paid_by_period["2026-08"] = ctx.paid_by_period.get("2026-08", Decimal("0")) + want_amount


def test_partial_pending_same_amount_confirms_existing():
    """Re-reporting a partial that is still pending must surface that pending
    record (confirm it) instead of creating a second one."""
    incomes = _paid_jan_jul() + [
        _inc("2026-08", IncomeStatus.pending, amount="40000.00", received="2026-08-11", income_id=77)
    ]
    result = match_from_leases([_ctx(incomes)], "1608 付了 40000", today=TODAY)
    best = result.best
    assert best.kind == MatchKind.PENDING
    assert best.income_id == 77
    assert best.amount == Decimal("40000.00")


def test_partial_same_amount_twice_is_two_payments_until_settled():
    """Two genuine 30k partials are both allowed while 30k remains; after the
    period is settled, re-reporting the same amount is a duplicate."""
    ctx = _ctx(_paid_jan_jul(amount="100000.00"), rent="100000.00")
    result = match_from_leases([ctx], "1608 付了 30000", today=TODAY)
    assert result.best.kind == MatchKind.OPEN
    assert result.best.remaining_balance == Decimal("70000.00")
    ctx.incomes.append(_inc("2026-08", IncomeStatus.confirmed, amount="30000.00", income_id=81))
    ctx.paid_by_period["2026-08"] = Decimal("30000.00")
    result = match_from_leases([ctx], "1608 付了 30000", today=TODAY)
    assert result.best.kind == MatchKind.OPEN
    assert result.best.remaining_balance == Decimal("40000.00")
    ctx.incomes.append(_inc("2026-08", IncomeStatus.confirmed, amount="30000.00", income_id=82))
    ctx.incomes.append(_inc("2026-08", IncomeStatus.confirmed, amount="40000.00", income_id=83))
    ctx.paid_by_period["2026-08"] = Decimal("100000.00")  # settled
    result = match_from_leases([ctx], "1608 付了 30000", today=TODAY)
    assert result.best.kind == MatchKind.DUPLICATE


def test_overpayment_is_blocked_not_booked():
    """A payment bigger than the remaining balance is an overpayment candidate
    (explain, never suggest confirmation)."""
    incomes = _paid_jan_jul() + [
        _inc("2026-08", IncomeStatus.confirmed, amount="40000.00", received="2026-08-10", income_id=51),
    ]
    result = match_from_leases([_ctx(incomes)], "1608 付了 50000", today=TODAY)
    best = result.best
    assert best is not None
    assert best.kind == MatchKind.OVERPAYMENT
    assert best.amount == Decimal("50000.00")
    assert best.paid_amount == Decimal("40000.00")
    assert best.due_amount == Decimal("70000.00")
    assert best.remaining_balance == Decimal("30000.00")


def test_invalid_zero_amount_rejected():
    for text in ("1608 付了 0", "1608 付了 0.00", "1608 付了 -5000"):
        result = match_from_leases([_ctx(_paid_jan_jul())], text, today=TODAY)
        best = result.best
        assert best is not None and best.kind == MatchKind.INVALID_AMOUNT, text


def test_no_amount_statement_on_partially_paid_period_settles_remaining():
    """A statement without an amount is interpreted as settling the remaining
    balance of the matched period (40k paid of 100k -> 60k remaining)."""
    ctx = _ctx(_paid_jan_jul(amount="100000.00"), rent="100000.00")
    ctx.paid_by_period["2026-08"] = Decimal("40000.00")
    result = match_from_leases([ctx], "1608 租金收到了", today=TODAY)
    best = result.best
    assert best.kind == MatchKind.OPEN
    assert best.amount == Decimal("60000.00")
    assert best.remaining_balance == Decimal("0.00")


def test_period_remaining_never_negative():
    assert period_remaining(Decimal("100000.00"), Decimal("120000.00")) == Decimal("0.00")
    assert period_remaining(Decimal("100000.00"), Decimal("70000.00")) == Decimal("30000.00")


def test_no_hints_ambiguous_across_leases():
    ctx_1608 = _ctx(_paid_jan_jul(), lease_id=1)
    ctx_1708 = _ctx(
        [
            _inc(f"2026-{m:02d}", IncomeStatus.confirmed, amount="65000.00",
                 received=f"2026-{m:02d}-10", income_id=20 + m)
            for m in range(1, 8)
        ],
        unit_no="1708", rent="65000.00", tenant="Ana Cruz", lease_id=2,
    )
    result = match_from_leases([ctx_1608, ctx_1708], "租金收到了", today=TODAY)
    assert len(result.candidates) == 2
    assert result.best.confidence == MatchConfidence.LOW


def test_period_hint_picks_month():
    incomes = _paid_jan_jul() + [
        _inc("2026-08", IncomeStatus.confirmed, received="2026-08-10", income_id=99)
    ]
    # July was reversed -> both Jul and Aug are... Aug is confirmed; only Jul open.
    incomes = incomes + [
        _inc("2026-07", IncomeStatus.reversed, received="2026-07-10", income_id=7)
    ]
    result = match_from_leases([_ctx(incomes)], "7月租金收到了", today=TODAY)
    assert result.best.period == "2026-07"


# --- API integration (needs the PostgreSQL test DB like the rest of the suite)


def _seed_payment_match_data(client, db_session, admin_headers):
    from tests.conftest import ensure_default_org

    _org = ensure_default_org(db_session)
    org_id = _org.id
    db_session.flush()
    prop = client.post(
        "/api/v1/properties",
        json={"name": "Bayshore", "address": "1 Roxas Blvd", "city": "Pasay", "total_units": 1, "organization_id": org_id},
        headers=admin_headers,
    )
    assert prop.status_code in {200, 201}, prop.text
    prop = prop.json()
    unit = client.post(
        "/api/v1/units",
        json={
            "property_id": prop["id"], "unit_number": "1608", "floor": "16",
            "size_sqm": "40.00", "monthly_rent": "70000.00", "status": "vacant",
        },
        headers=admin_headers,
    )
    assert unit.status_code in {200, 201}, unit.text
    unit = unit.json()
    tenant = client.post(
        "/api/v1/tenants",
        json={"full_name": "John Dela Cruz", "phone": "+639170000000", "organization_id": org_id},
        headers=admin_headers,
    )
    assert tenant.status_code in {200, 201}, tenant.text
    tenant = tenant.json()
    lease = client.post(
        "/api/v1/leases",
        json={
            "unit_id": unit["id"], "tenant_id": tenant["id"],
            "start_date": "2026-01-01", "end_date": "2026-12-31",
            "monthly_rent": "70000.00", "deposit": "140000.00", "status": "active",
        },
        headers=admin_headers,
    )
    assert lease.status_code in {200, 201}, lease.text
    lease = lease.json()
    for m in range(1, 8):
        r = client.post(
            "/api/v1/incomes",
            json={
                "lease_id": lease["id"], "amount": "70000.00",
                "received_date": f"2026-{m:02d}-10", "payment_method": "Bank",
                "status": "confirmed", "description": f"rent 2026-{m:02d}",
                "idempotency_key": f"seed-2026-{m:02d}",
            },
            headers=admin_headers,
        )
        assert r.status_code in {200, 201}, r.text
    return lease


def test_match_endpoint_exact_high(client, db_session, admin_headers, manager_headers, agent_headers):
    lease = _seed_payment_match_data(client, db_session, admin_headers)
    resp = client.post(
        "/api/v1/payments/match",
        json={"text": "1608租金收到了"},
        headers=admin_headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["candidates"]
    best = body["candidates"][0]
    assert best["kind"] == "open"
    assert best["confidence"] == "high"
    assert best["lease_id"] == lease["id"]
    assert best["unit_number"] == "1608"
    assert best["period"] == "2026-08"
    assert best["open_count"] == 1
    assert best["remaining_balance"] == "0.00"

    # manager (Secretary-class) can read the matcher too; agent cannot.
    resp = client.post("/api/v1/payments/match", json={"text": "1608租金收到了"}, headers=manager_headers)
    assert resp.status_code == 200
    resp = client.post("/api/v1/payments/match", json={"text": "1608租金收到了"}, headers=agent_headers)
    assert resp.status_code == 403


def test_match_endpoint_duplicate(client, db_session, admin_headers):
    lease = _seed_payment_match_data(client, db_session, admin_headers)
    client.post(
        "/api/v1/incomes",
        json={
            "lease_id": lease["id"], "amount": "70000.00", "received_date": "2026-08-10",
            "payment_method": "Bank", "status": "confirmed",
            "description": "rent 2026-08", "idempotency_key": "seed-2026-08",
        },
        headers=admin_headers,
    )
    resp = client.post(
        "/api/v1/payments/match",
        json={"text": "1608租金收到了"},
        headers=admin_headers,
    )
    assert resp.status_code == 200
    best = resp.json()["candidates"][0]
    assert best["kind"] == "duplicate"
