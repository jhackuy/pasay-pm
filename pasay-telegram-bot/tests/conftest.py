"""Shared fixtures: FakeBot, FakeBackend (httpx MockTransport), make_app."""
from __future__ import annotations

import asyncio
import json
import re
import time
from types import SimpleNamespace
from typing import Any, Optional
from decimal import Decimal

import httpx
import pytest
from datetime import date, timedelta
from telegram.error import BadRequest
from telegram import Update
from telegram import ReplyKeyboardMarkup

TODAY = date.today().isoformat()


def today_str():
    return date.today().isoformat()


def _add_days(iso: str, days: int) -> str:
    return (date.fromisoformat(iso) + timedelta(days=days)).isoformat()

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
        self._answered_ids: set[str] = set()
        # Real Telegram semantics: only messages sent WITHOUT a reply keyboard
        # (or with an inline keyboard) are editable. Track what the bot sent so
        # edit_message_text can reject the reply-keyboard case exactly like the
        # live API does ("Message can't be edited").
        self._sent_by_id: dict[int, dict] = {}

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
        message_id = len(self.calls)
        self.calls[-1]["message_id"] = message_id
        self._sent_by_id[message_id] = {"chat_id": chat_id, "reply_markup": reply_markup}
        return SimpleNamespace(chat_id=chat_id, message_id=message_id, text=text)

    async def edit_message_text(self, text=None, chat_id=None, message_id=None,
                                parse_mode=None, reply_markup=None, **kw):
        sent = self._sent_by_id.get(message_id)
        if sent is not None and isinstance(sent.get("reply_markup"), ReplyKeyboardMarkup):
            # Mirrors the live Telegram API: a message sent with a non-inline
            # reply keyboard can not be edited.
            raise BadRequest("Message can't be edited")
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
        # Real Telegram accepts exactly ONE answerCallbackQuery per callback
        # id; a second answer fails with QUERY_ID_INVALID / "query is too old".
        # Enforcing this in the fake keeps the button paths honest (a second
        # answer must never be relied on for user-visible feedback).
        if callback_query_id in self._answered_ids:
            raise BadRequest(
                "query is too old and response timeout expired or query ID is invalid"
            )
        self._answered_ids.add(callback_query_id)
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
        self.telegram_user_calls: list[Optional[str]] = []
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
        self.expenses: list[dict] = []
        self._next_expense_id = 1
        self.operational_tasks: list[dict] = []
        self.ops_forbidden_task_ids: set[int] = set()
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
        # --- V1.3 Slice 2: Entry B rent-payment matcher ---
        self.rent_match_response: Optional[dict] = None
        self.tasks: list[dict] = []
        self.timeout_after_write_paths: set[str] = set()
        self.timeout_before_write_paths: set[str] = set()
        self.timeout_without_effect_paths: set[str] = set()
        self.fail_status: dict[str, int] = {}
        # --- button-determinism test knobs ---
        self.delay_seconds = 0.0       # slow-backend simulation (code-side)
        self.raise_on_paths: set[str] = set()  # unexpected exception simulation
        # --- V1.2.2 C2 copilot (TODAY / WHY / recommend / confirm / execute) ---
        self.copilot_today_payload = {
            "top_items": [
                {"item_ref": "lease:3", "reason_why_important": "Unit 1608 租金严重逾期。",
                 "suggested_action": "联系租客确认付款日期。"},
                {"item_ref": "task:9", "reason_why_important": "空调保养本周到期。",
                 "suggested_action": "安排技师上门。"},
            ],
            "summary": "2 项待办。",
        }
        self.copilot_recommend_error = None      # (status, detail)
        self.copilot_recommend_response = None   # full CopilotRecommendOut dict
        self.copilot_confirm_error = None
        self.copilot_execute_error = None
        self.copilot_execute_response = None     # full CopilotExecuteOut dict
        self.copilot_cancel_error = None
        self.copilot_ask_payload = {
            "answer": "本月租金：已收到 ₱190,000，未收 ₱173,000。",
            "provider": "fake", "model": "fake", "fallback": False,
        }
        # BOT-V1-USABLE-001 P0-5: configurable grounded NL intent parse.
        self.nl_parse_payload: Optional[dict] = None
        self.nl_parse_status: Optional[int] = None
        self.auth_info = {"id": 7, "username": "ana", "role": "admin", "is_active": True}
        self._copilot_proposal_seq = 100
        self._copilot_execute_task_id = 77

    def add_income(self, status="pending", lease_id=1, amount="55000.00",
                   received_date="2026-08-10", payment_method="Bank",
                   description="rent 2026-08", income_id=None,
                   idempotency_key=None):
        inc = {
            "id": income_id or self._next_income_id,
            "lease_id": lease_id,
            "amount": f"{Decimal(str(amount)):.2f}",
            "received_date": received_date,
            "payment_method": payment_method,
            "idempotency_key": idempotency_key,
            "status": status,
            "description": description,
            "confirmed_by": None,
            "confirmed_at": None,
        }
        self._next_income_id = max(self._next_income_id, inc["id"]) + 1
        self.incomes.append(inc)
        return inc

    def add_expense(self, status="pending", expense_id=None, category="维修",
                    amount="5000.00", payee="Fix-It Co", unit_id=1,
                    expense_date="2026-08-01", due_date=None,
                    description=None, receipt_attachment_id=None):
        exp = {
            "id": expense_id or self._next_expense_id,
            "expense_date": expense_date,
            "due_date": due_date,
            "category": category,
            "amount": amount,
            "payee": payee,
            "description": description,
            "unit_id": unit_id,
            "status": status,
            "receipt_attachment_id": receipt_attachment_id,
            "approved_by": None,
            "approved_at": None,
        }
        self._next_expense_id = max(self._next_expense_id, exp["id"]) + 1
        self.expenses.append(exp)
        return exp

    def _get_expense(self, expense_id):
        return next((e for e in self.expenses if e["id"] == expense_id), None)

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
        self.telegram_user_calls.append(request.headers.get("x-telegram-user-id"))

        if path in self.raise_on_paths:
            raise RuntimeError(f"forced unexpected error on {method} {path}")
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)
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

        if path == "/reports/tasks":
            return httpx.Response(200, json=self.tasks)
        if path == "/incomes" and method == "POST":
            payload = body or {}
            inc = self.add_income(
                status=payload.get("status", "pending"),
                lease_id=payload.get("lease_id"),
                amount=str(payload.get("amount", "0")),
                received_date=payload.get("received_date", "2026-08-10"),
                payment_method=payload.get("payment_method"),
                description=payload.get("description"),
                idempotency_key=payload.get("idempotency_key"),
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

        # --- V1.3 Slice 2: Entry B rent-payment matcher ---
        if path == "/payments/match" and method == "POST":
            if self.rent_match_response is not None:
                return httpx.Response(200, json=self.rent_match_response)
            return httpx.Response(200, json=self._rent_match(body or {}))

        # --- V1.3 expense approval ---
        if path == "/expenses" and method == "GET":
            return httpx.Response(200, json=self.expenses)
        if path == "/expenses" and method == "POST":
            payload = body or {}
            exp = self.add_expense(
                status=payload.get("status", "pending"),
                category=payload.get("category", "其他"),
                amount=str(payload.get("amount", "0")),
                payee=payload.get("payee", "-"),
                unit_id=payload.get("unit_id"),
                expense_date=payload.get("expense_date", TODAY),
                due_date=payload.get("due_date"),
                description=payload.get("description"),
            )
            return httpx.Response(201, json=exp)
        if path.startswith("/expenses/") and path.endswith("/approve") and method == "POST":
            expense = self._get_expense(int(path.split("/")[2]))
            if expense is None:
                return httpx.Response(404, json={"detail": "Expense not found"})
            if expense["status"] != "pending":
                return httpx.Response(409, json={"detail": "Only pending expenses can be approved"})
            expense["status"] = "approved"
            expense["approved_at"] = "2026-08-12T12:00:00Z"
            return httpx.Response(200, json=expense)
        if path.startswith("/expenses/") and path.endswith("/reject") and method == "POST":
            expense = self._get_expense(int(path.split("/")[2]))
            if expense is None:
                return httpx.Response(404, json={"detail": "Expense not found"})
            if expense["status"] != "pending":
                return httpx.Response(409, json={"detail": "Only pending expenses can be rejected"})
            expense["status"] = "rejected"
            return httpx.Response(200, json=expense)
        if path.startswith("/expenses/") and method == "GET":
            expense = self._get_expense(int(path.split("/")[2]))
            if expense is None:
                return httpx.Response(404, json={"detail": "Expense not found"})
            return httpx.Response(200, json=expense)

        # --- V1.2.2 C2 confirmed-action copilot ---
        if path == "/operations/copilot/today" and method == "POST":
            return httpx.Response(200, json=self.copilot_today_payload)
        if path == "/operations/copilot/why" and method == "POST":
            item_ref = (body or {}).get("item_ref") or ""
            return httpx.Response(
                200,
                json={
                    "item_ref": item_ref,
                    "explanation": "这是本周最需要关注的待办事项。",
                    "recommendation": "今天就联系相关方确认处理。",
                    "provider": "fake",
                    "model": "fake",
                    "fallback": False,
                },
            )
        if path == "/operations/copilot/ask" and method == "POST":
            return httpx.Response(200, json=self.copilot_ask_payload)
        if path == "/operations/copilot/nl-parse" and method == "POST":
            if self.nl_parse_status:
                return httpx.Response(
                    self.nl_parse_status,
                    json={"detail": f"forced {self.nl_parse_status}"},
                )
            if self.nl_parse_payload is not None:
                return httpx.Response(200, json=self.nl_parse_payload)
            return httpx.Response(
                200,
                json={
                    "intent": "ambiguous",
                    "message": "",
                    "unit": "",
                    "unit_id": None,
                    "amount": None,
                    "category": "",
                    "month": "",
                    "missing": [],
                    "options": [],
                    "provider": "fake",
                    "model": "fake",
                    "fallback": True,
                },
            )
        if path == "/operations/copilot/recommend" and method == "POST":
            if self.copilot_recommend_error:
                status, detail = self.copilot_recommend_error
                return httpx.Response(status, json={"detail": detail})
            rec = self._copilot_recommend(body)
            if self.copilot_recommend_response is not None:
                rec = self.copilot_recommend_response
            return httpx.Response(201, json=rec)
        if method == "POST" and path.startswith("/operations/copilot/proposals/") and path.endswith("/confirm"):
            if self.copilot_confirm_error:
                status, detail = self.copilot_confirm_error
                return httpx.Response(status, json={"detail": detail})
            return httpx.Response(
                200,
                json={"proposal": {"id": int(path.split("/")[4])}, "detail": "Proposal confirmed"},
            )
        if method == "POST" and path.startswith("/operations/copilot/proposals/") and path.endswith("/execute"):
            if self.copilot_execute_error:
                status, detail = self.copilot_execute_error
                return httpx.Response(status, json={"detail": detail})
            ex = self.copilot_execute_response
            if ex is None:
                ex = self._copilot_execute(path)
            return httpx.Response(200, json=ex)
        if method == "POST" and path.startswith("/operations/copilot/proposals/") and path.endswith("/cancel"):
            if self.copilot_cancel_error:
                status, detail = self.copilot_cancel_error
                return httpx.Response(status, json={"detail": detail})
            return httpx.Response(
                200,
                json={"proposal": {"id": int(path.split("/")[4])}, "detail": "Proposal cancelled"},
            )
        if path == "/auth" and method == "POST":
            return httpx.Response(200, json=self.auth_info)

        # --- V1.2 operations center ---
        if path == "/operations/summary" and method == "GET":
            return httpx.Response(200, json=self._ops_summary())
        if path == "/operations/tasks" and method == "GET":
            status = request.url.params.get("status")
            rows = self.operational_tasks
            if status:
                rows = [r for r in rows if r.get("status") == status]
            return httpx.Response(200, json=rows)
        if path.startswith("/operations/tasks/") and method == "GET":
            task = self._ops_task(path)
            if task is None:
                return httpx.Response(404, json={"detail": "Operational task not found"})
            return httpx.Response(200, json=task)
        if path.startswith("/operations/tasks/") and path.endswith("/complete") and method == "POST":
            task = self._ops_task(path)
            if task is None:
                return httpx.Response(404, json={"detail": "Operational task not found"})
            if task["id"] in self.ops_forbidden_task_ids:
                return httpx.Response(403, json={"detail": "Cannot access a task assigned to another user"})
            if task.get("status") != "PENDING":
                return httpx.Response(409, json={"detail": "Cannot complete a cancelled task"})
            task["status"] = "COMPLETED"
            task["completed_at"] = "2026-08-10T12:00:00Z"
            return httpx.Response(200, json={"task": task, "detail": "Task completed"})
        if path.startswith("/operations/tasks/") and path.endswith("/snooze") and method == "POST":
            task = self._ops_task(path)
            if task is None:
                return httpx.Response(404, json={"detail": "Operational task not found"})
            if task["id"] in self.ops_forbidden_task_ids:
                return httpx.Response(403, json={"detail": "Cannot access a task assigned to another user"})
            until = (body or {}).get("until")
            preset = (body or {}).get("preset")
            if until:
                task["snoozed_until"] = until
            elif preset == "1h":
                task["snoozed_until"] = "2026-08-10T13:00:00Z"
            elif preset == "today_afternoon":
                task["snoozed_until"] = "2026-08-10T17:00:00Z"
            elif preset == "tomorrow_morning":
                task["snoozed_until"] = "2026-08-11T09:00:00Z"
            elif preset == "3d":
                task["snoozed_until"] = "2026-08-13T12:00:00Z"
            else:
                return httpx.Response(422, json={"detail": "preset must be one of ..."})
            return httpx.Response(200, json={"task": task, "detail": "Task snoozed"})

        return httpx.Response(404, json={"detail": f"no route {method} {path}"})

    # --- V1.3 Slice 2: Entry B matcher (deterministic fake over fake data) ---
    def _rent_match(self, body):
        text = str(body.get("text") or "")
        lower = text.lower()
        unit = None
        for u in self.units:
            un = (u.get("unit_number") or "").lower()
            if any(
                un == tok or un.endswith(tok) or tok.endswith(un)
                for tok in (
                    t.rstrip(".,;:!?")
                    for t in re.findall(r"[a-z0-9._-]+", lower)
                )
            ):
                unit = u
                break
        amount = None
        for m in re.findall(r"\d[\d,]*", text):
            val = int(m.replace(",", ""))
            if val >= 10000 and amount is None:
                amount = str(val)
        invalid = bool(
            re.search(r"(?<!\d)[-−]\s*\d[\d,]*", text)
            or re.search(r"(?<!\d)0(?:\.0+)?(?!\d)", text)
        )
        lease = None
        if unit:
            lease = next(
                (l for l in self.leases if l["unit_id"] == unit["id"] and l["status"] == "active"),
                None,
            )
        if lease is None:
            if invalid:
                return {
                    "received_date": TODAY,
                    "candidates": [self._match_candidate(
                        {"unit_number": ""}, {"id": 0, "monthly_rent": "0.00"}, "",
                        "invalid_amount", "high", 0, amount="0.00",
                    )],
                }
            return {"received_date": TODAY, "candidates": []}
        if invalid:
            return {
                "received_date": TODAY,
                "candidates": [self._match_candidate(
                    unit, lease, "", "invalid_amount", "high", 0, amount="0.00",
                )],
            }
        start = date.fromisoformat(lease["start_date"])
        today_m = TODAY[:7]
        periods = []
        y, m = start.year, start.month
        while f"{y:04d}-{m:02d}" <= today_m:
            periods.append(f"{y:04d}-{m:02d}")
            m += 1
            if m > 12:
                m, y = 1, y + 1
        paid = {}
        pending = set()
        rent = Decimal(str(lease["monthly_rent"]))
        for inc in self.incomes:
            if inc["lease_id"] != lease["id"]:
                continue
            desc = inc.get("description") or ""
            month = desc.split()[-1] if "rent " in desc else (inc.get("received_date") or "")[:7]
            if month not in periods:
                continue
            if inc["status"] == "confirmed":
                paid[month] = paid.get(month, Decimal("0")) + Decimal(str(inc["amount"]))
            elif inc["status"] == "pending":
                pending.add(month)
        open_p = [p for p in periods if paid.get(p, Decimal("0")) < rent and p not in pending]
        if amount:
            want = Decimal(amount)
            # Same statement while a pending row exists -> confirm that row.
            for inc in reversed(self.incomes):
                if inc["lease_id"] != lease["id"] or inc["status"] != "pending":
                    continue
                desc = inc.get("description") or ""
                month = desc.split()[-1] if "rent " in desc else (inc.get("received_date") or "")[:7]
                if Decimal(str(inc["amount"])) == want:
                    return {
                        "received_date": TODAY,
                        "candidates": [
                            self._match_candidate(
                                unit, lease, month, "pending", "high", 0,
                                income_id=inc["id"], income_status="pending",
                            )
                        ],
                    }
            if not open_p:
                # Fully settled month: same confirmed amount = already booked.
                for inc in reversed(self.incomes):
                    if inc["lease_id"] != lease["id"] or inc["status"] != "confirmed":
                        continue
                    desc = inc.get("description") or ""
                    month = desc.split()[-1] if "rent " in desc else (inc.get("received_date") or "")[:7]
                    if Decimal(str(inc["amount"])) == want:
                        return {
                            "received_date": TODAY,
                            "candidates": [
                                self._match_candidate(
                                    unit, lease, month, "duplicate", "high", 0,
                                    income_id=inc["id"], income_status="confirmed",
                                )
                            ],
                        }
                return {"received_date": TODAY, "candidates": []}
            # Overpayment guard: never suggest booking past the receivable.
            if want > rent - paid.get(open_p[0], Decimal("0")):
                return {
                    "received_date": TODAY,
                    "candidates": [
                        self._match_candidate(
                            unit, lease, open_p[0], "overpayment", "high", 0,
                            amount=amount,
                        )
                    ],
                }
            cand = self._match_candidate(
                unit, lease, open_p[0], "open",
                "high" if len(open_p) == 1 else "low",
                len(open_p),
                amount=amount,
            )
            return {"received_date": TODAY, "candidates": [cand]}
        if open_p:
            cand = self._match_candidate(
                unit, lease, open_p[0], "open",
                "high" if len(open_p) == 1 else "low",
                len(open_p),
            )
            return {"received_date": TODAY, "candidates": [cand]}
        for inc in reversed(self.incomes):
            if inc["lease_id"] != lease["id"]:
                continue
            desc = inc.get("description") or ""
            month = desc.split()[-1] if "rent " in desc else (inc.get("received_date") or "")[:7]
            if inc["status"] == "confirmed":
                return {
                    "received_date": TODAY,
                    "candidates": [
                        self._match_candidate(
                            unit, lease, month, "duplicate", "high", 0,
                            income_id=inc["id"], income_status="confirmed",
                        )
                    ],
                }
            if inc["status"] == "pending":
                return {
                    "received_date": TODAY,
                    "candidates": [
                        self._match_candidate(
                            unit, lease, month, "pending", "high", 0,
                            income_id=inc["id"], income_status="pending",
                        )
                    ],
                }
        return {"received_date": TODAY, "candidates": []}

    def _match_candidate(self, unit, lease, period, kind, confidence, open_count,
                         income_id=None, income_status=None, amount=None):
        prop = next(
            (p for p in self.properties if p["id"] == unit.get("property_id")),
            {"name": ""},
        ) if unit else {"name": ""}
        tenant = next(
            (t for t in self.tenants if t["id"] == lease.get("tenant_id")),
            {"full_name": ""},
        ) if lease else {"full_name": ""}
        rent = Decimal(str(lease["monthly_rent"]))
        if amount is not None:
            rent = Decimal(amount)
        paid = Decimal("0")
        if period and lease.get("id"):
            for inc in self.incomes:
                if inc["lease_id"] != lease["id"] or inc["status"] != "confirmed":
                    continue
                desc = inc.get("description") or ""
                month = desc.split()[-1] if "rent " in desc else (inc.get("received_date") or "")[:7]
                if month == period:
                    paid += Decimal(str(inc["amount"]))
        due = Decimal(str(lease["monthly_rent"]))
        remaining = max(due - paid, Decimal("0"))
        if kind == "overpayment":
            remaining_balance = str(remaining)
        elif kind == "open":
            amt = Decimal(amount) if amount is not None else remaining
            remaining_balance = str(
                max(remaining - amt, Decimal("0")) + max(open_count - 1, 0) * due
            )
        elif kind == "pending":
            remaining_balance = str(max(remaining - Decimal(amount), Decimal("0"))) if amount else "0.00"
        else:
            remaining_balance = str(remaining)
        return {
            "kind": kind,
            "confidence": confidence,
            "lease_id": lease.get("id") if lease else 0,
            "unit_id": unit.get("id") if unit else 0,
            "unit_number": unit.get("unit_number", "") if unit else "",
            "property_id": unit.get("property_id") if unit else 0,
            "property_name": prop["name"],
            "tenant_id": lease.get("tenant_id") if lease else 0,
            "tenant_name": tenant["full_name"],
            "period": period,
            "due_date": None,
            "amount": f"{rent:.2f}",
            "open_count": open_count,
            "due_amount": f"{due:.2f}",
            "paid_amount": f"{paid:.2f}",
            "remaining_balance": f"{Decimal(remaining_balance):.2f}",
            "income_id": income_id,
            "income_status": income_status,
        }

    # --- V1.2.2 C2 copilot helpers ---
    def _copilot_recommend(self, body):
        """Deterministic CopilotRecommendOut built from POST /recommend body."""
        self._copilot_proposal_seq += 1
        proposal_id = self._copilot_proposal_seq
        body = body or {}
        intent = str(body.get("intent") or "").lower()
        note = str(body.get("note") or "")
        if "snooze" in intent:
            task_ref = int(body.get("task_ref") or 0)
            preset = body.get("preset")
            due_at = "2026-08-12T01:00:00+00:00"  # tomorrow morning
            if preset == "today_afternoon":
                due_at = "2026-08-11T09:00:00+00:00"
            elif preset == "3d":
                due_at = "2026-08-14T09:00:00+00:00"
            return {
                "proposal_id": proposal_id,
                "action_type": "snooze_task",
                "status": "PENDING",
                "target_type": "task",
                "target_id": task_ref,
                "idempotency_key": f"snooze:{proposal_id}",
                "expires_at": None,
                "card": {
                    "action_type": "snooze_task",
                    "target_type": "task",
                    "target_id": task_ref,
                    "target_label": "#9 空调保养",
                    "reason_code": None,
                    "assignee_user_id": None,
                    "assignee_name": None,
                    "due_at": due_at,
                    "note": note,
                    "display_context": {"task_id": 9, "title": "空调保养"},
                },
                "detail": "Proposal created",
                "created": True,
            }
        if "assign" in intent:
            task_ref = int(body.get("task_ref") or 0)
            return {
                "proposal_id": proposal_id,
                "action_type": "assign_task",
                "status": "PENDING",
                "target_type": "task",
                "target_id": task_ref,
                "idempotency_key": f"assign:{proposal_id}",
                "expires_at": None,
                "card": {
                    "action_type": "assign_task",
                    "target_type": "task",
                    "target_id": task_ref,
                    "target_label": "#9 空调保养",
                    "reason_code": None,
                    "assignee_user_id": 2,
                    "assignee_name": "Maria",
                    "due_at": None,
                    "note": note,
                    "display_context": {"task_id": 9, "title": "空调保养"},
                },
                "detail": "Proposal created",
                "created": True,
            }
        return {
            "proposal_id": proposal_id,
            "action_type": "create_followup_task",
            "status": "PENDING",
            "target_type": str(body.get("source_type") or "lease"),
            "target_id": int(body.get("source_id") or 0),
            "idempotency_key": f"followup:{proposal_id}",
            "expires_at": None,
            "card": {
                "action_type": "create_followup_task",
                "target_type": str(body.get("source_type") or "lease"),
                "target_id": int(body.get("source_id") or 0),
                "target_label": "Lease #3 · 1608 · Juan Dela Cruz",
                "reason_code": "FOLLOWUP",
                "assignee_user_id": 2,
                "assignee_name": "Maria",
                "due_at": "2026-08-12T01:00:00+00:00",
                "note": note,
                "display_context": {"unit": "1608", "tenant": "Juan Dela Cruz", "lease_id": 3},
            },
            "detail": "Proposal created",
            "created": True,
        }

    def _copilot_execute(self, path):
        proposal_id = int(path.split("/")[4])
        return {
            "proposal": {"id": proposal_id},
            "result": {
                "action_type": "create_followup_task",
                "target_type": "lease",
                "target_id": 3,
                "task_id": self._copilot_execute_task_id,
                "assignee_user_id": 2,
                "due_at": "2026-08-12T01:00:00+00:00",
                "executed_at": "2026-08-11T04:00:00+00:00",
                "status": "EXECUTED",
                "replay": False,
                "detail": "Proposal executed",
            },
        }

    # --- V1.2 ops helpers ---
    def add_ops_task(self, task_id=1, title="季度空调保养", task_type="AC_MAINTENANCE",
                     status="PENDING", due_at=None, snoozed_until=None, property_id=1,
                     details=None, assigned_user_id=None):
        row = {
            "id": task_id,
            "task_type": task_type,
            "title": title,
            "description": None,
            "property_id": property_id,
            "tenant_id": None,
            "lease_id": None,
            "source_type": "recurring_rule",
            "source_id": 1,
            "assigned_user_id": assigned_user_id,
            "priority": "medium",
            "status": status,
            "due_at": due_at or "2026-08-10T00:00:00+08:00",
            "remind_at": None,
            "snoozed_until": snoozed_until,
            "completed_at": None,
            "completed_by": None,
            "dedupe_key": f"recurring:1:2026-Q3",
            "details": details or {"amount": "12000.00", "period": "2026-Q3"},
        }
        self.operational_tasks.append(row)
        return row

    def _ops_task(self, path):
        parts = path.split("/")
        try:
            task_id = int(parts[3])
        except (IndexError, ValueError):
            return None
        return next((t for t in self.operational_tasks if t["id"] == task_id), None)

    def _ops_summary(self):
        pending = [t for t in self.operational_tasks if t.get("status") == "PENDING"]
        overdue = today = next7 = 0
        for t in pending:
            due = (t.get("due_at") or "")[:10]
            if due and due < TODAY:
                overdue += 1
            elif due == TODAY:
                today += 1
            if due and today_str() <= due <= _add_days(TODAY, 7):
                next7 += 1
        return {"overdue": overdue, "due_today": today, "due_7_days": next7,
                "pending_total": len(pending)}


@pytest.fixture()
def make_app(tmp_path):
    created: list[tuple[Any, Any]] = []

    def _make(backend=None, api_key="manager-key", admin_api_key="admin-key",
              callback_ttl=900, state_db=None, bot=None):
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
        bot = bot or FakeBot()
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
