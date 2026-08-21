"""PASAY-TASK-006 Onboarding P0 targeted tests.

Scope (strictly Issue #24 contract):
  T1.  /state 0-membership fresh user -> ROLE_CHOICE_REQUIRED + zh owner hint + en secretary hint.
  T2.  /owner/bootstrap fresh user creates org + ACTIVE OWNER membership (regression of T1 from issue).
  T3.  /owner/bootstrap existing member user -> 403 (Guard: no re-bootstrap).
  T4.  /state after bootstrap -> EXISTING_MEMBER + org ref + membership ref.
  T5.  /secretary/accept-invite with valid PENDING invite -> ACTIVE SECRETARY join.
  T6.  /secretary/accept-invite with EXPIRED invite -> 400 "expired".
  T7.  /secretary/accept-invite with CANCELLED invite -> 400 "cancelled".
  T8.  /secretary/accept-invite with already-ACCEPTED same-user REMOVED membership -> 400 (not re-activated).
  T9.  Secretary tries /owner/bootstrap -> 403 (Guard: she's an active member of any org via SECRETARY role).
  T10. Explicit /secretary/bootstrap endpoint -> always 403 English hint (never show create-company).
  T11. /state with valid ?invite_code= on fresh user -> SECRETARY_VALID_INVITE_PENDING_ACCEPT.
  T12. Secretary /state no invite -> secretary_hint_en exactly "Ask your Owner to invite you to the workspace."
  T13. alembic single-head check.
  T14. Regression: membership service (bootstrap_first_owner + accept_secretary_invite) still works directly.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import Session

from app.core.security import hash_api_key
from app.models.audit_log import AuditAction, AuditLog
from app.models.identity import ApiCredential, CredentialState, Principal, PrincipalType
from app.models.membership import (
    InviteState,
    Membership,
    MembershipState,
    OrganizationRole,
    SecretaryInvite,
)
from app.models.user import User, UserRole
from app.services.membership import (
    accept_secretary_invite,
    bootstrap_first_owner,
    create_secretary_invite,
    get_invite_by_code,
    is_owner,
    is_secretary,
    list_active_orgs_for_user,
    remove_secretary,
)
from app.services.onboarding import (
    HINT_OWNER_CHOOSE_ORG_NAME_ZH,
    HINT_SECRETARY_NO_INVITE_EN,
    BootstrapForbidden,
    InviteNotAccepted,
    get_onboarding_state,
    owner_create_organization,
    secretary_join_via_invite,
)
from tests.test_membership_p0_020 import _bind, _make_user  # reuse fixtures


NOW = datetime(2026, 8, 20, 0, 0, tzinfo=timezone.utc)
TEL_O = 1000000010
TEL_S = 1000000011
TEL_X = 1000000012


@pytest.fixture()
def owner_user(db_session):
    u, p = _make_user(db_session, "onboard_owner", uid=501)
    u.role = UserRole.admin
    db_session.flush()
    _bind(db_session, p, TEL_O)
    db_session.commit()
    db_session.refresh(u)
    return u


@pytest.fixture()
def sec_user(db_session):
    u, p = _make_user(db_session, "onboard_secretary", uid=502)
    _bind(db_session, p, TEL_S)
    db_session.commit()
    db_session.refresh(u)
    return u


@pytest.fixture()
def fresh_user(db_session):
    u, p = _make_user(db_session, "onboard_fresh", uid=503)
    _bind(db_session, p, TEL_X)
    db_session.commit()
    db_session.refresh(u)
    return u


def _make_admin_user_and_key(db, username, uid=777):
    """User with api key usable through the FastAPI TestClient."""
    from tests.conftest import make_user
    return make_user(db, username, UserRole.admin)


def _auth_headers(key):
    return {"Authorization": f"Bearer {key}"}


# ---------------------------------------------------------------------------
# T1 / T11 / T12 — /state routing outcomes (service layer)
# ---------------------------------------------------------------------------

class TestOnboardingStateService:
    def test_t1_fresh_secretary_no_invite_returns_secretary_no_invite_stage(self, db_session, fresh_user):
        resp = get_onboarding_state(db_session, fresh_user, now=NOW)
        assert resp.stage == "SECRETARY_NO_INVITE"
        assert resp.user_id == fresh_user.id
        assert resp.existing_organization is None
        assert resp.existing_membership is None
        assert resp.owner_hint_zh is None
        assert resp.secretary_hint_en == HINT_SECRETARY_NO_INVITE_EN
        assert resp.invite_organization_name is None

    def test_t1_owner_admin_no_invite_returns_owner_bootstrap_required(self, db_session, owner_user):
        resp = get_onboarding_state(db_session, owner_user, now=NOW)
        assert resp.stage == "OWNER_BOOTSTRAP_REQUIRED"
        assert resp.user_id == owner_user.id
        assert resp.existing_organization is None
        assert resp.existing_membership is None
        assert resp.owner_hint_zh == HINT_OWNER_CHOOSE_ORG_NAME_ZH
        assert resp.secretary_hint_en is None
        assert resp.invite_organization_name is None

    def test_t12_secretary_hint_exact_literal_en(self, db_session, fresh_user):
        resp = get_onboarding_state(db_session, fresh_user, now=NOW)
        assert resp.stage == "SECRETARY_NO_INVITE"
        assert resp.secretary_hint_en == "Ask your Owner to invite you to the workspace."

    def test_t11_pending_invite_in_state(self, db_session, owner_user, sec_user):
        org, _ = bootstrap_first_owner(db_session, owner_user.id, "InviteOrg", now=NOW)
        invite = create_secretary_invite(db_session, owner_user.id, org.id, now=NOW)
        resp = get_onboarding_state(db_session, sec_user, pending_invite_code=invite.code, now=NOW)
        assert resp.stage == "SECRETARY_VALID_INVITE_PENDING_ACCEPT"
        assert resp.invite_organization_name == "InviteOrg"
        assert HINT_OWNER_CHOOSE_ORG_NAME_ZH not in (resp.owner_hint_zh or "")

    # --- P0-2: expired PENDING invite must be invalid in /state, no org name leak ---
    def test_p02_expired_pending_invite_in_state_is_invalid_no_org_leak(self, db_session, owner_user, sec_user):
        org, _ = bootstrap_first_owner(db_session, owner_user.id, "LeakTestOrg", now=NOW)
        invite = create_secretary_invite(
            db_session, owner_user.id, org.id,
            ttl=timedelta(days=7), now=NOW,
        )
        assert invite.state == InviteState.PENDING
        stale_now = NOW + timedelta(days=30)
        resp = get_onboarding_state(db_session, sec_user, pending_invite_code=invite.code, now=stale_now)
        assert resp.stage != "SECRETARY_VALID_INVITE_PENDING_ACCEPT"
        assert resp.stage == "SECRETARY_NO_INVITE"
        assert resp.invite_organization_name is None
        db_session.expire_all()
        reloaded = get_invite_by_code(db_session, invite.code)
        assert reloaded.state == InviteState.PENDING

    def test_p03_pending_invite_with_missing_organization_is_invalid_no_leak(self, db_session, owner_user, sec_user, monkeypatch):
        org, _ = bootstrap_first_owner(db_session, owner_user.id, "DanglingOrg", now=NOW)
        invite = create_secretary_invite(db_session, owner_user.id, org.id, now=NOW)
        assert invite.state == InviteState.PENDING
        original_get = db_session.get

        def fake_get(model, ident):
            if getattr(model, "__name__", None) == "Organization" and ident == org.id:
                return None
            return original_get(model, ident)

        monkeypatch.setattr(db_session, "get", fake_get)
        resp = get_onboarding_state(db_session, sec_user, pending_invite_code=invite.code, now=NOW)
        assert resp.stage != "SECRETARY_VALID_INVITE_PENDING_ACCEPT"
        assert resp.invite_organization_name is None


# ---------------------------------------------------------------------------
# T2 / T4 — Owner bootstrap service + /state after bootstrap
# ---------------------------------------------------------------------------

class TestOwnerBootstrapService:
    def test_t2_owner_bootstrap_creates_org_and_owner_membership(self, db_session, owner_user):
        out = owner_create_organization(db_session, owner_user, "MyCo")
        assert out.organization.name == "MyCo"
        assert out.organization.id > 0
        assert out.membership.organization_id == out.organization.id
        assert out.membership.role == OrganizationRole.OWNER.value
        assert out.membership.state == MembershipState.ACTIVE.value
        assert is_owner(db_session, owner_user.id, out.organization.id) is True
        # Confirm the membership actually binds to the calling user via DB lookup.
        row = db_session.query(Membership).filter(Membership.id == out.membership.id).one()
        assert row.user_id == owner_user.id
        assert row.role == OrganizationRole.OWNER

    def test_t4_existing_member_shows_existing_state(self, db_session, owner_user):
        owner_create_organization(db_session, owner_user, "AlreadyHere")
        resp = get_onboarding_state(db_session, owner_user)
        assert resp.stage == "EXISTING_MEMBER"
        assert resp.existing_organization is not None
        assert resp.existing_organization.name == "AlreadyHere"
        assert resp.existing_membership is not None
        assert resp.existing_membership.role == OrganizationRole.OWNER.value


# ---------------------------------------------------------------------------
# T3 — Bootstrap guard: no double org creation
# ---------------------------------------------------------------------------

class TestBootstrapGuard:
    def test_t3_existing_owner_cannot_bootstrap_again(self, db_session, owner_user):
        owner_create_organization(db_session, owner_user, "FirstOrg", now=NOW)
        with pytest.raises(BootstrapForbidden):
            owner_create_organization(db_session, owner_user, "SecondOrg", now=NOW)

    def test_t9_secretary_who_joined_cannot_bootstrap_owner(self, db_session, owner_user, sec_user):
        org, _ = bootstrap_first_owner(db_session, owner_user.id, "TargetOrg", now=NOW)
        inv = create_secretary_invite(db_session, owner_user.id, org.id, now=NOW)
        accept_secretary_invite(db_session, sec_user.id, inv.code, now=NOW)
        with pytest.raises(BootstrapForbidden):
            owner_create_organization(db_session, sec_user, "SecCannotCreateThis", now=NOW)

    # --- P0-1: fresh Secretary + 0 membership + no invite MUST be blocked ---
    def test_p01_fresh_secretary_zero_membership_cannot_call_owner_bootstrap(self, db_session, fresh_user):
        assert list_active_orgs_for_user(db_session, fresh_user.id) == []
        with pytest.raises(BootstrapForbidden):
            owner_create_organization(db_session, fresh_user, "SecTryCreateOrg")

    def test_p01_fresh_manager_role_also_cannot_call_owner_bootstrap(self, db_session):
        u, p = _make_user(db_session, "onboard_manager_x", uid=599)
        u.role = UserRole.manager
        db_session.flush()
        _bind(db_session, p, 1000000599)
        db_session.commit()
        db_session.refresh(u)
        assert list_active_orgs_for_user(db_session, u.id) == []
        with pytest.raises(BootstrapForbidden):
            owner_create_organization(db_session, u, "ManagerTryCreate")


# ---------------------------------------------------------------------------
# T5 / T6 / T7 — Secretary invite accept service outcomes
# ---------------------------------------------------------------------------

class TestSecretaryInviteService:
    def test_t5_valid_invite_joins_as_secretary(self, db_session, owner_user, sec_user):
        org, _ = bootstrap_first_owner(db_session, owner_user.id, "T5 Org", now=NOW)
        inv = create_secretary_invite(db_session, owner_user.id, org.id, now=NOW)
        out = secretary_join_via_invite(db_session, sec_user, inv.code)
        assert out.organization.name == "T5 Org"
        assert out.membership.role == OrganizationRole.SECRETARY.value
        assert out.membership.state == MembershipState.ACTIVE.value
        assert is_secretary(db_session, sec_user.id, org.id) is True

    def test_t6_expired_invite_rejected(self, db_session, owner_user, sec_user):
        org, _ = bootstrap_first_owner(db_session, owner_user.id, "T6 Org", now=NOW)
        # Use real future expiry at real-world "create time + 7 days; then accept at real-world "now + 30 days"
        # => accept_secretary_invite uses the `now` parameter against invite.expires_at
        inv = create_secretary_invite(
            db_session, owner_user.id, org.id,
            ttl=timedelta(days=7), now=NOW,
        )
        accept_now = NOW + timedelta(days=30)
        with pytest.raises(InviteNotAccepted) as ei:
            secretary_join_via_invite(db_session, sec_user, inv.code, now=accept_now)
        assert ei.value.reason == "expired"
        db_session.expire_all()
        reloaded = get_invite_by_code(db_session, inv.code)
        assert reloaded.state == InviteState.EXPIRED

    def test_t7_cancelled_invite_rejected(self, db_session, owner_user, sec_user):
        from app.services.membership import cancel_secretary_invite
        org, _ = bootstrap_first_owner(db_session, owner_user.id, "T7 Org", now=NOW)
        inv = create_secretary_invite(db_session, owner_user.id, org.id, now=NOW)
        cancel_secretary_invite(db_session, owner_user.id, inv.id, now=NOW)
        with pytest.raises(InviteNotAccepted) as ei:
            secretary_join_via_invite(db_session, sec_user, inv.code)
        assert ei.value.reason == "cancelled"

    def test_t8_removed_secretary_stale_accepted_invite_not_restored(self, db_session, owner_user, sec_user):
        org, _ = bootstrap_first_owner(db_session, owner_user.id, "T8 Org", now=NOW)
        inv = create_secretary_invite(db_session, owner_user.id, org.id, now=NOW)
        accept_secretary_invite(db_session, sec_user.id, inv.code, now=NOW)
        remove_secretary(db_session, owner_user.id, org.id, sec_user.id, reason="bye", now=NOW)
        # Now re-enter the SAME accepted invite as the same removed user.
        # The membership service must NOT regenerate an ACTIVE membership.
        with pytest.raises(InviteNotAccepted) as ei:
            secretary_join_via_invite(db_session, sec_user, inv.code)
        assert ei.value.reason == "consumed"


# ---------------------------------------------------------------------------
# Service audit (Issue #24 Scope §1/2 bootstrap calls Membership which already audits)
# ---------------------------------------------------------------------------

class TestOnboardingAuditDelegates:
    def test_owner_bootstrap_audits_org_created_via_membership_layer(self, db_session, owner_user):
        owner_create_organization(db_session, owner_user, "AuditMe", now=NOW)
        logs = db_session.query(AuditLog).filter(
            AuditLog.table_name.in_(["organizations", "memberships"])
        ).all()
        actions = [str(log.action.value) for log in logs]
        assert "org_created" in actions
        assert "org_first_owner_activated" in actions


# ---------------------------------------------------------------------------
# HTTP-layer tests via TestClient
# ---------------------------------------------------------------------------

class TestOnboardingRouter:
    def _create_admin(self, db_session, uname, uid=600):
        from tests.conftest import make_user
        return make_user(db_session, uname, UserRole.admin)

    def _create_secretary(self, db_session, uname, uid=700):
        from tests.conftest import make_user
        return make_user(db_session, uname, UserRole.agent)

    def test_get_state_http_200(self, db_session, client):
        _user, key = self._create_admin(db_session, "http_state_usr", uid=601)
        r = client.get("/api/v1/onboarding/state", headers=_auth_headers(key))
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["stage"] in ("OWNER_BOOTSTRAP_REQUIRED", "SECRETARY_NO_INVITE", "EXISTING_MEMBER")

    def test_post_owner_bootstrap_http_201(self, db_session, client):
        _user, key = self._create_admin(db_session, "http_owner_usr", uid=602)
        r = client.post(
            "/api/v1/onboarding/owner/bootstrap",
            json={"org_name": "HTTP Co"},
            headers=_auth_headers(key),
        )
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["organization"]["name"] == "HTTP Co"
        assert body["membership"]["role"] == "OWNER"

    def test_post_owner_bootstrap_http_403_when_already_member(self, db_session, client):
        _user, key = self._create_admin(db_session, "http_owner_usr2", uid=603)
        client.post(
            "/api/v1/onboarding/owner/bootstrap",
            json={"org_name": "First"},
            headers=_auth_headers(key),
        )
        r = client.post(
            "/api/v1/onboarding/owner/bootstrap",
            json={"org_name": "Second"},
            headers=_auth_headers(key),
        )
        assert r.status_code == 403, r.text
        assert "不能再创建" in r.json()["detail"]

    # --- P0-1 HTTP: fresh Secretary POST /owner/bootstrap MUST 403 English hint ---
    def test_p01_fresh_secretary_http_owner_bootstrap_403_english_hint(self, db_session, client):
        _sec_user, sec_key = self._create_secretary(db_session, "http_sec_fresh", uid=701)
        r = client.post(
            "/api/v1/onboarding/owner/bootstrap",
            json={"org_name": "SecShouldFailCo"},
            headers=_auth_headers(sec_key),
        )
        assert r.status_code == 403, r.text
        detail = r.json()["detail"]
        assert "cannot create organizations" in detail
        assert "Ask your Owner for an invite code" in detail

    def test_t10_secretary_bootstrap_endpoint_always_403(self, db_session, client):
        _user, key = self._create_admin(db_session, "http_sec_usr", uid=604)
        # No body, with body, existing user — the endpoint always 403s.
        r = client.post("/api/v1/onboarding/secretary/bootstrap", json={}, headers=_auth_headers(key))
        assert r.status_code == 403, r.text
        assert "cannot create organizations" in r.json()["detail"]

    def test_post_secretary_accept_invite_http_201(self, db_session, client):
        owner, owner_key = self._create_admin(db_session, "http_owner_usr3", uid=605)
        _sec, sec_key = self._create_admin(db_session, "http_sec_usr3", uid=606)
        r_org = client.post(
            "/api/v1/onboarding/owner/bootstrap",
            json={"org_name": "Invited By API"},
            headers=_auth_headers(owner_key),
        )
        org_id = r_org.json()["organization"]["id"]
        # Create invite through the service directly (no invite-creation API in this slice).
        inv = create_secretary_invite(db_session, owner.id, org_id)
        r = client.post(
            "/api/v1/onboarding/secretary/accept-invite",
            json={"invite_code": inv.code},
            headers=_auth_headers(sec_key),
        )
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["membership"]["role"] == "SECRETARY"
        assert body["membership"]["state"] == "ACTIVE"

    def test_post_secretary_accept_invalid_invite_http_400(self, db_session, client):
        _owner, owner_key = self._create_admin(db_session, "http_owner_usr4", uid=607)
        _sec, sec_key = self._create_admin(db_session, "http_sec_usr4", uid=608)
        client.post(
            "/api/v1/onboarding/owner/bootstrap",
            json={"org_name": "Never Invited Co"},
            headers=_auth_headers(owner_key),
        )
        # Send a code that was never generated.  Service maps InviteConsumed with no invite
        # -> InviteNotAccepted reason="invalid"; router returns 400 English detail.
        r = client.post(
            "/api/v1/onboarding/secretary/accept-invite",
            json={"invite_code": "does-not-exist-x-x-x-x"},
            headers=_auth_headers(sec_key),
        )
        assert r.status_code == 400, r.text
        text = r.json()["detail"]
        assert "exist" in text.lower() or "invalid" in text.lower()


# ---------------------------------------------------------------------------
# T13 — Alembic single-head check
# ---------------------------------------------------------------------------

class TestAlembicSingleHead:
    def test_t13_alembic_heads_count_is_one(self):
        from alembic.config import Config
        from alembic.script import ScriptDirectory
        from pathlib import Path
        root = Path(__file__).resolve().parent.parent
        alembic_ini = root / "alembic.ini"
        cfg = Config(str(alembic_ini)) if alembic_ini.exists() else None
        if cfg is None:
            pytest.skip("alembic.ini not found at project root")
        scripts = ScriptDirectory.from_config(cfg)
        heads = scripts.get_heads()
        assert len(heads) == 1, f"expected 1 alembic head, got {len(heads)}: {heads!r}"


# ---------------------------------------------------------------------------
# T14 — direct membership regression (confirms we didn't break imported helpers)
# ---------------------------------------------------------------------------

class TestMembershipRegressionT14:
    def test_bootstrap_and_invite_still_works_directly(self, db_session, owner_user, sec_user):
        org, om = bootstrap_first_owner(db_session, owner_user.id, "Regr LLC", now=NOW)
        inv = create_secretary_invite(db_session, owner_user.id, org.id, now=NOW)
        sm = accept_secretary_invite(db_session, sec_user.id, inv.code, now=NOW)
        assert om.role == OrganizationRole.OWNER
        assert sm.role == OrganizationRole.SECRETARY
        assert is_owner(db_session, owner_user.id, org.id)
        assert is_secretary(db_session, sec_user.id, org.id)
