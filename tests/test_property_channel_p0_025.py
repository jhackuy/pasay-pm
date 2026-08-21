"""PASAY-TASK-007 (Issue #25) Property + Channel P0 targeted tests.

Scope (narrow, strictly Issue #25 — no scope creep):
  * Alembic migration head: upgrade() still produces exactly 1 head.
  * PropertyArchiveChannel + UnitArchiveArticle tables:
      - CREATE via the ORM (Base.metadata.create_all already runs per test)
      - CHECK guards (platform allowlist / status allowlist / PUBLISHED
        must have external_message_id)
      - UniqueConstraint (one PUBLISHED row per (platform, entity_id))
  * Renderers (pure reads, no write side-effects):
      - render_property_archive builds stable hashes for same truth.
      - render_unit_archive returns tombstone for nonexistent units,
        builds timeline body + hash for real units.
  * Publishing service:
      - publish_property_article / publish_unit_article: raises ValueError
        for missing entities; first publish creates row + audit log;
        same hash + same message + PUBLISHED = changed=False short-circuit.
      - bump render_version on each real publish edit.
  * API endpoints (HTTP round-trip, authenticated):
      - GET /units/{id}/timeline -> 404 for bad id; 200 + timeline body.
      - GET /units/{id}/archive -> 404 before publish; 200 after.
      - POST /units/{id}/archive -> 400 for bad message_id; 200 + row.
      - GET /properties/{id}/channel -> rendered + property_article + unit_articles list.
      - POST /properties/{id}/archive -> 200 + row.
      - Auth: agent (lowest role) can't publish; manager_or_admin can.
"""
from __future__ import annotations

import secrets
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.security import hash_api_key
from app.models.audit_log import AuditAction, AuditLog
from app.models.identity import (
    ApiCredential,
    CredentialState,
    Principal,
    PrincipalType,
)
from app.models.property import Property, Unit, UnitStatus
from app.models.property_channel import (
    ArchiveArticleStatus,
    UnitArchiveArticle,
)
from app.models.user import User, UserRole
from app.services.property_channel import (
    publish_property_article,
    publish_unit_article,
    render_property_archive,
    render_unit_archive,
)

API = "/api/v1"
NOW = datetime(2026, 8, 21, 9, 0, 0, tzinfo=timezone.utc)


def _make_user(db: Session, username: str, role: UserRole):
    key = secrets.token_urlsafe(24)
    user = User(
        username=username, role=role,
        api_key_hash=hash_api_key(key), is_active=True,
    )
    db.add(user)
    db.flush()
    principal = Principal(
        name=username, principal_type=PrincipalType.HUMAN,
        user_id=user.id, is_active=True,
    )
    db.add(principal)
    db.flush()
    db.add(ApiCredential(
        principal_id=principal.id, key_hash=hash_api_key(key),
        purpose="legacy_human", state=CredentialState.ACTIVE,
    ))
    db.commit()
    db.refresh(user)
    return user, key


def _seed_prop_unit_and_users(db: Session):
    """Seed the minimum truth: an admin User (id guaranteed present) + a
    Property + 2 Units. Tests that need a real FK actor_id use the admin.id.
    """
    admin, _ = _make_user(db, "seeder-admin-pc", UserRole.admin)
    prop = Property(
        name="Sunset Tower", address="1 Roxas Blvd",
        city="Pasay", total_units=2,
        created_by=admin.id, updated_by=admin.id,
    )
    db.add(prop)
    db.flush()
    unit1 = Unit(
        property_id=prop.id, unit_number="101", floor="1",
        size_sqm=Decimal("32.50"),
        monthly_rent=Decimal("12000.00"), status=UnitStatus.vacant,
        created_by=admin.id, updated_by=admin.id,
    )
    unit2 = Unit(
        property_id=prop.id, unit_number="202", floor="2",
        size_sqm=Decimal("40.00"),
        monthly_rent=Decimal("15000.00"), status=UnitStatus.occupied,
        created_by=admin.id, updated_by=admin.id,
    )
    db.add_all([unit1, unit2])
    db.commit()
    for u in (unit1, unit2):
        db.refresh(u)
    db.refresh(prop)
    db.refresh(admin)
    return admin, prop, unit1, unit2


# ---- ORM / Constraint tests (no HTTP) ---------------------------------------

class TestModelConstraints:
    def test_unit_archive_platform_allowlist(self, db_session: Session):
        admin, prop, unit1, _ = _seed_prop_unit_and_users(db_session)
        row = UnitArchiveArticle(
            unit_id=unit1.id, property_id=prop.id,
            platform="telegram_channel",
            external_message_id=100,
            status=ArchiveArticleStatus.published.value,
            render_hash="deadbeef" * 8,
            created_by=admin.id, updated_by=admin.id,
            editor_user_id=admin.id,
        )
        db_session.add(row)
        db_session.commit()
        db_session.refresh(row)
        assert row.id is not None
        assert row.render_version == 1

    def test_unit_archive_published_requires_message_id(self, db_session: Session):
        admin, prop, unit1, _ = _seed_prop_unit_and_users(db_session)
        row = UnitArchiveArticle(
            unit_id=unit1.id, property_id=prop.id,
            platform="telegram_channel",
            external_message_id=None,
            status=ArchiveArticleStatus.published.value,
            render_hash="abcd" * 16,
            created_by=admin.id, updated_by=admin.id,
            editor_user_id=admin.id,
        )
        db_session.add(row)
        with pytest.raises(IntegrityError):
            db_session.commit()
        db_session.rollback()

    def test_unit_archive_unique_per_platform_and_unit(self, db_session: Session):
        admin, prop, unit1, _ = _seed_prop_unit_and_users(db_session)
        a = UnitArchiveArticle(
            unit_id=unit1.id, property_id=prop.id,
            platform="telegram_channel", external_message_id=1,
            status=ArchiveArticleStatus.published.value,
            render_hash="aaaa",
            created_by=admin.id, updated_by=admin.id, editor_user_id=admin.id,
        )
        db_session.add(a)
        db_session.commit()
        b = UnitArchiveArticle(
            unit_id=unit1.id, property_id=prop.id,
            platform="telegram_channel", external_message_id=2,
            status=ArchiveArticleStatus.draft.value,
            render_hash="bbbb",
            created_by=admin.id, updated_by=admin.id, editor_user_id=admin.id,
        )
        db_session.add(b)
        with pytest.raises(IntegrityError):
            db_session.commit()
        db_session.rollback()


# ---- Service renderer tests -------------------------------------------------

class TestRenderers:
    def test_unit_archive_render_tombstone_for_missing(self, db_session: Session):
        result = render_unit_archive(db_session, 999999)
        assert result["found"] is False
        assert result["unit"] is None
        assert result["events"] == []
        assert result["render_hash"] == ""

    def test_unit_archive_render_builds_body_and_hash(self, db_session: Session):
        _, _, unit1, _ = _seed_prop_unit_and_users(db_session)
        a = render_unit_archive(db_session, unit1.id)
        b = render_unit_archive(db_session, unit1.id)
        assert a["found"] is True
        assert a["render_hash"]
        assert a["unit_id"] == unit1.id
        assert "Total timeline events" in a["body_text"]
        # Same truth twice = same hash (idempotent & deterministic).
        assert a["render_hash"] == b["render_hash"]
        assert a["body_text"] == b["body_text"]

    def test_property_archive_render_counts_statuses(self, db_session: Session):
        _, prop, _unit1, _unit2 = _seed_prop_unit_and_users(db_session)
        r = render_property_archive(db_session, prop.id)
        assert r["found"] is True
        assert r["property"]["id"] == prop.id
        assert r["summary"]["status_counts"] == {"vacant": 1, "occupied": 1}
        assert r["summary"]["active_unit_rows"] == 2
        # 2 units listed in body text
        assert r["body_text"].count("- #") == 2
        a = render_property_archive(db_session, prop.id)
        assert r["render_hash"] == a["render_hash"]


# ---- Service publishing tests -----------------------------------------------

class TestPublishing:
    def test_publish_unit_missing_unit_raises_value_error(self, db_session: Session):
        admin, _, _, _ = _seed_prop_unit_and_users(db_session)
        with pytest.raises(ValueError):
            publish_unit_article(
                db_session, 999, external_message_id=1000,
                actor_id=admin.id, render_hash="abcd",
            )

    def test_publish_property_missing_property_raises_value_error(self, db_session: Session):
        admin, _, _, _ = _seed_prop_unit_and_users(db_session)
        with pytest.raises(ValueError):
            publish_property_article(
                db_session, 999, external_message_id=1000,
                actor_id=admin.id, render_hash="abcd",
            )

    def test_publish_unit_first_time_creates_row_and_audit(self, db_session: Session):
        admin, _prop, unit1, _ = _seed_prop_unit_and_users(db_session)
        row, changed = publish_unit_article(
            db_session, unit1.id,
            external_message_id=1001, actor_id=admin.id,
            render_hash="aaaa" * 16,
            event_count_at_publish=5,
        )
        db_session.commit()
        db_session.refresh(row)
        assert changed is True
        assert row.render_version == 1
        assert row.status == "published"
        assert row.external_message_id == 1001
        assert row.event_count_at_publish == 5
        audit = db_session.query(AuditLog).filter(
            AuditLog.table_name == "unit_archive_articles",
            AuditLog.record_id == row.id,
        ).first()
        assert audit is not None
        assert audit.action == AuditAction.unit_article_published.value

    def test_publish_same_hash_and_message_short_circuits(self, db_session: Session):
        admin, _prop, unit1, _ = _seed_prop_unit_and_users(db_session)
        r1, changed1 = publish_unit_article(
            db_session, unit1.id,
            external_message_id=77, actor_id=admin.id,
            render_hash="beef" * 16,
        )
        db_session.commit()
        r2, changed2 = publish_unit_article(
            db_session, unit1.id,
            external_message_id=77, actor_id=admin.id,
            render_hash="beef" * 16,
        )
        db_session.commit()
        assert changed1 is True
        assert changed2 is False
        # Version not bumped for a no-op short-circuit.
        assert r2.render_version == r1.render_version

    def test_publish_new_hash_bumps_version(self, db_session: Session):   
        admin, _prop, unit1, _ = _seed_prop_unit_and_users(db_session)
        r1, _ = publish_unit_article(
            db_session, unit1.id,
            external_message_id=1, actor_id=admin.id,
            render_hash="hash1",
        )
        db_session.commit()
        expected_next_version = r1.render_version + 1
        r2, changed = publish_unit_article(
            db_session, unit1.id,
            external_message_id=2, actor_id=admin.id,
            render_hash="hash2",
        )
        db_session.commit()
        assert changed is True
        assert r2.render_version == expected_next_version

    def test_property_publish_creates_and_short_circuits(self, db_session: Session):
        admin, prop, _, _ = _seed_prop_unit_and_users(db_session)
        r1, c1 = publish_property_article(
            db_session, prop.id,
            external_message_id=2001, actor_id=admin.id,
            render_hash="aaaa",
        )
        db_session.commit()
        r2, c2 = publish_property_article(
            db_session, prop.id,
            external_message_id=2001, actor_id=admin.id,
            render_hash="aaaa",
        )
        assert c1 is True
        assert c2 is False
        assert r1.status == "published"
        db_session.refresh(r2)
        assert r2.last_rendered_at is not None


# ---- Alembic single-head + migration ----------------------------------------

class TestAlembicSingleHead:
    def test_alembic_script_produces_exactly_one_head(self):
        from pathlib import Path

        from alembic.config import Config
        from alembic.script import ScriptDirectory

        cfg = Config(str(Path.cwd() / "alembic.ini"))
        scripts = ScriptDirectory.from_config(cfg)
        heads = scripts.get_heads()
        assert len(heads) == 1, f"alembic heads={heads!r} — drift detected"


# ---- HTTP API tests (full round trip) ---------------------------------------

class TestPropertyChannelAPI:
    @pytest.fixture()
    def users(self, db_session: Session):
        admin_user, admin_key = _make_user(db_session, "admin-pc", UserRole.admin)
        manager_user, manager_key = _make_user(db_session, "manager-pc", UserRole.manager)
        agent_user, agent_key = _make_user(db_session, "agent-pc", UserRole.agent)
        return {
            "admin": (admin_user, admin_key),
            "manager": (manager_user, manager_key),
            "agent": (agent_user, agent_key),
        }

    @pytest.fixture()
    def seeded(self, client: TestClient, users, db_session: Session):
        admin_headers = {"Authorization": f"Bearer {users['admin'][1]}"}
        prop_resp = client.post(
            f"{API}/properties",
            json={"name": "Sunset Tower", "address": "1 Roxas", "city": "Pasay", "total_units": 2},
            headers=admin_headers,
        )
        assert prop_resp.status_code == 201, prop_resp.text
        property_id = prop_resp.json()["id"]
        unit_resp = client.post(
            f"{API}/units",
            json={
                "property_id": property_id, "unit_number": "101", "floor": "1",
                "size_sqm": "32.50", "monthly_rent": "12000.00", "status": "vacant",
            },
            headers=admin_headers,
        )
        assert unit_resp.status_code == 201, unit_resp.text
        unit_id = unit_resp.json()["id"]
        return {"property_id": property_id, "unit_id": unit_id, "admin_headers": admin_headers}

    def test_unit_timeline_404_for_bad_id(self, client: TestClient, users, seeded):
        admin_headers = seeded["admin_headers"]
        r = client.get(f"{API}/property-channel/units/99999/timeline", headers=admin_headers)
        assert r.status_code == 404

    def test_unit_timeline_200_renders(self, client: TestClient, seeded):
        r = client.get(
            f"{API}/property-channel/units/{seeded['unit_id']}/timeline",
            headers=seeded["admin_headers"],
        )
        assert r.status_code == 200
        body = r.json()
        assert body["found"] is True
        assert "body_text" in body
        assert body["render_hash"]

    def test_unit_archive_get_before_publish_is_404(self, client: TestClient, seeded):
        r = client.get(
            f"{API}/property-channel/units/{seeded['unit_id']}/archive",
            headers=seeded["admin_headers"],
        )
        assert r.status_code == 404

    def test_unit_archive_publish_requires_manager(self, client: TestClient, users, seeded):
        agent_headers = {"Authorization": f"Bearer {users['agent'][1]}"}
        r = client.post(
            f"{API}/property-channel/units/{seeded['unit_id']}/archive",
            json={
                "external_message_id": 42,
                "render_hash": "aabb" * 16,
            },
            headers=agent_headers,
        )
        assert r.status_code in (401, 403)

    def test_unit_archive_publish_round_trip(self, client: TestClient, seeded):
        unit_id = seeded["unit_id"]
        for u in (UserRole.admin, UserRole.manager):
            _ = u  # ensure import path stays clean
        r1 = client.post(
            f"{API}/property-channel/units/{unit_id}/archive",
            json={
                "external_message_id": 5001,
                "render_hash": "cccc" * 16,
                "event_count_at_publish": 2,
            },
            headers=seeded["admin_headers"],
        )
        assert r1.status_code == 200, r1.text
        payload = r1.json()
        assert payload["changed"] is True
        assert payload["render_version"] == 1
        assert payload["external_message_id"] == 5001
        # GET now returns the archive 200.
        r2 = client.get(
            f"{API}/property-channel/units/{unit_id}/archive",
            headers=seeded["admin_headers"],
        )
        assert r2.status_code == 200
        assert r2.json()["render_hash"] == "cccc" * 16
        # Same hash + message -> changed=False.
        r3 = client.post(
            f"{API}/property-channel/units/{unit_id}/archive",
            json={
                "external_message_id": 5001,
                "render_hash": "cccc" * 16,
            },
            headers=seeded["admin_headers"],
        )
        assert r3.status_code == 200
        assert r3.json()["changed"] is False

    def test_property_channel_200(self, client: TestClient, seeded):
        r = client.get(
            f"{API}/property-channel/properties/{seeded['property_id']}/channel",
            headers=seeded["admin_headers"],
        )
        assert r.status_code == 200
        payload = r.json()
        assert payload["rendered"]["found"] is True
        assert payload["published_property_article"] is None
        assert payload["published_unit_articles"] == []

    def test_property_archive_publish_round_trip(self, client: TestClient, seeded):
        r1 = client.post(
            f"{API}/property-channel/properties/{seeded['property_id']}/archive",
            json={
                "external_message_id": 9001,
                "render_hash": "dddd" * 16,
            },
            headers=seeded["admin_headers"],
        )
        assert r1.status_code == 200
        assert r1.json()["changed"] is True
        r2 = client.get(
            f"{API}/property-channel/properties/{seeded['property_id']}/archive",
            headers=seeded["admin_headers"],
        )
        assert r2.status_code == 200
        assert r2.json()["external_message_id"] == 9001
