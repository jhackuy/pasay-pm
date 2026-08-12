"""V1.3 Gate A Owner-subject policy for income confirm and reverse."""
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from app.core.security import hash_api_key
from app.models.audit_log import AuditAction, AuditLog
from app.models.financial import Income, IncomeStatus
from app.models.identity import (
    ApiCredential,
    CredentialState,
    Principal,
    PrincipalType,
    TelegramIdentityBinding,
)
from app.models.user import User, UserRole


API = "/api/v1"
NOW = datetime(2026, 8, 12, tzinfo=timezone.utc)
OWNER_TELEGRAM_ID = 5_177_241_442


def _headers(raw_key: str, telegram_user_id: int | None = None) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {raw_key}"}
    if telegram_user_id is not None:
        headers["X-Telegram-User-Id"] = str(telegram_user_id)
    return headers


def _income(db, status: IncomeStatus) -> Income:
    row = Income(
        amount=Decimal("12000.00"),
        received_date=date(2026, 8, 12),
        payment_method="Bank",
        status=status,
        description="Gate A route test",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _human_principal(db, user: User) -> Principal:
    return db.query(Principal).filter_by(
        user_id=user.id,
        principal_type=PrincipalType.HUMAN,
    ).one()


def _native_bot(db, raw_key: str = "gate-a-native-bot-key"):
    # Retain a legacy credential-owner user to prove that SERVICE caller
    # ownership never becomes the financial HUMAN actor.
    credential_owner = User(
        username=f"native-bot-owner-{raw_key}",
        role=UserRole.admin,
        api_key_hash=hash_api_key(raw_key),
        is_active=True,
    )
    db.add(credential_owner)
    db.flush()
    caller = Principal(
        name="native-bot",
        principal_type=PrincipalType.SERVICE,
        user_id=credential_owner.id,
    )
    db.add(caller)
    db.flush()
    credential = ApiCredential(
        principal_id=caller.id,
        key_hash=hash_api_key(raw_key),
        purpose="telegram_bot",
        state=CredentialState.ACTIVE,
    )
    db.add(credential)
    db.flush()
    return credential_owner, caller, credential, raw_key


def _bind(
    db,
    subject: Principal,
    telegram_user_id: int,
    *,
    verified: bool = True,
    active: bool = True,
    revoked: bool = False,
) -> TelegramIdentityBinding:
    binding = TelegramIdentityBinding(
        external_user_id=telegram_user_id,
        human_principal_id=subject.id,
        verified_at=NOW if verified else None,
        revoked_at=NOW if revoked else None,
        is_active=active,
    )
    db.add(binding)
    db.flush()
    return binding


def _transition(transition: str) -> tuple[IncomeStatus, IncomeStatus]:
    if transition == "confirm":
        return IncomeStatus.pending, IncomeStatus.confirmed
    return IncomeStatus.confirmed, IncomeStatus.reversed


@pytest.mark.parametrize("transition", ["confirm", "reverse"])
def test_native_bot_authorizes_canonical_owner_and_records_full_provenance(
    transition, client, db_session, admin
):
    initial, expected = _transition(transition)
    owner, _ = admin
    subject = _human_principal(db_session, owner)
    credential_owner, caller, credential, bot_key = _native_bot(db_session)
    _bind(db_session, subject, OWNER_TELEGRAM_ID)
    db_session.commit()
    income = _income(db_session, initial)

    response = client.post(
        f"{API}/incomes/{income.id}/{transition}",
        headers=_headers(bot_key, OWNER_TELEGRAM_ID),
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == expected.value
    if transition == "confirm":
        assert response.json()["confirmed_by"] == owner.id
        assert response.json()["confirmed_by"] != credential_owner.id

    audit = db_session.query(AuditLog).filter_by(
        table_name="incomes",
        record_id=income.id,
        action=AuditAction(transition),
    ).one()
    assert (
        audit.actor_id,
        audit.subject_principal_id,
        audit.caller_principal_id,
        audit.credential_id,
        audit.channel,
    ) == (
        owner.id,
        subject.id,
        caller.id,
        credential.id,
        "telegram",
    )
    assert subject.principal_type == PrincipalType.HUMAN
    assert subject.user_id == owner.id and subject.is_active
    assert caller.principal_type == PrincipalType.SERVICE
    assert caller.name == "native-bot" and caller.is_active
    assert credential.state == CredentialState.ACTIVE
    assert credential.revoked_at is None


@pytest.mark.parametrize("transition", ["confirm", "reverse"])
def test_forged_secretary_callback_is_403_and_cannot_transition(
    transition, client, db_session, manager
):
    initial, _ = _transition(transition)
    secretary, _ = manager
    subject = _human_principal(db_session, secretary)
    _, _, _, bot_key = _native_bot(db_session)
    _bind(db_session, subject, 1_083_657_401)
    db_session.commit()
    income = _income(db_session, initial)

    response = client.post(
        f"{API}/incomes/{income.id}/{transition}",
        headers=_headers(bot_key, 1_083_657_401),
    )

    assert response.status_code == 403
    db_session.refresh(income)
    assert income.status == initial
    assert db_session.query(AuditLog).filter_by(
        table_name="incomes", record_id=income.id, action=AuditAction(transition)
    ).count() == 0


@pytest.mark.parametrize("transition", ["confirm", "reverse"])
@pytest.mark.parametrize("user_fixture", ["manager", "agent"])
def test_direct_manager_and_agent_credentials_are_403(
    transition, user_fixture, request, client, db_session
):
    initial, _ = _transition(transition)
    _, raw_key = request.getfixturevalue(user_fixture)
    income = _income(db_session, initial)

    response = client.post(
        f"{API}/incomes/{income.id}/{transition}",
        headers=_headers(raw_key),
    )

    assert response.status_code == 403
    db_session.refresh(income)
    assert income.status == initial


@pytest.mark.parametrize("transition", ["confirm", "reverse"])
def test_native_bot_service_credential_alone_is_403(
    transition, client, db_session
):
    initial, _ = _transition(transition)
    _, _, _, bot_key = _native_bot(db_session)
    db_session.commit()
    income = _income(db_session, initial)

    response = client.post(
        f"{API}/incomes/{income.id}/{transition}",
        headers=_headers(bot_key),
    )

    assert response.status_code == 403
    db_session.refresh(income)
    assert income.status == initial


@pytest.mark.parametrize("transition", ["confirm", "reverse"])
def test_revoked_native_bot_credential_never_falls_through_to_legacy_owner(
    transition, client, db_session
):
    initial, _ = _transition(transition)
    credential_owner, _, credential, bot_key = _native_bot(db_session)
    # If the revoked credential lookup ever fell through, this legacy HUMAN
    # principal and matching user key would otherwise satisfy Owner policy.
    db_session.add(Principal(
        name="native-bot-legacy-owner",
        principal_type=PrincipalType.HUMAN,
        user_id=credential_owner.id,
        is_active=True,
    ))
    credential.state = CredentialState.REVOKED
    credential.revoked_at = NOW
    db_session.commit()
    income = _income(db_session, initial)

    response = client.post(
        f"{API}/incomes/{income.id}/{transition}",
        headers=_headers(bot_key),
    )

    assert response.status_code == 403
    db_session.refresh(income)
    assert income.status == initial


@pytest.mark.parametrize("transition", ["confirm", "reverse"])
@pytest.mark.parametrize(
    "binding_state",
    ["unknown", "unverified", "revoked", "inactive"],
)
def test_unknown_unverified_revoked_and_inactive_telegram_identities_are_403(
    transition, binding_state, client, db_session, admin
):
    initial, _ = _transition(transition)
    owner, _ = admin
    subject = _human_principal(db_session, owner)
    _, _, _, bot_key = _native_bot(db_session)
    if binding_state != "unknown":
        _bind(
            db_session,
            subject,
            OWNER_TELEGRAM_ID,
            verified=binding_state != "unverified",
            active=binding_state != "inactive",
            revoked=binding_state == "revoked",
        )
    db_session.commit()
    income = _income(db_session, initial)

    response = client.post(
        f"{API}/incomes/{income.id}/{transition}",
        headers=_headers(bot_key, OWNER_TELEGRAM_ID),
    )

    assert response.status_code == 403
    db_session.refresh(income)
    assert income.status == initial


@pytest.mark.parametrize("transition", ["confirm", "reverse"])
@pytest.mark.parametrize(
    "request_headers",
    [{}, {"Authorization": "Bearer untrusted-direct-key"}],
    ids=["missing-credential", "invalid-credential"],
)
def test_untrusted_direct_backend_call_is_403(
    transition, request_headers, client, db_session
):
    initial, _ = _transition(transition)
    income = _income(db_session, initial)

    response = client.post(
        f"{API}/incomes/{income.id}/{transition}", headers=request_headers
    )

    assert response.status_code == 403
    db_session.refresh(income)
    assert income.status == initial


@pytest.mark.parametrize("transition", ["confirm", "reverse"])
def test_human_credential_cannot_forge_telegram_subject(
    transition, client, db_session, admin
):
    initial, _ = _transition(transition)
    _, owner_key = admin
    income = _income(db_session, initial)

    response = client.post(
        f"{API}/incomes/{income.id}/{transition}",
        headers=_headers(owner_key, OWNER_TELEGRAM_ID),
    )

    assert response.status_code == 403
    db_session.refresh(income)
    assert income.status == initial


@pytest.mark.parametrize("transition", ["confirm", "reverse"])
def test_exact_legacy_human_owner_key_remains_compatible(
    transition, client, db_session
):
    initial, expected = _transition(transition)
    raw_key = f"legacy-owner-{transition}"
    owner = User(
        username=f"legacy-owner-{transition}",
        role=UserRole.admin,
        api_key_hash=hash_api_key(raw_key),
        is_active=True,
    )
    db_session.add(owner)
    db_session.flush()
    subject = Principal(
        name=f"legacy-owner-{transition}",
        principal_type=PrincipalType.HUMAN,
        user_id=owner.id,
        is_active=True,
    )
    db_session.add(subject)
    db_session.commit()
    income = _income(db_session, initial)

    response = client.post(
        f"{API}/incomes/{income.id}/{transition}",
        headers=_headers(raw_key),
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == expected.value
    if transition == "confirm":
        assert response.json()["confirmed_by"] == owner.id
    audit = db_session.query(AuditLog).filter_by(
        table_name="incomes",
        record_id=income.id,
        action=AuditAction(transition),
    ).one()
    assert (
        audit.actor_id,
        audit.subject_principal_id,
        audit.caller_principal_id,
        audit.credential_id,
        audit.channel,
    ) == (owner.id, subject.id, subject.id, None, "api")


@pytest.mark.parametrize("transition", ["confirm", "reverse"])
def test_revoked_new_owner_credential_never_falls_through_to_legacy_key(
    transition, client, db_session
):
    initial, _ = _transition(transition)
    raw_key = f"revoked-owner-{transition}"
    owner = User(
        username=f"revoked-owner-{transition}",
        role=UserRole.admin,
        api_key_hash=hash_api_key(raw_key),
        is_active=True,
    )
    db_session.add(owner)
    db_session.flush()
    subject = Principal(
        name=f"revoked-owner-{transition}",
        principal_type=PrincipalType.HUMAN,
        user_id=owner.id,
        is_active=True,
    )
    db_session.add(subject)
    db_session.flush()
    db_session.add(ApiCredential(
        principal_id=subject.id,
        key_hash=hash_api_key(raw_key),
        purpose="legacy_human",
        state=CredentialState.REVOKED,
        revoked_at=NOW,
    ))
    db_session.commit()
    income = _income(db_session, initial)

    response = client.post(
        f"{API}/incomes/{income.id}/{transition}",
        headers=_headers(raw_key),
    )

    assert response.status_code == 403
    db_session.refresh(income)
    assert income.status == initial
