#!/usr/bin/env python3
"""Create or rotate a PASay-PM API client key.

.. deprecated::
    This script is DEPRECATED as of Issue #119 P0
    v1_api_credential_bootstrap (2026-09-07). It writes to the legacy
    ``users.api_key_hash`` / ``UserRole`` schema, which DOES NOT EXIST
    in the clean-rewrite V1 production schema (only ``v1_users`` /
    ``v1_memberships`` / ``v1_api_credentials`` are present). Running
    this script against the actual production database will fail with
    ``psycopg2.errors.UndefinedTable: relation "users" does not exist``.

    Use the new V1-aware helpers instead:

      * ``scripts/create_v1_api_key.py`` — create / rotate a credential.
        Maps the operator-facing ``--purpose`` to a coherent
        (principal_type, role) tuple:

        ===================  ===================  =================
        Worker secret        --purpose            --role
        ===================  ===================  =================
        PASSAY_API_KEY       manager              SECRETARY
        PASSAY_ADMIN_API_KEY admin                OWNER
        PASSAY_JOB_API_KEY   job                  (SYSTEM, no role)
        ===================  ===================  =================

      * ``scripts/rotate_v1_api_key.py`` — replace an existing active
        credential for a known identity without re-stating it.

The legacy file is kept in the tree ONLY as a historical marker so
contributors grepping for "create_api_key" land somewhere explicit.
It is not safe to run against current production.
"""
import argparse
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.security import hash_api_key
from app.database import SessionLocal
from app.models.user import User, UserRole


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--username", required=True, help="unique client username")
    parser.add_argument("--role", choices=[r.value for r in UserRole], required=True)
    parser.add_argument(
        "--rotate", action="store_true", help="generate a new key for an existing user"
    )
    args = parser.parse_args()

    # Issue #119 P0 v1_api_credential_bootstrap: refuse to run against
    # the current production schema. The legacy ``users`` / ``UserRole``
    # tables are not part of the V1 clean-rewrite schema, so any INSERT
    # here would fail at the SQL layer. Print a clear, actionable error
    # and exit before touching the DB.
    print(
        "ERROR: scripts/create_api_key.py is DEPRECATED and CANNOT run "
        "against the current V1 production schema (no legacy 'users' / "
        "'api_credentials' / 'principals' tables exist).",
        file=sys.stderr,
    )
    print(
        "Use scripts/create_v1_api_key.py (create) or "
        "scripts/rotate_v1_api_key.py (rotate) instead. See the "
        "Issue #119 P0 v1_api_credential_bootstrap migration notes for "
        "the canonical 3-credential mapping.",
        file=sys.stderr,
    )
    sys.exit(2)

    # The legacy implementation below is unreachable; kept only for
    # historical reference. Do NOT delete without a separate deprecation
    # notice — operators grepping for this script name should land on
    # this file and see the migration pointer above.
    with SessionLocal() as db:  # pragma: no cover - deprecated path
        user = db.query(User).filter(User.username == args.username).first()
        api_key = secrets.token_urlsafe(32)
        if user is None:
            user = User(
                username=args.username,
                role=args.role,
                api_key_hash=hash_api_key(api_key),
                is_active=True,
            )
            db.add(user)
            print(f"Created user '{args.username}' with role '{args.role}'")
        else:
            if not args.rotate:
                print(
                    f"User '{args.username}' already exists; pass --rotate to generate a new key",
                    file=sys.stderr,
                )
                sys.exit(1)
            user.role = args.role
            user.api_key_hash = hash_api_key(api_key)
            user.is_active = True
            print(f"Rotated key for user '{args.username}' (role '{args.role}')")
        db.commit()

    print(f"API key: {api_key}")
    print(f"Authorization: Bearer {api_key}")  # pragma: no cover


if __name__ == "__main__":
    main()
