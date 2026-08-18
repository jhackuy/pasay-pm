"""Owner reminder hotfix targeted tests only."""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from app.models.operations import OperationalTaskType
from app.services.operations.generation import (
    _notification_message,
    _task_navigation_reply_markup,
)


def _rent_overdue_task():
    return SimpleNamespace(
        id=501,
        task_type=OperationalTaskType.RENT_OVERDUE,
        title="租金逾期 · 1期",
        due_at=datetime(2026, 8, 15, tzinfo=timezone.utc),
        next_action="Secretary to contact tenant for overdue rent.",
        details={
            "unit_number": "1608",
            "tenant_name": "DEV Maria Santos",
            "amount": "70000",
            "total_outstanding": "70000",
            "periods": ["2026-08"],
        },
    )


def test_owner_rent_reminder_projects_task_context_without_acknowledge():
    text = _notification_message(_rent_overdue_task(), "zh", current_actor="Secretary")

    assert "🔔 租金逾期" in text
    assert "房号：1608" in text
    assert "租客：DEV Maria Santos" in text
    assert "欠款：₱70,000 · 1期" in text
    assert "账期：2026-08" in text
    assert "当前处理：Secretary" in text
    assert "下一步：Secretary 联系租客催收" in text
    assert "Acknowledge" not in text


def test_owner_rent_reminder_uses_task_navigation_not_acknowledge():
    kb = _task_navigation_reply_markup(_rent_overdue_task(), "zh")

    button = kb["inline_keyboard"][0][0]
    assert button["text"] == "✅ 查看待办"
    assert button["callback_data"] == "v1:tkd:ops:501"
    assert "ack" not in button["callback_data"]


def test_secretary_rent_reminder_stays_english():
    text = _notification_message(_rent_overdue_task(), "en", current_actor="Secretary")
    kb = _task_navigation_reply_markup(_rent_overdue_task(), "en")

    assert "🔔 Rent Overdue" in text
    assert "Unit: 1608" in text
    assert "Tenant: DEV Maria Santos" in text
    assert "Outstanding: ₱70,000 · 1 period" in text
    assert "Current actor: Secretary" in text
    assert kb["inline_keyboard"][0][0]["text"] == "✅ View Task"
