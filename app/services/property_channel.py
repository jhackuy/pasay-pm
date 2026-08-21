"""Property Channel service layer (PASAY-TASK-007).

Implements the three operations the bot & backend share:
  * ``render_unit_archive(db, unit_id)`` -> deterministic hash + text
    body built on top of ``build_unit_timeline`` from the existing
    deterministic Quick Views module (PRODUCT_CONFORMANCE_AUDIT_001 §4.2
    explicitly calls this re-use out).
  * ``publish_unit_article(db, unit_id, external_message_id, actor_id)``
    -> records the PUBLISHED row with the hash guard; subsequent renders
    with the same hash are no-ops.
  * ``publish_property_article(db, property_id, external_message_id,
    actor_id)`` -> same for the per-property "all units in this building"
    overview article.
  * ``render_property_archive(db, property_id)`` -> summary body with
    status/vacancy counts and per-unit links.

The renderers never write themselves — they are pure read + return. The
``publish_*`` functions accept a pre-rendered hash so the bot can ship
the actual ``editMessageText`` call and only then commit the PostgreSQL
mapping. This mirrors the ``evidence`` table pattern (storage bytes in
TG, PG keeps index + relationship authority).
"""
from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Iterable
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.audit_log import AuditAction
from app.models.property import Property, Unit
from app.models.property_channel import (
    ArchiveArticleStatus,
    PropertyArchiveChannel,
    UnitArchiveArticle,
)
from app.services.audit import record_audit, serialize_row
from app.services.operations.quick import build_unit_timeline


def _stable_hash(payload: dict | list | str) -> str:
    body = payload if isinstance(payload, str) else json.dumps(
        payload, sort_keys=True, ensure_ascii=False, default=str
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def render_unit_archive(
    db: Session, unit_id: int, *, now: datetime | None = None
) -> dict:
    """Pure render -> {unit, events, summary, body_text, render_hash}.

    The same truth always produces the same ``render_hash`` so callers
    can skip an editMessageText round-trip entirely when the stored
    hash equals the fresh one. Never raises for missing units — returns
    a structured tombstone instead so API consumers get a 404 upstream
    via the caller, not here.
    """
    timeline = build_unit_timeline(db, unit_id, now=now)
    unit_data = timeline.get("unit")
    events: list[dict] = timeline.get("events", [])
    if unit_data is None:
        return {
            "unit_id": unit_id,
            "found": False,
            "unit": None,
            "events": [],
            "summary": {},
            "body_text": "",
            "render_hash": "",
            "event_count": 0,
        }
    unit_obj: Unit | None = db.query(Unit).filter(Unit.id == unit_id, Unit.deleted_at.is_(None)).first()
    summary = {
        "event_count": len(events),
        "status": unit_obj.status.value if unit_obj else "unknown",
        "unit_state": getattr(unit_obj, "unit_state", None),
    }
    summary_lines = [
        f"Unit #{unit_data.get('id') or unit_id} — {summary['status']}",
        f"Total timeline events: {summary['event_count']}",
        "",
        "Recent events (newest first):",
    ]
    for ev in list(reversed(events))[:12]:
        at = ev.get("at", "?")[:10]
        label = ev.get("label", "")
        detail = ev.get("detail", "")
        summary_lines.append(f"- [{at}] {label}  {detail}".rstrip())
    body_text = "\n".join(summary_lines)
    h = _stable_hash({"u": unit_data, "e": events})
    return {
        "unit_id": unit_id,
        "found": True,
        "unit": unit_data,
        "events": events,
        "summary": summary,
        "body_text": body_text,
        "render_hash": h,
        "event_count": len(events),
        "rendered_at": (now or datetime.now(timezone.utc)).isoformat(),
    }


def render_property_archive(
    db: Session, property_id: int, *, now: datetime | None = None
) -> dict:
    prop = db.query(Property).filter(
        Property.id == property_id, Property.deleted_at.is_(None)
    ).first()
    if prop is None:
        return {
            "property_id": property_id,
            "found": False,
            "property": None,
            "units": [],
            "body_text": "",
            "render_hash": "",
        }
    units = (
        db.query(Unit)
        .filter(Unit.property_id == property_id, Unit.deleted_at.is_(None))
        .order_by(Unit.unit_number, Unit.id)
        .all()
    )
    status_counts: Counter[str] = Counter()
    unit_rows: list[dict] = []
    for u in units:
        status_counts[u.status.value] += 1
        unit_rows.append({
            "id": u.id,
            "unit_number": u.unit_number,
            "floor": u.floor,
            "monthly_rent": str(Decimal(u.monthly_rent).quantize(Decimal("0.01"))),
            "status": u.status.value,
            "unit_state": u.unit_state,
        })
    summary_dict = {
        "property_id": prop.id,
        "name": prop.name,
        "address": prop.address,
        "city": prop.city,
        "total_units_model": prop.total_units,
        "active_unit_rows": len(unit_rows),
        "status_counts": dict(status_counts),
    }
    lines = [
        f"{prop.name} — {prop.city}",
        prop.address,
        "",
        f"Active units: {len(unit_rows)} (model says total_units={prop.total_units})",
        f"Status mix: {', '.join(f'{k}={v}' for k, v in sorted(status_counts.items())) or '(none)'}",
        "",
        "Units:",
    ]
    for row in unit_rows:
        lines.append(
            f"- #{row['id']} {row['unit_number']} "
            f"[{row['status']}]  ₱{row['monthly_rent']}/mo"
        )
    body_text = "\n".join(lines)
    h = _stable_hash({"p": serialize_row(prop), "u": unit_rows})
    return {
        "property_id": property_id,
        "found": True,
        "property": {
            "id": prop.id,
            "name": prop.name,
            "address": prop.address,
            "city": prop.city,
            "total_units": prop.total_units,
            "is_active": prop.is_active,
            "management_company": prop.management_company,
            "operational_notes": prop.operational_notes,
        },
        "units": unit_rows,
        "summary": summary_dict,
        "body_text": body_text,
        "render_hash": h,
        "rendered_at": (now or datetime.now(timezone.utc)).isoformat(),
    }


def _touch(actor_id: int | None, row: object) -> None:
    now = datetime.now(timezone.utc)
    if hasattr(row, "updated_by"):
        row.updated_by = actor_id
    if hasattr(row, "updated_at"):
        row.updated_at = now


def publish_unit_article(
    db: Session,
    unit_id: int,
    *,
    external_message_id: int,
    actor_id: int | None,
    render_hash: str,
    channel_chat_id: int | None = None,
    event_count_at_publish: int | None = None,
    platform: str = "telegram_channel",
) -> tuple[UnitArchiveArticle, bool]:
    """Insert-or-update the unit archive article.

    Returns ``(row, changed)`` where ``changed`` is False when the
    stored hash already matches the new one and the row status is
    already PUBLISHED — callers MUST skip the Telegram
    ``editMessageText`` when ``changed`` is False to avoid spamming
    the channel with identical payloads.

    Raises ``ValueError`` for a missing unit (caller should produce
    the HTTP 404) — never silently creates rows for nonexistent IDs.
    """
    if not external_message_id:
        raise ValueError("external_message_id is required for PUBLISHED unit articles")
    unit = db.query(Unit).filter(Unit.id == unit_id, Unit.deleted_at.is_(None)).first()
    if unit is None:
        raise ValueError(f"unit {unit_id} not found")
    existing = db.query(UnitArchiveArticle).filter(
        UnitArchiveArticle.unit_id == unit_id,
        UnitArchiveArticle.platform == platform,
    ).first()
    changed = True
    now = datetime.now(timezone.utc)
    if existing is None:
        existing = UnitArchiveArticle(
            unit_id=unit_id,
            property_id=unit.property_id,
            platform=platform,
            channel_chat_id=channel_chat_id,
            external_message_id=external_message_id,
            status=ArchiveArticleStatus.published.value,
            render_hash=render_hash,
            render_version=1,
            last_published_at=now,
            last_rendered_at=now,
            editor_user_id=actor_id,
            event_count_at_publish=event_count_at_publish,
            created_by=actor_id,
            updated_by=actor_id,
        )
        db.add(existing)
        db.flush()
        record_audit(
            db,
            table_name="unit_archive_articles",
            record_id=existing.id,
            action=AuditAction.unit_article_published.value,
            actor_id=actor_id,
            new_value=serialize_row(existing),
        )
    else:
        same_hash = (existing.render_hash == render_hash)
        same_message = (existing.external_message_id == external_message_id)
        already_published = (existing.status == ArchiveArticleStatus.published.value)
        if same_hash and same_message and already_published:
            changed = False
            existing.last_rendered_at = now
            _touch(actor_id, existing)
            db.flush()
            record_audit(
                db,
                table_name="unit_archive_articles",
                record_id=existing.id,
                action=AuditAction.unit_archive_rendered.value,
                actor_id=actor_id,
                new_value={"render_hash_match": True, "changed": False},
            )
            return existing, changed
        old = serialize_row(existing)
        existing.property_id = unit.property_id
        existing.channel_chat_id = channel_chat_id
        existing.external_message_id = external_message_id
        existing.render_hash = render_hash
        existing.render_version = (existing.render_version or 1) + 1
        existing.status = ArchiveArticleStatus.published.value
        existing.last_published_at = now
        existing.last_rendered_at = now
        existing.editor_user_id = actor_id
        existing.event_count_at_publish = event_count_at_publish
        _touch(actor_id, existing)
        db.flush()
        record_audit(
            db,
            table_name="unit_archive_articles",
            record_id=existing.id,
            action=AuditAction.unit_article_edited.value,
            actor_id=actor_id,
            old_value=old,
            new_value=serialize_row(existing),
        )
    return existing, changed


def publish_property_article(
    db: Session,
    property_id: int,
    *,
    external_message_id: int,
    actor_id: int | None,
    render_hash: str,
    channel_chat_id: int | None = None,
    platform: str = "telegram_channel",
) -> tuple[PropertyArchiveChannel, bool]:
    """Insert-or-update the property-level archive article.

    Same semantics as ``publish_unit_article`` but scoped to the
    property row. Missing property raises ``ValueError``.
    """
    if not external_message_id:
        raise ValueError("external_message_id is required for PUBLISHED property articles")
    prop = db.query(Property).filter(
        Property.id == property_id, Property.deleted_at.is_(None)
    ).first()
    if prop is None:
        raise ValueError(f"property {property_id} not found")
    existing = db.query(PropertyArchiveChannel).filter(
        PropertyArchiveChannel.property_id == property_id,
        PropertyArchiveChannel.platform == platform,
    ).first()
    changed = True
    now = datetime.now(timezone.utc)
    if existing is None:
        existing = PropertyArchiveChannel(
            property_id=property_id,
            platform=platform,
            channel_chat_id=channel_chat_id,
            external_message_id=external_message_id,
            status=ArchiveArticleStatus.published.value,
            render_hash=render_hash,
            last_published_at=now,
            last_rendered_at=now,
            editor_user_id=actor_id,
            created_by=actor_id,
            updated_by=actor_id,
        )
        db.add(existing)
        db.flush()
        record_audit(
            db,
            table_name="property_archive_channels",
            record_id=existing.id,
            action=AuditAction.property_article_published.value,
            actor_id=actor_id,
            new_value=serialize_row(existing),
        )
    else:
        same_hash = (existing.render_hash == render_hash)
        same_message = (existing.external_message_id == external_message_id)
        already_published = (existing.status == ArchiveArticleStatus.published.value)
        if same_hash and same_message and already_published:
            changed = False
            existing.last_rendered_at = now
            _touch(actor_id, existing)
            db.flush()
            record_audit(
                db,
                table_name="property_archive_channels",
                record_id=existing.id,
                action=AuditAction.unit_archive_rendered.value,
                actor_id=actor_id,
                new_value={"render_hash_match": True, "changed": False},
            )
            return existing, changed
        old = serialize_row(existing)
        existing.channel_chat_id = channel_chat_id
        existing.external_message_id = external_message_id
        existing.render_hash = render_hash
        existing.status = ArchiveArticleStatus.published.value
        existing.last_published_at = now
        existing.last_rendered_at = now
        existing.editor_user_id = actor_id
        _touch(actor_id, existing)
        db.flush()
        record_audit(
            db,
            table_name="property_archive_channels",
            record_id=existing.id,
            action=AuditAction.property_article_edited.value,
            actor_id=actor_id,
            old_value=old,
            new_value=serialize_row(existing),
        )
    return existing, changed


def list_unit_articles_for_property(
    db: Session, property_id: int
) -> Iterable[UnitArchiveArticle]:
    stmt = (
        select(UnitArchiveArticle)
        .where(UnitArchiveArticle.property_id == property_id)
        .order_by(UnitArchiveArticle.unit_id, UnitArchiveArticle.id)
    )
    return db.execute(stmt).scalars().all()


def get_unit_article(db: Session, unit_id: int, platform: str = "telegram_channel") -> UnitArchiveArticle | None:
    return db.query(UnitArchiveArticle).filter(
        UnitArchiveArticle.unit_id == unit_id,
        UnitArchiveArticle.platform == platform,
    ).first()


def get_property_article(db: Session, property_id: int, platform: str = "telegram_channel") -> PropertyArchiveChannel | None:
    return db.query(PropertyArchiveChannel).filter(
        PropertyArchiveChannel.property_id == property_id,
        PropertyArchiveChannel.platform == platform,
    ).first()
