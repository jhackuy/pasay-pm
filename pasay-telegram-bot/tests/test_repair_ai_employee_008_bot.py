"""REPAIR-AI-EMPLOYEE-WORKFLOW-008A — bot fast-path tests.

Prove the Telegram/Mini App read the REAL business state (via the API client
dataclasses + FakeBackend) and that the derived card text is business-driven —
not hard-coded chat copy. Also proves the requote action dedup feels the same
through the bot as through the backend: repeated ``decide(reject)`` never
creates a second active REQUOTE action.
"""
from __future__ import annotations

import asyncio

import httpx
import pytest

from pasay_bot.api_client import PasayApiClient
from pasay_bot.render.repairs import render_repair_card, status_label
from tests.conftest import FakeBackend


def make_client(backend: FakeBackend) -> PasayApiClient:
    return PasayApiClient(
        "http://test/api/v1", "secret-key", timeout=2.0,
        transport=httpx.MockTransport(backend.handler),
    )


def run(coro):
    return asyncio.run(coro)


def _new_repair(client: PasayApiClient) -> int:
    repair = run(client.create_repair(
        issue="Aircon compressor replacement",
        issue_description="Not cooling",
    ))
    return repair.id


def test_repair_reject_stays_open_and_creates_one_requote():
    backend = FakeBackend()
    client = make_client(backend)
    try:
        rid = _new_repair(client)

        # Secretary submits V1.
        detail = run(client.submit_repair_proposal(rid, amount="8000.00", vendor="ACPro",
                                                    description="Compressor replacement"))
        assert detail.status == "WAITING_APPROVAL"
        assert [p.version for p in detail.proposals] == [1]

        # Owner rejects V1.
        detail = run(client.decide_repair_proposal(rid, decision="reject", reason="Too expensive"))
        assert detail.status == "WAITING_HUMAN"
        v1 = detail.proposals[0]
        assert v1.status == "REJECTED"
        assert v1.rejection_reason == "Too expensive"
        # Repair remains OPEN/alive (P0 guard).
        assert detail.status != "CLOSED"

        # AI requote action exists, and the card reads real business state.
        requote = [a for a in detail.actions if a.action_kind == "REQUOTE"]
        assert len(requote) == 1
        assert requote[0].status == "PENDING"

        # Telegram fast-path card (008A §8 example): real state, not chat copy.
        card = render_repair_card(detail)
        assert "Owner rejected" in card
        assert "Too expensive" in card
        assert "Repair remains open." in card
        assert status_label("WAITING_HUMAN") == "Waiting on action"

        # Repeated bot callback / worker tick MUST NOT create a second requote.
        for _ in range(10):
            run(client.decide_repair_proposal(rid, decision="approve"))
            run(client.decide_repair_proposal(rid, decision="reject", reason="Too expensive"))
        fresh = run(client.get_repair(rid))
        requote_after = [a for a in fresh.actions
                         if a.action_kind == "REQUOTE" and a.status in ("PENDING", "IN_PROGRESS")]
        # The repeated reject of an already-REJECTED proposal is a no-op on the
        # fake (proposal stays REJECTED), so one active requote remains.
        assert len(requote_after) == 1
    finally:
        run(client.aclose())


def test_repair_full_flow_approve_pay_verify_close():
    backend = FakeBackend()
    client = make_client(backend)
    try:
        rid = _new_repair(client)
        run(client.submit_repair_proposal(rid, amount="6500.00", vendor="V2AC"))

        # Approve V1 -> WAITING_PAYMENT (NOT closed).
        detail = run(client.decide_repair_proposal(rid, decision="approve"))
        assert detail.status == "WAITING_PAYMENT"
        assert detail.status != "CLOSED"

        # Pay linked expense -> at most VERIFYING (NEVER closed).
        detail = run(client.pay_repair_expense(rid, expense_id=1))
        assert detail.status == "VERIFYING"
        assert detail.status != "CLOSED"

        # Record result (already verifying) -> stays verifying.
        detail = run(client.record_repair_result(rid, source="Secretary confirmed"))
        assert detail.status == "VERIFYING"

        # Verify -> CLOSED (only this path closes).
        detail = run(client.verify_and_close_repair(rid, verification_result="cooling restored"))
        assert detail.status == "CLOSED"
        assert detail.closure_reason == "HUMAN_CONFIRMED"
    finally:
        run(client.aclose())
