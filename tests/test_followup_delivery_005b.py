from __future__ import annotations

import concurrent.futures
import threading
from datetime import datetime, timezone

from sqlalchemy.orm import sessionmaker

from app.api.routers import operations as ops_router
from app.models.membership import Membership, MembershipState, OrganizationRole
from app.models.operations import OperationalTask, OperationalTaskStatus, OperationalTaskType
from app.models.user import User, UserRole
from app.schemas.operations import TaskFollowupDeliveryIn

NOW = datetime(2026, 8, 18, 12, 0, 0, tzinfo=timezone.utc)


def _user(db, username: str, role: UserRole, telegram_chat_id: str | None = None) -> User:
    user = User(
        username=username,
        role=role,
        api_key_hash=f"key-{username}",
        is_active=True,
        telegram_chat_id=telegram_chat_id,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _task(db, *, assigned_user_id=None) -> OperationalTask:
    from tests.conftest import seed_property
    _p = seed_property(db)
    task = OperationalTask(
        task_type=OperationalTaskType.FOLLOWUP,
        title="Rent follow-up",
        source_type="lease",
        source_id=1,
        assigned_user_id=assigned_user_id,
        status=OperationalTaskStatus.PENDING,
        due_at=NOW,
        property_id=_p.id,
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    return task


class _SharedSender:
    def __init__(self, sent: list[tuple]):
        self.sent = sent

    def send(self, recipient, text, reply_markup=None):
        self.sent.append((recipient, text, reply_markup))
        return "777"


class _FailOnceSender:
    def __init__(self):
        self.calls = 0

    def send(self, recipient, text, reply_markup=None):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("telegram down")
        return "778"


def test_followup_delivery_concurrent_single_send(db_session, test_engine, monkeypatch, admin):
    actor = db_session.get(User, admin[0].id)
    assignee = _user(db_session, "sec-005b", UserRole.agent, telegram_chat_id="tg-sec-005b")
    task = _task(db_session)
    payload = TaskFollowupDeliveryIn(
        assignee_user_id=assignee.id,
        message="Follow up with tenant",
        reply_markup={"inline_keyboard": [[{"text": "Done", "callback_data": "v1:sfc:1"}]]},
    )
    sent: list[tuple] = []
    monkeypatch.setattr(ops_router, "_build_notification_sender", lambda db: _SharedSender(sent))

    Session = sessionmaker(bind=test_engine, autoflush=False, expire_on_commit=False)
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def _run():
        db = Session()
        try:
            local_actor = db.get(User, actor.id)
            local_membership = db.query(Membership).filter(
                Membership.user_id == local_actor.id,
                Membership.state == MembershipState.ACTIVE,
                Membership.role.in_([OrganizationRole.OWNER, OrganizationRole.SECRETARY]),
                Membership.removed_at.is_(None),
            ).first()
            barrier.wait(timeout=20)
            ops_router.deliver_task_followup(task.id, payload, db=db, user=local_actor, membership=local_membership)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            db.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(_run) for _ in range(2)]
        for f in futures:
            f.result(timeout=60)

    assert not errors, errors
    assert len(sent) == 1
    db_session.refresh(task)
    assert task.assigned_user_id == assignee.id
    assert (task.details or {}).get("assigned_to") == assignee.id
    assert task.status == OperationalTaskStatus.PENDING


def test_followup_delivery_failure_then_retry(db_session, monkeypatch, admin):
    actor = db_session.get(User, admin[0].id)
    membership = db_session.query(Membership).filter(
        Membership.user_id == actor.id,
        Membership.state == MembershipState.ACTIVE,
        Membership.role.in_([OrganizationRole.OWNER, OrganizationRole.SECRETARY]),
        Membership.removed_at.is_(None),
    ).first()
    assignee = _user(db_session, "sec-005b-fail", UserRole.agent, telegram_chat_id="tg-sec-005b-fail")
    task = _task(db_session)
    payload = TaskFollowupDeliveryIn(assignee_user_id=assignee.id, message="Retry me", reply_markup=None)
    sender = _FailOnceSender()
    monkeypatch.setattr(ops_router, "_build_notification_sender", lambda db: sender)

    first = ops_router.deliver_task_followup(task.id, payload, db=db_session, user=actor, membership=membership)
    assert first.delivery_state == "FAILED"
    db_session.refresh(task)
    assert task.assigned_user_id is None
    assert (task.details or {}).get("assigned_to") is None
    assert task.status == OperationalTaskStatus.PENDING

    second = ops_router.deliver_task_followup(task.id, payload, db=db_session, user=actor, membership=membership)
    assert second.delivery_state == "DELIVERED"
    db_session.refresh(task)
    assert task.assigned_user_id == assignee.id
    assert (task.details or {}).get("assigned_to") == assignee.id
    assert task.status == OperationalTaskStatus.PENDING


def test_followup_delivery_sequential_duplicate_no_resend(db_session, monkeypatch, admin):
    actor = db_session.get(User, admin[0].id)
    membership = db_session.query(Membership).filter(
        Membership.user_id == actor.id,
        Membership.state == MembershipState.ACTIVE,
        Membership.role.in_([OrganizationRole.OWNER, OrganizationRole.SECRETARY]),
        Membership.removed_at.is_(None),
    ).first()
    assignee = _user(db_session, "sec-005b-seq", UserRole.agent, telegram_chat_id="tg-sec-005b-seq")
    task = _task(db_session)
    payload = TaskFollowupDeliveryIn(assignee_user_id=assignee.id, message="Only once", reply_markup=None)
    sent: list[tuple] = []
    monkeypatch.setattr(ops_router, "_build_notification_sender", lambda db: _SharedSender(sent))

    first = ops_router.deliver_task_followup(task.id, payload, db=db_session, user=actor, membership=membership)
    second = ops_router.deliver_task_followup(task.id, payload, db=db_session, user=actor, membership=membership)

    assert first.delivery_state == "DELIVERED"
    assert second.delivery_state == "ALREADY_DELIVERED"
    assert len(sent) == 1


def test_followup_delivery_retry_refreshes_render_payload(db_session, monkeypatch, admin):
    actor = db_session.get(User, admin[0].id)
    membership = db_session.query(Membership).filter(
        Membership.user_id == actor.id,
        Membership.state == MembershipState.ACTIVE,
        Membership.role.in_([OrganizationRole.OWNER, OrganizationRole.SECRETARY]),
        Membership.removed_at.is_(None),
    ).first()
    assignee = _user(db_session, "sec-005c-render", UserRole.agent, telegram_chat_id="tg-sec-005c")
    task = _task(db_session)
    first_payload = TaskFollowupDeliveryIn(
        assignee_user_id=assignee.id,
        message="Old generic reminder",
        reply_markup=None,
    )
    second_payload = TaskFollowupDeliveryIn(
        assignee_user_id=assignee.id,
        message="Rent collection follow-up\nUnit: 1680",
        reply_markup={"inline_keyboard": [[{"text": "Done", "callback_data": "v1:sfc:1"}]]},
    )
    sender = _FailOnceSender()
    sent: list[tuple] = []

    class _MixedSender:
        def send(self, recipient, text, reply_markup=None):
            if sender.calls == 0:
                return sender.send(recipient, text, reply_markup)
            sent.append((recipient, text, reply_markup))
            return "779"

    monkeypatch.setattr(ops_router, "_build_notification_sender", lambda db: _MixedSender())

    first = ops_router.deliver_task_followup(task.id, first_payload, db=db_session, user=actor, membership=membership)
    assert first.delivery_state == "FAILED"
    second = ops_router.deliver_task_followup(task.id, second_payload, db=db_session, user=actor, membership=membership)
    assert second.delivery_state == "DELIVERED"
    assert sent
    assert "Rent collection follow-up" in sent[-1][1]
    assert sent[-1][2] == second_payload.reply_markup
