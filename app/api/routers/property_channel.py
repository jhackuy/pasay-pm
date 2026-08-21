"""Property Channel API endpoints (PASAY-TASK-007).

Three surface areas:
  1. Unit archive render + publish:   ``GET /units/{id}/timeline`` +
     ``POST /units/{id}/archive``
  2. Property archive render + publish: ``GET /properties/{id}/channel``
     + ``POST /properties/{id}/archive``
  3. Property-channel listing (the bot queries this before rendering a
     fresh copy so it can short-circuit a no-op hash match).

Auth policy mirrors the rest of the V1 API:
  * GET read endpoints -> any authenticated user (``get_current_user``)
  * POST publish endpoints -> manager_or_admin (Owner or Secretary)
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, manager_or_admin
from app.database import get_db
from app.models.property import Property, Unit
from app.models.user import User
from app.services.property_channel import (
    get_property_article,
    get_unit_article,
    list_unit_articles_for_property,
    publish_property_article,
    publish_unit_article,
    render_property_archive,
    render_unit_archive,
)

router = APIRouter(prefix="/property-channel", tags=["property_channel"])


class PublishUnitArticleRequest(BaseModel):
    external_message_id: int = Field(gt=0)
    channel_chat_id: int | None = Field(default=None, ge=0)
    render_hash: str = Field(min_length=1, max_length=64)
    event_count_at_publish: int | None = Field(default=None, ge=0)
    platform: str = Field(default="telegram_channel", min_length=1, max_length=30)


class PublishPropertyArticleRequest(BaseModel):
    external_message_id: int = Field(gt=0)
    channel_chat_id: int | None = Field(default=None, ge=0)
    render_hash: str = Field(min_length=1, max_length=64)
    platform: str = Field(default="telegram_channel", min_length=1, max_length=30)


def _get_unit_or_404(db: Session, unit_id: int) -> Unit:
    obj = db.query(Unit).filter(Unit.id == unit_id, Unit.deleted_at.is_(None)).first()
    if obj is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unit not found")
    return obj


def _get_property_or_404(db: Session, property_id: int) -> Property:
    obj = (
        db.query(Property)
        .filter(Property.id == property_id, Property.deleted_at.is_(None))
        .first()
    )
    if obj is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Property not found")
    return obj


@router.get("/units/{unit_id}/timeline")
def unit_timeline(
    unit_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    _get_unit_or_404(db, unit_id)
    return render_unit_archive(db, unit_id)


@router.post("/units/{unit_id}/archive")
def unit_archive_publish(
    unit_id: int,
    payload: PublishUnitArticleRequest,
    db: Session = Depends(get_db),
    actor: User = Depends(manager_or_admin),
):
    try:
        row, changed = publish_unit_article(
            db,
            unit_id,
            external_message_id=payload.external_message_id,
            channel_chat_id=payload.channel_chat_id,
            render_hash=payload.render_hash,
            event_count_at_publish=payload.event_count_at_publish,
            platform=payload.platform,
            actor_id=actor.id,
        )
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
    db.commit()
    db.refresh(row)
    return {
        "id": row.id,
        "unit_id": row.unit_id,
        "external_message_id": row.external_message_id,
        "render_hash": row.render_hash,
        "render_version": row.render_version,
        "status": row.status,
        "changed": changed,
    }


@router.get("/units/{unit_id}/archive")
def unit_archive_get(
    unit_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    _get_unit_or_404(db, unit_id)
    row = get_unit_article(db, unit_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unit archive not published yet")
    return {
        "id": row.id,
        "unit_id": row.unit_id,
        "property_id": row.property_id,
        "external_message_id": row.external_message_id,
        "render_hash": row.render_hash,
        "render_version": row.render_version,
        "status": row.status,
        "last_published_at": row.last_published_at,
        "last_rendered_at": row.last_rendered_at,
        "event_count_at_publish": row.event_count_at_publish,
        "platform": row.platform,
    }


@router.get("/properties/{property_id}/channel")
def property_channel(
    property_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    _get_property_or_404(db, property_id)
    rendered = render_property_archive(db, property_id)
    published = get_property_article(db, property_id)
    unit_articles = [
        {
            "id": a.id,
            "unit_id": a.unit_id,
            "external_message_id": a.external_message_id,
            "render_hash": a.render_hash,
            "render_version": a.render_version,
            "status": a.status,
            "last_published_at": a.last_published_at,
        }
        for a in list_unit_articles_for_property(db, property_id)
    ]
    return {
        "rendered": rendered,
        "published_property_article": {
            "id": published.id,
            "external_message_id": published.external_message_id,
            "render_hash": published.render_hash,
            "status": published.status,
            "last_published_at": published.last_published_at,
            "platform": published.platform,
        } if published else None,
        "published_unit_articles": unit_articles,
    }


@router.post("/properties/{property_id}/archive")
def property_archive_publish(
    property_id: int,
    payload: PublishPropertyArticleRequest,
    db: Session = Depends(get_db),
    actor: User = Depends(manager_or_admin),
):
    try:
        row, changed = publish_property_article(
            db,
            property_id,
            external_message_id=payload.external_message_id,
            channel_chat_id=payload.channel_chat_id,
            render_hash=payload.render_hash,
            platform=payload.platform,
            actor_id=actor.id,
        )
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
    db.commit()
    db.refresh(row)
    return {
        "id": row.id,
        "property_id": row.property_id,
        "external_message_id": row.external_message_id,
        "render_hash": row.render_hash,
        "status": row.status,
        "changed": changed,
    }


@router.get("/properties/{property_id}/archive")
def property_archive_get(
    property_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    _get_property_or_404(db, property_id)
    row = get_property_article(db, property_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Property archive not published yet")
    return {
        "id": row.id,
        "property_id": row.property_id,
        "external_message_id": row.external_message_id,
        "render_hash": row.render_hash,
        "status": row.status,
        "last_published_at": row.last_published_at,
        "last_rendered_at": row.last_rendered_at,
        "platform": row.platform,
    }
