from datetime import datetime, timezone

import pytest
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.api.deps import get_current_user
from app.core.security import hash_api_key
from app.models.audit_log import AuditLog
from app.models.identity import (
    ApiCredential,
    CommunicationEndpoint,
    CredentialLifecycle,
    CredentialState,
    Principal,
    PrincipalType,
    SecurityEvent,
    TelegramIdentityBinding,
)
from app.models.user import User, UserRole
from app.services.audit import audit_context, record_audit
from app.services.identity import (
    MAX_TELEGRAM_USER_ID,
    eligible_human,
    normalize_telegram_user_id,
    resolve_telegram_destination,
    resolve_telegram_human,
)


NOW = datetime(2026, 8, 12, 0, 0, tzinfo=timezone.utc)
API = "/api/v1"


def human(db, username="alice", user_id=None, *, key=None, chat_id=None, principal_active=True):
    raw_key = key or f"legacy-{username}"
    user = User(
        username=username,
        role=UserRole.admin,
        api_key_hash=hash_api_key(raw_key),
        is_active=True,
        telegram_chat_id=chat_id,
    )
    if user_id is not None:
        user.id = user_id
    db.add(user)
    db.flush()
    principal = Principal(
        name=username,
        principal_type=PrincipalType.HUMAN,
        user_id=user.id,
        is_active=principal_active,
    )
    db.add(principal)
    db.flush()
    return user, principal, raw_key


def credential(db, principal, raw_key, *, purpose="legacy_human"):
    row = ApiCredential(
        principal_id=principal.id,
        key_hash=hash_api_key(raw_key),
        purpose=purpose,
        state=CredentialState.ACTIVE,
    )
    db.add(row)
    db.flush()
    return row


def native_bot(db, raw_key="native-bot-test-key"):
    principal = Principal(name="native-bot", principal_type=PrincipalType.SERVICE)
    db.add(principal)
    db.flush()
    return principal, credential(db, principal, raw_key, purpose="telegram_bot"), raw_key


def bind(db, principal, external_user_id, *, verified=True):
    row = TelegramIdentityBinding(
        external_user_id=external_user_id,
        human_principal_id=principal.id,
        verified_at=NOW if verified else None,
    )
    db.add(row)
    db.flush()
    return row


def endpoint(db, principal, destination, *, verified=True, active=True, revoked=False):
    row = CommunicationEndpoint(
        human_principal_id=principal.id,
        channel="telegram",
        destination=destination,
        verified_at=NOW if verified else None,
        revoked_at=NOW if revoked else None,
        is_active=active,
    )
    db.add(row)
    db.flush()
    return row


def auth(client, raw_key, telegram_user_id=None, **headers):
    request_headers = {"Authorization": f"Bearer {raw_key}", **headers}
    if telegram_user_id is not None:
        request_headers["X-Telegram-User-Id"] = str(telegram_user_id)
    return client.post(f"{API}/auth", headers=request_headers)


@pytest.mark.parametrize(
    "value",
    [True, 0, -1, MAX_TELEGRAM_USER_ID + 1, "", " 123", "+123", "00123", "１２３", "-100123"],
)
def test_telegram_user_id_normalization_rejects_noncanonical_values(value):
    with pytest.raises(LookupError):
        normalize_telegram_user_id(value)


def test_unknown_identity_and_group_chat_id_are_not_human_identity(db_session):
    user, principal, _ = human(db_session)
    bind(db_session, principal, 123)
    db_session.commit()
    assert normalize_telegram_user_id("123") == 123
    assert resolve_telegram_human(db_session, 123)[0].id == user.id
    with pytest.raises(LookupError):
        resolve_telegram_human(db_session, -100123)
    with pytest.raises(LookupError):
        resolve_telegram_human(db_session, 999)


def test_active_binding_uniqueness_is_enforced_by_database(db_session):
    _, first, _ = human(db_session, "binding-a")
    _, second, _ = human(db_session, "binding-b")
    bind(db_session, first, 101)
    db_session.commit()

    with pytest.raises(IntegrityError):
        bind(db_session, second, 101)
    db_session.rollback()
    with pytest.raises(IntegrityError):
        bind(db_session, first, 202)
    db_session.rollback()
    assert db_session.query(TelegramIdentityBinding).count() == 1


def test_unverified_revoked_and_inactive_bindings_are_rejected(db_session):
    user, principal, _ = human(db_session)
    row = bind(db_session, principal, 123, verified=False)
    db_session.commit()
    with pytest.raises(LookupError):
        resolve_telegram_human(db_session, 123)
    row.verified_at = NOW
    row.revoked_at = NOW
    db_session.commit()
    with pytest.raises(LookupError):
        resolve_telegram_human(db_session, 123)
    row.revoked_at = None
    row.is_active = False
    db_session.commit()
    with pytest.raises(LookupError):
        resolve_telegram_human(db_session, 123)
    assert user.is_active


def test_native_bot_delegates_only_verified_effective_user_not_chat_context(client, db_session):
    alice, alice_principal, _ = human(db_session, "tg-alice")
    bob, bob_principal, _ = human(db_session, "tg-bob")
    bind(db_session, alice_principal, 111)
    bind(db_session, bob_principal, 222)
    endpoint(db_session, alice_principal, "-100777")
    endpoint(db_session, bob_principal, "-100777")
    _, _, bot_key = native_bot(db_session)
    db_session.commit()

    assert auth(client, bot_key).status_code == 401
    assert auth(client, bot_key, 999).status_code == 401
    first = auth(client, bot_key, 111, **{"X-Telegram-Chat-Id": "-100777"})
    second = auth(client, bot_key, 222, **{"X-Telegram-Chat-Id": "-100777"})
    assert first.status_code == second.status_code == 200
    assert first.json()["id"] == alice.id
    assert second.json()["id"] == bob.id
    assert resolve_telegram_destination(db_session, alice.id) == "-100777"
    assert resolve_telegram_destination(db_session, bob.id) == "-100777"


def test_only_native_bot_service_may_present_telegram_subject(client, db_session):
    user, human_principal, _ = human(db_session)
    bind(db_session, human_principal, 123)
    other_service = Principal(name="other-service", principal_type=PrincipalType.SERVICE)
    db_session.add(other_service)
    db_session.flush()
    credential(db_session, other_service, "other-service-key", purpose="telegram_bot")
    db_session.commit()
    response = auth(client, "other-service-key", 123)
    assert response.status_code == 401
    assert response.json()["detail"]
    assert user.is_active


def test_direct_human_credentials_cannot_delegate_and_inactive_principal_fails(client, db_session):
    user, principal, raw_key = human(db_session, "direct-human")
    row = credential(db_session, principal, raw_key)
    db_session.commit()
    assert auth(client, raw_key).json()["id"] == user.id
    assert auth(client, raw_key, 999).status_code == 401
    principal.is_active = False
    db_session.commit()
    assert auth(client, raw_key).status_code == 401
    principal.is_active = True
    row.state = CredentialState.REVOKED
    row.revoked_at = NOW
    db_session.commit()
    assert auth(client, raw_key).status_code == 401


def test_dual_read_is_consistent_and_revocation_never_falls_back(client, db_session):
    user, principal, raw_key = human(db_session, "dual-read")
    db_session.commit()
    response = auth(client, raw_key)
    assert response.status_code == 200 and response.json()["id"] == user.id
    resolved = get_current_user(
        credentials=HTTPAuthorizationCredentials(scheme="Bearer", credentials=raw_key),
        x_telegram_user_id=None,
        db=db_session,
    )
    assert resolved.id == user.id
    assert audit_context.get() == (principal.id, principal.id, None, "api")
    assert auth(client, raw_key, 123).status_code == 401

    row = credential(db_session, principal, raw_key)
    db_session.commit()
    response = auth(client, raw_key)
    assert response.status_code == 200 and response.json()["id"] == user.id
    resolved = get_current_user(
        credentials=HTTPAuthorizationCredentials(scheme="Bearer", credentials=raw_key),
        x_telegram_user_id=None,
        db=db_session,
    )
    assert resolved.id == user.id
    assert audit_context.get() == (principal.id, principal.id, row.id, "api")
    row.state = CredentialState.REVOKED
    row.revoked_at = NOW
    db_session.commit()
    assert auth(client, raw_key).status_code == 401


def test_legacy_key_rejects_existing_inactive_human_principal(client, db_session):
    _, principal, raw_key = human(db_session, "legacy-inactive", principal_active=False)
    db_session.commit()
    assert auth(client, raw_key).status_code == 401
    assert principal.is_active is False


def test_legacy_key_without_canonical_human_principal_fails_closed(
    client, db_session
):
    raw_key = "missing-canonical-principal"
    db_session.add(User(
        username="missing-principal",
        role=UserRole.admin,
        api_key_hash=hash_api_key(raw_key),
        is_active=True,
    ))
    db_session.commit()
    assert auth(client, raw_key).status_code == 401


def test_maria_and_user_14_cannot_authenticate_bind_or_route(client, db_session):
    legacy_service = User(
        id=14,
        username="legacy-secretary-user",
        role=UserRole.admin,
        api_key_hash=hash_api_key("legacy-service-key"),
        is_active=True,
        telegram_chat_id="legacy-14-destination",
    )
    maria = User(
        username="MaRiA",
        role=UserRole.admin,
        api_key_hash=hash_api_key("maria-key"),
        is_active=True,
        telegram_chat_id="maria-destination",
    )
    db_session.add_all([legacy_service, maria])
    db_session.flush()
    service_principal = Principal(
        name="legacy-secretary",
        principal_type=PrincipalType.SERVICE,
        user_id=legacy_service.id,
    )
    maria_principal = Principal(
        name="maria-test-human",
        principal_type=PrincipalType.HUMAN,
        user_id=maria.id,
    )
    db_session.add_all([service_principal, maria_principal])
    db_session.flush()
    bind(db_session, maria_principal, 333)
    db_session.commit()

    assert not eligible_human(legacy_service)
    assert not eligible_human(maria)
    assert auth(client, "legacy-service-key").status_code == 401
    assert auth(client, "maria-key").status_code == 401
    with pytest.raises(LookupError):
        resolve_telegram_human(db_session, 333)
    with pytest.raises(LookupError):
        resolve_telegram_destination(db_session, legacy_service.id)
    with pytest.raises(LookupError):
        resolve_telegram_destination(db_session, maria.id)


def test_legacy_destination_fallback_only_without_endpoint_history(db_session):
    user, principal, _ = human(db_session, "legacy-destination", chat_id="legacy-chat")
    db_session.commit()
    assert resolve_telegram_destination(db_session, user.id) == "legacy-chat"

    endpoint(db_session, principal, "unverified", verified=False)
    db_session.commit()
    with pytest.raises(LookupError):
        resolve_telegram_destination(db_session, user.id)
    db_session.expire_all()
    assert db_session.query(SecurityEvent).filter_by(user_id=user.id).count() == 1


def test_destination_missing_duplicate_revoked_and_inactive_fail_closed(db_session):
    missing_user, _, _ = human(db_session, "destination-missing")
    duplicate_user, duplicate_principal, _ = human(db_session, "destination-duplicate")
    endpoint(db_session, duplicate_principal, "one")
    endpoint(db_session, duplicate_principal, "two")
    revoked_user, revoked_principal, _ = human(db_session, "destination-revoked")
    endpoint(db_session, revoked_principal, "revoked", revoked=True)
    inactive_user, inactive_principal, _ = human(db_session, "destination-inactive")
    endpoint(db_session, inactive_principal, "inactive")
    inactive_principal.is_active = False
    db_session.commit()

    for user in (missing_user, duplicate_user, revoked_user, inactive_user):
        with pytest.raises(LookupError):
            resolve_telegram_destination(db_session, user.id)
    db_session.expire_all()
    event_ids = {
        row.user_id for row in db_session.query(SecurityEvent).filter(
            SecurityEvent.user_id.in_([u.id for u in (missing_user, duplicate_user, revoked_user, inactive_user)])
        )
    }
    assert event_ids == {missing_user.id, duplicate_user.id, revoked_user.id, inactive_user.id}


def test_destination_failure_event_survives_caller_rollback(db_session):
    user, _, _ = human(db_session, "durable-event")
    db_session.commit()
    with pytest.raises(LookupError):
        resolve_telegram_destination(db_session, user.id)
    db_session.rollback()
    event = db_session.query(SecurityEvent).filter_by(user_id=user.id).one()
    assert event.event_type == "telegram_destination_rejected"
    assert event.channel == "telegram"


def test_missing_assignee_records_durable_event_without_broken_foreign_key(db_session):
    with pytest.raises(LookupError):
        resolve_telegram_destination(db_session, 987654321)
    db_session.rollback()
    event = db_session.query(SecurityEvent).filter(SecurityEvent.reason.contains("987654321")).one()
    assert event.user_id is None


def test_blank_endpoint_destination_is_rejected_by_database(db_session):
    _, principal, _ = human(db_session, "blank-endpoint")
    db_session.add(CommunicationEndpoint(
        human_principal_id=principal.id,
        channel="telegram",
        destination="   ",
        verified_at=NOW,
    ))
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_credential_rotation_retires_old_record_and_preserves_history(
    client, db_session, test_engine, monkeypatch, capsys
):
    from scripts import bootstrap_identity

    user, human_principal, _ = human(db_session, "rotation-human")
    bind(db_session, human_principal, 444)
    bot, old, _ = native_bot(db_session, "rotation-old-key")
    db_session.add(CredentialLifecycle(
        credential_id=old.id,
        state=CredentialState.ACTIVE,
        occurred_at=NOW,
        reason="initial",
    ))
    db_session.commit()
    ReviewSession = sessionmaker(bind=test_engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(bootstrap_identity, "SessionLocal", ReviewSession)

    bootstrap_identity.main(["--apply", "--bot-key", "rotation-new-key"])
    output = capsys.readouterr().out
    assert "rotation-old-key" not in output and "rotation-new-key" not in output
    db_session.expire_all()
    old = db_session.get(ApiCredential, old.id)
    active = db_session.query(ApiCredential).filter_by(
        principal_id=bot.id,
        purpose="telegram_bot",
        state=CredentialState.ACTIVE,
    ).one()
    assert old.state == CredentialState.REVOKED and old.revoked_at is not None
    assert active.supersedes_id == old.id
    history = db_session.query(CredentialLifecycle).order_by(CredentialLifecycle.id).all()
    assert [(row.credential_id, row.state) for row in history] == [
        (old.id, CredentialState.ACTIVE),
        (old.id, CredentialState.REVOKED),
        (active.id, CredentialState.ACTIVE),
    ]
    assert auth(client, "rotation-old-key", 444).status_code == 401
    assert auth(client, "rotation-new-key", 444).json()["id"] == user.id

    bootstrap_identity.main(["--bot-key", "rotation-dry-run-key"])
    capsys.readouterr()
    db_session.expire_all()
    assert db_session.query(ApiCredential).filter_by(
        principal_id=bot.id, state=CredentialState.ACTIVE
    ).one().id == active.id
    assert db_session.query(ApiCredential).filter_by(
        key_hash=hash_api_key("rotation-dry-run-key")
    ).count() == 0


def test_audit_provenance_preserves_canonical_human_actor(db_session):
    user, subject, _ = human(db_session, "audit-human")
    caller = Principal(name="audit-bot", principal_type=PrincipalType.SERVICE)
    db_session.add(caller)
    db_session.flush()
    caller_credential = credential(
        db_session, caller, "audit-caller-key", purpose="telegram_bot"
    )
    audit_context.set((subject.id, caller.id, caller_credential.id, "telegram"))
    record_audit(
        db_session,
        table_name="users",
        record_id=user.id,
        action="update",
        actor_id=user.id,
    )
    db_session.flush()
    row = db_session.query(AuditLog).one()
    assert (
        row.actor_id,
        row.subject_principal_id,
        row.caller_principal_id,
        row.credential_id,
        row.channel,
    ) == (user.id, subject.id, caller.id, caller_credential.id, "telegram")


def test_native_bot_mutation_audit_keeps_human_and_caller_provenance(
    client, db_session
):
    from app.models.membership import Membership, MembershipState, OrganizationRole
    from tests.conftest import ensure_default_org
    user, subject, _ = human(db_session, "telegram-audit-human")
    bind(db_session, subject, 515151)
    caller, caller_credential, bot_key = native_bot(
        db_session, "telegram-audit-bot-key"
    )
    db_session.commit()
    _org = ensure_default_org(db_session)
    _existing_ms = db_session.query(Membership.id).filter(
        Membership.user_id == user.id,
        Membership.organization_id == _org.id,
        Membership.state == MembershipState.ACTIVE,
    ).first()
    if _existing_ms is None:
        db_session.add(Membership(
            user_id=user.id,
            organization_id=_org.id,
            role=OrganizationRole.OWNER,
            state=MembershipState.ACTIVE,
        ))
        db_session.commit()

    response = client.post(
        f"{API}/properties",
        headers={
            "Authorization": f"Bearer {bot_key}",
            "X-Telegram-User-Id": "515151",
        },
        json={
            "name": "Identity Audit Property",
            "address": "Gate A",
            "city": "Pasay",
            "total_units": 1,
            "organization_id": _org.id,
        },
    )
    assert response.status_code == 201, response.text
    audit = db_session.query(AuditLog).filter_by(
        table_name="properties",
        record_id=response.json()["id"],
        action="create",
    ).one()
    assert (
        audit.actor_id,
        audit.subject_principal_id,
        audit.caller_principal_id,
        audit.credential_id,
        audit.channel,
    ) == (
        user.id,
        subject.id,
        caller.id,
        caller_credential.id,
        "telegram",
    )
