"""V1.3 Gate A trusted identity foundation.

Revision ID: e3b4c5d6e7f8
Revises: c2a1b2c3d4e5
"""
from alembic import op
import sqlalchemy as sa
import hashlib

revision = "e3b4c5d6e7f8"
down_revision = "c2a1b2c3d4e5"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("principals",
        sa.Column("principal_type", sa.String(50), nullable=False), sa.Column("name", sa.String(100), nullable=False),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.id")), sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False), sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("created_by", sa.BigInteger()), sa.Column("updated_by", sa.BigInteger()),
        sa.CheckConstraint("principal_type IN ('HUMAN','SERVICE','AI_AGENT','SYSTEM')", name="ck_principals_principal_type"))
    op.create_index("ix_principals_principal_type", "principals", ["principal_type"])
    op.create_index("ix_principals_user_id", "principals", ["user_id"])
    op.create_index("uq_principals_human_user", "principals", ["user_id"], unique=True, postgresql_where=sa.text("principal_type = 'HUMAN'"))
    op.create_index("uq_principals_name_type", "principals", ["name", "principal_type"], unique=True)
    op.create_table("api_credentials",
        sa.Column("principal_id", sa.BigInteger(), sa.ForeignKey("principals.id"), nullable=False), sa.Column("key_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("purpose", sa.String(100), nullable=False), sa.Column("state", sa.String(50), nullable=False), sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("supersedes_id", sa.BigInteger(), sa.ForeignKey("api_credentials.id")), sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False), sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("created_by", sa.BigInteger()), sa.Column("updated_by", sa.BigInteger()),
        sa.CheckConstraint("state IN ('ACTIVE','REVOKED')", name="ck_api_credentials_state"),
        sa.CheckConstraint("(state = 'ACTIVE' AND revoked_at IS NULL) OR (state = 'REVOKED' AND revoked_at IS NOT NULL)", name="ck_api_credentials_state_revocation"))
    op.create_index("ix_api_credentials_hash_state", "api_credentials", ["key_hash", "state"])
    op.create_index("ix_api_credentials_principal_id", "api_credentials", ["principal_id"])
    op.create_index("ix_api_credentials_purpose", "api_credentials", ["purpose"])
    op.create_index("uq_api_credentials_active_principal_purpose", "api_credentials", ["principal_id", "purpose"], unique=True, postgresql_where=sa.text("state = 'ACTIVE' AND revoked_at IS NULL"))
    op.create_table("credential_lifecycle_history",
        sa.Column("credential_id", sa.BigInteger(), sa.ForeignKey("api_credentials.id"), nullable=False), sa.Column("state", sa.String(50), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False), sa.Column("reason", sa.Text()), sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False), sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("created_by", sa.BigInteger()), sa.Column("updated_by", sa.BigInteger()), sa.CheckConstraint("state IN ('ACTIVE','REVOKED')", name="ck_credential_lifecycle_state"))
    op.create_index("ix_credential_lifecycle_history_credential_id", "credential_lifecycle_history", ["credential_id"])
    op.create_table("telegram_identity_bindings",
        sa.Column("external_user_id", sa.BigInteger(), nullable=False), sa.Column("human_principal_id", sa.BigInteger(), sa.ForeignKey("principals.id"), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True)), sa.Column("revoked_at", sa.DateTime(timezone=True)), sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True), sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False), sa.Column("created_by", sa.BigInteger()), sa.Column("updated_by", sa.BigInteger()),
        sa.CheckConstraint("external_user_id > 0", name="ck_telegram_external_user_positive"))
    op.create_index("uq_telegram_binding_external_active", "telegram_identity_bindings", ["external_user_id"], unique=True, postgresql_where=sa.text("is_active AND revoked_at IS NULL"))
    op.create_index("uq_telegram_binding_human_active", "telegram_identity_bindings", ["human_principal_id"], unique=True, postgresql_where=sa.text("is_active AND revoked_at IS NULL"))
    op.create_table("communication_endpoints",
        sa.Column("human_principal_id", sa.BigInteger(), sa.ForeignKey("principals.id"), nullable=False), sa.Column("channel", sa.String(30), nullable=False), sa.Column("destination", sa.String(200), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True)), sa.Column("revoked_at", sa.DateTime(timezone=True)), sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True), sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False), sa.Column("created_by", sa.BigInteger()), sa.Column("updated_by", sa.BigInteger()),
        sa.CheckConstraint("length(btrim(destination)) > 0", name="ck_communication_endpoint_destination_nonblank"))
    op.create_index("ix_endpoint_owner_channel", "communication_endpoints", ["human_principal_id", "channel"])
    op.create_table("security_events", sa.Column("event_type", sa.String(100), nullable=False), sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.id")), sa.Column("channel", sa.String(30)), sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True), sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False), sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False), sa.Column("created_by", sa.BigInteger()), sa.Column("updated_by", sa.BigInteger()))
    op.create_index("ix_security_events_event_type", "security_events", ["event_type"])
    for col, target in (("subject_principal_id", "principals.id"), ("caller_principal_id", "principals.id"), ("credential_id", "api_credentials.id")):
        op.add_column("audit_logs", sa.Column(col, sa.BigInteger(), nullable=True)); op.create_foreign_key(f"fk_audit_{col}", "audit_logs", target.split('.')[0], [col], ["id"])
    op.add_column("audit_logs", sa.Column("channel", sa.String(30)))
    for col in ("proposed_principal_id", "confirmed_principal_id", "executed_principal_id"):
        op.add_column("copilot_action_proposals", sa.Column(col, sa.BigInteger(), sa.ForeignKey("principals.id")))
    op.execute("INSERT INTO principals (principal_type,name,user_id) SELECT 'HUMAN', username, id FROM users WHERE id <> 14 AND lower(username) <> 'maria'")
    op.execute("INSERT INTO principals (principal_type,name,user_id) SELECT 'SERVICE','legacy-secretary',id FROM users WHERE id=14")
    op.execute("INSERT INTO principals (principal_type,name) VALUES ('SERVICE','native-bot'),('SYSTEM','scheduler'),('SYSTEM','reconcile'),('SYSTEM','notifier'),('SYSTEM','backfill'),('AI_AGENT','lily'),('AI_AGENT','hermes')")
    op.execute("INSERT INTO api_credentials (principal_id,key_hash,purpose,state) SELECT p.id,u.api_key_hash,'legacy_human','ACTIVE' FROM users u JOIN principals p ON p.user_id=u.id AND p.principal_type='HUMAN'")
    op.execute("INSERT INTO credential_lifecycle_history (credential_id,state,occurred_at,reason) SELECT id,state,created_at,'V1.3 legacy-human backfill' FROM api_credentials")
    conn = op.get_bind()
    for name in ("scheduler", "reconcile", "notifier", "backfill"):
        key_hash = hashlib.sha256(f"pasay-v13-internal-record:{name}".encode()).hexdigest()
        conn.execute(sa.text("INSERT INTO api_credentials (principal_id,key_hash,purpose,state) SELECT id,:hash,:purpose,'ACTIVE' FROM principals WHERE name=:name AND principal_type='SYSTEM'"), {"hash": key_hash, "purpose": f"internal:{name}", "name": name})
    op.execute("INSERT INTO credential_lifecycle_history (credential_id,state,occurred_at,reason) SELECT id,state,created_at,'V1.3 internal worker backfill' FROM api_credentials WHERE purpose LIKE 'internal:%'")


def downgrade():
    for col in ("executed_principal_id", "confirmed_principal_id", "proposed_principal_id"): op.drop_column("copilot_action_proposals", col)
    op.drop_column("audit_logs", "channel")
    for col in ("credential_id", "caller_principal_id", "subject_principal_id"): op.drop_constraint(f"fk_audit_{col}", "audit_logs", type_="foreignkey"); op.drop_column("audit_logs", col)
    for table in ("security_events", "communication_endpoints", "telegram_identity_bindings", "credential_lifecycle_history", "api_credentials", "principals"): op.drop_table(table)
