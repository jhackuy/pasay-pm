"""Property / Unit scoped helpers + Unit Channel Binding service (Issue #25 P0).

Replaces the PRODUCT_CONFORMANCE_AUDIT_001 archive-article/render-publish
layer that was out-of-contract for Issue #25. This module implements exactly
what the Issue #25 P0 contract requires for the NEW code paths:

  1. organization-scoped Property / Unit access (cross-org isolation)
  2. ``organization + property_id + unit_number → Unit`` stable lookup
  3. Unit ↔ Telegram Channel minimal binding lifecycle:
        bind (OWNER-only) / replace (OWNER-only → old REVOKED, new ACTIVE)
        / revoke (OWNER-only → REVOKED + history preserved)
  4. Active-binding uniqueness: one ACTIVE per (unit_id, purpose)

Permission philosophy (mirrors Issue #25 §5):
  * New Property / Unit / Binding write paths in this slice use **Membership
    state + role** as the exclusive authority. Legacy ``users.role`` is NEVER
    consulted here.
  * ``ACTIVE OWNER``: create Property, bind/revoke/replace Unit channels,
    edit ANY field on Property/Unit.
  * ``ACTIVE SECRETARY``: daily-maintenance edits on Property/Unit only
    (management_*, operational_notes, is_active, total_units on Property;
    floor/size_sqm/monthly_rent/status/unit_state/is_active/unit_number
    on Unit); NOT allowed to touch channel bindings.
  * ``REMOVED`` membership or no ACTIVE row → immediate 403 on every path.

Concurrency (CodeRabbit "concurrent publish 500" fix):
  * ``bind_unit_channel`` locks any currently-ACTIVE binding for the same
    (unit_id, purpose) with ``with_for_update`` before the REVOKE + INSERT
    dance, so concurrent binders serialize instead of IntegrityError + 500.
"""
from __future__ import annotations

from datetime import datetime, timezone
from collections.abc import Iterable

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from app.models.audit_log import AuditAction
from app.models.membership import Membership, MembershipState, Organization, OrganizationRole
from app.models.property import Property, Unit
from app.models.property_channel import BindingStatus, ChannelPurpose, UnitChannelBinding
from app.services.audit import record_audit, serialize_row
from app.services.membership import has_active_membership


# ---------------------------------------------------------------------------
# Organization-scope resolution helpers
# ---------------------------------------------------------------------------

class ScopeBlocked(PermissionError):
    """Caller has no ACTIVE Membership in the target organization/property."""


class OwnerRequired(PermissionError):
    """Caller is an ACTIVE SECRETARY but this action needs ACTIVE OWNER."""


def resolve_org_membership(
    db: Session, user_id: int, organization_id: int,
    *, role: OrganizationRole | Iterable[OrganizationRole] | None = None,
) -> Membership:
    """Return the ACTIVE Membership for (user_id, organization_id) or raise ScopeBlocked."""
    m = has_active_membership(db, user_id, organization_id, role=role)
    if m is None:
        if role is None:
            hint = "ACTIVE"
        elif isinstance(role, OrganizationRole):
            hint = f"ACTIVE {role.value}"
        else:
            hint = "ACTIVE " + "/".join(r.value for r in role)
        raise ScopeBlocked(
            f"user_id={user_id!r} has no {hint} membership in org={organization_id!r}"
        )
    return m


def property_org_id(db: Session, property_id: int) -> int | None:
    row = db.query(Property.organization_id).filter(
        Property.id == property_id, Property.deleted_at.is_(None)
    ).one_or_none()
    return row[0] if row else None


def unit_org_id(db: Session, unit_id: int) -> int | None:
    row = db.query(Property.organization_id).select_from(Unit).join(
        Property, Property.id == Unit.property_id
    ).filter(
        Unit.id == unit_id, Unit.deleted_at.is_(None)
    ).one_or_none()
    return row[0] if row else None


# ---------------------------------------------------------------------------
# Organization-scoped Property / Unit access (cross-org isolation enforced)
# ---------------------------------------------------------------------------

def scoped_get_property(
    db: Session, property_id: int, *, for_user_id: int,
) -> tuple[Property, Membership]:
    org_id = property_org_id(db, property_id)
    if org_id is None:
        raise LookupError(f"property {property_id} not found or has no organization")
    membership = resolve_org_membership(db, for_user_id, org_id)
    prop = db.query(Property).filter(
        Property.id == property_id, Property.deleted_at.is_(None)
    ).first()
    if prop is None:
        raise LookupError(f"property {property_id} not found")
    return prop, membership


def scoped_list_properties(
    db: Session, *, for_user_id: int,
) -> list[Property]:
    rows = (
        db.query(Property, Membership)
        .join(Membership, Membership.organization_id == Property.organization_id)
        .filter(
            Membership.user_id == for_user_id,
            Membership.state == MembershipState.ACTIVE,
            Property.deleted_at.is_(None),
        )
        .order_by(Property.id)
        .all()
    )
    return [p for p, _m in rows]


def scoped_get_unit(
    db: Session, unit_id: int, *, for_user_id: int,
) -> tuple[Unit, Membership]:
    org_id = unit_org_id(db, unit_id)
    if org_id is None:
        raise LookupError(f"unit {unit_id} not found or property has no organization")
    membership = resolve_org_membership(db, for_user_id, org_id)
    unit = db.query(Unit).filter(
        Unit.id == unit_id, Unit.deleted_at.is_(None)
    ).first()
    if unit is None:
        raise LookupError(f"unit {unit_id} not found")
    return unit, membership


def scoped_lookup_unit(
    db: Session,
    *,
    organization_id: int,
    property_id: int,
    unit_number: str,
    for_user_id: int,
) -> tuple[Unit, Membership]:
    """Stable ``organization + property + unit_number → Unit`` lookup.

    This is the Issue #25 §3 canonical unit-positioner so the rest of the
    system never confuses Org X / Building Bayshore / 1608 with Org Y /
    Building Whatever / 1608 — an ACTIVE Membership in organization_id is
    mandatory before we even look at the unit row.
    """
    membership = resolve_org_membership(db, for_user_id, organization_id)
    unit = (
        db.query(Unit)
        .join(Property, Property.id == Unit.property_id)
        .filter(
            Property.organization_id == organization_id,
            Property.id == property_id,
            Property.deleted_at.is_(None),
            Unit.unit_number == unit_number,
            Unit.deleted_at.is_(None),
            Unit.is_active.is_(True),
        )
        .first()
    )
    if unit is None:
        raise LookupError(
            f"unit org={organization_id} property={property_id} "
            f"unit_number={unit_number!r} not found"
        )
    return unit, membership


# ---------------------------------------------------------------------------
# SECRETARY allowlist for Property / Unit "daily maintenance" updates
# (all other fields require OWNER)
# ---------------------------------------------------------------------------

SECRETARY_EDITABLE_PROPERTY_FIELDS: frozenset[str] = frozenset({
    "management_company", "management_office_phone",
    "management_contact_person", "management_email",
    "management_office_location", "operational_notes",
    "is_active", "total_units",
})

SECRETARY_EDITABLE_UNIT_FIELDS: frozenset[str] = frozenset({
    "floor", "size_sqm", "monthly_rent",
    "status", "unit_state", "is_active", "unit_number",
})


def filter_secretary_property_updates(fields: set[str]) -> set[str]:
    """If any non-allowlisted field is present, raise OwnerRequired."""
    extra = fields - SECRETARY_EDITABLE_PROPERTY_FIELDS
    if extra:
        raise OwnerRequired(
            f"SECRETARY cannot update property fields {sorted(extra)}; "
            f"only ACTIVE OWNER may"
        )
    return fields & SECRETARY_EDITABLE_PROPERTY_FIELDS


def filter_secretary_unit_updates(fields: set[str]) -> set[str]:
    extra = fields - SECRETARY_EDITABLE_UNIT_FIELDS
    if extra:
        raise OwnerRequired(
            f"SECRETARY cannot update unit fields {sorted(extra)}; "
            f"only ACTIVE OWNER may"
        )
    return fields & SECRETARY_EDITABLE_UNIT_FIELDS


# ---------------------------------------------------------------------------
# Unit ↔ Channel binding lifecycle (OWNER only)
# ---------------------------------------------------------------------------

_PURPOSES_ALLOWED = {ChannelPurpose.archive.value, ChannelPurpose.business_group.value}


def _validate_purpose(purpose: str) -> str:
    if purpose not in _PURPOSES_ALLOWED:
        raise ValueError(
            f"purpose={purpose!r} not in {sorted(_PURPOSES_ALLOWED)}"
        )
    return purpose


def _now() -> datetime:
    return datetime.now(timezone.utc)


def list_bindings_for_unit(
    db: Session, unit_id: int, *, status: BindingStatus | None = None,
) -> list[UnitChannelBinding]:
    q = db.query(UnitChannelBinding).filter(UnitChannelBinding.unit_id == unit_id)
    if status is not None:
        q = q.filter(UnitChannelBinding.status == status.value)
    return q.order_by(UnitChannelBinding.id.desc()).all()


def get_active_binding(db: Session, unit_id: int, purpose: str) -> UnitChannelBinding | None:
    return db.query(UnitChannelBinding).filter(
        UnitChannelBinding.unit_id == unit_id,
        UnitChannelBinding.purpose == purpose,
        UnitChannelBinding.status == BindingStatus.ACTIVE.value,
    ).one_or_none()


def bind_unit_channel(
    db: Session,
    *,
    unit_id: int,
    purpose: str,
    channel_chat_id: int,
    thread_topic_id: int | None,
    actor_user_id: int,
    notes: str | None = None,
) -> UnitChannelBinding:
    """Bind or replace the ACTIVE binding for (unit_id, purpose). OWNER-only.

    Replace semantics (Issue #25 §4 "更换 binding → 旧 binding 失效但历史保留"):
      1. Verify actor has ACTIVE OWNER in the unit's org.
      2. SELECT … FOR UPDATE any currently-ACTIVE binding for (unit_id, purpose)
         so concurrent binders serialize instead of raising IntegrityError.
      3. If an ACTIVE exists and matches (chat_id/thread_id), return it unchanged.
      4. Otherwise mark the old one REVOKED (timestamp + revoked_by_membership_id
         + audit unit_channel_revoked) and INSERT the new ACTIVE one
         (+ audit unit_channel_replaced for a replace / unit_channel_bound for
           a first-time bind).
    """
    purpose = _validate_purpose(purpose)
    # (a) Scope & OWNER check
    org_id = unit_org_id(db, unit_id)
    if org_id is None:
        raise LookupError(f"unit {unit_id} not found or orphaned")
    owner_membership = resolve_org_membership(
        db, actor_user_id, org_id, role=OrganizationRole.OWNER,
    )
    unit = db.query(Unit).filter(
        Unit.id == unit_id, Unit.deleted_at.is_(None)
    ).first()
    if unit is None:
        raise LookupError(f"unit {unit_id} not found")

    now = _now()

    # (b) Row-level lock on currently-active binding → serialize concurrent binders
    lock_q = (
        select(UnitChannelBinding)
        .where(
            UnitChannelBinding.unit_id == unit_id,
            UnitChannelBinding.purpose == purpose,
            UnitChannelBinding.status == BindingStatus.ACTIVE.value,
        )
        .with_for_update()
    )
    existing = db.execute(lock_q).scalar_one_or_none()

    if (
        existing is not None
        and existing.channel_chat_id == channel_chat_id
        and existing.thread_topic_id == thread_topic_id
    ):
        # Same destination — pure no-op (mirrors the short-circuit semantic from
        # the archive hash guard; kept for deterministic callers).
        return existing

    action = AuditAction.unit_channel_replaced.value
    if existing is not None:
        # (c) Replace: revoke existing (REVOKED → history)
        old_serialized = serialize_row(existing)
        existing.status = BindingStatus.REVOKED.value
        existing.revoked_at = now
        existing.revoked_by_membership_id = owner_membership.id
        existing.updated_by = actor_user_id
        existing.updated_at = now
        db.flush()
        record_audit(
            db,
            table_name="unit_channel_bindings",
            record_id=existing.id,
            action=AuditAction.unit_channel_revoked.value,
            actor_id=actor_user_id,
            old_value=old_serialized,
            new_value=serialize_row(existing),
        )
    else:
        action = AuditAction.unit_channel_bound.value

    # (d) Insert new ACTIVE row
    new_binding = UnitChannelBinding(
        organization_id=org_id,
        unit_id=unit_id,
        purpose=purpose,
        channel_chat_id=channel_chat_id,
        thread_topic_id=thread_topic_id,
        status=BindingStatus.ACTIVE.value,
        revoked_at=None,
        revoked_by_membership_id=None,
        notes=notes,
        created_by=actor_user_id,
        updated_by=actor_user_id,
        created_at=now,
        updated_at=now,
    )
    db.add(new_binding)
    db.flush()
    record_audit(
        db,
        table_name="unit_channel_bindings",
        record_id=new_binding.id,
        action=action,
        actor_id=actor_user_id,
        new_value=serialize_row(new_binding),
    )
    return new_binding


def revoke_unit_channel(
    db: Session,
    *,
    binding_id: int,
    actor_user_id: int,
) -> UnitChannelBinding:
    """OWNER-only: revoke the current ACTIVE binding. Row is NOT deleted."""
    # Scope via binding.organization_id (not unit_org_id) so revoked-history
    # rows still enforce cross-org isolation on the revocation action itself.
    binding = db.query(UnitChannelBinding).filter(
        UnitChannelBinding.id == binding_id
    ).first()
    if binding is None:
        raise LookupError(f"binding {binding_id} not found")
    owner_membership = resolve_org_membership(
        db, actor_user_id, binding.organization_id, role=OrganizationRole.OWNER,
    )
    if binding.status == BindingStatus.REVOKED.value:
        return binding  # idempotent

    now = _now()
    old_serialized = serialize_row(binding)
    binding.status = BindingStatus.REVOKED.value
    binding.revoked_at = now
    binding.revoked_by_membership_id = owner_membership.id
    binding.updated_by = actor_user_id
    binding.updated_at = now
    db.flush()
    record_audit(
        db,
        table_name="unit_channel_bindings",
        record_id=binding.id,
        action=AuditAction.unit_channel_revoked.value,
        actor_id=actor_user_id,
        old_value=old_serialized,
        new_value=serialize_row(binding),
    )
    return binding
