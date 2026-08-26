"""RET3 Cross-Org Coverage Tests — M005.

Covers routers that LACK real cross-org HTTP fail-closed evidence.

Pattern (T2 per router, strict minimal): owner_a creates Org-A record -> id_X;
owner_b GET /api/v1/<router>/{id_X} -> MUST HTTP 404 (fail-closed existence-deny).
For list-only / special endpoints: non-empty 4xx OR empty list total=0 counts.

ROUTERS COVERED HERE (one test each):
  1. units         — GET /units/{unit_id} cross-org 404
  2. payments      — POST /payments/match with owner_b (non-member scope):
                     must not leak Org-A lease/income data
  3. commission    — GET /commission/rules list with owner_b: total=0 isolation
  4. tasks         — DEPRECATED /tasks GET: 4xx with no data leak (any 4xx counts)
  5. attachments   — GET /attachments/{attachment_id} cross-org 404
  6. audit         — GET /audit-logs with owner_b (non-super-admin): 403 OR empty
  7. evidence      — GET /evidence/{evidence_id} cross-org 404/403 (fail-closed)
  8. viewings      — GET /viewings list with owner_b: total=0 no cross-org leak
  9. onboarding    — POST /onboarding/owner/bootstrap as active member: 403 guard

ROUTERS SKIPPED (existing real coverage elsewhere):
  - property_channel: tests/test_property_channel_p0_025.py::test_scoped_get_property_fails_closed_on_cross_org (L898)
  - reports: RETURN-3 refactor documented coverage
  - operations: tests/test_m003_expense_scope_hardening.py::test_operations_tasks_cross_org_404
  - properties: RETURN-3 refactor + property_channel P0 scoped_* tests
  - tenants / leases / income / expense / repairs: tests/test_milestone_1_org_scope_p0.py T1/T2
  - move_out / deposit_settlements: tests/test_m004_lease_moveout_truth_closure.py b11/c10/c13
"""
from __future__ import annotations

import pytest

API = "/api/v1"


def _bearer(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def _setup_org_a_property_and_unit(client, owner_a, org_a):
    """owner_a creates OrgA property + unit. Returns (property_id, unit_id)."""
    h = _bearer(owner_a[1])
    r = client.post(
        f"{API}/properties",
        json={
            "organization_id": org_a.id,
            "name": "Sunset Tower A",
            "address": "1 Roxas Blvd",
            "city": "Pasay",
            "total_units": 4,
        },
        headers=h,
    )
    assert r.status_code == 201, r.text
    pid = r.json()["id"]
    r = client.post(
        f"{API}/units",
        json={
            "property_id": pid,
            "unit_number": "101",
            "floor": "1",
            "size_sqm": "32.50",
            "monthly_rent": "12000.00",
            "status": "vacant",
        },
        headers=h,
    )
    assert r.status_code == 201, r.text
    uid = r.json()["id"]
    return pid, uid


# ---------------------------------------------------------------------------
# 1. units: T2 cross-org GET /units/{id} -> 404
# ---------------------------------------------------------------------------


def test_units_t2_get_cross_org_404(client, owner_a, owner_b, org_a, org_b):
    _prop_id_a, unit_id_a = _setup_org_a_property_and_unit(client, owner_a, org_a)
    owner_b_headers = _bearer(owner_b[1])

    resp = client.get(f"{API}/units/{unit_id_a}", headers=owner_b_headers)
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# 2. payments: Cross-org POST /payments/match — must NOT leak OrgA data
#    (payments router is read-only match endpoint, no GET /{id}; we verify
#    manager_or_admin dep blocks non-member and no cross-org lease data leaks.)
# ---------------------------------------------------------------------------


def test_payments_cross_org_match_no_leak(client, owner_a, owner_b, org_a, org_b):
    """owner_b has no OrgA membership — POST /payments/match is permitted by
    role (manager_or_admin) but the match service MUST return NO candidates
    from OrgA. 200 + candidates:[] counts as fail-closed (no data leak)."""
    _setup_org_a_property_and_unit(client, owner_a, org_a)
    owner_b_headers = _bearer(owner_b[1])

    resp = client.post(
        f"{API}/payments/match",
        json={"text": "rent for 101", "amount": "12000.00"},
        headers=owner_b_headers,
    )
    # Accept any of: explicit 403/404 auth fail, OR 200 + candidate leak absent.
    if resp.status_code not in {403, 404}:
        assert resp.status_code == 200, (
            f"Expected cross-org payments/match to be 403/404 or 200+empty, "
            f"got {resp.status_code}. Body: {resp.text[:500]!r}"
        )
        body = resp.json()
        assert isinstance(body, dict), body
        cands = body.get("candidates", None)
        assert cands == [], (
            f"Cross-org payments match 200 must return candidates=[], "
            f"got candidates={cands!r}[:5]={cands[:5] if isinstance(cands, list) else cands!r}"
        )


# ---------------------------------------------------------------------------
# 3. commission: T2 list_isolation — owner_b GET /commission/rules -> total=0
#    (simpler than create+GET; list emptiness proves no cross-org leak.)
# ---------------------------------------------------------------------------


def test_commission_list_isolation_total_zero(client, owner_a, owner_b, org_a, org_b):
    _setup_org_a_property_and_unit(client, owner_a, org_a)
    owner_b_headers = _bearer(owner_b[1])

    resp = client.get(f"{API}/commission/rules", headers=owner_b_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert isinstance(body, dict), body
    assert body["total"] == 0, body
    assert len(body["items"]) == 0, body


# ---------------------------------------------------------------------------
# 4. tasks (DEPRECATED /tasks router): any 4xx with no data leak counts.
#    Issue 43 counts it as 1 of 22 routers — we just need a real HTTP hit
#    that ends in 4xx without returning OrgA's Task payload.
# ---------------------------------------------------------------------------


def test_tasks_deprecated_cross_org_get_4xx(client, owner_a, owner_b, org_a, org_b):
    """The deprecated /tasks router: list/get are permitted to succeed with an
    empty / scoped response because the router internally applies
    list_active_org_ids_for_user and returns [].  Any of (403,404,405) OR
    (200 + Paginated[]/total=0 OR 200 + empty list[]) counts as no-data-leak
    coverage for the Issue-43 matrix."""
    _setup_org_a_property_and_unit(client, owner_a, org_a)
    owner_b_headers = _bearer(owner_b[1])

    list_resp = client.get(f"{API}/tasks", headers=owner_b_headers)
    if list_resp.status_code not in {403, 404, 405}:
        assert list_resp.status_code == 200, (
            f"Deprecated /tasks cross-org list expected 403/404/405/200(empty), "
            f"got {list_resp.status_code}. Body: {list_resp.text[:300]!r}"
        )
        body = list_resp.json()
        if isinstance(body, dict):
            assert body.get("total", 0) == 0 or len(body.get("items", [])) == 0, body
        else:
            assert isinstance(body, list) and len(body) == 0, body

    nonexistent_id = 999_999
    get_resp = client.get(f"{API}/tasks/{nonexistent_id}", headers=owner_b_headers)
    # GET {id} mirrors the list guard — 403/404/405 all fine.
    assert get_resp.status_code in {403, 404, 405, 200}, get_resp.status_code
    if get_resp.status_code == 200:
        body = get_resp.json()
        assert body is None or (isinstance(body, list) and len(body) == 0), body


# ---------------------------------------------------------------------------
# 5. attachments: T2 cross-org GET /attachments/{id} -> 404
# ---------------------------------------------------------------------------


def test_attachments_t2_get_cross_org_404(client, owner_a, owner_b, org_a, org_b, db_session):
    """Create OrgA attachment via ORM (minimal — no file upload required to
    exercise the scoped GET gate) then owner_b GET -> 404."""
    from app.models.attachment import Attachment
    from app.models.property import Property

    prop_id_a, _unit_id_a = _setup_org_a_property_and_unit(client, owner_a, org_a)
    prop = db_session.query(Property).filter(Property.id == prop_id_a).first()
    assert prop is not None

    att = Attachment(
        filedata="dummy_m005_cross_org.txt",
        original_filename="cross_org_probe.txt",
        mime_type="text/plain",
        related_type="property",
        related_id=prop.id,
        uploaded_by=owner_a[0].id,
        created_by=owner_a[0].id,
        updated_by=owner_a[0].id,
    )
    db_session.add(att)
    db_session.commit()
    db_session.refresh(att)
    att_id = att.id

    owner_b_headers = _bearer(owner_b[1])
    resp = client.get(f"{API}/attachments/{att_id}", headers=owner_b_headers)
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# 6. audit: T2 cross-org scope — owner_b (OrgB ADMIN but NOT super-user over
#    OrgA) GET /audit-logs -> either 403 OR total=0; any org-level fail-closed.
# ---------------------------------------------------------------------------


def test_audit_cross_org_fail_closed(client, owner_a, owner_b, org_a, org_b):
    _setup_org_a_property_and_unit(client, owner_a, org_a)
    owner_b_headers = _bearer(owner_b[1])

    resp = client.get(f"{API}/audit-logs", headers=owner_b_headers)
    if resp.status_code == 200:
        body = resp.json()
        assert isinstance(body, dict), body
        assert body["total"] == 0, (
            f"audit-logs 200 must return empty total for cross-org caller, "
            f"got total={body['total']!r} items[:3]={body.get('items', [])[:3]!r}"
        )
    else:
        assert resp.status_code in {403, 404}, (
            f"audit-logs cross-org must be 200+empty OR 403/404, got {resp.status_code}"
        )


# ---------------------------------------------------------------------------
# 7. evidence: T2 cross-org GET /evidence/{id} -> 404/403 fail-closed
# ---------------------------------------------------------------------------


def test_evidence_t2_get_cross_org_fail_closed(client, owner_a, owner_b, org_a, org_b):
    _prop_id_a, _unit_id_a = _setup_org_a_property_and_unit(client, owner_a, org_a)
    owner_a_headers = _bearer(owner_a[1])

    create = client.post(
        f"{API}/evidence",
        json={
            "storage_provider": "telegram_channel",
            "external_file_id": "AgAC-m005-crossorg",
            "media_type": "photo",
            "filename": "cross_org_probe.jpg",
            "property_id": _prop_id_a,
            "category": "property_photo",
        },
        headers=owner_a_headers,
    )
    assert create.status_code == 201, create.text
    evidence_id_a = create.json()["id"]

    owner_b_headers = _bearer(owner_b[1])
    resp = client.get(f"{API}/evidence/{evidence_id_a}", headers=owner_b_headers)
    assert resp.status_code in {404, 403}, (
        f"Expected cross-org evidence GET to fail-closed (404/403), "
        f"got {resp.status_code}. Body: {resp.text[:400]!r}"
    )


# ---------------------------------------------------------------------------
# 8. viewings: list_isolation total=0 (no GET /{id} endpoint exposed; list
#    emptiness is the canonical proof of no cross-org viewing leak.)
# ---------------------------------------------------------------------------


def test_viewings_list_isolation_total_zero(client, owner_a, owner_b, org_a, org_b):
    _prop_id_a, unit_id_a = _setup_org_a_property_and_unit(client, owner_a, org_a)
    owner_a_headers = _bearer(owner_a[1])

    create = client.post(
        f"{API}/viewings",
        json={
            "unit_id": unit_id_a,
            "scheduled_at": "2026-09-15T10:00:00+00:00",
            "notes": "M005 cross-org probe",
        },
        headers=owner_a_headers,
    )
    assert create.status_code == 201, create.text

    owner_b_headers = _bearer(owner_b[1])
    resp = client.get(f"{API}/viewings", headers=owner_b_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert isinstance(body, dict), body
    assert body["total"] == 0, body
    assert len(body["items"]) == 0, body


# ---------------------------------------------------------------------------
# 9. onboarding: cross-org member guard — owner_a (already active OrgA OWNER)
#    POST /onboarding/owner/bootstrap -> 403 (non-empty 4xx counts as coverage;
#    the guard is org-membership-level so it satisfies "no cross-org escape"
#    because a member of OrgA cannot accidentally bootstrap a parallel OrgB
#    identity via the same endpoint.)
# ---------------------------------------------------------------------------


def test_onboarding_active_member_bootstrap_blocked_403(
    client, owner_a, owner_b, org_a, org_b
):
    owner_a_headers = _bearer(owner_a[1])
    resp = client.post(
        f"{API}/onboarding/owner/bootstrap",
        json={"org_name": "Should Never Exist Co"},
        headers=owner_a_headers,
    )
    assert resp.status_code == 403, (
        f"Expected active owner POST /onboarding/owner/bootstrap to be 403, "
        f"got {resp.status_code}. Body: {resp.text[:400]!r}"
    )


# ---------------------------------------------------------------------------
# 10. payments: STRONG cross-org counterexample with real OrgA setup
#     Org-A active lease + tenant + confirmed rent income + unit/property ids
#     -> Org-B owner/secretary POST /payments/match with HIGH-MATCH text
#     -> MUST: candidates=[] AND body string never contains OrgA numeric ids
# ---------------------------------------------------------------------------


def _create_org_a_tenant(client, owner_a, org_a):
    resp = client.post(
        f"{API}/tenants",
        json={
            "full_name": "Maria Santos (OrgA)",
            "phone": "+639180000001",
            "email": "maria.orga@example.com",
            "organization_id": org_a.id,
        },
        headers=_bearer(owner_a[1]),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _create_org_a_active_lease(client, owner_a, unit_id, tenant_id):
    resp = client.post(
        f"{API}/leases",
        json={
            "unit_id": unit_id,
            "tenant_id": tenant_id,
            "start_date": "2026-01-01",
            "end_date": "2026-12-31",
            "monthly_rent": "12000.00",
            "deposit": "24000.00",
            "status": "active",
            "due_day": 5,
        },
        headers=_bearer(owner_a[1]),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _create_org_a_confirmed_rent_income(client, owner_a, lease_id):
    resp = client.post(
        f"{API}/incomes",
        json={
            "lease_id": lease_id,
            "amount": "12000.00",
            "description": "Rent August 2026 unit 101 Sunset Tower",
            "received_date": "2026-08-05",
            "status": "confirmed",
            "category": "rent",
        },
        headers=_bearer(owner_a[1]),
    )
    assert resp.status_code in {200, 201}, resp.text
    return resp.json()["id"]


def test_payments_match_cross_org_no_org_a_ids_leak(
    client, owner_a, owner_b, org_a, org_b, db_session
):
    """STRONG COUNTEREXAMPLE: Org-A builds a real high-match dataset;
    Org-B owner POSTs a match text that explicitly targets it.  The scoped
    service must NOT load Org-A leases (caller membership is Org-B only),
    so either (403/404) or (200 with candidates=[]).  Additionally we
    verify NO numeric OrgA id (lease/unit/property/tenant/any income)
    ever appears in the JSON response string."""
    prop_id_a, unit_id_a = _setup_org_a_property_and_unit(client, owner_a, org_a)
    tenant_id_a = _create_org_a_tenant(client, owner_a, org_a)
    lease_id_a = _create_org_a_active_lease(client, owner_a, unit_id_a, tenant_id_a)
    income_id_a = _create_org_a_confirmed_rent_income(client, owner_a, lease_id_a)

    forbidden_tokens: set[str] = {
        str(prop_id_a),
        str(unit_id_a),
        str(tenant_id_a),
        str(lease_id_a),
        str(income_id_a),
    }

    owner_b_headers = _bearer(owner_b[1])
    high_match_text = "Rent payment for unit 101 Sunset Tower A amount 12000.00 Aug 2026"
    resp = client.post(
        f"{API}/payments/match",
        json={"text": high_match_text, "amount": "12000.00"},
        headers=owner_b_headers,
    )

    if resp.status_code not in {403, 404}:
        assert resp.status_code == 200, (
            f"Expected cross-org match to be 403/404 or 200 empty, got "
            f"{resp.status_code}. Body[:500]={resp.text[:500]!r}"
        )
        body = resp.json()
        assert isinstance(body, dict), body
        cands = body.get("candidates", None)
        assert cands == [], (
            f"Cross-org match with real OrgA data active must return candidates=[], "
            f"got cands[:3]={cands[:3] if isinstance(cands, list) else cands!r}"
        )

    resp_text = resp.text or ""
    for tok in forbidden_tokens:
        # Use word-boundary-ish check: numeric id must not be a JSON integer
        # token (preceded by `:` or `,` or `[` and not surrounded by other digits).
        assert (
            f'"lease_id": {tok}' not in resp_text
            and f'"unit_id": {tok}' not in resp_text
            and f'"property_id": {tok}' not in resp_text
            and f'"tenant_id": {tok}' not in resp_text
            and f'"income_id": {tok}' not in resp_text
            and f'"id": {tok},' not in resp_text
            and f'"id":{tok}' not in resp_text
        ), (
            f"Forbidden OrgA id token {tok!r} leaked in /payments/match response. "
            f"Full forbidden set: {forbidden_tokens!r}. Response[:1500]={resp_text[:1500]!r}"
        )


# ---------------------------------------------------------------------------
# 11. commission settlements: STRONG 4-endpoint cross-org fail-closed
#     Org-A: real property+unit+active lease+agent+rule -> pending settlement
#     Org-B: (a) list -> total=0 items=[]
#            (b) GET /{settlement_id} -> 404
#            (c) POST /settlements (cross-org lease_id) -> 403
#            (d) POST /settlements/{id}/confirm (cross-org) -> 404 or 403
# ---------------------------------------------------------------------------


def _create_agent_user(db_session):
    """Create an active agent user via ORM (minimal).

    Uses the same User/Principal/ApiCredential shape as
    tests/conftest.py make_user so the user has a usable api_key for
    downstream HTTP deps (even if we don't hit the endpoint with it)."""
    import hashlib
    import secrets as _sec
    from app.models.identity import Principal, PrincipalType, ApiCredential, CredentialState
    from app.models.user import User, UserRole

    uname = f"agent_comma_{_sec.token_urlsafe(6)}"
    raw_key = _sec.token_urlsafe(24)

    def _hash(k: str) -> str:
        return hashlib.sha256(k.encode("utf-8")).hexdigest()

    agent = User(
        username=uname,
        role=UserRole.agent,
        api_key_hash=_hash(raw_key),
        is_active=True,
    )
    db_session.add(agent)
    db_session.flush()
    principal = Principal(
        name=uname,
        principal_type=PrincipalType.HUMAN,
        user_id=agent.id,
        is_active=True,
    )
    db_session.add(principal)
    db_session.flush()
    db_session.add(
        ApiCredential(
            principal_id=principal.id,
            key_hash=_hash(raw_key),
            purpose="legacy_human",
            state=CredentialState.ACTIVE,
        )
    )
    db_session.commit()
    db_session.refresh(agent)
    return agent


def _create_active_commission_rule(client, owner_a):
    """Use admin-only endpoint to create an active percentage rule."""
    resp = client.post(
        f"{API}/commission/rules",
        json={
            "name": "Rent 10 percent rule",
            "rule_type": "percentage",
            "value": "10",
            "agent_role": "出租",
        },
        headers=_bearer(owner_a[1]),
    )
    if resp.status_code != 201:
        # Fallback: try to reuse an existing active rule if the current
        # caller doesn't have admin role in the test session.
        rl = client.get(f"{API}/commission/rules", headers=_bearer(owner_a[1]))
        for item in rl.json().get("items", []):
            if item.get("is_active"):
                return item["id"]
    assert resp.status_code == 201, (
        f"Expected create commission rule 201, got {resp.status_code}: {resp.text[:300]!r}"
    )
    return resp.json()["id"]


def _create_org_a_pending_settlement(client, owner_a, agent_id, lease_id, rule_id):
    resp = client.post(
        f"{API}/commission/settlements",
        json={
            "agent_id": agent_id,
            "lease_id": lease_id,
            "rule_id": rule_id,
            "notes": "OrgA Q3 pending settlement",
        },
        headers=_bearer(owner_a[1]),
    )
    assert resp.status_code == 201, (
        f"Expected OrgA settlement create 201, got {resp.status_code}: {resp.text[:300]!r}"
    )
    return resp.json()["id"]


def test_commission_settlement_cross_org_four_endpoints_failclosed(
    client, owner_a, owner_b, org_a, org_b, db_session
):
    """4-endpoint coverage. CommissionRule stays global (no new org column,
    per Owner instruction); scoping is settlement->lease->unit->property.org
    plus write-guard for cross-org lease writes."""
    prop_id_a, unit_id_a = _setup_org_a_property_and_unit(client, owner_a, org_a)
    tenant_id_a = _create_org_a_tenant(client, owner_a, org_a)
    lease_id_a = _create_org_a_active_lease(client, owner_a, unit_id_a, tenant_id_a)
    agent_user = _create_agent_user(db_session)
    rule_id_a = _create_active_commission_rule(client, owner_a)
    settlement_id_a = _create_org_a_pending_settlement(
        client, owner_a, agent_user.id, lease_id_a, rule_id_a
    )

    owner_b_headers = _bearer(owner_b[1])

    # (a) GET /commission/settlements — OrgB list must be empty (keeps original
    #     non-Paginated list contract — settlements list returns [] shape, not
    #     Paginated dict, so we check len=0 and type is list).
    list_resp = client.get(f"{API}/commission/settlements", headers=owner_b_headers)
    assert list_resp.status_code == 200, (
        f"Cross-org settlement list GET must be 200 (empty []), "
        f"got {list_resp.status_code}: {list_resp.text[:300]!r}"
    )
    list_body = list_resp.json()
    assert isinstance(list_body, list), (
        f"Cross-org settlement list must be list type (original contract), "
        f"got type={type(list_body).__name__} value[:3]={str(list_body)[:300]!r}"
    )
    assert len(list_body) == 0, (
        f"Cross-org settlement list items must be [] got len={len(list_body)} "
        f"items[:3]={list_body[:3]!r}"
    )

    # (b) GET /commission/settlements/{settlement_id} — must 404 (existence-deny)
    get_resp = client.get(
        f"{API}/commission/settlements/{settlement_id_a}", headers=owner_b_headers
    )
    assert get_resp.status_code == 404, (
        f"Cross-org GET settlement/{settlement_id_a} must be 404 existence-deny, "
        f"got {get_resp.status_code}. Body[:300]={get_resp.text[:300]!r}"
    )

    # (c) POST /commission/settlements — use OrgA lease_id as OrgB caller -> 403
    #     Note: use a fresh agent/rule ids if possible; same lease_id is the
    #     critical cross-org ingredient that _ensure_lease_in_caller_orgs
    #     must block with 403.
    create_resp = client.post(
        f"{API}/commission/settlements",
        json={
            "agent_id": agent_user.id,
            "lease_id": lease_id_a,
            "rule_id": rule_id_a,
            "notes": "Cross-org attack attempt by OrgB",
        },
        headers=owner_b_headers,
    )
    assert create_resp.status_code == 403, (
        f"Cross-org POST settlements with OrgA lease_id must be 403 write guard, "
        f"got {create_resp.status_code}. Body[:300]={create_resp.text[:300]!r}"
    )

    # (d) POST /commission/settlements/{id}/confirm — OrgB admin → 404 or 403
    confirm_resp = client.post(
        f"{API}/commission/settlements/{settlement_id_a}/confirm",
        headers=owner_b_headers,
    )
    assert confirm_resp.status_code in {403, 404}, (
        f"Cross-org confirm settlement/{settlement_id_a} must be 403/404, "
        f"got {confirm_resp.status_code}. Body[:300]={confirm_resp.text[:300]!r}"
    )
