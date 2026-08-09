import secrets

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

TEST_DB_NAME = "pasay_pm_test"


def _test_url():
    return make_url(settings.database_url).set(database=TEST_DB_NAME)


@pytest.fixture(scope="session")
def test_engine():
    """Create the dedicated `pasay_pm_test` database once per session."""
    admin_url = make_url(settings.database_url).set(database="postgres")
    admin_engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with admin_engine.connect() as conn:
        exists = conn.execute(
            text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": TEST_DB_NAME}
        ).scalar()
        if not exists:
            conn.execute(text(f'CREATE DATABASE "{TEST_DB_NAME}"'))
    admin_engine.dispose()

    engine = create_engine(_test_url())
    yield engine
    engine.dispose()


@pytest.fixture()
def db_session(test_engine):
    """Rebuild the schema for every test (simple, deterministic)."""
    Base.metadata.drop_all(test_engine)
    Base.metadata.create_all(test_engine)
    Session = sessionmaker(bind=test_engine, autoflush=False, expire_on_commit=False)
    db = Session()
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
    db.commit()
    db.refresh(user)
    return user, key


@pytest.fixture()
def admin(db_session):
    return make_user(db_session, "admin", UserRole.admin)


@pytest.fixture()
def manager(db_session):
    return make_user(db_session, "manager", UserRole.manager)


@pytest.fixture()
def agent(db_session):
    return make_user(db_session, "agent", UserRole.agent)


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


# --- shared data fixtures (created through the API as admin) ---

@pytest.fixture()
def property_id(client, admin_headers):
    resp = client.post(
        "/api/v1/properties",
        json={"name": "Sunset Tower", "address": "1 Roxas Blvd", "city": "Pasay", "total_units": 4},
        headers=admin_headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


@pytest.fixture()
def unit_id(client, admin_headers, property_id):
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
        headers=admin_headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


@pytest.fixture()
def tenant_id(client, admin_headers):
    resp = client.post(
        "/api/v1/tenants",
        json={"full_name": "Juan Dela Cruz", "phone": "+639170000000", "email": "juan@example.com"},
        headers=admin_headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


@pytest.fixture()
def lease_id(client, admin_headers, unit_id, tenant_id):
    resp = client.post(
        "/api/v1/leases",
        json={
            "unit_id": unit_id,
            "tenant_id": tenant_id,
            "start_date": "2026-01-01",
            "end_date": "2026-12-31",
            "monthly_rent": "12000.00",
            "deposit": "24000.00",
            "status": "active",
        },
        headers=admin_headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]
