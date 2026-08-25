import re
import secrets
import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from app import models  # noqa: F401  (register all tables on Base.metadata)
from app.config import settings
from app.core.security import hash_api_key
from app.database import get_db
from app.models.base import Base
from app.main import app
from app.models.user import User, UserRole
from app.models.identity import ApiCredential, CredentialState, Principal, PrincipalType
from app.models.membership import Organization, Membership, OrganizationRole, MembershipState
from app.models.property import Property, Unit
from app.models.tenant import Tenant
from app.models.financial import Expense, ExpenseStatus
from app.services.audit import audit_context
from decimal import Decimal
from datetime import date as _date

TEST_DB_NAME = os.getenv("PASAY_TEST_DB_NAME", "pasay_pm_test")

# P1-PASAY-NIGHTLY-PRODUCT-HARDENING-008 B1: fail closed — automated tests must
# deterministically use an isolated test DB and must never silently fall back
# to the live/production database. The session test engine DROPS and recreates
# every table per test, so a PASAY_TEST_DB_NAME override (or a misconfigured
# DATABASE_URL) pointing at the live/production database would destroy data.
# Any such configuration is refused here, deterministically, before any engine
# is created.
_CONFIGURED_DB = make_url(settings.database_url).database
_FORBIDDEN_TEST_DBS = {"pasay_pm", "pasay_pm_win_test"}

# Strict pasay_pm_* / pasay_*_gate_* / pasay_freeze_* / pasay_closeout_* prefix
# are the only allowed isolated test-database name families. Any other name
# (including the bare legacy "pasay_pm_test" default fallback when not otherwise
# overridden) is refused unless it matches one of the approved patterns.
_ALLOWED_TEST_DB_PREFIX_RE = re.compile(
    r"^pasay_(?:pm|gate|freeze|closeout|return|fresh|alembic)[a-z0-9_]*_[a-zA-Z0-9_]+$"
)


def _test_db_allowed(name: str, configured_db: str) -> bool:
    """True only when the requested test DB is a real, isolated test database
    (never the configured live DB and never a production/live-named DB).

    Strict prefix whitelist (§四·原则6): only names matching
    ``pasay_(pm|gate*|freeze|closeout|return|fresh|alembic)_*`` are accepted.
    Any other name including the historical bare ``pasay_pm_test" pattern without
    underscore prefix-family is refused; forbidden literal names are also refused."""
    if not name:
        return False
    if name == configured_db:
        return False
    if name in _FORBIDDEN_TEST_DBS:
        return False
    return bool(_ALLOWED_TEST_DB_PREFIX_RE.match(name))


if not _test_db_allowed(TEST_DB_NAME, _CONFIGURED_DB):
    raise SystemExit(
        "REFUSED: PASAY_TEST_DB_NAME=%r would run tests against the "
        "live/production database (configured DATABASE_URL db=%r). "
        "Set PASAY_TEST_DB_NAME to an isolated test database "
        "(e.g. pasay_pm_r1_20260101_001 or pasay_freeze_gate_001)."
        % (TEST_DB_NAME, _CONFIGURED_DB)
    )


def _test_url():
    return make_url(settings.database_url).set(database=TEST_DB_NAME)


@pytest.fixture(scope="session")
def test_engine():
    """Create the dedicated test database once per session.

    PostgreSQL-only test harness (PASAY-TASK-011 FIX8: SQLite compatibility
    route was a wrong CI direction — GitHub Actions uses PostgreSQL 16).
    """
    base_url = make_url(settings.database_url)
    base_dialect = base_url.get_dialect().name

    if base_dialect != "postgresql":
        raise RuntimeError(
            "tests/conftest.py test_engine: unsupported dialect %r "
            "(PASAY-TASK-011 FIX8: test harness is PostgreSQL-only; "
            "set DATABASE_URL to a postgresql+psycopg2:// URL)" % base_dialect
        )

    admin_url = base_url.set(database="postgres")
    admin_engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        with admin_engine.connect() as conn:
            exists = conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :name"),
                {"name": TEST_DB_NAME},
            ).scalar()
            if not exists:
                conn.execute(text(f'CREATE DATABASE "{TEST_DB_NAME}"'))
    finally:
        admin_engine.dispose()
    engine = create_engine(_test_url())

    try:
        yield engine
    finally:
        engine.dispose()


def _pg_drop_all_tables_cascade(conn):
    """Drop every public table with CASCADE, bypassing SQLA's fragile
    metadata-driven constraint-drop order that raises UndefinedObject for
    cross-table composite FKs (leases<->moi<->ds triangle + self-ref FK) when
    the schema is empty or in a partial state.
    """
    rows = conn.execute(text(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
    )).all()
    for (tablename,) in rows:
        conn.execute(text(f'DROP TABLE IF EXISTS "{tablename}" CASCADE'))
    for seq in conn.execute(text(
        "SELECT sequencename FROM pg_sequences WHERE schemaname = 'public'"
    )).all():
        conn.execute(text(f'DROP SEQUENCE IF EXISTS "{seq[0]}" CASCADE'))
    for enum_t in conn.execute(text(
        "SELECT t.typname FROM pg_type t JOIN pg_enum e ON e.enumtypid = t.oid "
        "JOIN pg_namespace n ON n.oid = t.typnamespace WHERE n.nspname = 'public' "
        "GROUP BY t.typname"
    )).all():
        conn.execute(text(f'DROP TYPE IF EXISTS "{enum_t[0]}" CASCADE'))


@pytest.fixture()
def db_session(test_engine):
    """Rebuild the schema for every test (simple, deterministic)."""
    audit_context.set((None, None, None, None))
    with test_engine.connect() as conn:
        _pg_drop_all_tables_cascade(conn)
        conn.commit()
        Base.metadata.create_all(conn)
        conn.commit()
    Session = sessionmaker(bind=test_engine, autoflush=False, expire_on_commit=False)
    db = Session()
    for name in ("scheduler", "reconcile", "notifier", "backfill"):
        principal = Principal(name=name, principal_type=PrincipalType.SYSTEM)
        db.add(principal)
        db.flush()
        db.add(ApiCredential(principal_id=principal.id,
            key_hash=hash_api_key(f"pasay-v13-internal-record:{name}"),
            purpose=f"internal:{name}", state=CredentialState.ACTIVE))
    db.add_all([
        Principal(name="lily", principal_type=PrincipalType.AI_AGENT),
        Principal(name="hermes", principal_type=PrincipalType.AI_AGENT),
    ])
    db.commit()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture()
def client(db_session):
    def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def make_user(db, username, role, active=True):
    key = secrets.token_urlsafe(24)
    user = User(username=username, role=role, api_key_hash=hash_api_key(key), is_active=active)
    db.add(user)
    db.flush()
    principal = Principal(
        name=username,
        principal_type=PrincipalType.HUMAN,
        user_id=user.id,
        is_active=active,
    )
    db.add(principal)
    db.flush()
    db.add(ApiCredential(
        principal_id=principal.id,
        key_hash=hash_api_key(key),
        purpose="legacy_human",
        state=CredentialState.ACTIVE,
    ))
    db.commit()
    db.refresh(user)
    return user, key


@pytest.fixture()
def admin(db_session):
    user, _key = make_user(db_session, "admin", UserRole.admin)
    ensure_default_org(db_session)
    from app.models.membership import Membership, OrganizationRole, MembershipState
    from app.models.membership import Organization
    org = db_session.query(Organization).order_by(Organization.id.asc()).first()
    if org:
        exists = db_session.query(Membership.id).filter(
            Membership.user_id == user.id,
            Membership.organization_id == org.id,
            Membership.state == MembershipState.ACTIVE,
        ).first()
        if not exists:
            m = Membership(user_id=user.id, organization_id=org.id,
                           role=OrganizationRole.OWNER, state=MembershipState.ACTIVE)
            db_session.add(m)
            db_session.commit()
    return user, _key


@pytest.fixture()
def manager(db_session):
    user, _key = make_user(db_session, "manager", UserRole.manager)
    ensure_default_org(db_session)
    from app.models.membership import Membership, OrganizationRole, MembershipState
    from app.models.membership import Organization
    org = db_session.query(Organization).order_by(Organization.id.asc()).first()
    if org:
        exists = db_session.query(Membership.id).filter(
            Membership.user_id == user.id,
            Membership.organization_id == org.id,
            Membership.state == MembershipState.ACTIVE,
        ).first()
        if not exists:
            m = Membership(user_id=user.id, organization_id=org.id,
                           role=OrganizationRole.SECRETARY, state=MembershipState.ACTIVE)
            db_session.add(m)
            db_session.commit()
    return user, _key


@pytest.fixture()
def agent(db_session):
    user, _key = make_user(db_session, "agent", UserRole.agent)
    ensure_default_org(db_session)
    from app.models.membership import Membership, OrganizationRole, MembershipState
    from app.models.membership import Organization
    org = db_session.query(Organization).order_by(Organization.id.asc()).first()
    if org:
        exists = db_session.query(Membership.id).filter(
            Membership.user_id == user.id,
            Membership.organization_id == org.id,
            Membership.state == MembershipState.ACTIVE,
        ).first()
        if not exists:
            m = Membership(user_id=user.id, organization_id=org.id,
                           role=OrganizationRole.SECRETARY, state=MembershipState.ACTIVE)
            db_session.add(m)
            db_session.commit()
    return user, _key


def _headers(user_key):
    return {"Authorization": f"Bearer {user_key}"}


@pytest.fixture()
def admin_headers(admin):
    return _headers(admin[1])


@pytest.fixture()
def manager_headers(manager):
    return _headers(manager[1])


@pytest.fixture()
def agent_headers(agent):
    return _headers(agent[1])


# --- organization fixtures ---

@pytest.fixture()
def org_a(db_session):
    org = Organization(name="Org-A", display_name="Pasay Org A")
    db_session.add(org)
    db_session.flush()
    admin_user = db_session.query(User).filter(User.username == "admin").first()
    if admin_user is not None:
        db_session.add(Membership(
            organization_id=org.id,
            user_id=admin_user.id,
            role=OrganizationRole.OWNER,
            state=MembershipState.ACTIVE,
        ))
    manager_user = db_session.query(User).filter(User.username == "manager").first()
    if manager_user is not None:
        db_session.add(Membership(
            organization_id=org.id,
            user_id=manager_user.id,
            role=OrganizationRole.SECRETARY,
            state=MembershipState.ACTIVE,
        ))
    agent_user = db_session.query(User).filter(User.username == "agent").first()
    if agent_user is not None:
        db_session.add(Membership(
            organization_id=org.id,
            user_id=agent_user.id,
            role=OrganizationRole.SECRETARY,
            state=MembershipState.ACTIVE,
        ))
    db_session.commit()
    db_session.refresh(org)
    return org


@pytest.fixture()
def org_b(db_session):
    org = Organization(name="Org-B", display_name="Pasay Org B")
    db_session.add(org)
    db_session.commit()
    db_session.refresh(org)
    return org


@pytest.fixture()
def owner_a(db_session, org_a):
    user, api_key = make_user(db_session, "owner_a", UserRole.admin)
    membership = Membership(
        organization_id=org_a.id,
        user_id=user.id,
        role=OrganizationRole.OWNER,
        state=MembershipState.ACTIVE,
    )
    db_session.add(membership)
    db_session.commit()
    db_session.refresh(membership)
    return user, api_key, membership


@pytest.fixture()
def secretary_a(db_session, org_a):
    user, api_key = make_user(db_session, "secretary_a", UserRole.manager)
    membership = Membership(
        organization_id=org_a.id,
        user_id=user.id,
        role=OrganizationRole.SECRETARY,
        state=MembershipState.ACTIVE,
    )
    db_session.add(membership)
    db_session.commit()
    db_session.refresh(membership)
    return user, api_key, membership


@pytest.fixture()
def owner_b(db_session, org_b):
    user, api_key = make_user(db_session, "owner_b", UserRole.admin)
    membership = Membership(
        organization_id=org_b.id,
        user_id=user.id,
        role=OrganizationRole.OWNER,
        state=MembershipState.ACTIVE,
    )
    db_session.add(membership)
    db_session.commit()
    db_session.refresh(membership)
    return user, api_key, membership


# --- shared factory helpers (for tests that directly build ORM objects) --------

def ensure_default_org(db, *, name: str = "Org-A", display_name: str = "Pasay Org A"):
    """Lazily create (or return the existing) named Organization with a deterministic
    admin/manager/agent membership chain.

    Used by tests that directly construct Property/Tenant/ExpenseClaim ORM objects
    instead of going through the API fixtures. Always builds a real
    ``org → (membership → User.role) → Property → Unit/Tenant → Lease → Expense``
    chain when combined with :func:`seed_property` / :func:`seed_tenant` below.

    Idempotent per session/database: returns the same Organization when an org
    with the same ``name`` already exists (no duplicate org creation)."""
    existing = db.query(Organization).filter(Organization.name == name).first()
    if existing is not None:
        return existing
    org = Organization(name=name, display_name=display_name)
    db.add(org)
    db.flush()
    role_mapping = {
        UserRole.admin: OrganizationRole.OWNER,
        UserRole.manager: OrganizationRole.SECRETARY,
        UserRole.agent: OrganizationRole.SECRETARY,
    }
    for (username, user_role) in (
        ("admin", UserRole.admin),
        ("manager", UserRole.manager),
        ("agent", UserRole.agent),
    ):
        user = db.query(User).filter(User.username == username).first()
        if user is None:
            continue
        db.add(Membership(
            organization_id=org.id,
            user_id=user.id,
            role=role_mapping[user_role],
            state=MembershipState.ACTIVE,
        ))
    db.flush()
    return org


def seed_property(db, *, org=None, **kw):
    """Construct and flush a Property with a real organization_id.

    When ``org`` is explicitly None the property is attached to the deterministic
    default organization produced by :func:`ensure_default_org`. Any extra keyword
    arguments override defaults (name/address/city/total_units)."""
    if org is None:
        org = ensure_default_org(db)
    data = {
        "name": "Sunset Tower",
        "address": "1 Roxas Blvd",
        "city": "Pasay",
        "total_units": 4,
    }
    data.update(kw)
    data["organization_id"] = org.id
    prop = Property(**data)
    db.add(prop)
    db.flush()
    return prop


def seed_unit(db, *, prop=None, **kw):
    if prop is None:
        prop = seed_property(db)
    data = {
        "property_id": prop.id,
        "unit_number": "101",
        "floor": "1",
        "size_sqm": Decimal("32.50"),
        "monthly_rent": Decimal("12000.00"),
        "status": "vacant",
    }
    data.update(kw)
    unit = Unit(**data)
    db.add(unit)
    db.flush()
    return unit


def seed_tenant(db, *, org=None, **kw):
    if org is None:
        org = ensure_default_org(db)
    data = {
        "full_name": "Juan Dela Cruz",
        "phone": "+639170000000",
        "email": "juan@example.com",
        "organization_id": org.id,
    }
    data.update(kw)
    tenant = Tenant(**data)
    db.add(tenant)
    db.flush()
    return tenant


def seed_expense(db, *, prop=None, **kw):
    if prop is None:
        prop = seed_property(db)
    data = {
        "property_id": prop.id,
        "expense_date": _date.today(),
        "category": "maintenance",
        "description": "Plumbing fix",
        "amount": Decimal("2500.00"),
        "payee": "Local Vendor Co",
        "status": ExpenseStatus.pending,
    }
    data.update(kw)
    exp = Expense(**data)
    db.add(exp)
    db.flush()
    return exp


# --- shared data fixtures (created through the API as Org Owner) ---

@pytest.fixture()
def property_id(client, owner_a, org_a):
    resp = client.post(
        "/api/v1/properties",
        json={
            "name": "Sunset Tower",
            "address": "1 Roxas Blvd",
            "city": "Pasay",
            "total_units": 4,
            "organization_id": org_a.id,
        },
        headers=_headers(owner_a[1]),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


@pytest.fixture()
def unit_id(client, owner_a, property_id):
    resp = client.post(
        "/api/v1/units",
        json={
            "property_id": property_id,
            "unit_number": "101",
            "floor": "1",
            "size_sqm": "32.50",
            "monthly_rent": "12000.00",
            "status": "vacant",
        },
        headers=_headers(owner_a[1]),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


@pytest.fixture()
def tenant_id(client, owner_a, org_a):
    resp = client.post(
        "/api/v1/tenants",
        json={
            "full_name": "Juan Dela Cruz",
            "phone": "+639170000000",
            "email": "juan@example.com",
            "organization_id": org_a.id,
        },
        headers=_headers(owner_a[1]),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


@pytest.fixture()
def lease_id(client, owner_a, unit_id, tenant_id):
    resp = client.post(
        "/api/v1/leases",
        json={
            "unit_id": unit_id,
            "tenant_id": tenant_id,
            "start_date": "2025-07-01",
            "end_date": "2026-06-30",
            "monthly_rent": "12000.00",
            "deposit": "24000.00",
            "status": "active",
        },
        headers=_headers(owner_a[1]),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]
