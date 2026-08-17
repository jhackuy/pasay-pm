"""Shared fixtures: FakeBot, FakeBackend (httpx MockTransport), make_app."""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
import uuid
from pathlib import Path
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


def _split_query(qs: str):
    """Split a query string into a list of (key, value) pairs (FakeBackend)."""
    from urllib.parse import parse_qsl

    return parse_qsl(qs)


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


class _WritableTempPathFactory:
    """Test tmp-dir factory that never creates mode-0o700 directories.

    The DSH file sandbox on this Windows node maps POSIX mode bits into
    Windows DACLs: a directory created with mode 0o700 becomes unreadable by
    every later process (pytest's own make_numbered_dir uses 0o700). We create
    per-test dirs explicitly with a wide mode so the whole suite is runnable
    under the sandbox. Root is overridable via ``PASAY_TEST_TMP``.
    """

    def __init__(self, root: Path):
        root.mkdir(parents=True, exist_ok=True, mode=0o777)
        self._root = root
        # pytest's tmp_path fixture reads these at teardown; "all" keeps every
        # per-test dir (no sandbox-blocked recursive deletion).
        self._retention_policy = "all"
        self._retention_count = 0

    def getbasetemp(self):
        return self._root

    def mktemp(self, basename, numbered=True):
        path = self._root / f"{basename}-{uuid.uuid4().hex[:8]}"
        path.mkdir(mode=0o777)
        return path


@pytest.fixture(scope="session")
def tmp_path_factory():
    root = Path(
        os.environ.get(
            "PASAY_TEST_TMP",
            "D:/AI-Review/pasay-pm/.runtime/pytest-tmp-aiops",
        )
    )
    return _WritableTempPathFactory(root)


class FakeBot:
    def __init__(self):
        self.calls: list[dict] = []
        self.username = "pasay_test_bot"
        self.id = 999
        self._answered_ids: set[str] = set()
        self.command_menu: dict = {}
        # Real Telegram semantics: only messages sent WITHOUT a reply keyboard
        # (or with an inline keyboard) are editable. Track what the bot sent so
        # edit_message_text can reject the reply-keyboard case exactly like the
        # live API does ("Message can't be edited").
        self._sent_by_id: dict[int, dict] = {}

    def clear(self):
        """Reset recorded calls (used to inspect a fresh window of output)."""
        self.calls = []
        self._answered_ids = set()
        self._sent_by_id = {}

    async def initialize(self):
        pass

    async def shutdown(self):
        pass
    async def get_me(self):
        return SimpleNamespace(username=self.username, id=self.id)

    async def set_my_commands(self, commands, scope=None, language_code=None, **kw):
        """Record a setMyCommands publication. ``commands`` is a list of
        (command, description) tuples or BotCommand objects; ``scope`` is a
        BotCommandScope (None == default scope)."""
        self.command_menu = self.command_menu or {}
        names = [
            getattr(c, "command", None) or (c[0] if isinstance(c, (tuple, list)) else None)
            for c in (commands or [])
        ]
        scope_label = getattr(scope, "type", None) if scope is not None else "default"
        self.command_menu[scope_label] = [n for n in names if n]
        self.calls.append(
            {
                "type": "set_my_commands",
                "commands": names,
                "scope": scope_label,
                "language_code": language_code,
            }
        )

    def published_commands(self):
        """Commands published to each Telegram command scope (None == default).
        Empty list means that scope was cleared."""
        return getattr(self, "command_menu", {}) or {}

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

    # AI-OPS-FOUNDATION-001 §12/§14: archive forwarding + media resend.
    # ``forward_error`` (when set) makes forward_message raise, simulating the
    # archive channel being unavailable / the bot lacking permission.
    forward_error = None

    async def forward_message(self, chat_id, from_chat_id, message_id, **kw):
        if self.forward_error is not None:
            raise self.forward_error
        self.calls.append(
            {
                "type": "forward_message",
                "chat_id": chat_id,
                "from_chat_id": from_chat_id,
                "message_id": message_id,
            }
        )
        return SimpleNamespace(
            message_id=9000 + len(self.calls), chat_id=chat_id,
            photo=[SimpleNamespace(file_id="fwd_photo_id", file_size=1234)],
        )

    async def send_photo(self, chat_id, photo=None, caption=None, **kw):
        self.calls.append(
            {"type": "send_photo", "chat_id": chat_id, "photo": photo, "caption": caption}
        )
        return SimpleNamespace(chat_id=chat_id, message_id=len(self.calls))

    async def send_document(self, chat_id, document=None, caption=None, **kw):
        self.calls.append(
            {"type": "send_document", "chat_id": chat_id, "document": document, "caption": caption}
        )
        return SimpleNamespace(chat_id=chat_id, message_id=len(self.calls))

    async def send_video(self, chat_id, video=None, caption=None, **kw):
        self.calls.append(
            {"type": "send_video", "chat_id": chat_id, "video": video, "caption": caption}
        )
        return SimpleNamespace(chat_id=chat_id, message_id=len(self.calls))

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
        # --- PASAY-V2-FOUNDATION-001: quick views / digest / task create+patch ---
        self.quick_tasks: list[dict] = []
        self.quick_properties: list[dict] = []
        self.quick_rent: dict = {"overdue": [], "outstanding_total": "0.00"}
        self.quick_expense: dict = {
            "month_total": "0.00",
            "pending_approval_count": 0,
            "pending_approval_amount": "0.00",
            "unresolved_expense_tasks": [],
            "records": [],
        }
        # PASAY-V2-EXPENSE-PAYABLE-TASK-006: advisory possible-duplicate matcher.
        self.expense_duplicates: Optional[list] = None
        self.digest: dict = {"pending": [], "in_progress": [], "recently_completed": []}
        self._next_v2_task_id = 1000
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
        # AI-OPS-FOUNDATION-001 §12/§15: evidence index + unit timeline.
        self.evidence: list[dict] = []
        self._next_evidence_id = 1
        # PASAY-AI-EMPLOYEE-FOUNDATION-007: promised payment recording capture.
        self.payment_promises: list[dict] = []
        self.last_resume: Optional[dict] = None
        self.unit_timeline: dict = {"unit": None, "events": []}
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
        # REPAIR-AI-EMPLOYEE-WORKFLOW-008A: repair operations + proposals + actions.
        self.repairs: list[dict] = []
        self.repair_proposals: list[dict] = []
        self.repair_actions: list[dict] = []
        self._next_repair_id = 1000
        self._next_repair_proposal_id = 2000
        self._next_repair_action_id = 3000

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
        if status == "confirmed":
            self._recompute_overdue()
        return inc

    @staticmethod
    def _income_period(inc: dict) -> str | None:
        """YYYY-MM rent period an income maps to (description first; received-
        date month fallback when the description has no period). Mirrors the
        real backend so the fake overdue report stays consistent."""
        import re as _re
        desc = inc.get("description") or ""
        match = _re.search(r"(?<!\d)(\d{4})(?:[-/.])?(\d{1,2})(?!\d)", desc)
        if match is not None:
            year, month = int(match.group(1)), int(match.group(2))
            if 1 <= month <= 12:
                return f"{year:04d}-{month:02d}"
        rd = inc.get("received_date")
        if rd:
            return str(rd)[:7]
        return None

    @staticmethod
    def _clean_label(value) -> str | None:
        """Drop placeholder/empty sentinels (mirrors the backend read-model
        cleaner): `??`, None, null, empty string and bare dash."""
        if not value:
            return None
        text = " ".join(str(value).split())
        if not text or text.lower() in {"none", "null", "??", "-"}:
            return None
        return text

    def _expense_purpose(self, exp: dict) -> str:
        """Backend-mirror purpose chain: category -> description -> payee."""
        for field in ("category", "description", "payee"):
            text = self._clean_label(exp.get(field))
            if text:
                return text
        return ""

    def _recompute_overdue(self) -> None:
        """Keep the fake overdue report consistent with confirmed income (the
        real backend recomputes it dynamically): a confirmed payment covering
        a due period removes that period from the report."""
        covered_by_lease: dict[int, set[str]] = {}
        for inc in self.incomes:
            if inc.get("status") != "confirmed" or not inc.get("lease_id"):
                continue
            period = self._income_period(inc)
            if period:
                covered_by_lease.setdefault(int(inc["lease_id"]), set()).add(period)
        kept = []
        for row in self.overdue_rows:
            lease_id = int(row.get("lease_id") or 0)
            covered = covered_by_lease.get(lease_id, set())
            row_periods = row.get("overdue_periods") or []
            if not row_periods:
                # opaque row (no period detail) -> keep as configured
                kept.append(row)
                continue
            periods = [
                p for p in row_periods
                if p.get("month") not in covered
            ]
            if not periods:
                continue
            per_month = Decimal(str(row.get("amount_per_month") or 0))
            row["overdue_periods"] = periods
            row["overdue_months"] = len(periods)
            row["total_outstanding"] = f"{per_month * len(periods):.2f}"
            kept.append(row)
        self.overdue_rows = kept

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

    # --- REPAIR-AI-EMPLOYEE-WORKFLOW-008A helpers ---
    def _latest_proposal(self, repair_id):
        props = [p for p in self.repair_proposals if p["repair_id"] == repair_id]
        if not props:
            return None
        return max(props, key=lambda p: p["version"])

    def _repair_detail(self, repair):
        rid = repair["id"]
        props = [p for p in self.repair_proposals if p["repair_id"] == rid]
        actions = [a for a in self.repair_actions if a["repair_id"] == rid]
        return {
            **repair,
            "proposals": sorted(props, key=lambda p: p["version"]),
            "actions": actions,
            "expense_ids": [p["expense_id"] for p in props if p["expense_id"]],
        }

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
        # AI-OPS-FOUNDATION-001 §14: Telegram-first Unit create (POST first;
        # the GET below only answers read requests).
        if path == "/units" and method == "POST":
            payload = body or {}
            unit = {
                "id": (max([u["id"] for u in self.units], default=0) + 1),
                "property_id": payload.get("property_id", 1),
                "unit_number": payload.get("unit_number", ""),
                "floor": payload.get("floor"),
                "size_sqm": payload.get("size_sqm"),
                "monthly_rent": str(payload.get("monthly_rent", "0")),
                "status": payload.get("status", "vacant"),
                "is_active": True,
            }
            self.units.append(unit)
            return httpx.Response(201, json=unit)
        if path == "/units" and method == "GET":
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
        if path.startswith("/tenants/") and method == "PATCH":
            # PASAY-AI-EMPLOYEE-FOUNDATION-007: low-risk tenant write (safe shape).
            tid = int(path.split("/")[2])
            tenant = next((t for t in self.tenants if t["id"] == tid), None)
            if tenant is None:
                return httpx.Response(404, json={"detail": "Tenant not found"})
            tenant = {**tenant, **{k: v for k, v in (body or {}).items() if v is not None}}
            tenant["id_registered"] = bool(tenant.get("id_number"))
            for i, t in enumerate(self.tenants):
                if t["id"] == tid:
                    self.tenants[i] = tenant
            return httpx.Response(200, json=tenant)
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
        if path.startswith("/expenses/") and path.endswith("/pay") and method == "POST":
            expense = self._get_expense(int(path.split("/")[2]))
            if expense is None:
                return httpx.Response(404, json={"detail": "Expense not found"})
            if expense["status"] != "approved":
                return httpx.Response(409, json={"detail": "Only approved expenses can be paid"})
            expense["status"] = "paid"
            # P0-EXPENSE-PAID-CLOSEOUT-001: paying closes the linked
            # APPROVAL_PENDING / PAYMENT_PENDING operational task (mirrors the
            # backend's atomic closure).
            expense_id = int(path.split("/")[2])
            for task in self.operational_tasks:
                if (
                    task.get("source_type") == "expense"
                    and task.get("source_id") == expense_id
                    and task.get("task_type") in ("APPROVAL_PENDING", "PAYMENT_PENDING")
                    and task.get("status") in ("PENDING", "IN_PROGRESS")
                ):
                    task["status"] = "COMPLETED"
                    task["completed_at"] = "2026-08-15T10:00:00Z"
            return httpx.Response(200, json=expense)
        if path.startswith("/expenses/") and method == "GET":
            expense = self._get_expense(int(path.split("/")[2]))
            if expense is None:
                return httpx.Response(404, json={"detail": "Expense not found"})
            return httpx.Response(200, json=expense)

        # --- AI-OPS-FOUNDATION-001 §17: viewings ---
        if path == "/viewings" and method == "POST":
            payload = body or {}
            self.viewings = getattr(self, "viewings", [])
            row = {
                "id": len(self.viewings) + 1,
                "unit_id": payload.get("unit_id"),
                "property_id": 1,
                "scheduled_at": payload.get("scheduled_at"),
                "status": "scheduled",
                "outcome": None,
                "reason": None,
                "notes": payload.get("notes"),
                "created_by": 1,
            }
            self.viewings.append(row)
            return httpx.Response(201, json=row)

        # --- AI-OPS-FOUNDATION-001 §12/§15: evidence + unit timeline ---
        if path == "/evidence" and method == "POST":
            payload = body or {}
            row = {
                "id": self._next_evidence_id,
                "storage_provider": payload.get("storage_provider", "telegram_channel"),
                "external_file_id": payload.get("external_file_id", ""),
                "external_message_id": payload.get("external_message_id"),
                "media_type": payload.get("media_type"),
                "mime_type": payload.get("mime_type"),
                "filename": payload.get("filename"),
                "size_bytes": payload.get("size_bytes"),
                "checksum": payload.get("checksum"),
                "category": payload.get("category"),
                "property_id": payload.get("property_id"),
                "unit_id": payload.get("unit_id"),
                "entity_type": payload.get("entity_type"),
                "entity_id": payload.get("entity_id"),
                "uploaded_by": 1,
                "created_at": "2026-08-16T08:00:00Z",
            }
            self._next_evidence_id += 1
            self.evidence.append(row)
            # Repair-task evidence closes its secretary evidence follow-up.
            if row.get("entity_type") == "task" and row.get("entity_id") is not None:
                for task in self.operational_tasks:
                    if (
                        task.get("task_type") == "FOLLOWUP"
                        and task.get("dedupe_key") == f"repair-evidence:{row['entity_id']}"
                        and task.get("status") in ("PENDING", "IN_PROGRESS")
                    ):
                        task["status"] = "COMPLETED"
            return httpx.Response(201, json=row)
        if path == "/evidence" and method == "GET":
            rows = self.evidence
            params = request.url.params
            if "unit_id" in params:
                rows = [r for r in rows if r.get("unit_id") == int(params["unit_id"])]
            if "entity_type" in params:
                rows = [r for r in rows if r.get("entity_type") == params["entity_type"]]
            if "entity_id" in params:
                rows = [r for r in rows if r.get("entity_id") == int(params["entity_id"])]
            return httpx.Response(200, json=rows)
        if path == "/operations/quick/unit-timeline" and method == "GET":
            return httpx.Response(200, json=self.unit_timeline)

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
            scope = request.url.params.get("scope")
            rows = self.operational_tasks
            if status:
                rows = [r for r in rows if r.get("status") == status]
            if scope == "owner":
                # AI-OPS-FOUNDATION-001 §5 mirror: the Owner queue keeps only
                # approvals / Owner payments / decisions / escalations.
                rows = [
                    r for r in rows
                    if r.get("task_type") in ("APPROVAL_PENDING", "PAYMENT_PENDING")
                    or (r.get("details") or {}).get("escalation", {}).get("level") == "owner"
                ]
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

        # --- PASAY-V2-FOUNDATION-001: quick views / digest / task write ---
        if path == "/operations/quick/tasks" and method == "GET":
            rows = list(self.quick_tasks)
            # PASAY-V2-EXPENSE-PAYABLE-TASK-006: approved (unpaid) expenses are
            # Owner-actionable payable task rows in the ✅ Tasks Quick View.
            # P1-...-008 A3: the purpose mirrors the backend fallback chain
            # (category -> description -> payee) so an incomplete record like
            # E7/E8 (`??` category) still resolves to its truthful payee.
            for exp in self.expenses:
                if (exp.get("status") or "").lower() != "approved":
                    continue
                label = next(
                    (u.get("unit_number", "") for u in self.units if u["id"] == exp.get("unit_id")),
                    "",
                )
                waiting_days = 0
                approved_at = exp.get("approved_at")
                if approved_at:
                    try:
                        from datetime import date as _d, datetime as _dt
                        waiting_days = max((_d.today() - _dt.fromisoformat(str(approved_at)[:10]).date()).days, 0)
                    except (ValueError, TypeError):
                        waiting_days = 0
                rows.append(
                    {
                        "kind": "payable_expense",
                        "expense_id": exp["id"],
                        "unit": label,
                        "purpose": self._expense_purpose(exp),
                        "payee": exp.get("payee"),
                        "amount": exp.get("amount"),
                        "status": "approved",
                        "expense_date": exp.get("expense_date"),
                        "waiting_days": waiting_days,
                        "has_receipt": exp.get("receipt_attachment_id") is not None,
                    }
                )
            return httpx.Response(200, json=rows)
        if path == "/operations/quick/properties" and method == "GET":
            return httpx.Response(200, json=self.quick_properties)
        if path == "/operations/quick/rent" and method == "GET":
            return httpx.Response(200, json=self.quick_rent)
        if path == "/operations/quick/expense" and method == "GET":
            return httpx.Response(200, json=self.quick_expense)
        if path == "/operations/quick/expense-duplicates" and method == "GET":
            if self.expense_duplicates is not None:
                return httpx.Response(200, json=self.expense_duplicates)
            return httpx.Response(200, json=[])
        if path == "/operations/digest" and method == "GET":
            return httpx.Response(200, json=self.digest)
        if path == "/operations/remind-owner-target" and method == "GET":
            # ZERO-LEARNING-004 §4: the canonical Owner DM target for a REAL
            # Remind-Owner private message.
            return httpx.Response(200, json={"telegram_chat_id": str(OWNER_ID)})
        if path == "/operations/secretary-target" and method == "GET":
            # TELEGRAM-OPS-REAL-WORLD-CLOSURE-005 §2.2/§9: the canonical
            # Secretary DM target for a REAL 催租 assign-to-Secretary message.
            return httpx.Response(200, json={"telegram_chat_id": str(SECRETARY_ID),
                                             "principal_id": 2})
        if path == "/operations/action-pack" and method == "GET":
            # PASAY-AI-EMPLOYEE-FOUNDATION-007 §13: Rent Action Pack.
            params = dict(_split_query(request.url.query))
            unit_id = int(params.get("unit_id", 0))
            return httpx.Response(200, json=self._action_pack(unit_id))
        if path == "/operations/route" and method == "GET":
            params = dict(_split_query(request.url.query))
            at = params.get("action_type", "")
            return httpx.Response(200, json={"action_type": at,
                                             "route": f"{at}->SECRETARY",
                                             "responsibility": "SECRETARY"})
        if path == "/operations/promise" and method == "POST":
            # PASAY-AI-EMPLOYEE-FOUNDATION-007 §17: record a payment promise.
            self.payment_promises.append((body or {}))
            return httpx.Response(201, json={"task_id": 9, "amount": "30000.00",
                                             "promised_date": "2026-08-20",
                                             "recorded_by": 2, "status": "open"})
        if path == "/operations/resume" and method == "POST":
            # PASAY-AI-EMPLOYEE-FOUNDATION-007 §8: self-healing resume.
            payload = body or {}
            lease_id = payload.get("lease_id") or 1
            # apply the low-risk phone write to the tenant.
            lease = next((l for l in self.leases if l["id"] == lease_id), self.leases[0])
            tid = lease.get("tenant_id")
            for i, t in enumerate(self.tenants):
                if t["id"] == tid:
                    self.tenants[i] = {**t, "phone": payload.get("value")}
            self.last_resume = body or {}
            return httpx.Response(200, json={"resolved": True,
                                             "blocked_action": "assign_to_secretary",
                                             "message": "已记录租客电话"})
        if path == "/operations/tasks" and method == "POST":
            payload = body or {}
            self._next_v2_task_id += 1
            row = {
                "id": self._next_v2_task_id,
                "task_type": payload.get("task_type", "AC_MAINTENANCE"),
                "title": payload.get("title", ""),
                "description": payload.get("description"),
                "property_id": payload.get("property_id"),
                "property_code": None,
                "tenant_id": None,
                "lease_id": None,
                "source_type": "conversation",
                "source_id": None,
                "source_event": payload.get("source_event"),
                "assigned_user_id": payload.get("assigned_user_id"),
                "priority": payload.get("priority", "medium"),
                "status": payload.get("status", "PENDING"),
                "due_at": payload.get("due_at") or "2026-08-21T00:00:00+08:00",
                "remind_at": None,
                "snoozed_until": None,
                "next_action": payload.get("next_action"),
                "next_check_at": payload.get("next_check_at"),
                "context": payload.get("context"),
                "completion_condition": payload.get("completion_condition"),
                "completed_at": None,
                "completed_by": None,
                "dedupe_key": payload.get("dedupe_key"),
                "details": payload.get("details") or {},
            }
            existing = next(
                (t for t in self.operational_tasks if t.get("dedupe_key") == payload.get("dedupe_key")),
                None,
            )
            if existing is not None:
                return httpx.Response(201, json={"task": existing, "detail": "Task already exists"})
            self.operational_tasks.append(row)
            self.quick_tasks.append(row)
            return httpx.Response(201, json={"task": row, "detail": "Task created"})
        if path.startswith("/operations/tasks/") and method == "PATCH":
            task = self._ops_task(path)
            if task is None:
                return httpx.Response(404, json={"detail": "Operational task not found"})
            payload = body or {}
            want_status = payload.get("status")
            if want_status == "IN_PROGRESS" and not (
                payload.get("next_action") or task.get("next_action")
            ) and not (payload.get("next_check_at") or task.get("next_check_at")):
                return httpx.Response(
                    422, json={"detail": "IN_PROGRESS requires next_action and next_check_at"}
                )
            for key in ("title", "status", "due_at", "next_action", "next_check_at",
                        "context", "completion_condition"):
                if key in payload:
                    task[key] = payload[key]
            if payload.get("details") is not None:
                # AI-OPS-FOUNDATION-001 §8: merge structured details (promise)
                # like the backend's PATCH handler.
                merged = dict(task.get("details") or {})
                for dk, dv in payload["details"].items():
                    if isinstance(dv, dict) and isinstance(merged.get(dk), dict):
                        merged[dk] = {**merged[dk], **dv}
                    else:
                        merged[dk] = dv
                task["details"] = merged
            if want_status == "COMPLETED":
                task["completed_at"] = "2026-08-20T10:00:00Z"
                task["completed_by"] = 1
            self.quick_tasks = [
                t for t in self.quick_tasks if t.get("id") != task.get("id")
            ]
            self.quick_tasks.append(
                {
                    "id": task["id"],
                    "task_type": task.get("task_type"),
                    "title": task.get("title"),
                    "status": task.get("status"),
                    "property_code": task.get("property_code"),
                    "due_at": task.get("due_at"),
                    "next_action": task.get("next_action"),
                    "next_check_at": task.get("next_check_at"),
                }
            )
            return httpx.Response(200, json={"task": task, "detail": "Task updated"})

        # --- REPAIR-AI-EMPLOYEE-WORKFLOW-008A: Repair Operation fast path ---
        if path == "/repairs" and method == "GET":
            return httpx.Response(200, json={"items": self.repairs, "total": len(self.repairs)})
        if path == "/repairs" and method == "POST":
            payload = body or {}
            rid = self._next_repair_id
            self._next_repair_id += 1
            row = {
                "id": rid,
                "merchant_id": payload.get("merchant_id"),
                "property_id": payload.get("property_id"),
                "unit_id": payload.get("unit_id"),
                "issue": payload.get("issue", ""),
                "issue_description": payload.get("issue_description"),
                "created_source": payload.get("created_source", "manual"),
                "reported_by": payload.get("reported_by"),
                "assignee_user_id": payload.get("assignee_user_id"),
                "status": "OPEN",
                "next_action": "Awaiting a solution proposal or a work plan.",
                "waiting_on": None,
                "blocked_reason": None,
                "next_check_at": None,
                "closure_criteria": payload.get("closure_criteria"),
                "verified_by": None,
                "verified_at": None,
                "verification_result": None,
                "closed_at": None,
                "closure_reason": None,
                "operational_task_id": None,
                "created_at": "2026-08-17T08:00:00Z",
                "proposals": [],
                "actions": [],
                "expense_ids": [],
            }
            self.repairs.append(row)
            return httpx.Response(201, json=row)
        if path.startswith("/repairs/") and method == "GET":
            rid = int(path.split("/")[2])
            repair = next((r for r in self.repairs if r["id"] == rid), None)
            if repair is None:
                return httpx.Response(404, json={"detail": "Repair not found"})
            return httpx.Response(200, json=self._repair_detail(repair))
        if path.startswith("/repairs/") and path.endswith("/proposals") and method == "POST":
            rid = int(path.split("/")[2])
            repair = next((r for r in self.repairs if r["id"] == rid), None)
            if repair is None:
                return httpx.Response(404, json={"detail": "Repair not found"})
            payload = body or {}
            version = 1 + len([p for p in self.repair_proposals if p["repair_id"] == rid])
            pid = self._next_repair_proposal_id
            self._next_repair_proposal_id += 1
            prop = {
                "id": pid,
                "repair_id": rid,
                "version": version,
                "vendor": payload.get("vendor"),
                "source": payload.get("source"),
                "description": payload.get("description"),
                "amount": str(payload.get("amount", "0")),
                "submitted_by": 1,
                "submitted_at": "2026-08-17T08:00:00Z",
                "status": "PENDING",
                "decision_by": None,
                "decision_at": None,
                "rejection_reason": None,
                "expense_id": None,
            }
            self.repair_proposals.append(prop)
            repair["status"] = "WAITING_APPROVAL"
            repair["next_action"] = f"Proposal V{version} awaits owner decision."
            repair["waiting_on"] = "owner"
            return httpx.Response(201, json=self._repair_detail(repair))
        if path.startswith("/repairs/") and path.endswith("/decide") and method == "POST":
            rid = int(path.split("/")[2])
            repair = next((r for r in self.repairs if r["id"] == rid), None)
            if repair is None:
                return httpx.Response(404, json={"detail": "Repair not found"})
            payload = body or {}
            decision = payload.get("decision", "")
            prop = self._latest_proposal(rid)
            if prop is None:
                return httpx.Response(404, json={"detail": "Repair has no proposals"})
            if decision == "reject":
                prop["status"] = "REJECTED"
                prop["rejection_reason"] = payload.get("reason")
                prop["decision_at"] = "2026-08-17T08:05:00Z"
                repair["status"] = "WAITING_HUMAN"
                repair["next_action"] = "Get another quote — repair stays open."
                repair["waiting_on"] = "secretary"
                # AI requote action (dedup: one PER (repair, rejected version)).
                dedupe = f"repair:{rid}:requote:v{prop['version']}"
                if not any(a.get("dedupe_key") == dedupe and a.get("status") in ("PENDING", "IN_PROGRESS")
                           for a in self.repair_actions):
                    self.repair_actions.append({
                        "id": self._next_repair_action_id,
                        "repair_id": rid,
                        "action_kind": "REQUOTE",
                        "title": f"Get another quote for repair R-{rid}",
                        "description": "Owner rejected the quote.",
                        "status": "PENDING",
                        "assigned_user_id": None,
                        "due_at": None,
                        "next_check_at": None,
                        "dedupe_key": dedupe,
                        "source_event": f"proposal_rejected:v{prop['version']}",
                        "resolved_at": None,
                        "resolved_by": None,
                        "created_at": "2026-08-17T08:05:00Z",
                    })
                    self._next_repair_action_id += 1
            elif decision == "approve":
                prop["status"] = "APPROVED"
                prop["decision_at"] = "2026-08-17T08:05:00Z"
                repair["status"] = "WAITING_PAYMENT"
                repair["next_action"] = "Quote approved; the linked expense awaits payment."
                repair["waiting_on"] = "payer"
            return httpx.Response(200, json=self._repair_detail(repair))
        if path.startswith("/repairs/") and path.endswith("/pay-expense") and method == "POST":
            rid = int(path.split("/")[2])
            repair = next((r for r in self.repairs if r["id"] == rid), None)
            if repair is None:
                return httpx.Response(404, json={"detail": "Repair not found"})
            if repair["status"] == "WAITING_PAYMENT":
                repair["status"] = "VERIFYING"
                repair["next_action"] = "Expense paid. The repair now needs real-world verification before it can close."
                repair["waiting_on"] = "secretary"
            return httpx.Response(200, json=self._repair_detail(repair))
        if path.startswith("/repairs/") and path.endswith("/record-result") and method == "POST":
            rid = int(path.split("/")[2])
            repair = next((r for r in self.repairs if r["id"] == rid), None)
            if repair is None:
                return httpx.Response(404, json={"detail": "Repair not found"})
            repair["status"] = "VERIFYING"
            repair["next_action"] = "Awaiting verification: confirm the problem is actually fixed before closing."
            repair["waiting_on"] = "secretary"
            return httpx.Response(200, json=self._repair_detail(repair))
        if path.startswith("/repairs/") and path.endswith("/verify") and method == "POST":
            rid = int(path.split("/")[2])
            repair = next((r for r in self.repairs if r["id"] == rid), None)
            if repair is None:
                return httpx.Response(404, json={"detail": "Repair not found"})
            repair["status"] = "CLOSED"
            repair["next_action"] = "Repair closed."
            repair["waiting_on"] = None
            repair["verified_at"] = "2026-08-17T08:10:00Z"
            repair["closure_reason"] = (body or {}).get("closure_signal", "HUMAN_CONFIRMED")
            repair["closed_at"] = "2026-08-17T08:10:00Z"
            return httpx.Response(200, json=self._repair_detail(repair))

        return httpx.Response(404, json={"detail": f"no route {method} {path}"})

    # PASAY-AI-EMPLOYEE-FOUNDATION-007 §13: Rent Action Pack (deterministic fake).
    def _action_pack(self, unit_id):
        unit = next((u for u in self.units if u.get("id") == unit_id), self.units[0])
        lease = next((l for l in self.leases if l.get("unit_id") == unit.get("id")), None)
        tenant = None
        if lease is not None:
            tenant = next((t for t in self.tenants if t.get("id") == lease.get("tenant_id")), None)
        phone = (tenant or {}).get("phone") or ""
        return {
            "unit_id": unit.get("id"),
            "unit_number": unit.get("unit_number"),
            "tenant_name": (tenant or {}).get("full_name", ""),
            "tenant_phone": phone,
            "contact_status": (tenant or {}).get("contact_status", ""),
            "outstanding_total": "75000.00",
            "outstanding_periods": 3,
            "unpaid_periods": ["2026-05", "2026-06", "2026-07"],
            "overdue_days": 104,
            "last_follow_up": None,
            "latest_promise": None,
            "payment_method": None,
            "assignable": bool(phone),
            "blocked_hint": ("1680 租客电话 09XXXXXXXXX" if not phone else ""),
            "call_script": f"Hi {(tenant or {}).get('full_name','')}...Unit {unit.get('unit_number')}...",
            "message_script": f"Hi {(tenant or {}).get('full_name','')}...",
        }

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
                     details=None, assigned_user_id=None, next_action=None,
                     next_check_at=None, property_code=None, source_type="recurring_rule",
                     source_id=1):
        row = {
            "id": task_id,
            "task_type": task_type,
            "title": title,
            "description": None,
            "property_id": property_id,
            "property_code": property_code,
            "tenant_id": None,
            "lease_id": None,
            "source_type": source_type,
            "source_id": source_id,
            "source_event": None,
            "assigned_user_id": assigned_user_id,
            "priority": "medium",
            "status": status,
            "due_at": due_at or "2026-08-10T00:00:00+08:00",
            "remind_at": None,
            "snoozed_until": snoozed_until,
            "next_action": next_action,
            "next_check_at": next_check_at,
            "context": None,
            "completion_condition": None,
            "completed_at": None,
            "completed_by": None,
            "dedupe_key": f"recurring:1:2026-Q3",
            "details": details or {"amount": "12000.00", "period": "2026-Q3"},
        }
        self.operational_tasks.append(row)
        self.quick_tasks.append(
            {
                "id": task_id,
                "task_type": task_type,
                "title": title,
                "status": status,
                "property_code": property_code,
                "due_at": row["due_at"],
                "next_action": next_action,
                "next_check_at": next_check_at,
            }
        )
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
    created: list[tuple[Any, Any, Any]] = []

    def _make(backend=None, api_key="manager-key", admin_api_key="admin-key",
              callback_ttl=900, state_db=None, bot=None, job_api_key=""):
        backend = backend or FakeBackend()
        settings = Settings(
            state_db=state_db or str(tmp_path / f"state_{len(created)}.db"),
            pasay_tg_bot_token="123:TEST",
            pasay_api_base="http://test/api/v1",
            callback_ttl_seconds=callback_ttl,
            pasay_admin_api_key=admin_api_key,
            pasay_job_api_key=job_api_key,
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
        # JOB-SERVICE-AUTH-002: background jobs use a dedicated SYSTEM-keyed
        # client (never the human-bound interactive client). Tests opt in with
        # job_api_key; the default disables the jobs (fail closed).
        job_api = None
        if job_api_key:
            job_api = PasayApiClient(
                settings.pasay_api_base,
                job_api_key,
                timeout=1.0,
                transport=httpx.MockTransport(backend.handler),
            )
        bot = bot or FakeBot()
        app = build_application(
            settings, api, store, bot=bot, admin_api_client=admin_api,
            job_api_client=job_api,
        )
        created.append((api, admin_api, store, job_api))
        return SimpleNamespace(
            app=app, bot=bot, backend=backend, store=store, guard=guard,
            settings=settings, api=api, admin_api=admin_api, job_api=job_api,
        )

    yield _make

    for api, admin_api, store, job_api in created:
        try:
            asyncio.run(api.aclose())
        except Exception:
            pass
        if admin_api is not None:
            try:
                asyncio.run(admin_api.aclose())
            except Exception:
                pass
        if job_api is not None:
            try:
                asyncio.run(job_api.aclose())
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
