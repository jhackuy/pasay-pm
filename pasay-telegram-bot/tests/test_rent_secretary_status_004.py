"""P0-RENT-SECRETARY-STATUS-004 regression tests.

The rent entry ("💰 收租") must never emit the "Rent paid this month"
empty-state while /reports/overdue-rents is non-empty. The overdue report is
the single authority for "exists outstanding" - including units whose unpaid
periods do not contain the current month (e.g. Jun-Jul while Aug is not yet
due). Both Owner (zh) and Secretary (en) locales are covered.
"""
from __future__ import annotations

import asyncio

import httpx

from conftest import (
    OWNER_ID,
    SECRETARY_ID,
    make_callback_update,
    make_text_update,
    run_updates,
)
from pasay_bot.api_client import OverdueRent, PasayApiClient
from pasay_bot.handlers.commands import build_rent_collect_list
from pasay_bot.keyboards import ACTION_RENT, encode
from pasay_bot.render import cards
from pasay_bot.render.i18n import t


def _overdue_rows():
    """3 overdue rows whose unpaid periods do NOT include the current month -
    the exact scenario that used to flip the who-unpaid answer to "all paid"
    while the collect page still listed 3 units."""
    return [
        {
            "lease_id": 6, "unit_id": 7, "tenant_id": 6, "unit": "DEV-BAY-1680",
            "tenant": "Lena Cruz", "overdue_months": 3, "amount_per_month": "25000.00",
            "total_outstanding": "75000.00", "oldest_due_date": "2026-05-01",
            "overdue_days": 101,
            "overdue_periods": [
                {"month": "2026-05", "amount": "25000.00"},
                {"month": "2026-06", "amount": "25000.00"},
                {"month": "2026-07", "amount": "25000.00"},
            ],
        },
        {
            "lease_id": 3, "unit_id": 3, "tenant_id": 3, "unit": "DEV-BAY-2208",
            "tenant": "Carlo Reyes", "overdue_months": 2, "amount_per_month": "55000.00",
            "total_outstanding": "110000.00", "oldest_due_date": "2026-06-20",
            "overdue_days": 55,
            "overdue_periods": [
                {"month": "2026-06", "amount": "55000.00"},
                {"month": "2026-07", "amount": "55000.00"},
            ],
        },
        {
            "lease_id": 5, "unit_id": 5, "tenant_id": 5, "unit": "DEV-SOL-1805",
            "tenant": "Paolo Cruz", "overdue_months": 2, "amount_per_month": "48000.00",
            "total_outstanding": "96000.00", "oldest_due_date": "2026-07-05",
            "overdue_days": 44,
            "overdue_periods": [
                {"month": "2026-07", "amount": "48000.00"},
                {"month": "2026-08", "amount": "48000.00"},
            ],
        },
    ]


def _overdue_leases():
    return [
        {"id": 6, "unit_id": 7, "tenant_id": 6, "start_date": "2026-01-01", "end_date": "2026-12-31",
         "accounting_start_date": "2026-05-01", "monthly_rent": "25000.00", "status": "active", "due_day": 1},
        {"id": 3, "unit_id": 3, "tenant_id": 3, "start_date": "2026-01-01", "end_date": "2026-12-31",
         "accounting_start_date": "2026-03-01", "monthly_rent": "55000.00", "status": "active", "due_day": 20},
        {"id": 5, "unit_id": 5, "tenant_id": 5, "start_date": "2026-01-01", "end_date": "2026-12-31",
         "accounting_start_date": "2026-06-01", "monthly_rent": "48000.00", "status": "active", "due_day": 5},
    ]


def _overdue_units():
    return [
        {"id": 7, "property_id": 1, "unit_number": "DEV-BAY-1680", "monthly_rent": "25000.00", "status": "occupied", "is_active": True},
        {"id": 3, "property_id": 1, "unit_number": "DEV-BAY-2208", "monthly_rent": "55000.00", "status": "occupied", "is_active": True},
        {"id": 5, "property_id": 2, "unit_number": "DEV-SOL-1805", "monthly_rent": "48000.00", "status": "occupied", "is_active": True},
    ]


def _client(handler) -> PasayApiClient:
    return PasayApiClient(
        "http://test/api/v1", "k", timeout=2.0,
        transport=httpx.MockTransport(handler),
    )


def _run(coro):
    return asyncio.run(coro)


def _backend(overdue):
    props = [
        {"id": 1, "name": "DEV - Bayshore", "address": "x", "city": "Pasay", "is_active": True},
        {"id": 2, "name": "DEV - Solemare", "address": "x", "city": "Pasay", "is_active": True},
    ]

    def handler(request):
        path = request.url.path
        if path.endswith("/properties"):
            return httpx.Response(200, json=props)
        if path.endswith("/units"):
            return httpx.Response(200, json=_overdue_units())
        if path.endswith("/leases"):
            return httpx.Response(200, json=_overdue_leases())
        if path.endswith("/incomes"):
            return httpx.Response(200, json=[])
        if path.endswith("/reports/overdue-rents"):
            return httpx.Response(200, json=overdue)
        return httpx.Response(404, json={})

    return handler


def test_rent_collect_never_paid_state_when_overdue_nonempty():
    """3 overdue rows (even when periods skip the current month) -> collect
    page lists 3 units and NEVER the paid-clear copy, in both locales."""
    for locale, paid in (("zh", "本月租金已全部收齐"), ("en", "All rent collected this month")):
        client = _client(_backend(_overdue_rows()))
        try:
            text, _ = _run(build_rent_collect_list(client, locale))
        finally:
            _run(client.aclose())
        for unit in ("DEV-BAY-1680", "DEV-BAY-2208", "DEV-SOL-1805"):
            assert unit in text
        assert paid not in text


def test_rent_collect_paid_state_only_when_overdue_empty():
    """0 overdue rows -> the paid-clear copy is the only acceptable state."""
    for locale in ("zh", "en"):
        client = _client(_backend([]))
        try:
            text, _ = _run(build_rent_collect_list(client, locale))
        finally:
            _run(client.aclose())
        assert t("rent.collect_all_paid", locale) in text


def test_unpaid_card_never_paid_state_when_rows_nonempty():
    month = "2026-08"
    rows = [OverdueRent.from_dict(d) for d in _overdue_rows()]
    for locale, paid in (("zh", "本月租金已全部收齐"), ("en", "All rent collected this month")):
        text = cards.unpaid_list_card(rows, month, locale)
        assert paid not in text
        for unit in ("DEV-BAY-1680", "DEV-BAY-2208", "DEV-SOL-1805"):
            assert unit in text
        empty = cards.unpaid_list_card([], month, locale)
        assert paid in empty


def test_who_unpaid_query_uses_overdue_report_authority(make_app):
    """Secretary's 'who hasn't paid' answer must match the overdue report:
    with 3 overdue rows whose periods skip the current month, the answer lists
    all 3 and never says 'All rent collected this month'."""
    env = make_app()
    env.backend.overdue = _overdue_rows()
    run_updates(
        env,
        [make_text_update(SECRETARY_ID, SECRETARY_ID, "who hasn't paid this month", bot=env.bot)],
    )
    sent = env.bot.last_send()["text"]
    for unit in ("DEV-BAY-1680", "DEV-BAY-2208", "DEV-SOL-1805"):
        assert unit in sent
    assert t("rent.collect_all_paid", "en") not in sent


def test_who_unpaid_paid_state_only_when_report_empty(make_app):
    env = make_app()
    env.backend.overdue = []
    run_updates(
        env,
        [make_text_update(OWNER_ID, OWNER_ID, "谁还没交房租", bot=env.bot)],
    )
    sent = env.bot.last_send()["text"]
    assert t("rent.collect_all_paid", "zh") in sent


def test_unit_entry_not_blocked_when_incomes_land_other_months(make_app):
    """A unit whose confirmed incomes were received this month but describe
    other months must still enter the collection flow (never 'already paid')."""
    env = make_app()
    for month in ("2026-05", "2026-06", "2026-07"):
        env.backend.add_income(
            status="confirmed", lease_id=1, amount="55000.00",
            received_date="2026-08-10", description=f"DEV-{month} rent",
        )
    run_updates(
        env,
        [make_callback_update(OWNER_ID, OWNER_ID, encode(ACTION_RENT, "go", "1"), bot=env.bot)],
    )
    edited = env.bot.last_edit()["text"] or ""
    assert t("unit.payment_paid", "zh") not in edited
    assert t("rent.confirm_title", "zh") in edited or "确认" in edited
