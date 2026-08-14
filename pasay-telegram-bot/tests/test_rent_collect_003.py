"""P0-RENT-COLLECTION-UX-003 targeted tests.

The fixed 💰收租 entry must show EVERY currently collectible unit (backend
overdue report is the authority), with Unit / Tenant / 应收 / 已收 / 尚欠 /
到期状态 clearly separated, and must never:
* pick only the first / earliest-overdue unit;
* hide a unit because an unrelated income was recorded in the current month;
* repeat identical 💰收租 buttons at the same level;
* call Hermes/LLM or drop the X-Telegram-User-Id identity header.
"""
from __future__ import annotations

import asyncio

import httpx
import pytest

from conftest import (
    OWNER_ID,
    make_text_update,
    run_updates,
)
from pasay_bot.api_client import PasayApiClient
from pasay_bot.handlers import nl_bridge
from pasay_bot.handlers.commands import build_rent_collect_list
from pasay_bot.keyboards import (
    FIXED_MENU_ROUTES,
    collect_list_keyboard,
    todo_keyboard,
)
from pasay_bot.render import html as H
from pasay_bot.render.i18n import t

GROUP_CHAT_ID = -100777888999


def _client(handler) -> PasayApiClient:
    return PasayApiClient(
        "http://test/api/v1", "k", timeout=2.0,
        transport=httpx.MockTransport(handler),
    )


def _real_like_backend():
    """Mirror of the live runtime data (2026-08-14): 3 overdue units."""
    units = [
        {"id": 1, "property_id": 1, "unit_number": "DEV-BAY-1203", "monthly_rent": "65000.00", "status": "occupied", "is_active": True},
        {"id": 3, "property_id": 1, "unit_number": "DEV-BAY-2208", "monthly_rent": "55000.00", "status": "occupied", "is_active": True},
        {"id": 5, "property_id": 2, "unit_number": "DEV-SOL-1805", "monthly_rent": "48000.00", "status": "occupied", "is_active": True},
        {"id": 7, "property_id": 1, "unit_number": "DEV-BAY-1680", "monthly_rent": "25000.00", "status": "occupied", "is_active": True},
    ]
    leases = [
        {"id": 1, "unit_id": 1, "tenant_id": 1, "start_date": "2026-01-01", "end_date": "2026-12-31", "monthly_rent": "65000.00", "status": "active", "due_day": 5},
        {"id": 3, "unit_id": 3, "tenant_id": 3, "start_date": "2026-01-01", "end_date": "2026-12-31", "accounting_start_date": "2026-03-01", "monthly_rent": "55000.00", "status": "active", "due_day": 20},
        {"id": 5, "unit_id": 5, "tenant_id": 5, "start_date": "2026-01-01", "end_date": "2026-12-31", "accounting_start_date": "2026-06-01", "monthly_rent": "48000.00", "status": "active", "due_day": 5},
        {"id": 6, "unit_id": 7, "tenant_id": 6, "start_date": "2026-01-01", "end_date": "2026-12-31", "accounting_start_date": "2026-05-01", "monthly_rent": "25000.00", "status": "active", "due_day": 1},
    ]
    incomes = [
        {"id": 1, "lease_id": 1, "amount": "65000.00", "status": "confirmed", "received_date": "2026-08-10", "description": "DEV-2026-08 rent"},
        {"id": 5, "lease_id": 3, "amount": "55000.00", "status": "confirmed", "received_date": "2026-08-10", "description": "DEV-2026-03 rent"},
        {"id": 6, "lease_id": 3, "amount": "55000.00", "status": "confirmed", "received_date": "2026-08-10", "description": "DEV-2026-04 rent"},
        {"id": 7, "lease_id": 3, "amount": "55000.00", "status": "confirmed", "received_date": "2026-08-10", "description": "DEV-2026-05 rent"},
        {"id": 10, "lease_id": 5, "amount": "48000.00", "status": "confirmed", "received_date": "2026-08-10", "description": "DEV-2026-06 rent"},
    ]
    overdue = [
        {
            "lease_id": 6, "unit_id": 7, "tenant_id": 6, "unit": "DEV-BAY-1680", "tenant": "DEV Lena Cruz",
            "overdue_months": 4, "amount_per_month": "25000.00", "total_outstanding": "100000.00",
            "oldest_due_date": "2026-05-01", "overdue_days": 101,
            "overdue_periods": [
                {"month": "2026-05", "amount": "25000.00"}, {"month": "2026-06", "amount": "25000.00"},
                {"month": "2026-07", "amount": "25000.00"}, {"month": "2026-08", "amount": "25000.00"},
            ],
        },
        {
            "lease_id": 3, "unit_id": 3, "tenant_id": 3, "unit": "DEV-BAY-2208", "tenant": "DEV Carlo Reyes",
            "overdue_months": 2, "amount_per_month": "55000.00", "total_outstanding": "110000.00",
            "oldest_due_date": "2026-06-20", "overdue_days": 55,
            "overdue_periods": [
                {"month": "2026-06", "amount": "55000.00"}, {"month": "2026-07", "amount": "55000.00"},
            ],
        },
        {
            "lease_id": 5, "unit_id": 5, "tenant_id": 5, "unit": "DEV-SOL-1805", "tenant": "DEV Paolo Cruz",
            "overdue_months": 2, "amount_per_month": "48000.00", "total_outstanding": "96000.00",
            "oldest_due_date": "2026-07-05", "overdue_days": 44,
            "overdue_periods": [
                {"month": "2026-07", "amount": "48000.00"}, {"month": "2026-08", "amount": "48000.00"},
            ],
        },
    ]
    props = [
        {"id": 1, "name": "DEV - Bayshore", "address": "5 Roxas Blvd", "city": "Pasay", "is_active": True},
        {"id": 2, "name": "DEV - Solemare", "address": "7 Roxas Blvd", "city": "Pasay", "is_active": True},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/properties"):
            return httpx.Response(200, json=props)
        if path.endswith("/units"):
            return httpx.Response(200, json=units)
        if path.endswith("/leases"):
            return httpx.Response(200, json=leases)
        if path.endswith("/incomes"):
            return httpx.Response(200, json=incomes)
        if path.endswith("/reports/overdue-rents"):
            return httpx.Response(200, json=overdue)
        return httpx.Response(404, json={"detail": f"no mock for {path}"})

    return handler


def _run(coro):
    return asyncio.run(coro)


def test_rent_collect_lists_all_overdue_units_not_first_only():
    client = _client(_real_like_backend())
    try:
        text, keyboard = _run(build_rent_collect_list(client, "zh"))
    finally:
        _run(client.aclose())
    assert "DEV-BAY-1680" in text
    assert "DEV-BAY-2208" in text
    assert "DEV-SOL-1805" in text
    # fully paid unit never appears in the collect list
    assert "DEV-BAY-1203" not in text
    # per-unit amount semantics present for every unit
    for amount in ("100,000", "110,000", "96,000"):
        assert amount in text


def test_rent_collect_partial_payment_amounts_distinct():
    """Partially paid lease: 应收 ₱100,000 · 已收 ₱75,000 · 尚欠 ₱25,000."""
    units = [
        {"id": 7, "property_id": 1, "unit_number": "DEV-BAY-1680", "monthly_rent": "25000.00", "status": "occupied", "is_active": True},
    ]
    leases = [
        {"id": 6, "unit_id": 7, "tenant_id": 6, "start_date": "2026-05-01", "end_date": "2026-12-31",
         "accounting_start_date": "2026-05-01", "monthly_rent": "25000.00", "status": "active", "due_day": 1},
    ]
    incomes = [
        {"id": 21, "lease_id": 6, "amount": "25000.00", "status": "confirmed", "received_date": "2026-05-10", "description": "DEV-2026-05 rent"},
        {"id": 22, "lease_id": 6, "amount": "25000.00", "status": "confirmed", "received_date": "2026-06-10", "description": "DEV-2026-06 rent"},
        {"id": 23, "lease_id": 6, "amount": "25000.00", "status": "confirmed", "received_date": "2026-07-10", "description": "DEV-2026-07 rent"},
    ]
    overdue = [
        {
            "lease_id": 6, "unit_id": 7, "tenant_id": 6, "unit": "DEV-BAY-1680", "tenant": "Maria Santos",
            "overdue_months": 1, "amount_per_month": "25000.00", "total_outstanding": "25000.00",
            "oldest_due_date": "2026-08-01", "overdue_days": 13,
            "overdue_periods": [{"month": "2026-08", "amount": "25000.00"}],
        },
    ]
    props = [{"id": 1, "name": "DEV - Bayshore", "address": "x", "city": "Pasay", "is_active": True}]

    def handler(request):
        path = request.url.path
        if path.endswith("/properties"):
            return httpx.Response(200, json=props)
        if path.endswith("/units"):
            return httpx.Response(200, json=units)
        if path.endswith("/leases"):
            return httpx.Response(200, json=leases)
        if path.endswith("/incomes"):
            return httpx.Response(200, json=incomes)
        if path.endswith("/reports/overdue-rents"):
            return httpx.Response(200, json=overdue)
        return httpx.Response(404, json={})

    client = _client(handler)
    try:
        text, keyboard = _run(build_rent_collect_list(client, "zh"))
    finally:
        _run(client.aclose())
    expected = (
        f"{t('rent.receivable', 'zh')} {H.money('100000')} · "
        f"{t('rent.received', 'zh')} {H.money('75000')} · "
        f"{t('rent.outstanding', 'zh')} {H.money('25000')}"
    )
    assert expected in text
    # the monthly-rent figure alone (₱25,000) must never masquerade as the
    # whole debt: it only appears as the outstanding balance.
    assert "DEV-BAY-1680" in text
    assert "Maria Santos" in text


def test_rent_collect_keyboards_have_no_duplicate_rent_buttons():
    rows = [
        {"unit_id": 1, "unit": "DEV-BAY-1680", "unit_number": "DEV-BAY-1680", "outstanding": "100000", "overdue_days": 101},
        {"unit_id": 2, "unit": "DEV-BAY-2208", "unit_number": "DEV-BAY-2208", "outstanding": "110000", "overdue_days": 55},
        {"unit_id": 3, "unit": "DEV-SOL-1805", "unit_number": "DEV-SOL-1805", "outstanding": "96000", "overdue_days": 44},
    ]
    collect = collect_list_keyboard(rows, "zh")
    collect_labels = [b.text for row in collect.inline_keyboard for b in row]
    assert len(collect_labels) == len(set(collect_labels))

    todo = todo_keyboard({"overdue": rows}, owner_view=True, locale="zh")
    todo_labels = [b.text for row in todo.inline_keyboard for b in row]
    assert len(todo_labels) == len(set(todo_labels))
    # the bare generic label must not repeat; every action carries its unit
    assert t("todo.collect", "zh") not in todo_labels
    assert sum(1 for label in todo_labels if t("todo.collect", "zh") in label) == len(rows)


def test_rent_menu_flow_identity_headers_and_no_nl(make_app, monkeypatch):
    """Fixed 💰收租 menu click: X-Telegram-User-Id = real sender, and the NL
    bridge is provably never reached (monkeypatched to fail)."""
    env = make_app()

    def _boom(*args, **kwargs):
        raise AssertionError("nl_bridge.handle_nl must never run for the fixed rent menu")

    monkeypatch.setattr(nl_bridge, "handle_nl", _boom)
    rent_label = next(
        label for label, route in FIXED_MENU_ROUTES.items() if route == "rent"
    )
    run_updates(env, [make_text_update(OWNER_ID, GROUP_CHAT_ID, rent_label, bot=env.bot)])
    headers = env.backend.telegram_user_calls
    assert headers
    assert set(headers) == {str(OWNER_ID)}
    assert str(GROUP_CHAT_ID) not in headers
