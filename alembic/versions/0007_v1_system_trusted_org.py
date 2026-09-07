"""Bind each V1 SYSTEM ApiCredential to exactly one trusted organization.

Issue #119 P0 v1_api_credential_bootstrap (independent review follow-up):

A SYSTEM scheduled-job credential is the most privileged non-HUMAN
identity in the V1 clean rewrite. The previous migration set
(``0005_v1_api_cred_system``) added ``principal_type=SYSTEM`` and
``purpose='internal:scheduler'`` columns, but it did NOT pin the
credential to a single organization. The route handlers then asked the
caller to name ``org_id`` in the query string and accepted any positive
value — meaning a leaked SYSTEM key could read EVERY org in the
database as long as the caller knew the org ids.

This migration closes that gap by adding
``v1_api_credentials.trusted_organization_id`` (nullable BigInt FK to
``v1_organizations.id``) and a compound index on
``(principal_type, purpose, trusted_organization_id)`` so the
``get_system_principal`` lookup stays a single index hit.

Security invariants (AGENTS.md §3 + §4):

  * ``trusted_organization_id`` is **nullable** so we can apply the
    migration on a populated database without backfilling bogus org
    rows. The V1 dependency layer rejects a SYSTEM credential whose
    ``trusted_organization_id`` is NULL — so a credential with a
    NULL binding cannot authenticate, period. Operators must re-run
    the bootstrap script (``scripts/create_v1_api_key.py --purpose job
    --workspace <name>`` or ``--trusted-organization-id <id>``) so the
    binding is filled in.
  * ``trusted_organization_id`` is the **single** authoritative org
    scope for a SYSTEM credential. The V1 SYSTEM endpoints derive the
    target org strictly from this column. A caller-supplied ``org_id``
    query parameter is accepted only if it matches; otherwise the
    request is rejected (403) so an attacker cannot probe other orgs.
  * The FK to ``v1_organizations.id`` prevents binding a SYSTEM
    credential to a non-existent org. The bootstrap script also
    validates the workspace exists before the INSERT.

Round-trip safety:

  * ``downgrade`` drops the FK + index + column in inverse order. No
    data is copied, moved, or backfilled. The migration does not
    silently rebind existing SYSTEM credentials — if the operator
    downgrades, those credentials remain bound to the org they were
    bound to, but the column is dropped, so the V1 SYSTEM auth will
    reject them all (fail closed).
  * upgrade() / downgrade() are exact inverses for a fresh database.
    On a populated database with existing SYSTEM credentials that
    have ``trusted_organization_id = NULL``, the V1 dep layer rejects
    them until the operator runs the bootstrap script to bind them
    — that is the explicit, fail-closed path required by the
    review.

NOT touched (out of Issue #119 P0 scope):

  * No legacy ``users`` / ``principals`` / ``api_credentials`` tables.
  * No bot env contract, no Worker forwarding, no Telegram runtime.
  * No business rules, no Mini App.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0007_v1_system_trusted_org"
down_revision = "0006_v1_orm_alignment"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1) Add the column NULLABLE so the migration applies on a populated
    #    database without backfilling bogus org ids. The dep layer
    #    rejects a NULL binding at runtime.
    op.add_column(
        "v1_api_credentials",
        sa.Column(
            "trusted_organization_id",
            sa.BigInteger(),
            nullable=True,
        ),
    )
    # 2) FK to v1_organizations. RESTRICT on delete is the same pattern
    #    the existing v1_memberships FK uses — we never want to lose an
    #    org and silently orphan a SYSTEM binding.
    op.create_foreign_key(
        "fk_v1_api_credentials_trusted_organization_id",
        "v1_api_credentials",
        "v1_organizations",
        ["trusted_organization_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    # 3) Compound index for the SYSTEM hot path:
    #    get_system_principal does (principal_type, purpose, is_active)
    #    and the org-bound endpoints do (principal_type, purpose,
    #    trusted_organization_id) lookups. A single compound index
    #    serves both.
    op.create_index(
        "ix_v1_api_credentials_system_org_binding",
        "v1_api_credentials",
        ["principal_type", "purpose", "trusted_organization_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_v1_api_credentials_system_org_binding",
        table_name="v1_api_credentials",
    )
    op.drop_constraint(
        "fk_v1_api_credentials_trusted_organization_id",
        "v1_api_credentials",
        type_="foreignkey",
    )
    op.drop_column("v1_api_credentials", "trusted_organization_id")
