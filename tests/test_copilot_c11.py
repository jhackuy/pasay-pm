"""V1.2.2 Phase C1.1 — FAST UX / latency gate tests (real PostgreSQL).

Deliverables under test (brief §7):
  A. deterministic-first TODAY: ``build_today_deterministic`` returns <=3
     items, top refs match ``rank_items`` top-K, NO LLM invoked; the default
     ``/today`` endpoint is fast even when the provider is DOWN (no 503, no
     hang, no client construction).
  B. WHY enrichment: provider explanation on success; deterministic HTTP-200
     fallback when the provider raises; invented amounts/refs stripped and
     flagged.
  C. ASK enrichment: grounded answer on success; friendly deterministic
     fallback on provider-down; ungrounded amounts refused (stripped/flagged).
  D. latency instrumentation: all six phases present, monotonic, ``llm_ms=0``
     for the fast TODAY path.
  E. provider profile map (TODAY=None / EXPLAIN=fast / ASK=strong) + the
     wired fast non-reasoning lane (``deepseek-chat``).
  I. mutation invariant: today/why/ask (incl. fallbacks) change NO
     operational/financial rows — only the optional ``copilot_runs`` audit.
"""
from __future__ import annotations

import json
import re
import secrets
from datetime import date, datetime, timedelta

import pytest

from app.core.security import hash_api_key
from app.models.commission import CommissionSettlement
from app.models.copilot import CopilotActionProposal, CopilotRun
from app.models.financial import Expense, Income, IncomeStatus
from app.models.lease import Lease, LeaseStatus
from app.models.operations import OperationalTask, OperationalTaskStatus, OperationalTaskType
from app.models.property import Property, Unit, UnitStatus
from app.models.task import Task
from app.models.tenant import Tenant
from app.models.user import User, UserRole
from app.services.copilot import ask, llm, ranking, shared, today_fast, why
from app.services.copilot.llm import LLMProviderError
from app.services.operations.copilot import build_copilot_context
from app.services.operations.timeclock import MANILA_TZ, clock

API = "/api/v1"
NOW_MANILA = datetime(2026, 8, 11, 12, 0, 0, tzinfo=MANILA_TZ)
_REF_PATTERN = re.compile(
    r"\b(?:task|lease|property|expense|income|settlement|rule|tenant):\d+\b"
)


@pytest.fixture(autouse=True)
def _reset_clock():
    yield
    clock.set_override(None)


# ---------------------------------------------------------------------------
# helpers (self-contained; same seeding as the C1 mandated scenario)
# ---------------------------------------------------------------------------

def _user_with_key(db, username, role):
    key = secrets.token_urlsafe(24)
    user = User(
        username=username,
        role=role,
        api_key_hash=hash_api_key(key),
        is_active=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user, key


def _headers(key):
    return {"Authorization": f"Bearer {key}"}


def _seed_property(db, name="Sunset Tower"):
    prop = Property(name=name, address="1 Roxas Blvd", city="Pasay", total_units=4)
    db.add(prop)
    db.flush()
    return prop


def _seed_lease(db, *, prop=None, unit_no="101", monthly_rent="12000.00",
                start=date(2026, 1, 1), end=date(2026, 12, 31), due_day=5):
    if prop is None:
        prop = _seed_property(db)
    unit = Unit(property_id=prop.id, unit_number=unit_no, floor="1",
                size_sqm="32.50", monthly_rent=monthly_rent,
                status=UnitStatus.occupied)
    tenant = Tenant(full_name="Juan Dela Cruz", phone="+639170000000")
    db.add_all([unit, tenant])
    db.flush()
    lease = Lease(unit_id=unit.id, tenant_id=tenant.id, start_date=start,
                  end_date=end, monthly_rent=monthly_rent, deposit="24000.00",
                  status=LeaseStatus.active, due_day=due_day)
    db.add(lease)
    db.flush()
    return lease


def _seed_income(db, lease, month: str, amount="12000.00"):
    income = Income(
        lease_id=lease.id,
        amount=amount,
        received_date=date(int(month[:4]), int(month[5:7]), 5),
        description=month,
        status=IncomeStatus.confirmed,
    )
    db.add(income)
    db.flush()
    return income


def _months_between(start: str, end: str) -> list[str]:
    out = []
    y, m = (int(start[:4]), int(start[5:7]))
    ey, em = (int(end[:4]), int(end[5:7]))
    while (y, m) <= (ey, em):
        out.append(f"{y:04d}-{m:02d}")
        m += 1
        if m > 12:
            m = 1
            y += 1
    return out


def _seed_overdue_lease(db, *, covered_through="2026-05"):
    lease = _seed_lease(db)
    for month in _months_between("2026-01", covered_through):
        _seed_income(db, lease, month)
    return lease


def _seed_operational_task(db, *, title="购买办公用品", priority="low",
                           due_at=None):
    task = OperationalTask(
        task_type=OperationalTaskType.AC_MAINTENANCE,
        title=title,
        description="low-amount todo " + "x" * 2000,
        priority=priority,
        status=OperationalTaskStatus.PENDING,
        due_at=due_at or NOW_MANILA + timedelta(days=10),
        remind_at=None,
        snoozed_until=None,
        source_type="manual",
        source_id=None,
        dedupe_key=f"c11-{secrets.token_urlsafe(8)}",
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    return task


def _seed_maintenance(db, *, title="空调保养", priority="medium", due_date=None,
                      assigned_to=None):
    task = Task(
        title=title,
        description=None,
        status="open",
        priority=priority,
        due_date=due_date or date(2026, 8, 12),
        assigned_to=assigned_to,
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    return task


def _seed_mandated_scenario(db):
    """Severe overdue rent + lease expiring <=7d + maintenance + long-note
    low-amount todo (the C1 mandated req-6 mix)."""
    prop = _seed_property(db)
    overdue_lease = _seed_overdue_lease(db, covered_through="2026-05")
    expiring = _seed_lease(db, prop=prop, unit_no="102", monthly_rent="15000.00",
                           end=date(2026, 8, 14))
    for month in _months_between("2026-01", "2026-07"):
        _seed_income(db, expiring, month, amount="15000.00")
    db.add(Task(title="dummy completed", status="completed"))
    db.flush()
    admin = db.query(User).filter_by(role=UserRole.admin).first()
    maint = _seed_maintenance(db, assigned_to=admin.id if admin else None)
    low = _seed_operational_task(db)
    db.commit()
    return {
        "overdue_lease": overdue_lease,
        "expiring_lease": expiring,
        "maintenance": maint,
        "low_todo": low,
    }


class _FakeLLM:
    """Deterministic fake provider (counts invocations; injectable text/error)."""

    def __init__(self, *, text=None, error=None, provider="deepseek",
                 model="fake-v1"):
        self.text = text
        self.error = error
        self.provider = provider
        self.model = model
        self.completed = 0
        self.messages = None

    def complete(self, messages, **kwargs):
        self.completed += 1
        self.messages = messages
        if self.error is not None:
            raise self.error
        payload = self.text if self.text is not None else json.dumps({})
        return llm.LLMResult(
            text=payload,
            model=self.model,
            provider=self.provider,
            latency_ms=17,
            version="fake-model-1",
        )


# ---------------------------------------------------------------------------
# A. deterministic-first TODAY
# ---------------------------------------------------------------------------

def test_build_today_deterministic_no_llm_and_topk(db_session, monkeypatch):
    admin, _ = _user_with_key(db_session, "admin-c11-det", UserRole.admin)
    seeded = _seed_mandated_scenario(db_session)
    db_session.commit()
    clock.set_override(NOW_MANILA)
    calls: list = []

    def _boom(provider=None):
        calls.append(provider)
        raise AssertionError("LLM must never be invoked by the deterministic path")

    monkeypatch.setattr("app.services.copilot.llm.get_llm_client", _boom)

    result = today_fast.build_today_deterministic(db_session, admin, now=NOW_MANILA)
    assert calls == []  # NO LLM client was ever constructed

    context = build_copilot_context(db_session, admin, now=NOW_MANILA)
    det_top = ranking.top_k(context, k=3)
    assert result.deterministic_top_refs == [r.item_ref for r in det_top]
    assert len(result.top_items) <= 3
    assert [i.item_ref for i in result.top_items] == result.deterministic_top_refs

    # reasons/actions come from the existing deterministic priority engine
    ranked_map = {r.item_ref: r for r in ranking.rank_items(context)}
    for item in result.top_items:
        assert item.reason_why_important == ranked_map[item.item_ref].reason
        assert item.suggested_action == shared.default_action(ranked_map[item.item_ref])

    assert result.enriched is False
    assert result.provider == today_fast.DETERMINISTIC_PROVIDER
    assert result.model == today_fast.DETERMINISTIC_MODEL
    # mandated mix: severe overdue > expiring <=7d > maintenance
    kinds = [ranked_map[r].kind for r in result.deterministic_top_refs]
    assert kinds == ["severe_overdue_rent", "lease_expiring", "maintenance"]
    # a low-risk long-note item can never displace a high-risk one
    assert f"task:{seeded['low_todo'].id}" not in result.deterministic_top_refs


def test_deterministic_summary_versioned_and_grounded(db_session):
    admin, _ = _user_with_key(db_session, "admin-c11-sum", UserRole.admin)
    _seed_mandated_scenario(db_session)
    db_session.commit()
    clock.set_override(NOW_MANILA)

    result = today_fast.build_today_deterministic(db_session, admin, now=NOW_MANILA)
    assert result.summary_version == "det_v1"
    assert result.summary
    # <= 2 sentences, no backend refs, no JSON artifacts
    assert len(re.findall(r"[.!?](?:\s|$)", result.summary)) <= 2
    assert not _REF_PATTERN.search(result.summary)
    assert "{" not in result.summary and "}" not in result.summary
    # grounded amounts appear in the summary (severe overdue total)
    assert "PHP 36,000.00" in result.summary
    assert "lease" in result.summary
    assert "maintenance" in result.summary


def test_deterministic_summary_empty_portfolio(db_session):
    """No urgent items -> friendly deterministic 'nothing urgent' summary."""
    admin, _ = _user_with_key(db_session, "admin-c11-empty", UserRole.admin)
    db_session.commit()
    clock.set_override(NOW_MANILA)

    result = today_fast.build_today_deterministic(db_session, admin, now=NOW_MANILA)
    assert result.summary_version == "det_v1"
    assert result.top_items == []
    assert "No urgent operational items" in result.summary


def test_today_deterministic_latency_llm_zero_and_monotonic(db_session):
    admin, _ = _user_with_key(db_session, "admin-c11-lat", UserRole.admin)
    _seed_mandated_scenario(db_session)
    db_session.commit()
    clock.set_override(NOW_MANILA)

    result = today_fast.build_today_deterministic(db_session, admin, now=NOW_MANILA)
    lat = result.latency
    assert lat.llm_ms == 0  # fast TODAY has NO LLM phase
    for name in ("context_build_ms", "priority_ms", "grounding_ms",
                 "llm_ms", "render_ms", "total_ms"):
        assert getattr(lat, name) >= 0, name
    assert lat.total_ms == result.latency_ms
    # monotonic: total covers every phase (each phase can only be <= total)
    assert lat.total_ms >= lat.context_build_ms
    assert lat.total_ms >= lat.priority_ms
    assert lat.total_ms >= lat.grounding_ms
    assert lat.total_ms >= lat.render_ms


# ---------------------------------------------------------------------------
# B. WHY enrichment
# ---------------------------------------------------------------------------

def _overdue_ref_and_ranked(db_session, admin):
    context = build_copilot_context(db_session, admin, now=NOW_MANILA)
    ranked = ranking.rank_items(context)
    overdue = next(r for r in ranked if r.kind == "severe_overdue_rent")
    return overdue.item_ref, overdue


def test_why_success_grounded_explanation(db_session, client, manager_headers, monkeypatch):
    admin, _ = _user_with_key(db_session, "admin-c11-why", UserRole.admin)
    _seed_mandated_scenario(db_session)
    db_session.commit()
    clock.set_override(NOW_MANILA)
    overdue_ref, overdue = _overdue_ref_and_ranked(db_session, admin)
    outstanding = str(overdue.payload["total_outstanding"])

    fake = _FakeLLM(text=json.dumps({
        "explanation": (
            "This lease has unpaid rent. "
            f"PHP {outstanding} is outstanding on the account."
        ),
        "recommendation": "Follow up with the tenant today.",
    }))
    monkeypatch.setattr("app.services.copilot.llm.get_llm_client", lambda provider=None: fake)

    resp = client.post(f"{API}/operations/copilot/why", headers=manager_headers,
                       json={"item_ref": overdue_ref})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["fallback"] is False
    assert data["item_ref"] == overdue_ref
    assert "unpaid rent" in data["explanation"]
    assert outstanding in data["explanation"]
    assert data["provider"] == "deepseek"
    assert data["model"] == "fake-v1"
    assert overdue_ref in data["grounded_refs"]
    # no backend refs / JSON artifacts leak into displayed text
    assert not _REF_PATTERN.search(data["explanation"] + data["recommendation"])
    assert all(ch not in (data["explanation"] + data["recommendation"]) for ch in "{}[]")
    # latency breakdown present
    assert data["latency"]["llm_ms"] >= 0
    assert data["latency"]["total_ms"] >= data["latency"]["llm_ms"]
    assert fake.completed == 1


def test_why_fallback_when_provider_down(db_session, client, manager_headers, monkeypatch):
    admin, _ = _user_with_key(db_session, "admin-c11-whyfb", UserRole.admin)
    _seed_mandated_scenario(db_session)
    db_session.commit()
    clock.set_override(NOW_MANILA)
    overdue_ref, overdue = _overdue_ref_and_ranked(db_session, admin)

    def _boom(provider=None):
        raise LLMProviderError("provider unreachable")

    monkeypatch.setattr("app.services.copilot.llm.get_llm_client", _boom)

    resp = client.post(f"{API}/operations/copilot/why", headers=manager_headers,
                       json={"item_ref": overdue_ref})
    # Requirement 8: HTTP 200 with the deterministic reason/action, no 503.
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["fallback"] is True
    assert data["explanation"] == overdue.reason
    assert data["recommendation"] == shared.default_action(overdue)
    assert "provider_error" in data["flags"]
    assert data["model"] == why.FALLBACK_MODEL
    assert not _REF_PATTERN.search(data["explanation"] + data["recommendation"])


def test_why_strips_ungrounded_amounts_and_refs(db_session, client, manager_headers, monkeypatch):
    admin, _ = _user_with_key(db_session, "admin-c11-whygr", UserRole.admin)
    _seed_mandated_scenario(db_session)
    db_session.commit()
    clock.set_override(NOW_MANILA)
    overdue_ref, overdue = _overdue_ref_and_ranked(db_session, admin)
    outstanding = str(overdue.payload["total_outstanding"])

    fake = _FakeLLM(text=json.dumps({
        "explanation": (
            "The lease:3 is overdue with an invented PHP 999,999 balance; "
            f"the real amount is {outstanding}."
        ),
        "recommendation": "Follow up today.",
    }))
    monkeypatch.setattr("app.services.copilot.llm.get_llm_client", lambda provider=None: fake)

    resp = client.post(f"{API}/operations/copilot/why", headers=manager_headers,
                       json={"item_ref": overdue_ref})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["fallback"] is False
    assert "999" not in data["explanation"]          # invented amount stripped
    assert outstanding in data["explanation"]         # grounded amount kept
    assert "lease:3" not in data["explanation"]       # backend ref stripped
    assert "lease" in data["explanation"]             # human text survives
    assert "ungrounded_amount" in data["flags"]


def test_why_not_grounded_404_and_rbac_and_bad_provider(db_session, client, monkeypatch):
    admin, admin_key = _user_with_key(db_session, "admin-c11-why404", UserRole.admin)
    agent, agent_key = _user_with_key(db_session, "ag-c11-why403", UserRole.agent)
    _seed_mandated_scenario(db_session)
    db_session.commit()
    clock.set_override(NOW_MANILA)

    # ungrounded item_ref -> 404
    resp = client.post(f"{API}/operations/copilot/why", headers=_headers(admin_key),
                       json={"item_ref": "lease:999999"})
    assert resp.status_code == 404

    # agent -> 403
    resp = client.post(f"{API}/operations/copilot/why", headers=_headers(agent_key),
                       json={"item_ref": "lease:1"})
    assert resp.status_code == 403

    # unknown provider -> 422
    resp = client.post(f"{API}/operations/copilot/why", headers=_headers(admin_key),
                       json={"item_ref": "lease:1", "provider": "bogus"})
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# C. ASK enrichment
# ---------------------------------------------------------------------------

def test_ask_success_grounded_answer(db_session, client, manager_headers, monkeypatch):
    admin, _ = _user_with_key(db_session, "admin-c11-ask", UserRole.admin)
    _seed_mandated_scenario(db_session)
    db_session.commit()
    clock.set_override(NOW_MANILA)
    _, overdue = _overdue_ref_and_ranked(db_session, admin)
    outstanding = str(overdue.payload["total_outstanding"])

    fake = _FakeLLM(text=json.dumps({
        "answer": (
            "One lease is severely overdue with "
            f"{outstanding} PHP outstanding on it."
        ),
    }))
    monkeypatch.setattr("app.services.copilot.llm.get_llm_client", lambda provider=None: fake)

    resp = client.post(f"{API}/operations/copilot/ask", headers=manager_headers,
                       json={"question": "谁没交租？"})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["fallback"] is False
    assert "overdue" in data["answer"]
    assert outstanding in data["answer"]
    assert data["provider"] == "deepseek"
    assert data["model"] == "fake-v1"
    assert not _REF_PATTERN.search(data["answer"])
    assert data["latency"]["llm_ms"] >= 0
    assert fake.completed == 1


def test_ask_fallback_when_provider_down(db_session, client, manager_headers, monkeypatch):
    admin, _ = _user_with_key(db_session, "admin-c11-askfb", UserRole.admin)
    _seed_mandated_scenario(db_session)
    db_session.commit()
    clock.set_override(NOW_MANILA)

    def _boom(provider=None):
        raise LLMProviderError("provider unreachable")

    monkeypatch.setattr("app.services.copilot.llm.get_llm_client", _boom)

    resp = client.post(f"{API}/operations/copilot/ask", headers=manager_headers,
                       json={"question": "这个月哪个房子维修费最高？"})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["fallback"] is True
    assert data["answer"] == ask.FALLBACK_ANSWER
    assert "provider_error" in data["flags"]
    assert data["model"] == ask.FALLBACK_MODEL


def test_ask_refuses_ungrounded_amounts(db_session, client, manager_headers, monkeypatch):
    admin, _ = _user_with_key(db_session, "admin-c11-askgr", UserRole.admin)
    _seed_mandated_scenario(db_session)
    db_session.commit()
    clock.set_override(NOW_MANILA)
    _, overdue = _overdue_ref_and_ranked(db_session, admin)
    outstanding = str(overdue.payload["total_outstanding"])

    fake = _FakeLLM(text=json.dumps({
        "answer": (
            "The worst lease owes PHP 1,999,999.50 but the grounded balance is "
            f"{outstanding}. Also there are 3 overdue months."
        ),
    }))
    monkeypatch.setattr("app.services.copilot.llm.get_llm_client", lambda provider=None: fake)

    resp = client.post(f"{API}/operations/copilot/ask", headers=manager_headers,
                       json={"question": "谁欠得最多？"})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert "1,999,999" not in data["answer"]       # invented amount stripped
    assert "999" not in data["answer"]
    assert outstanding in data["answer"]           # grounded amount kept
    assert "3 overdue months" in data["answer"]    # non-financial text survives
    assert "ungrounded_amount" in data["flags"]


def test_ask_validation_and_rbac(db_session, client, monkeypatch):
    admin, admin_key = _user_with_key(db_session, "admin-c11-askv", UserRole.admin)
    agent, agent_key = _user_with_key(db_session, "ag-c11-ask403", UserRole.agent)
    db_session.commit()

    # agent -> 403
    resp = client.post(f"{API}/operations/copilot/ask", headers=_headers(agent_key),
                       json={"question": "hi"})
    assert resp.status_code == 403

    # empty question -> 422
    resp = client.post(f"{API}/operations/copilot/ask", headers=_headers(admin_key),
                       json={"question": ""})
    assert resp.status_code == 422

    # unknown provider -> 422
    resp = client.post(f"{API}/operations/copilot/ask", headers=_headers(admin_key),
                       json={"question": "hi", "provider": "bogus"})
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# D/E. latency + provider profile map
# ---------------------------------------------------------------------------

def test_provider_profile_map_and_fast_non_reasoning_lane(monkeypatch):
    assert "deepseek-chat" in llm.list_providers()
    assert llm.PROVIDER_KINDS["deepseek-chat"] == "non-reasoning"
    # centralized map (Requirement 6): TODAY=no LLM, EXPLAIN=fast, ASK=strong
    assert llm.profile_provider("TODAY") is None
    assert llm.profile_provider("EXPLAIN") == "deepseek-chat"
    assert llm.profile_provider("ASK") == "deepseek-pro"
    # an explicit requested provider always wins (no scattered ifs)
    assert llm.profile_provider("EXPLAIN", "deepseek") == "deepseek"
    # env-tunable per profile
    monkeypatch.setenv("COPILOT_LLM_PROFILE_ASK", "deepseek-chat")
    assert llm.profile_provider("ASK") == "deepseek-chat"
    # deepseek-chat resolves through the same key path
    config = llm.provider_config("deepseek-chat")
    assert config.name == "deepseek-chat"
    assert config.model == "deepseek-chat"
    assert config.base_url == "https://api.deepseek.com/v1"


def test_endpoint_latency_fields_present_for_all_surfaces(
    db_session, client, admin_headers, monkeypatch
):
    admin, _ = _user_with_key(db_session, "admin-c11-latall", UserRole.admin)
    seeded = _seed_mandated_scenario(db_session)
    db_session.commit()
    clock.set_override(NOW_MANILA)
    fake = _FakeLLM(text=json.dumps({
        "explanation": "grounded explanation text",
        "recommendation": "Follow up today",
    }))
    monkeypatch.setattr("app.services.copilot.llm.get_llm_client", lambda provider=None: fake)

    today_resp = client.post(f"{API}/operations/copilot/today",
                             headers=admin_headers, json={})
    why_resp = client.post(
        f"{API}/operations/copilot/why", headers=admin_headers,
        json={"item_ref": f"lease:{seeded['overdue_lease'].id}"},
    )
    ask_resp = client.post(f"{API}/operations/copilot/ask", headers=admin_headers,
                           json={"question": "这个月哪个房子维修费最高？"})

    for resp in (today_resp, why_resp, ask_resp):
        assert resp.status_code == 200, resp.text
        fields = resp.json()["latency"]
        assert set(fields) == {
            "context_build_ms", "priority_ms", "grounding_ms",
            "llm_ms", "render_ms", "total_ms",
        }
        for value in fields.values():
            assert value >= 0
        assert fields["total_ms"] == resp.json()["latency_ms"]
    assert today_resp.json()["latency"]["llm_ms"] == 0  # fast TODAY
    assert why_resp.json()["latency"]["llm_ms"] >= 0
    assert ask_resp.json()["latency"]["llm_ms"] >= 0


# ---------------------------------------------------------------------------
# I. mutation invariant (Requirement 7): read-only surface
# ---------------------------------------------------------------------------

def test_mutation_invariant_today_why_ask(db_session, client, admin_headers, monkeypatch):
    admin, _ = _user_with_key(db_session, "admin-c11-ro", UserRole.admin)
    seeded = _seed_mandated_scenario(db_session)
    db_session.commit()
    clock.set_override(NOW_MANILA)
    fake = _FakeLLM(text=json.dumps({
        "explanation": "grounded explanation",
        "recommendation": "Follow up today",
    }))
    monkeypatch.setattr("app.services.copilot.llm.get_llm_client", lambda provider=None: fake)

    tables = {
        "tasks": Task,
        "operational_tasks": OperationalTask,
        "expenses": Expense,
        "incomes": Income,
        "leases": Lease,
        "settlements": CommissionSettlement,
        "users": User,
        "proposals": CopilotActionProposal,
    }
    before = {name: db_session.query(model).count() for name, model in tables.items()}
    runs_before = db_session.query(CopilotRun).count()

    # default TODAY (deterministic, provider-down safe), WHY, ASK
    today_resp = client.post(f"{API}/operations/copilot/today",
                             headers=admin_headers, json={})
    why_resp = client.post(
        f"{API}/operations/copilot/why", headers=admin_headers,
        json={"item_ref": f"lease:{seeded['overdue_lease'].id}"},
    )
    ask_resp = client.post(f"{API}/operations/copilot/ask", headers=admin_headers,
                           json={"question": "谁没交租？"})
    assert today_resp.status_code == 200
    assert why_resp.status_code == 200
    assert ask_resp.status_code == 200

    db_session.expire_all()
    # NO operational/financial rows changed — only the 3 copilot_runs audit rows
    for name, model in tables.items():
        assert db_session.query(model).count() == before[name], name
    assert db_session.query(CopilotRun).count() == runs_before + 3
    for intent in ("copilot_today", "copilot_why", "copilot_ask"):
        assert db_session.query(CopilotRun).filter_by(intent=intent).count() == 1


# ---------------------------------------------------------------------------
# F. live-LLM eval smoke (deselected by default; run with `-m eval`)
# ---------------------------------------------------------------------------

@pytest.mark.eval
def test_eval_smoke_why_live_llm(db_session, monkeypatch):
    """Live WHY smoke against the real EXPLAIN provider (eval only)."""
    admin, _ = _user_with_key(db_session, "admin-c11-evalwhy", UserRole.admin)
    _seed_mandated_scenario(db_session)
    db_session.commit()
    clock.set_override(NOW_MANILA)
    overdue_ref, overdue = _overdue_ref_and_ranked(db_session, admin)
    monkeypatch.delenv("COPILOT_LLM_PROFILE_EXPLAIN", raising=False)
    result = why.explain_item(db_session, admin, overdue_ref)
    assert result.fallback is False
    assert result.explanation
    assert result.recommendation
    assert not _REF_PATTERN.search(result.explanation)
    assert "ungrounded_amount" not in result.flags


@pytest.mark.eval
def test_eval_smoke_ask_live_llm(db_session, monkeypatch):
    """Live ASK smoke against the real ASK provider (eval only)."""
    admin, _ = _user_with_key(db_session, "admin-c11-evalask", UserRole.admin)
    _seed_mandated_scenario(db_session)
    db_session.commit()
    clock.set_override(NOW_MANILA)
    monkeypatch.delenv("COPILOT_LLM_PROFILE_ASK", raising=False)
    result = ask.ask_question(db_session, admin, "谁没交租？")
    assert result.fallback is False
    assert result.answer
    assert "ungrounded_amount" not in result.flags
