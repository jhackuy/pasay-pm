from datetime import datetime, timezone

import pytest

from app.core.security import hash_api_key
from app.models.audit_log import AuditLog
from app.models.identity import (ApiCredential, CommunicationEndpoint, CredentialState,
    Principal, PrincipalType, SecurityEvent, TelegramIdentityBinding)
from app.models.user import User, UserRole
from app.services.audit import audit_context, record_audit
from app.services.identity import resolve_telegram_destination, resolve_telegram_human


NOW = datetime.now(timezone.utc)


def human(db, username="alice", user_id=None):
    user = User(username=username, role=UserRole.admin, api_key_hash=hash_api_key(f"legacy-{username}"), is_active=True)
    if user_id is not None: user.id = user_id
    db.add(user); db.flush()
    principal = Principal(name=username, principal_type=PrincipalType.HUMAN, user_id=user.id)
    db.add(principal); db.flush()
    return user, principal


def test_unique_verified_binding_and_group_chat_separation(db_session):
    user, principal = human(db_session)
    db_session.add(TelegramIdentityBinding(external_user_id=123, human_principal_id=principal.id, verified_at=NOW))
    db_session.commit()
    assert resolve_telegram_human(db_session, 123)[0].id == user.id
    with pytest.raises(LookupError): resolve_telegram_human(db_session, -100123)  # a chat id is not identity
    with pytest.raises(LookupError): resolve_telegram_human(db_session, 999)


def test_unverified_and_revoked_bindings_rejected(db_session):
    _, principal = human(db_session)
    row = TelegramIdentityBinding(external_user_id=123, human_principal_id=principal.id)
    db_session.add(row); db_session.commit()
    with pytest.raises(LookupError): resolve_telegram_human(db_session, 123)
    row.verified_at = NOW; row.revoked_at = NOW; db_session.commit()
    with pytest.raises(LookupError): resolve_telegram_human(db_session, 123)


def test_endpoint_history_fails_closed_and_records_event(db_session):
    user, principal = human(db_session); user.telegram_chat_id = "legacy"
    db_session.add(CommunicationEndpoint(human_principal_id=principal.id, channel="telegram", destination="new"))
    db_session.commit()
    with pytest.raises(LookupError): resolve_telegram_destination(db_session, user.id)
    assert db_session.query(SecurityEvent).filter_by(user_id=user.id).count() == 1


def test_shared_destination_is_allowed(db_session):
    destinations = []
    for name in ("a", "b"):
        user, principal = human(db_session, name)
        db_session.add(CommunicationEndpoint(human_principal_id=principal.id, channel="telegram", destination="shared", verified_at=NOW))
        db_session.flush(); destinations.append(resolve_telegram_destination(db_session, user.id))
    assert destinations == ["shared", "shared"]


def test_service_requires_header_and_revocation_is_immediate(client, db_session):
    user, human_principal = human(db_session)
    service = Principal(name="native-bot", principal_type=PrincipalType.SERVICE)
    db_session.add(service); db_session.flush()
    credential = ApiCredential(principal_id=service.id, key_hash=hash_api_key("bot-key"), purpose="telegram_bot", state=CredentialState.ACTIVE)
    db_session.add_all([credential, TelegramIdentityBinding(external_user_id=123, human_principal_id=human_principal.id, verified_at=NOW)])
    db_session.commit(); headers={"Authorization":"Bearer bot-key"}
    assert client.post("/api/v1/auth", headers=headers).status_code == 401
    assert client.post("/api/v1/auth", headers={**headers, "X-Telegram-User-Id":"123"}).json()["id"] == user.id
    credential.state=CredentialState.REVOKED; credential.revoked_at=NOW; db_session.commit()
    assert client.post("/api/v1/auth", headers={**headers, "X-Telegram-User-Id":"123"}).status_code == 401


def test_maria_and_user_14_are_not_humans(client, db_session):
    for username, user_id in (("maria", None), ("secretary", 14)):
        user = User(username=username, role=UserRole.admin, api_key_hash=hash_api_key(username), is_active=True)
        if user_id: user.id=user_id
        db_session.add(user); db_session.commit()
        assert client.post("/api/v1/auth", headers={"Authorization":f"Bearer {username}"}).status_code == 401


def test_audit_provenance_preserves_human_actor(db_session):
    user, subject = human(db_session); caller = Principal(name="bot", principal_type=PrincipalType.SERVICE)
    db_session.add(caller); db_session.flush(); credential = ApiCredential(principal_id=caller.id, key_hash=hash_api_key("x"), purpose="telegram_bot", state=CredentialState.ACTIVE)
    db_session.add(credential); db_session.flush(); audit_context.set((subject.id, caller.id, credential.id, "telegram"))
    record_audit(db_session, table_name="users", record_id=user.id, action="update", actor_id=user.id)
    db_session.flush(); row=db_session.query(AuditLog).one()
    assert (row.actor_id, row.subject_principal_id, row.caller_principal_id, row.credential_id, row.channel) == (user.id, subject.id, caller.id, credential.id, "telegram")
