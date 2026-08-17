"""PASAY-AI-EMPLOYEE-FOUNDATION-007 — AI Employee data foundation.

Revision ID: c4d5e6f7a8b9
Revises: b2c3d4e5f6a7
Create Date: 2026-08-17

Changes (appended only; CHECK/enum allowlists kept in sync with the models):
1. ``tenants``: add structured contact fields + contact_status lifecycle +
   sensitive identity storage + structured emergency contact. The legacy
   ``id_document`` / ``emergency_contact`` columns stay untouched (the ORM no
   longer manages them; they become dormant orphan columns on the DB).
2. ``properties``: add property-management contact + free-form operational
   notes.
3. ``leases``: add renewal_notice_period_days / management_fee_included /
   special_terms (structured lease truth — a contract is never just a PDF).

ROLLBACK:
    alembic downgrade b2c3d4e5f6a7
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'c4d5e6f7a8b9'
down_revision: Union[str, None] = 'b2c3d4e5f6a7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. tenants — structured contact + contact_status + identity + emergency.
    op.add_column('tenants', sa.Column('secondary_phone', sa.String(length=50), nullable=True))
    op.add_column('tenants', sa.Column('telegram', sa.String(length=100), nullable=True))
    op.add_column('tenants', sa.Column('whatsapp', sa.String(length=50), nullable=True))
    op.add_column('tenants', sa.Column(
        'contact_status', sa.String(length=50), nullable=True,
    ))
    op.create_check_constraint(
        "ck_tenants_contact_status",
        'tenants',
        "contact_status IN ('UNKNOWN','UNVERIFIED','VERIFIED','WRONG_NUMBER',"
        "'UNREACHABLE','CHANGED') OR contact_status IS NULL",
    )
    op.add_column('tenants', sa.Column('last_confirmed_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('tenants', sa.Column('last_confirmed_by', sa.String(length=200), nullable=True))
    op.add_column('tenants', sa.Column('notes', sa.String(length=1000), nullable=True))
    # Sensitive identity (stored; reads redact by default).
    op.add_column('tenants', sa.Column('id_number', sa.String(length=100), nullable=True))
    op.add_column('tenants', sa.Column('id_front_file_id', sa.String(length=300), nullable=True))
    op.add_column('tenants', sa.Column('id_back_file_id', sa.String(length=300), nullable=True))
    # Structured emergency contact.
    op.add_column('tenants', sa.Column('emergency_name', sa.String(length=200), nullable=True))
    op.add_column('tenants', sa.Column('emergency_relationship', sa.String(length=100), nullable=True))
    op.add_column('tenants', sa.Column('emergency_phone', sa.String(length=50), nullable=True))

    # 2. properties — management contact + operational notes.
    op.add_column('properties', sa.Column('management_company', sa.String(length=200), nullable=True))
    op.add_column('properties', sa.Column('management_office_phone', sa.String(length=50), nullable=True))
    op.add_column('properties', sa.Column('management_contact_person', sa.String(length=200), nullable=True))
    op.add_column('properties', sa.Column('management_email', sa.String(length=200), nullable=True))
    op.add_column('properties', sa.Column('management_office_location', sa.String(length=300), nullable=True))
    op.add_column('properties', sa.Column('operational_notes', sa.Text(), nullable=True))

    # 3. leases — structured lease truth.
    op.add_column('leases', sa.Column('renewal_notice_period_days', sa.Integer(), nullable=True))
    op.add_column('leases', sa.Column('management_fee_included', sa.Boolean(), nullable=True))
    op.add_column('leases', sa.Column('special_terms', sa.Text(), nullable=True))


def downgrade() -> None:
    # 3. leases
    op.drop_column('leases', 'special_terms')
    op.drop_column('leases', 'management_fee_included')
    op.drop_column('leases', 'renewal_notice_period_days')
    # 2. properties
    op.drop_column('properties', 'operational_notes')
    op.drop_column('properties', 'management_office_location')
    op.drop_column('properties', 'management_email')
    op.drop_column('properties', 'management_contact_person')
    op.drop_column('properties', 'management_office_phone')
    op.drop_column('properties', 'management_company')
    # 1. tenants
    op.drop_column('tenants', 'emergency_phone')
    op.drop_column('tenants', 'emergency_relationship')
    op.drop_column('tenants', 'emergency_name')
    op.drop_column('tenants', 'id_back_file_id')
    op.drop_column('tenants', 'id_front_file_id')
    op.drop_column('tenants', 'id_number')
    op.drop_column('tenants', 'notes')
    op.drop_column('tenants', 'last_confirmed_by')
    op.drop_column('tenants', 'last_confirmed_at')
    op.drop_constraint('ck_tenants_contact_status', 'tenants', type_='check')
    op.drop_column('tenants', 'contact_status')
    op.drop_column('tenants', 'whatsapp')
    op.drop_column('tenants', 'telegram')
    op.drop_column('tenants', 'secondary_phone')
