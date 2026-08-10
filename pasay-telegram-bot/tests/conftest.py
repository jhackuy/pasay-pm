"""Shared fixtures: FakeBot, FakeBackend (httpx MockTransport), make_app."""
from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace
from typing import Any, Optional

import httpx
import pytest
from telegram import Update

from pasay_bot.api_client import PasayApiClient
from pasay_bot.config import Settings
from pasay_bot.main import build_application
from pasay_bot.state.idempotency import IdempotencyGuard
from pasay_bot.state.store import StateStore

OWNER_ID = 5177241442
SECRETARY_ID = 1083657401
UNKNOWN_ID = 999888777


class FakeBot:
    def __init__(self):
        self.calls: list[dict] = []
        self.username = "pasay_test_bot"
        self.id = 999

    async def initialize(self):
        pass

    async def shutdown(self):
        pass

    async def get_me(self):
        return SimpleNamespace(username=self.username, id=self.id)

    async def send_message(self, chat_id, text=None, parse_mode=None, reply_markup=None, **kw):
        self.calls.append(
            {
                "type": "send_message",
                "chat_id": chat_id,
                "text": text,
                "parse_mode": parse_mode,
                "reply_markup": reply_markup,
            }
        )
        return SimpleNamespace(chat_id=chat_id, message_id=len(self.calls), text=text)

    async def edit_message_text(self, text=None, chat_id=None, message_id=None,
                                parse_mode=None, reply_markup=None, **kw):
        self.calls.append(
            {
                "type": "edit_message_text",
                "chat_id": chat_id,
                "message_id": message_id,
                "text": text,
                "parse_mode": parse_mode,
                "reply_markup": reply_markup,
            }
        )
        return SimpleNamespace(chat_id=chat_id, message_id=message_id, text=text)

    async def answer_callback_query(self, callback_query_id, text=None, show_alert=False, **kw):
        self.calls.append(
            {"type": "answer_callback_query", "id": callback_query_id, "text": text}
        )

    # --- test helpers ---
    def of_type(self, kind):
        return [c for c in self.calls if c["type"] == kind]

    def sends(self):
        return self.of_type("send_message")

    def edits(self):
        return self.of_type("edit_message_text")

    def answers(self):
        return self.of_type("answer_callback_query")

    def last_send(self):
        return self.sends()[-1]

    def last_edit(self):
        return self.edits()[-1]

    def last_answer(self):
        return self.answers()[-1]

    def all_texts(self):
        return [c["text"] or "" for c in self.calls if c.get("text") is not None]


class FakeBackend:
    """httpx MockTransport handler simulating the Pasay PM API."""

    def __init__(self):
        self.calls: list[tuple[str, str, Optional[dict]]] = []
        self.auth_calls: list[str] = []
        self.properties = [
            {"id": 1, "name": "Pasay Premier Residences", "address": "5 Roxas Blvd",
             "city": "Pasay", "total_units": 2, "is_active": True},
            {"id": 2, "name": "Bayshore & Tower", "address": "5 > 3 Street",
             "city": "Pasay", "total_units": 1, "is_active": True},
        ]
        self.units = [
            {"id": 1, "property_id": 1, "unit_number": "16B", "floor": "16",
             "size_sqm": "40.00", "monthly_rent": "55000.00", "status": "occupied", "is_active": True},
            {"id": 2, "property_id": 1, "unit_number": "17A", "floor": "17",
             "size_sqm": "35.00", "monthly_rent": "45000.00", "status": "vacant", "is_active": True},
            {"id": 3, "property_id": 2, "unit_number": "2C", "floor": "2",
             "size_sqm": "25.00", "monthly_rent": "12000.00", "status": "occupied", "is_active": True},
        ]
        self.leases = [
            {"id": 1, "unit_id": 1, "tenant_id": 1, "start_date": "2026-01-01",
             "end_date": "2026-12-31", "accounting_start_date": None, "monthly_rent": "55000.00",
             "deposit": "110000.00", "status": "active", "due_day": 5, "notes": None},
            {"id": 2, "unit_id": 3, "tenant_id": 2, "start_date": "2026-03-01",
             "end_date": "2026-12-31", "accounting_start_date": None, "monthly_rent": "12000.00",
             "deposit": "24000.00", "status": "active", "due_day": 10, "notes": None},
        ]
        self.tenants = [
            {"id": 1, "full_name": "Juan Dela Cruz", "phone": "+639170000000",
             "email": "juan@example.com", "is_active": True},
            {"id": 2, "full_name": "Maria <Admin>", "phone": None, "email": None, "is_active": True},
        ]
        self.incomes: list[dict] = []
        self._next_income_id = 1
        self.financial_summary = {
            "month": "2026-08", "expected_rent_total": "363000.00",
            "collected_rent": "190000.00", "outstanding_rent": "173000.00",
            "total_income": "721000.00", "total_expense": "19650.00",
            "net_income": "701350.00", "units_count": 3, "occupied_units": 2, "vacant_units": 1,
        }
        self.overdue_rows = [
            {"lease_id": 1, "unit_id": 1, "tenant_id": 1, "unit": "16B", "tenant": "Juan Dela Cruz",
             "overdue_months": 1, "overdue_periods": [{"month": "2026-08", "amount": "55000.00"}],
             "amount_per_month": "55000.00", "total_outstanding": "55000.00",
             "oldest_due_date": "2026-08-05", "overdue_days": 5,
             "outstanding": "55000.00", "days_overdue": 5},
            {"lease_id": 2, "unit_id": 3, "tenant_id": 2, "unit": "2C", "tenant": "Maria <Admin>",
             "overdue_months": 2, "overdue_periods": [], "amount_per_month": "12000.00",
             "total_outstanding": "24000.00", "oldest_due_date": "2026-07-10", "overdue_days": 40,
             "outstanding": "24000.00", "days_overdue": 40},
        ]
        self.overdue: Optional[list] = None
        self.timeout_after_write_paths: set[str] = set()
        self.timeout_before_write_paths: set[str] = set()
        self.timeout_without_effect_paths: set[str] = set()
        self.fail_status: dict[str, int] = {}

    def add_income(self, status="pending", lease_id=1, amount="55000.00",
                   received_date="2026-08-10", payment_method="Bank",
                   description="rent 2026-08", income_id=None):
        inc = {
            "id": income_id or self._next_income_id,
            "lease_id": lease_id,
            "amount": amount,
            "received_date": received_date,
            "payment_method": payment_method,
            "status": status,
            "description": description,
            "confirmed_by": None,
            "confirmed_at": None,
        }
        self._next_income_id = max(self._next_income_id, inc["id"]) + 1
        self.incomes.append(inc)
        return inc

    def _get_income(self, income_id):
        return next((i for i in self.incomes if i["id"] == income_id), None)

    def count_calls(self, method: str, path: str) -> int:
        return sum(1 for m, p, _ in self.calls if m == method and p == path)

    def auth_for(self, method: str, path: str) -> Optional[str]:
        """Last Authorization header sent for a given method+path (F2)."""
        auth = None
        for (m, p, _), a in zip(self.calls, self.auth_calls):
            if m == method and p == path:
                auth = a
        return auth

    async def handler(self, request: httpx.Request) -> httpx.Response:
        method, path = request.method, request.url.path
        if path.startswith("/api/v1"):
            path = path[len("/api/v1"):]
        body = None
        if request.content:
            try:
                body = json.loads(request.content)
            except Exception:
                body = None
        self.calls.append((method, path, body))
        self.auth_calls.append(request.headers.get("authorization") or "")

        if path in self.fail_status:
            return httpx.Response(self.fail_status[path], json={"detail": f"forced {self.fail_status[path]}"})
        if method == "POST" and path in self.timeout_before_write_paths:
            raise httpx.ReadTimeout("simulated timeout before write")

        if path == "/properties":
            return httpx.Response(200, json=self.properties)
        if path == "/units":
            return httpx.Response(200, json=self.units)
        if path.startswith("/units/"):
            uid = int(path.rsplit("/", 1)[1])
            u = next((x for x in self.units if x["id"] == uid), None)
            if u is None:
                return httpx.Response(404, json={"detail": "Unit not found"})
            return httpx.Response(200, json=u)
        if path == "/leases":
            return httpx.Response(200, json=self.leases)
        if path == "/tenants":
            return httpx.Response(200, json=self.tenants)
        if path == "/reports/financial-summary":
            return httpx.Response(200, json=self.financial_summary)
        if path == "/reports/overdue-rents":
            rows = self.overdue if self.overdue is not None else self.overdue_rows
            return httpx.Response(200, json=rows)

        if path == "/incomes" and method == "POST":
            payload = body or {}
            inc = self.add_income(
                status=payload.get("status", "pending"),
                lease_id=payload.get("lease_id"),
                amount=str(payload.get("amount", "0")),
                received_date=payload.get("received_date", "2026-08-10"),
                payment_method=payload.get("payment_method"),
                description=payload.get("description"),
            )
            if path in self.timeout_after_write_paths:
                raise httpx.ReadTimeout("simulated response timeout after create")
            return httpx.Response(201, json=inc)
        if path.startswith("/incomes/") and path.endswith("/confirm") and method == "POST":
            if path in self.timeout_without_effect_paths:
                raise httpx.ReadTimeout("simulated timeout without effect")
            income_id = int(path.split("/")[2])
            inc = self._get_income(income_id)
            if inc is None:
                return httpx.Response(404, json={"detail": "Income not found"})
            if inc["status"] != "pending":
                return httpx.Response(409, json={"detail": "Only pending income can be confirmed"})
            inc["status"] = "confirmed"
            inc["confirmed_by"] = 1
            inc["confirmed_at"] = "2026-08-10T12:00:00Z"
            if path in self.timeout_after_write_paths:
                raise httpx.ReadTimeout("simulated response timeout after write")
            return httpx.Response(200, json=inc)
        if path.startswith("/incomes/") and path.endswith("/reverse") and method == "POST":
            if path in self.timeout_without_effect_paths:
                raise httpx.ReadTimeout("simulated reverse timeout without effect")
            income_id = int(path.split("/")[2])
            inc = self._get_income(income_id)
            if inc is None:
                return httpx.Response(404, json={"detail": "Income not found"})
            if inc["status"] != "confirmed":
                return httpx.Response(409, json={"detail": "Only confirmed income can be reversed"})
            inc["status"] = "reversed"
            return httpx.Response(200, json=inc)
        if path.startswith("/incomes/") and method == "GET":
            income_id = int(path.split("/")[2])
            inc = self._get_income(income_id)
            if inc is None:
                return httpx.Response(404, json={"detail": "Income not found"})
            return httpx.Response(200, json=inc)
        if path == "/incomes" and method == "GET":
            return httpx.Response(200, json=self.incomes)

        return httpx.Response(404, json={"detail": f"no route {method} {path}"})


@pytest.fixture()
def make_app(tmp_path):
    created: list[tuple[Any, Any]] = []

    def _make(backend=None, api_key="manager-key", admin_api_key="admin-key",
              callback_ttl=900, state_db=None):
        backend = backend or FakeBackend()
        settings = Settings(
            state_db=state_db or str(tmp_path / f"state_{len(created)}.db"),
            pasay_tg_bot_token="123:TEST",
            pasay_api_base="http://test/api/v1",
            callback_ttl_seconds=callback_ttl,
            pasay_admin_api_key=admin_api_key,
        )
        store = StateStore(settings.state_db)
        guard = IdempotencyGuard(store)
        api = PasayApiClient(
            settings.pasay_api_base,
            api_key,
            timeout=1.0,
            transport=httpx.MockTransport(backend.handler),
        )
        admin_api = None
        if admin_api_key:
            admin_api = PasayApiClient(
                settings.pasay_api_base,
                admin_api_key,
                timeout=1.0,
                transport=httpx.MockTransport(backend.handler),
            )
        bot = FakeBot()
        app = build_application(settings, api, store, bot=bot, admin_api_client=admin_api)
        created.append((api, admin_api, store))
        return SimpleNamespace(
            app=app, bot=bot, backend=backend, store=store, guard=guard,
            settings=settings, api=api, admin_api=admin_api,
        )

    yield _make

    for api, admin_api, store in created:
        try:
            asyncio.run(api.aclose())
        except Exception:
            pass
        if admin_api is not None:
            try:
                asyncio.run(admin_api.aclose())
            except Exception:
                pass
        try:
            store.close()
        except Exception:
            pass


def make_text_update(user_id, chat_id, text, message_id=1, update_id=1, bot=None):
    msg = {
        "message_id": message_id,
        "date": int(time.time()),
        "chat": {"id": chat_id, "type": "private"},
        "from": {"id": user_id, "is_bot": False, "first_name": "T", "username": "t"},
        "text": text,
    }
    if text.startswith("/"):
        msg["entities"] = [
            {"type": "bot_command", "offset": 0, "length": len(text.split()[0])}
        ]
    return Update.de_json({"update_id": update_id, "message": msg}, bot)


def make_callback_update(user_id, chat_id, data, message_id=10, update_id=1, bot=None):
    return Update.de_json(
        {
            "update_id": update_id,
            "callback_query": {
                "id": f"cq{update_id}",
                "from": {"id": user_id, "is_bot": False, "first_name": "T", "username": "t"},
                "message": {
                    "message_id": message_id,
                    "date": int(time.time()),
                    "chat": {"id": chat_id, "type": "private"},
                    "text": "card",
                },
                "data": data,
                "chat_instance": "ci1",
            },
        },
        bot,
    )


def run_updates(env, updates):
    async def _run():
        async with env.app:
            for u in updates:
                await env.app.process_update(u)

    asyncio.run(_run())


def callback_data_of(env, call_type="edit_message_text", index=-1):
    """Extract the first button's callback_data from a recorded keyboard call."""
    call = env.bot.of_type(call_type)[index]
    kb = call["reply_markup"]
    return kb.inline_keyboard[0][0].callback_data
