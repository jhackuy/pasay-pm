"""Pasay API client: MockTransport 200/401/403/409/timeout + 409 semantics."""
import asyncio

import httpx
import pytest

from pasay_bot.api_client import (
    PasayApiAuthError,
    PasayApiClient,
    PasayApiConflictError,
    PasayApiError,
    PasayApiPermissionError,
    PasayApiTimeoutError,
)


def make_client(handler, api_key="secret-key"):
    return PasayApiClient(
        "http://test/api/v1", api_key, timeout=1.0,
        transport=httpx.MockTransport(handler),
    )


def run(coro):
    return asyncio.run(coro)


def test_get_properties_parsed():
    async def handler(request):
        return httpx.Response(200, json=[
            {"id": 1, "name": "Bayshore", "address": "5 Roxas Blvd",
             "city": "Pasay", "total_units": 2, "is_active": True},
        ])

    client = make_client(handler)
    try:
        props = run(client.get_properties())
        assert len(props) == 1
        assert props[0].name == "Bayshore"
        assert props[0].total_units == 2
    finally:
        run(client.aclose())


def test_create_income_payload_and_parsed():
    captured = {}

    async def handler(request):
        captured["json"] = __import__("json").loads(request.content)
        return httpx.Response(201, json={
            "id": 7, "lease_id": 1, "amount": "55000.00", "received_date": "2026-08-10",
            "payment_method": "Bank", "status": "pending", "description": "rent 2026-08",
            "confirmed_by": None, "confirmed_at": None,
        })

    client = make_client(handler)
    try:
        income = run(client.create_income(
            lease_id=1, amount="55000.00", received_date="2026-08-10",
            payment_method="Bank", description="rent 2026-08",
        ))
        assert income.id == 7
        assert income.status == "pending"
        assert captured["json"]["status"] == "pending"
        assert captured["json"]["amount"] == "55000.00"  # string, never float
    finally:
        run(client.aclose())


def test_401_auth_error():
    async def handler(request):
        return httpx.Response(401, json={"detail": "Invalid API key"})

    client = make_client(handler)
    try:
        with pytest.raises(PasayApiAuthError) as ei:
            run(client.get_properties())
        assert ei.value.status_code == 401
    finally:
        run(client.aclose())


def test_403_permission_error():
    async def handler(request):
        return httpx.Response(403, json={"detail": "Insufficient permissions"})

    client = make_client(handler)
    try:
        with pytest.raises(PasayApiPermissionError) as ei:
            run(client.get_properties())
        assert ei.value.status_code == 403
    finally:
        run(client.aclose())


def test_409_conflict_error():
    async def handler(request):
        return httpx.Response(409, json={"detail": "Only pending income can be confirmed"})

    client = make_client(handler)
    try:
        with pytest.raises(PasayApiConflictError) as ei:
            run(client.confirm_income(42))
        assert ei.value.status_code == 409
        assert "Only pending income" in ei.value.detail
    finally:
        run(client.aclose())


def test_timeout_raises_timeout_error():
    async def handler(request):
        raise httpx.ReadTimeout("read timeout")

    client = make_client(handler)
    try:
        with pytest.raises(PasayApiTimeoutError):
            run(client.get_properties())
    finally:
        run(client.aclose())


def test_app_api_client_409_conflict_means_already_confirmed():
    """Double-confirm: first call succeeds, the second is a 409 -> the caller
    knows the income is already confirmed (not an error to scare the user)."""
    state = {"status": "pending"}

    async def handler(request):
        if state["status"] != "pending":
            return httpx.Response(409, json={"detail": "Only pending income can be confirmed"})
        state["status"] = "confirmed"
        return httpx.Response(200, json={
            "id": 1, "lease_id": 1, "amount": "55000.00", "received_date": "2026-08-10",
            "payment_method": "Bank", "status": "confirmed", "description": "rent 2026-08",
            "confirmed_by": 1, "confirmed_at": "2026-08-10T12:00:00Z",
        })

    client = make_client(handler)
    try:
        first = run(client.confirm_income(1))
        assert first.status == "confirmed"
        with pytest.raises(PasayApiConflictError) as ei:
            run(client.confirm_income(1))
        assert ei.value.status_code == 409
    finally:
        run(client.aclose())


def test_unknown_status_error_is_pasay_api_error():
    async def handler(request):
        return httpx.Response(500, json={"detail": "boom"})

    client = make_client(handler)
    try:
        with pytest.raises(PasayApiError):
            run(client.get_properties())
    finally:
        run(client.aclose())
