"""Real-PostgreSQL migration round trip for the V1.3 Gate A identity schema."""
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url

from app.config import settings


PREVIOUS_REVISION = "c2a1b2c3d4e5"
V13_REVISION = "e3b4c5d6e7f8"
SCRATCH_DATABASE = "pasay_pm_test_mig_v13_gatea"
REPO_ROOT = Path(__file__).resolve().parents[1]


def _financial_snapshot(engine):
    with engine.connect() as connection:
        incomes = connection.execute(text(
            "SELECT id, amount::text, received_date::text, status, description "
            "FROM incomes ORDER BY id"
        )).all()
        expenses = connection.execute(text(
            "SELECT id, expense_date::text, category, amount::text, payee, status "
            "FROM expenses ORDER BY id"
        )).all()
    return [tuple(row) for row in incomes], [tuple(row) for row in expenses]


def test_v13_upgrade_downgrade_reupgrade_preserves_financial_rows(
    monkeypatch, test_engine
):
    original_url = settings.database_url
    admin_url = make_url(original_url).set(database="postgres")
    scratch_url = make_url(original_url).set(database=SCRATCH_DATABASE)

    def drop_scratch():
        admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
        try:
            with admin.connect() as connection:
                connection.execute(text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :name AND pid <> pg_backend_pid()"
                ), {"name": SCRATCH_DATABASE})
                connection.execute(text(f'DROP DATABASE IF EXISTS "{SCRATCH_DATABASE}"'))
        finally:
            admin.dispose()

    drop_scratch()
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{SCRATCH_DATABASE}"'))
    finally:
        admin.dispose()

    monkeypatch.setattr(
        settings,
        "database_url",
        scratch_url.render_as_string(hide_password=False),
    )
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    try:
        command.upgrade(config, PREVIOUS_REVISION)
        engine = create_engine(scratch_url)
        try:
            with engine.begin() as connection:
                connection.execute(text(
                    "INSERT INTO users (id, username, role, api_key_hash, is_active) VALUES "
                    "(14, 'legacy-service-user', 'admin', :service_hash, true), "
                    "(21, 'canonical-owner', 'admin', :human_hash, true), "
                    "(22, 'maria', 'manager', :maria_hash, true)"
                ), {
                    "service_hash": "1" * 64,
                    "human_hash": "2" * 64,
                    "maria_hash": "3" * 64,
                })
                connection.execute(text(
                    "INSERT INTO incomes "
                    "(id, amount, received_date, payment_method, status, description) "
                    "VALUES (501, 12345.67, DATE '2026-08-01', 'Bank', "
                    "'confirmed', 'historical income')"
                ))
                connection.execute(text(
                    "INSERT INTO expenses "
                    "(id, expense_date, category, amount, payee, status, description) "
                    "VALUES (601, DATE '2026-08-02', 'repair', 765.43, "
                    "'Historical Vendor', 'paid', 'historical expense')"
                ))
            expected_financial = _financial_snapshot(engine)
        finally:
            engine.dispose()

        command.upgrade(config, V13_REVISION)
        engine = create_engine(scratch_url)
        try:
            inspector = inspect(engine)
            assert _financial_snapshot(engine) == expected_financial
            assert {
                "principals",
                "api_credentials",
                "credential_lifecycle_history",
                "telegram_identity_bindings",
                "communication_endpoints",
                "security_events",
            } <= set(inspector.get_table_names())
            assert {
                "ix_principals_principal_type",
                "ix_principals_user_id",
                "uq_principals_human_user",
                "uq_principals_name_type",
            } <= {row["name"] for row in inspector.get_indexes("principals")}
            assert {
                "ix_api_credentials_hash_state",
                "ix_api_credentials_principal_id",
                "ix_api_credentials_purpose",
                "uq_api_credentials_active_principal_purpose",
            } <= {row["name"] for row in inspector.get_indexes("api_credentials")}
            assert {
                "ck_api_credentials_state",
                "ck_api_credentials_state_revocation",
            } <= {row["name"] for row in inspector.get_check_constraints("api_credentials")}
            with engine.connect() as connection:
                principals = connection.execute(text(
                    "SELECT principal_type, name, user_id FROM principals "
                    "WHERE user_id IN (14, 21, 22) ORDER BY user_id"
                )).all()
                assert [tuple(row) for row in principals] == [
                    ("SERVICE", "legacy-secretary", 14),
                    ("HUMAN", "canonical-owner", 21),
                ]
                credential_row = connection.execute(text(
                    "SELECT c.key_hash, c.purpose, c.state, h.state "
                    "FROM api_credentials c JOIN principals p ON p.id=c.principal_id "
                    "JOIN credential_lifecycle_history h ON h.credential_id=c.id "
                    "WHERE p.user_id=21"
                )).one()
                assert tuple(credential_row) == (
                    "2" * 64,
                    "legacy_human",
                    "ACTIVE",
                    "ACTIVE",
                )
        finally:
            engine.dispose()

        command.downgrade(config, PREVIOUS_REVISION)
        engine = create_engine(scratch_url)
        try:
            inspector = inspect(engine)
            assert _financial_snapshot(engine) == expected_financial
            assert "principals" not in inspector.get_table_names()
            assert "subject_principal_id" not in {
                column["name"] for column in inspector.get_columns("audit_logs")
            }
            assert "proposed_principal_id" not in {
                column["name"]
                for column in inspector.get_columns("copilot_action_proposals")
            }
        finally:
            engine.dispose()

        command.upgrade(config, V13_REVISION)
        engine = create_engine(scratch_url)
        try:
            assert _financial_snapshot(engine) == expected_financial
            assert "principals" in inspect(engine).get_table_names()
        finally:
            engine.dispose()
    finally:
        monkeypatch.setattr(settings, "database_url", original_url)
        drop_scratch()
