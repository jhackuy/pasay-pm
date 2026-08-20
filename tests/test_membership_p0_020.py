"""PASAY-TASK-002 targeted tests — Organization / Membership / Secretary Invite loop.

This file intentionally covers ONLY the P0 slices listed in the Issue contract:
  1. bootstrap 幂等 + first Owner 激活
  2. invite / accept 幂等 + 一次性 + 过期
  3. remove 后权限即时失效
  4. Membership auth helpers (is_owner / is_secretary / has_active_membership)
  5. TelegramIdentityBinding → HUMAN Principal → User → Membership 解析链
  6. 现有 auth / identity regression 的最小直接相关集

Full-warehouse tests (300+ cases) are NEVER run for this slice.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.exc import IntegrityError

from app.core.security import hash_api_key
from app.models.audit_log import AuditAction, AuditLog
from app.models.identity import (
    Principal,
    PrincipalType,
    TelegramIdentityBinding,
)
from app.models.membership import (
    InviteState,
    Membership,
    MembershipState,
    Organization,
    OrganizationRole,
    SecretaryInvite,
)
from app.models.user import User, UserRole
from app.services.identity import resolve_telegram_human
from app.services.membership import (
    BootstrapBlocked,
    InviteBlocked,
    InviteConsumed,
    RemovalBlocked,
    accept_secretary_invite,
    bootstrap_first_owner,
    cancel_secretary_invite,
    create_secretary_invite,
    get_invite_by_code,
    has_active_membership,
    is_owner,
    is_secretary,
    list_active_orgs_for_user,
    remove_secretary,
    resolve_telegram_org_membership,
)


NOW = datetime(2026, 8, 20, 0, 0, tzinfo=timezone.utc)
TEL_A = 1000000001
TEL_B = 1000000002
TEL_C = 1000000003


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_user(db, username, *, uid=None, chat_id=None):
    user = User(
        username=username,
        role=UserRole.agent,
        api_key_hash=hash_api_key(f"legacy-{username}"),
        is_active=True,
        telegram_chat_id=chat_id,
    )
    if uid is not None:
        user.id = uid
    db.add(user)
    db.flush()
    principal = Principal(
        name=username,
        principal_type=PrincipalType.HUMAN,
        user_id=user.id,
        is_active=True,
    )
    db.add(principal)
    db.flush()
    return user, principal


def _bind(db, principal, external_id, *, verified=True):
    row = TelegramIdentityBinding(
        external_user_id=external_id,
        human_principal_id=principal.id,
        verified_at=NOW if verified else None,
    )
    db.add(row)
    db.flush()
    return row


@pytest.fixture()
def user_a(db_session):
    u, p = _make_user(db_session, "alice", uid=101)
    _bind(db_session, p, TEL_A)
    db_session.commit()
    db_session.refresh(u)
    db_session.refresh(p)
    return u, p


@pytest.fixture()
def user_b(db_session):
    u, p = _make_user(db_session, "bob", uid=102)
    _bind(db_session, p, TEL_B)
    db_session.commit()
    db_session.refresh(u)
    db_session.refresh(p)
    return u, p


@pytest.fixture()
def user_c(db_session):
    """Carol — bound Telegram user but never granted any membership."""
    u, p = _make_user(db_session, "carol", uid=103)
    _bind(db_session, p, TEL_C)
    db_session.commit()
    db_session.refresh(u)
    db_session.refresh(p)
    return u, p


# ---------------------------------------------------------------------------
# 1. Bootstrap — first Owner creates an organization
# ---------------------------------------------------------------------------

class TestBootstrap:
    def test_first_owner_bootstrap_creates_org_and_membership(self, db_session, user_a):
        alice, _ = user_a
        org, m = bootstrap_first_owner(db_session, alice.id, "Acme Properties")
        assert org.id > 0
        assert org.name == "Acme Properties"
        assert m.organization_id == org.id
        assert m.user_id == alice.id
        assert m.role == OrganizationRole.OWNER
        assert m.state == MembershipState.ACTIVE
        assert m.removed_at is None
        assert is_owner(db_session, alice.id, org.id) is True
        assert is_secretary(db_session, alice.id, org.id) is False

    def test_bootstrap_audits_org_created_and_owner_activated(self, db_session, user_a):
        alice, _ = user_a
        org, m = bootstrap_first_owner(db_session, alice.id, "Beta Homes")
        logs = (
            db_session.query(AuditLog)
            .filter(AuditLog.table_name.in_(["organizations", "memberships"]))
            .order_by(AuditLog.id.asc())
            .all()
        )
        actions = [str(l.action.value) for l in logs]
        assert "org_created" in actions
        assert "org_first_owner_activated" in actions
        # org_created audit must reference the new org row
        org_audit = next(
            l for l in logs if l.action == AuditAction.org_created
        )
        assert org_audit.record_id == org.id
        owner_audit = next(
            l for l in logs if l.action == AuditAction.org_first_owner_activated
        )
        assert owner_audit.record_id == m.id

    def test_bootstrap_rejects_empty_org_name(self, db_session, user_a):
        alice, _ = user_a
        with pytest.raises(ValueError):
            bootstrap_first_owner(db_session, alice.id, "   ")

    def test_bootstrap_rejects_inactive_user(self, db_session):
        ghost = User(
            username="ghost",
            role=UserRole.agent,
            api_key_hash=hash_api_key("legacy-ghost"),
            is_active=False,
        )
        db_session.add(ghost)
        db_session.commit()
        db_session.refresh(ghost)
        with pytest.raises(LookupError):
            bootstrap_first_owner(db_session, ghost.id, "Ghost LLC")

    def test_bootstrap_rejects_nonexistent_user(self, db_session):
        with pytest.raises(LookupError):
            bootstrap_first_owner(db_session, 999999, "Nope Inc")

    def test_bootstrap_is_idempotent_for_same_org_name(self, db_session, user_a):
        alice, _ = user_a
        org1, m1 = bootstrap_first_owner(db_session, alice.id, "IdemCo")
        org2, m2 = bootstrap_first_owner(db_session, alice.id, "idemco")  # case-insensitive
        assert org2.id == org1.id
        assert m2.id == m1.id
        # No duplicate org / membership created
        assert db_session.query(Organization).count() == 1
        assert db_session.query(Membership).count() == 1

    def test_bootstrap_refuses_second_org_for_existing_owner(self, db_session, user_a):
        alice, _ = user_a
        bootstrap_first_owner(db_session, alice.id, "First Co")
        with pytest.raises(BootstrapBlocked):
            bootstrap_first_owner(db_session, alice.id, "Second Co")

    def test_bootstrap_refuses_if_user_is_already_a_secretary(self, db_session, user_a, user_b):
        alice, _ = user_a
        bob, _ = user_b
        org, owner_m = bootstrap_first_owner(db_session, alice.id, "Host Corp")
        invite = create_secretary_invite(db_session, alice.id, org.id)
        accept_secretary_invite(db_session, bob.id, invite.code)
        # Bob now has an ACTIVE SECRETARY membership. A second bootstrap must fail.
        with pytest.raises(BootstrapBlocked):
            bootstrap_first_owner(db_session, bob.id, "Bob's New Org")

    def test_bootstrap_returns_existing_org_for_duplicate_name(self, db_session, user_a):
        alice, _ = user_a
        org1, m1 = bootstrap_first_owner(db_session, alice.id, "  WhitespaceCo  ")
        org2, m2 = bootstrap_first_owner(db_session, alice.id, "WhitespaceCo")
        assert org2.id == org1.id
        assert m2.id == m1.id


# ---------------------------------------------------------------------------
# 2. Secretary Invite + Accept
# ---------------------------------------------------------------------------

class TestInviteAccept:
    def _bootstrap(self, db, user_a):
        alice, _ = user_a
        return bootstrap_first_owner(db, alice.id, "InviteLand")

    def test_owner_can_create_pending_invite(self, db_session, user_a):
        org, _ = self._bootstrap(db_session, user_a)
        alice, _ = user_a
        invite = create_secretary_invite(
            db_session, alice.id, org.id,
            invited_name_hint="Assistant Bob", note="Welcome!",
        )
        assert invite.code and len(invite.code) >= 20
        assert invite.state == InviteState.PENDING
        assert invite.organization_id == org.id
        assert invite.invited_name_hint == "Assistant Bob"
        assert invite.note == "Welcome!"
        assert invite.expires_at > NOW
        # Audit produced
        logs = db_session.query(AuditLog).filter(
            AuditLog.table_name == "secretary_invites",
            AuditLog.action == AuditAction.secretary_invited,
        ).all()
        assert len(logs) == 1
        assert logs[0].record_id == invite.id

    def test_invite_codes_are_unique(self, db_session, user_a):
        org, _ = self._bootstrap(db_session, user_a)
        alice, _ = user_a
        codes = {create_secretary_invite(db_session, alice.id, org.id).code for _ in range(5)}
        assert len(codes) == 5

    def test_invite_rejects_custom_expiry_in_the_past(self, db_session, user_a):
        org, _ = self._bootstrap(db_session, user_a)
        alice, _ = user_a
        with pytest.raises(ValueError):
            create_secretary_invite(
                db_session, alice.id, org.id,
                expires_at=NOW - timedelta(days=1),
            )

    def test_non_owner_cannot_create_invite(self, db_session, user_a, user_b):
        org, _ = self._bootstrap(db_session, user_a)
        alice, _ = user_a
        bob, _ = user_b
        invite = create_secretary_invite(db_session, alice.id, org.id)
        accept_secretary_invite(db_session, bob.id, invite.code)
        # Bob is now SECRETARY — a Secretary must NOT be allowed to create invites.
        with pytest.raises(InviteBlocked):
            create_secretary_invite(db_session, bob.id, org.id)

    def test_stranger_cannot_create_invite(self, db_session, user_a, user_c):
        org, _ = self._bootstrap(db_session, user_a)
        carol, _ = user_c  # no membership at all
        with pytest.raises(InviteBlocked):
            create_secretary_invite(db_session, carol.id, org.id)

    def test_bob_accepts_invite_becomes_secretary(self, db_session, user_a, user_b):
        org, _ = self._bootstrap(db_session, user_a)
        alice, _ = user_a
        bob, _ = user_b
        invite = create_secretary_invite(db_session, alice.id, org.id)
        m = accept_secretary_invite(db_session, bob.id, invite.code)
        assert m.organization_id == org.id
        assert m.user_id == bob.id
        assert m.role == OrganizationRole.SECRETARY
        assert m.state == MembershipState.ACTIVE
        assert is_secretary(db_session, bob.id, org.id) is True
        assert is_owner(db_session, bob.id, org.id) is False
        # Invite advanced to ACCEPTED
        db_session.refresh(invite)
        assert invite.state == InviteState.ACCEPTED
        assert invite.accepted_by_user_id == bob.id
        assert invite.created_membership_id == m.id
        # Audit for invite_accepted present on both invite and membership tables
        invite_log = db_session.query(AuditLog).filter(
            AuditLog.table_name == "secretary_invites",
            AuditLog.record_id == invite.id,
            AuditLog.action == AuditAction.secretary_invite_accepted,
        ).one()
        assert invite_log.actor_id == bob.id
        member_log = db_session.query(AuditLog).filter(
            AuditLog.table_name == "memberships",
            AuditLog.record_id == m.id,
            AuditLog.action == AuditAction.secretary_invite_accepted,
        ).one()
        assert member_log.actor_id == bob.id

    def test_accept_is_idempotent_for_same_user(self, db_session, user_a, user_b):
        org, _ = self._bootstrap(db_session, user_a)
        alice, _ = user_a
        bob, _ = user_b
        invite = create_secretary_invite(db_session, alice.id, org.id)
        m1 = accept_secretary_invite(db_session, bob.id, invite.code)
        m2 = accept_secretary_invite(db_session, bob.id, invite.code)
        assert m2.id == m1.id  # exact same Membership returned
        assert db_session.query(Membership).count() == 2  # OWNER + 1 SECRETARY

    def test_accept_rejects_second_user_trying_to_reuse_code(self, db_session, user_a, user_b, user_c):
        org, _ = self._bootstrap(db_session, user_a)
        alice, _ = user_a
        bob, _ = user_b
        carol, _ = user_c
        invite = create_secretary_invite(db_session, alice.id, org.id)
        accept_secretary_invite(db_session, bob.id, invite.code)
        with pytest.raises(InviteConsumed):
            accept_secretary_invite(db_session, carol.id, invite.code)

    def test_accept_rejects_cancelled_invite(self, db_session, user_a, user_b):
        org, _ = self._bootstrap(db_session, user_a)
        alice, _ = user_a
        bob, _ = user_b
        invite = create_secretary_invite(db_session, alice.id, org.id)
        cancel_secretary_invite(db_session, alice.id, invite.id)
        with pytest.raises(InviteConsumed):
            accept_secretary_invite(db_session, bob.id, invite.code)

    def test_accept_rejects_expired_invite(self, db_session, user_a, user_b):
        org, _ = self._bootstrap(db_session, user_a)
        alice, _ = user_a
        bob, _ = user_b
        # Create a valid short-lived invite. expires_at = actual DB created_at + 1 min.
        invite = create_secretary_invite(
            db_session, alice.id, org.id,
            ttl=timedelta(minutes=1),
        )
        # Time-travel the accept call to AFTER expiry — expired check must flip state -> EXPIRED.
        future_now = invite.expires_at + timedelta(seconds=10)
        with pytest.raises(InviteConsumed):
            accept_secretary_invite(db_session, bob.id, invite.code, now=future_now)
        db_session.refresh(invite)
        assert invite.state == InviteState.EXPIRED

    def test_accept_unknown_code_raises_consumed(self, db_session, user_b):
        bob, _ = user_b
        with pytest.raises(InviteConsumed):
            accept_secretary_invite(db_session, bob.id, "does-not-exist-xyz")

    def test_accept_blocked_if_user_already_member(self, db_session, user_a, user_b):
        org, _ = self._bootstrap(db_session, user_a)
        alice, _ = user_a
        bob, _ = user_b
        invite1 = create_secretary_invite(db_session, alice.id, org.id)
        accept_secretary_invite(db_session, bob.id, invite1.code)
        # A second invite for the same org + user should fail on accept (not on invite creation).
        invite2 = create_secretary_invite(db_session, alice.id, org.id)
        with pytest.raises(BootstrapBlocked):
            accept_secretary_invite(db_session, bob.id, invite2.code)

    def test_accept_rejects_inactive_user(self, db_session, user_a):
        org, _ = self._bootstrap(db_session, user_a)
        alice, _ = user_a
        sleepy = User(
            username="sleepy", role=UserRole.agent,
            api_key_hash=hash_api_key("legacy-sleepy"), is_active=False,
        )
        db_session.add(sleepy)
        db_session.commit()
        db_session.refresh(sleepy)
        invite = create_secretary_invite(db_session, alice.id, org.id)
        with pytest.raises(LookupError):
            accept_secretary_invite(db_session, sleepy.id, invite.code)


# ---------------------------------------------------------------------------
# 3. Secretary removal (软删 REMOVED + 权限即时失效)
# ---------------------------------------------------------------------------

class TestSecretaryRemoval:
    def _scenario(self, db_session, user_a, user_b):
        alice, _ = user_a
        bob, _ = user_b
        org, _ = bootstrap_first_owner(db_session, alice.id, "RemovalTest Inc")
        invite = create_secretary_invite(db_session, alice.id, org.id)
        secretary = accept_secretary_invite(db_session, bob.id, invite.code)
        return org, alice, bob, secretary

    def test_owner_can_remove_secretary_and_removed_state_is_auditably_set(self, db_session, user_a, user_b):
        org, alice, bob, m_before = self._scenario(db_session, user_a, user_b)
        assert is_secretary(db_session, bob.id, org.id) is True
        m_after = remove_secretary(
            db_session, alice.id, org.id, bob.id, reason="Contract ended",
        )
        assert m_after.id == m_before.id
        assert m_after.state == MembershipState.REMOVED
        assert m_after.removed_at is not None
        assert m_after.removal_reason == "Contract ended"
        # Authority immediately revoked via Membership truth table
        assert is_secretary(db_session, bob.id, org.id) is False
        assert has_active_membership(db_session, bob.id, org.id) is None
        # Audit logged once, with precise changed_fields
        log = db_session.query(AuditLog).filter(
            AuditLog.table_name == "memberships",
            AuditLog.record_id == m_after.id,
            AuditLog.action == AuditAction.secretary_removed,
        ).one()
        assert log.actor_id == alice.id
        assert log.changed_fields and "state" in log.changed_fields
        assert log.changed_fields["state"] == ["ACTIVE", "REMOVED"]

    def test_secretary_removal_is_idempotent(self, db_session, user_a, user_b):
        org, alice, bob, _ = self._scenario(db_session, user_a, user_b)
        first = remove_secretary(db_session, alice.id, org.id, bob.id, reason="r1")
        second = remove_secretary(db_session, alice.id, org.id, bob.id, reason="r2")
        assert second.id == first.id
        # The first removal reason/who wins; the second call short-circuits.
        assert second.removal_reason == "r1"
        assert db_session.query(AuditLog).filter(
            AuditLog.action == AuditAction.secretary_removed
        ).count() == 1

    def test_removed_secretary_cannot_perform_business_actions_via_membership(self, db_session, user_a, user_b):
        org, alice, bob, _ = self._scenario(db_session, user_a, user_b)
        # Before removal, bob can create an invite (well, no — only OWNER can).
        # Instead, verify the auth helper truth: bob's permission check before/after.
        assert is_secretary(db_session, bob.id, org.id) is True
        remove_secretary(db_session, alice.id, org.id, bob.id)
        # Any future action that relies on `is_secretary` / `has_active_membership`
        # must immediately fail. This test is the AC for "权限即时失效".
        assert is_secretary(db_session, bob.id, org.id) is False
        assert has_active_membership(db_session, bob.id, org.id) is None
        active = list_active_orgs_for_user(db_session, bob.id)
        assert active == []
        # And explicitly confirm: Secretary, once removed, cannot invite (InviteBlocked
        # should fire because Bob is no longer an OWNER in this org).
        with pytest.raises(InviteBlocked):
            create_secretary_invite(db_session, bob.id, org.id)

    def test_secretary_cannot_remove_owner(self, db_session, user_a, user_b):
        org, alice, bob, _ = self._scenario(db_session, user_a, user_b)
        with pytest.raises(RemovalBlocked):
            remove_secretary(db_session, bob.id, org.id, alice.id)

    def test_owner_cannot_use_remove_secretary_to_demote_self(self, db_session, user_a):
        alice, _ = user_a
        org, _ = bootstrap_first_owner(db_session, alice.id, "DemoteTest")
        # The helper's role check says target must be SECRETARY. Alice is OWNER.
        with pytest.raises(RemovalBlocked):
            remove_secretary(db_session, alice.id, org.id, alice.id)

    def test_stranger_cannot_remove_secretary(self, db_session, user_a, user_b, user_c):
        org, alice, bob, _ = self._scenario(db_session, user_a, user_b)
        carol, _ = user_c
        with pytest.raises(RemovalBlocked):
            remove_secretary(db_session, carol.id, org.id, bob.id)

    def test_nonexistent_member_raises_lookup(self, db_session, user_a, user_c):
        alice, _ = user_a
        carol, _ = user_c
        org, _ = bootstrap_first_owner(db_session, alice.id, "Lonely Corp")
        with pytest.raises(LookupError):
            remove_secretary(db_session, alice.id, org.id, carol.id)


# ---------------------------------------------------------------------------
# 4. Membership auth helpers + model constraints
# ---------------------------------------------------------------------------

class TestAuthHelpersAndConstraints:
    def test_has_active_membership_invalid_inputs_return_none(self, db_session):
        assert has_active_membership(db_session, 0, 1) is None
        assert has_active_membership(db_session, -5, 1) is None
        assert has_active_membership(db_session, 1, 0) is None
        assert has_active_membership(db_session, 1, -1) is None
        assert has_active_membership(db_session, None, 1) is None  # type: ignore[arg-type]

    def test_db_enforces_single_active_membership_per_org_user(self, db_session, user_a, user_b):
        alice, _ = user_a
        bob, _ = user_b
        org, _ = bootstrap_first_owner(db_session, alice.id, "ConstraintLand")
        invite = create_secretary_invite(db_session, alice.id, org.id)
        accept_secretary_invite(db_session, bob.id, invite.code)
        # Attempting to INSERT a second ACTIVE row via raw SQLAlchemy should
        # trip the partial unique index. This validates the "不允许同一用户
        # 在同一组织多个 ACTIVE Membership" contract at the database layer.
        duplicate = Membership(
            organization_id=org.id, user_id=bob.id,
            role=OrganizationRole.SECRETARY, state=MembershipState.ACTIVE,
        )
        db_session.add(duplicate)
        with pytest.raises(IntegrityError):
            db_session.flush()
        db_session.rollback()

    def test_membership_state_removed_at_allowed_values(self, db_session, user_a, user_b):
        alice, _ = user_a
        bob, _ = user_b
        org, _ = bootstrap_first_owner(db_session, alice.id, "RemovalStateLand")
        invite = create_secretary_invite(db_session, alice.id, org.id)
        accept_secretary_invite(db_session, bob.id, invite.code)
        # ACTIVE rows may NOT have removed_at set. Bypass the helper to force a
        # direct INSERT, ensuring the CHECK constraint catches the invalid row.
        bad_active = Membership(
            organization_id=org.id, user_id=alice.id + 9000,
            role=OrganizationRole.SECRETARY, state=MembershipState.ACTIVE,
            removed_at=NOW,
        )
        # Need to create the user first for FK.
        u = User(
            username="badrow", role=UserRole.agent,
            api_key_hash=hash_api_key("legacy-badrow"), is_active=True,
        )
        db_session.add(u)
        db_session.flush()
        bad_active.user_id = u.id
        db_session.add(bad_active)
        with pytest.raises(IntegrityError):
            db_session.flush()
        db_session.rollback()


# ---------------------------------------------------------------------------
# 5. Canonical Telegram → Membership 解析链
# ---------------------------------------------------------------------------

class TestTelegramMembershipChain:
    def test_telegram_resolve_chain_returns_triple_for_owner(self, db_session, user_a):
        alice, _ = user_a
        org, _ = bootstrap_first_owner(db_session, alice.id, "TeleCo")
        u, p, m = resolve_telegram_org_membership(db_session, TEL_A, org.id)
        assert u.id == alice.id
        assert p.principal_type == PrincipalType.HUMAN
        assert p.user_id == alice.id
        assert m.role == OrganizationRole.OWNER
        assert m.state == MembershipState.ACTIVE

    def test_telegram_resolve_chain_returns_triple_for_secretary(self, db_session, user_a, user_b):
        alice, _ = user_a
        bob, _ = user_b
        org, _ = bootstrap_first_owner(db_session, alice.id, "TeleSecretary")
        invite = create_secretary_invite(db_session, alice.id, org.id)
        accept_secretary_invite(db_session, bob.id, invite.code)
        u, p, m = resolve_telegram_org_membership(
            db_session, TEL_B, org.id, role=OrganizationRole.SECRETARY,
        )
        assert u.id == bob.id
        assert m.role == OrganizationRole.SECRETARY

    def test_telegram_resolve_chain_role_filter_rejects_wrong_role(self, db_session, user_a):
        alice, _ = user_a
        org, _ = bootstrap_first_owner(db_session, alice.id, "RoleFilter")
        # Alice is OWNER; asking for SECRETARY role should fail.
        with pytest.raises(LookupError):
            resolve_telegram_org_membership(
                db_session, TEL_A, org.id, role=OrganizationRole.SECRETARY,
            )

    def test_telegram_bound_but_no_membership_raises_lookup(self, db_session, user_a, user_c):
        alice, _ = user_a
        carol, _ = user_c
        org, _ = bootstrap_first_owner(db_session, alice.id, "Exclusion Co")
        # Carol has a valid TelegramIdentityBinding → User but ZERO memberships.
        with pytest.raises(LookupError) as exc:
            resolve_telegram_org_membership(db_session, TEL_C, org.id)
        assert "no ACTIVE" in str(exc.value)

    def test_removed_secretary_telegram_resolve_fails_immediately(self, db_session, user_a, user_b):
        alice, _ = user_a
        bob, _ = user_b
        org, _ = bootstrap_first_owner(db_session, alice.id, "TeleRemoval")
        invite = create_secretary_invite(db_session, alice.id, org.id)
        accept_secretary_invite(db_session, bob.id, invite.code)
        # Precondition: chain resolves OK before removal.
        resolve_telegram_org_membership(db_session, TEL_B, org.id)
        remove_secretary(db_session, alice.id, org.id, bob.id)
        # After removal, the chain must fail with LookupError.
        with pytest.raises(LookupError):
            resolve_telegram_org_membership(db_session, TEL_B, org.id)

    def test_upstream_identity_resolver_still_works_regression(self, db_session, user_a):
        """Existing auth identity regression: resolve_telegram_human untouched."""
        alice, _ = user_a
        u, p = resolve_telegram_human(db_session, TEL_A)
        assert u.id == alice.id
        assert p.principal_type == PrincipalType.HUMAN


# ---------------------------------------------------------------------------
# 6. 真实路径端到端 (Happy path — 必测真实路径 1-6)
# ---------------------------------------------------------------------------

class TestHappyPathEndToEnd:
    def test_full_lifecycle_a_bootstrap_b_invite_b_accept_b_identified_a_remove_b_gone(self, db_session, user_a, user_b):
        # 1) User A (无组织) → 创建 Organization X → A 成为 OWNER
        alice, _ = user_a
        bob, _ = user_b
        org, owner_m = bootstrap_first_owner(db_session, alice.id, "FullCycle Co")

        # 2) A → 创建 Secretary invite
        invite = create_secretary_invite(db_session, alice.id, org.id)
        assert invite.state == InviteState.PENDING

        # 3) B → 接受 invite → B 成为 X 的 SECRETARY
        sec_m = accept_secretary_invite(db_session, bob.id, invite.code)

        # 4) B 可通过 membership helper 被识别为 ACTIVE SECRETARY
        found = has_active_membership(
            db_session, bob.id, org.id, role=OrganizationRole.SECRETARY,
        )
        assert found is not None
        assert found.id == sec_m.id

        # 5) A → 移除 B
        removed = remove_secretary(db_session, alice.id, org.id, bob.id)
        assert removed.state == MembershipState.REMOVED

        # 6) B 立即不再拥有 X 的 Secretary 权限
        assert is_secretary(db_session, bob.id, org.id) is False
        with pytest.raises(LookupError):
            resolve_telegram_org_membership(db_session, TEL_B, org.id)
