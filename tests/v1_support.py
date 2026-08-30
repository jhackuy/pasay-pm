"""Shared helpers for the V1 rewrite test-suite.

Deliberately NOT a conftest: the legacy ``tests/conftest.py`` owns the
PostgreSQL harness plus the ``db_session`` / ``client`` fixture names, so the
V1 rewrite tests build their own isolated engine and never reuse those names.

The engine is a throwaway *file-backed* SQLite database rather than
``:memory:`` because FastAPI's TestClient runs sync endpoints on worker
threads, and an in-memory SQLite database is not shared across threads.
"""
from __future__ import annotations

import os
import shutil
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Iterator

from sqlalchemy import event

from app.core.permissions import Principal, Role
from app.core.security import generate_api_key, hash_api_key
from app.db.session import bind_engine, get_session_factory, reset_engine_cache
from app.v1.models.base import (
    LeaseState,
    MembershipState,
    UnitStatus,
    V1Base,
)
from app.v1.models.foundation import (
    ApiCredential,
    Membership,
    Organization,
    User,
)
from app.v1.models.property import Property, Unit
from app.v1.models.tenant_lease import Lease, Tenant

# Imported for its side effect: registers the rent/payment tables on
# V1Base.metadata so create_all() builds the full schema.
import app.v1.models.rent_payment  # noqa: F401


@dataclass(frozen=True)
class Workspace:
    """A fully seeded org: OWNER + SECRETARY + property/unit/tenant/ACTIVE lease."""

    org_id: int
    owner_user_id: int
    owner_api_key: str
    secretary_user_id: int
    secretary_api_key: str
    property_id: int
    unit_id: int
    tenant_id: int
    lease_id: int

    @property
    def owner(self) -> Principal:
        return Principal(
            user_id=self.owner_user_id, org_id=self.org_id, role=Role.OWNER,
        )

    @property
    def secretary(self) -> Principal:
        return Principal(
            user_id=self.secretary_user_id,
            org_id=self.org_id,
            role=Role.SECRETARY,
        )

    def owner_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.owner_api_key}"}

    def secretary_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.secretary_api_key}"}


@contextmanager
def v1_engine_ctx() -> Iterator[object]:
    """Bind a throwaway SQLite engine with the whole V1 schema created."""
    tmpdir = tempfile.mkdtemp(prefix="pasay-v1-test-")
    url = f"sqlite:///{os.path.join(tmpdir, 'v1.db')}?check_same_thread=False"
    previous_url = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = url
    reset_engine_cache()
    engine = bind_engine(url)

    @event.listens_for(engine, "connect")
    def _enable_sqlite_foreign_keys(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    V1Base.metadata.create_all(engine)
    try:
        yield engine
    finally:
        reset_engine_cache()
        if previous_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous_url
        shutil.rmtree(tmpdir, ignore_errors=True)


@contextmanager
def v1_session_ctx() -> Iterator[object]:
    """Bind the engine and yield a single Session for the whole test."""
    with v1_engine_ctx():
        session = get_session_factory()()
        try:
            yield session
        finally:
            session.close()


def seed_workspace(session, *, name: str) -> Workspace:
    """Create org + OWNER + SECRETARY + property/unit/tenant/ACTIVE lease."""
    org = Organization(name=name)
    session.add(org)
    session.flush()

    owner = User(
        username=f"{name}-owner",
        display_name=f"{name} Owner",
        default_language="en-US",
    )
    secretary = User(
        username=f"{name}-secretary",
        display_name=f"{name} Secretary",
        default_language="en-US",
    )
    session.add_all([owner, secretary])
    session.flush()

    owner_key = generate_api_key()
    secretary_key = generate_api_key()
    session.add_all(
        [
            Membership(
                org_id=org.id,
                user_id=owner.id,
                role=Role.OWNER.value,
                state=MembershipState.ACTIVE.value,
                is_bootstrap=True,
            ),
            Membership(
                org_id=org.id,
                user_id=secretary.id,
                role=Role.SECRETARY.value,
                state=MembershipState.ACTIVE.value,
            ),
            ApiCredential(
                user_id=owner.id,
                key_hash=hash_api_key(owner_key),
                is_active=True,
            ),
            ApiCredential(
                user_id=secretary.id,
                key_hash=hash_api_key(secretary_key),
                is_active=True,
            ),
        ]
    )

    prop = Property(
        org_id=org.id,
        name=f"{name} Tower",
        address_line1="1 Roxas Blvd",
        city="Pasay",
    )
    session.add(prop)
    session.flush()

    unit = Unit(
        property_id=prop.id,
        org_id=org.id,
        label="7777",
        bedrooms=1,
        bathrooms=1,
        monthly_rent=Decimal("12000.00"),
        status=UnitStatus.OCCUPIED.value,
    )
    session.add(unit)
    session.flush()

    tenant = Tenant(
        org_id=org.id,
        full_name=f"{name} Tenant",
        contact_phone="+639170000000",
    )
    session.add(tenant)
    session.flush()

    lease = Lease(
        org_id=org.id,
        unit_id=unit.id,
        tenant_id=tenant.id,
        start_date=date(2026, 1, 1),
        end_date=date(2026, 12, 31),
        monthly_rent=Decimal("12000.00"),
        deposit=Decimal("24000.00"),
        state=LeaseState.ACTIVE.value,
    )
    session.add(lease)
    session.commit()

    return Workspace(
        org_id=org.id,
        owner_user_id=owner.id,
        owner_api_key=owner_key,
        secretary_user_id=secretary.id,
        secretary_api_key=secretary_key,
        property_id=prop.id,
        unit_id=unit.id,
        tenant_id=tenant.id,
        lease_id=lease.id,
    )


def seed_draft_lease(session, workspace: Workspace) -> int:
    """Add a second unit/tenant/lease left in DRAFT for negative tests."""
    unit = Unit(
        property_id=workspace.property_id,
        org_id=workspace.org_id,
        label="draft-unit",
        bedrooms=1,
        bathrooms=1,
        monthly_rent=Decimal("9000.00"),
        status=UnitStatus.AVAILABLE.value,
    )
    session.add(unit)
    session.flush()
    tenant = Tenant(org_id=workspace.org_id, full_name="Draft Tenant")
    session.add(tenant)
    session.flush()
    lease = Lease(
        org_id=workspace.org_id,
        unit_id=unit.id,
        tenant_id=tenant.id,
        start_date=date(2026, 2, 1),
        end_date=date(2026, 11, 30),
        monthly_rent=Decimal("9000.00"),
        deposit=Decimal("0.00"),
        state=LeaseState.DRAFT.value,
    )
    session.add(lease)
    session.commit()
    return lease.id


__all__ = [
    "Workspace",
    "seed_draft_lease",
    "seed_workspace",
    "v1_engine_ctx",
    "v1_session_ctx",
]
