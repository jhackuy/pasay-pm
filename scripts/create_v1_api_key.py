#!/usr/bin/env python3
"""Create or rotate a V1 PASAY API credential.

Issue #119 P0 v1_api_credential_bootstrap: this is the canonical V1
credential bootstrap / rotation helper. The legacy
``scripts/create_api_key.py`` writes to legacy ``users.api_key_hash``
+ ``UserRole`` and CANNOT run against the clean-rewrite production
schema (no legacy ``users`` / ``principals`` / ``api_credentials``
tables — only ``v1_*``). The legacy helper remains in place for
historical reasons but is no longer the source of truth.

Canonical 3-credential mapping (Issue #119 P0):

  | Worker secret         | --purpose  | --principal-type | --role     | caller                |
  | --------------------- | ---------- | ---------------- | ---------- | --------------------- |
  | PASSAY_API_KEY        | manager    | HUMAN            | SECRETARY  | interactive secretary |
  | PASSAY_ADMIN_API_KEY  | admin      | HUMAN            | OWNER      | interactive owner     |
  | PASSAY_JOB_API_KEY    | job        | SYSTEM           | (sentinel) | scheduled-job SYSTEM  |

The mapping is enforced by the script: ``--purpose=manager`` requires
``--role=SECRETARY``; ``--purpose=admin`` requires ``--role=OWNER``;
``--purpose=job`` requires ``--principal-type=SYSTEM`` (the role is
ignored for SYSTEM credentials — they never bind a HUMAN membership).

Security invariants (AGENTS.md §3 + §4):

  * The raw API key is generated ONCE via ``secrets.token_urlsafe(32)``
    (256 bits of entropy), printed to the operator's stdout ONCE, and
    NEVER persisted: only ``hash_api_key(raw_key)`` (SHA-256 hex) is
    stored in ``v1_api_credentials.key_hash``.
  * The script never logs, prints, or echoes the raw key beyond the
    one-time operator output line. Operators MUST capture it from the
    terminal / redirect it into their secret store before the script
    exits — there is no recovery path.
  * Dry-run by default. ``--apply`` is required to commit. Dry-run
    prints exactly what would happen without writing.
  * Rotation (--rotate): marks the previous credential ``is_active=false``
    and creates a new one. The old raw key stops authenticating
    immediately after the rotation commits.

Usage (HUMAN interactive manager — PASSAY_API_KEY):

    python scripts/create_v1_api_key.py \\
        --workspace "Pasay Holdings" \\
        --username pasay-manager \\
        --principal-type HUMAN \\
        --role SECRETARY \\
        --purpose manager \\
        --apply

Usage (HUMAN interactive admin — PASSAY_ADMIN_API_KEY):

    python scripts/create_v1_api_key.py \\
        --workspace "Pasay Holdings" \\
        --username pasay-admin \\
        --principal-type HUMAN \\
        --role OWNER \\
        --purpose admin \\
        --apply

Usage (SYSTEM scheduled job — PASSAY_JOB_API_KEY):

    python scripts/create_v1_api_key.py \\
        --principal-type SYSTEM \\
        --purpose job \\
        --apply

The SYSTEM bootstrap creates / reuses a single sentinel User row (no
telegram_user_id, no v1_memberships entry) so the FK constraint on
``v1_api_credentials.user_id`` is satisfied without impersonating a
HUMAN Owner. ``get_system_principal`` never resolves a user_id /
org_id, so the sentinel User's lack of memberships is a feature, not
a bug.
"""
from __future__ import annotations

import argparse
import secrets
import sys
from pathlib import Path

# Make the repository importable when run from any CWD (operators
# routinely run this script via ``docker compose exec api ...`` or from
# the repo root with a relative ``scripts/`` path).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy.orm import Session

from app.core.permissions import Role
from app.core.security import generate_api_key, hash_api_key
from app.db.session import get_session_factory, reset_engine_cache
from app.v1.models.base import (
    MembershipState,
    V1PrincipalType,
)
from app.v1.models.foundation import (
    ApiCredential,
    Membership,
    Organization,
    User,
)


# Sentinel username for the SYSTEM scheduled-job credential. The User
# row exists only to satisfy the v1_api_credentials.user_id FK — it is
# never exposed through any HUMAN-bound API surface.
SYSTEM_SENTINEL_USERNAME = "v1-system-scheduler"

# Purpose mapping: operator-facing name -> DB column value. Only one
# SYSTEM purpose is currently accepted by ``get_system_principal``
# (V1_SYSTEM_PURPOSES in app.v1.models.base); the script enforces that
# mapping here so the operator gets a clear error rather than a 401 at
# the first /operations/digest call.
PURPOSE_TO_DB_VALUE = {
    "manager": None,                   # HUMAN: no purpose
    "admin": None,                     # HUMAN: no purpose
    "job": "internal:scheduler",       # SYSTEM
}


def _coerce_role(raw: str) -> Role:
    try:
        return Role.parse(raw)
    except Exception as exc:  # noqa: BLE001 - script exit path
        raise SystemExit(f"--role invalid: {exc}")


def _validate_purpose_role_alignment(args: argparse.Namespace) -> None:
    """Fail-closed check: a purpose must agree with principal_type + role.

    Two valid (purpose, principal_type, role) tuples, no others:

      * (manager, HUMAN, SECRETARY)
      * (admin,   HUMAN, OWNER)
      * (job,     SYSTEM, ANY — ignored)

    Anything else aborts the script before any DB write.
    """
    purpose = args.purpose
    pt = args.principal_type
    role = args.role
    if purpose in ("manager", "admin"):
        if pt != V1PrincipalType.HUMAN.value:
            raise SystemExit(
                f"--purpose={purpose} requires --principal-type=HUMAN "
                f"(got --principal-type={pt})"
            )
        expected_role = (
            Role.SECRETARY if purpose == "manager" else Role.OWNER
        )
        if role != expected_role.value:
            raise SystemExit(
                f"--purpose={purpose} requires --role={expected_role.value} "
                f"(got --role={role})"
            )
        if not args.workspace:
            raise SystemExit(
                f"--purpose={purpose} requires --workspace (the "
                f"Organization that owns the membership)"
            )
        if not args.username:
            raise SystemExit(
                f"--purpose={purpose} requires --username"
            )
    elif purpose == "job":
        if pt != V1PrincipalType.SYSTEM.value:
            raise SystemExit(
                f"--purpose=job requires --principal-type=SYSTEM "
                f"(got --principal-type={pt})"
            )
    else:  # pragma: no cover - argparse choices already cover this
        raise SystemExit(f"unsupported --purpose: {purpose!r}")


def _ensure_human_credential(
    db: Session,
    *,
    workspace_name: str,
    username: str,
    role: Role,
    raw_key: str,
    key_hash: str,
    display_name: str | None,
    telegram_user_id: int | None,
    rotate: bool,
) -> tuple[User, Organization, ApiCredential]:
    """Create/find org + user + membership, create/rotate ApiCredential."""
    org = (
        db.query(Organization)
        .filter(Organization.name == workspace_name)
        .one_or_none()
    )
    if org is None:
        org = Organization(name=workspace_name)
        db.add(org)
        db.flush()
    user = (
        db.query(User).filter(User.username == username).one_or_none()
    )
    if user is None:
        user = User(
            telegram_user_id=telegram_user_id,
            username=username,
            display_name=display_name or username,
            default_language="en-US",
        )
        db.add(user)
        db.flush()
    elif telegram_user_id is not None and user.telegram_user_id != telegram_user_id:
        # Operator is correcting a binding. Updating is safe because
        # telegram_user_id is UNIQUE NULLABLE — any collision is caught
        # by the DB and surfaces as an IntegrityError.
        user.telegram_user_id = telegram_user_id

    membership = (
        db.query(Membership)
        .filter(
            Membership.org_id == org.id,
            Membership.user_id == user.id,
        )
        .order_by(Membership.id.asc())
        .first()
    )
    if membership is None:
        membership = Membership(
            org_id=org.id,
            user_id=user.id,
            role=role.value,
            state=MembershipState.ACTIVE.value,
        )
        db.add(membership)
    else:
        # Bring the membership to the requested role / state without
        # touching is_bootstrap (bootstrap stays sticky on the OWNER).
        membership.role = role.value
        membership.state = MembershipState.ACTIVE.value

    existing_active = (
        db.query(ApiCredential)
        .filter(
            ApiCredential.user_id == user.id,
            ApiCredential.is_active.is_(True),
            ApiCredential.principal_type == V1PrincipalType.HUMAN.value,
        )
        .order_by(ApiCredential.id.asc())
        .all()
    )
    if existing_active and not rotate:
        names = [c.id for c in existing_active]
        raise SystemExit(
            f"user {username!r} already has active HUMAN credential(s) "
            f"id(s)={names}; pass --rotate to supersede them"
        )
    for old in existing_active:
        old.is_active = False

    cred = ApiCredential(
        user_id=user.id,
        key_hash=key_hash,
        is_active=True,
        principal_type=V1PrincipalType.HUMAN.value,
        purpose=None,
    )
    db.add(cred)
    db.flush()
    return user, org, cred


def _ensure_system_credential(
    db: Session,
    *,
    raw_key: str,
    key_hash: str,
    purpose_db: str,
    rotate: bool,
) -> tuple[User, ApiCredential]:
    """Create/find the SYSTEM sentinel user + rotate/create the credential."""
    user = (
        db.query(User)
        .filter(User.username == SYSTEM_SENTINEL_USERNAME)
        .one_or_none()
    )
    if user is None:
        user = User(
            telegram_user_id=None,
            username=SYSTEM_SENTINEL_USERNAME,
            display_name="V1 SYSTEM scheduler sentinel",
            default_language="en-US",
        )
        db.add(user)
        db.flush()
    existing_active = (
        db.query(ApiCredential)
        .filter(
            ApiCredential.user_id == user.id,
            ApiCredential.is_active.is_(True),
            ApiCredential.principal_type == V1PrincipalType.SYSTEM.value,
        )
        .order_by(ApiCredential.id.asc())
        .all()
    )
    if existing_active and not rotate:
        names = [c.id for c in existing_active]
        raise SystemExit(
            f"system credential(s) already active id(s)={names}; "
            f"pass --rotate to supersede"
        )
    for old in existing_active:
        old.is_active = False
    cred = ApiCredential(
        user_id=user.id,
        key_hash=key_hash,
        is_active=True,
        principal_type=V1PrincipalType.SYSTEM.value,
        purpose=purpose_db,
    )
    db.add(cred)
    db.flush()
    return user, cred


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="commit changes (default rolls back / dry-run)",
    )
    parser.add_argument(
        "--workspace", default=None,
        help="Organization name (HUMAN purposes only)",
    )
    parser.add_argument(
        "--username", default=None,
        help="unique client username (HUMAN purposes only)",
    )
    parser.add_argument(
        "--role", default=None,
        choices=[r.value for r in Role],
        help="role for HUMAN credentials (REQUIRED for manager/admin)",
    )
    parser.add_argument(
        "--principal-type", default=V1PrincipalType.HUMAN.value,
        choices=[p.value for p in V1PrincipalType],
        help=f"principal type (default HUMAN)",
    )
    parser.add_argument(
        "--purpose", required=True,
        choices=["manager", "admin", "job"],
        help="operator-facing purpose label; controls --principal-type / --role",
    )
    parser.add_argument(
        "--telegram-user-id", type=int, default=None,
        help="optional Telegram id binding for the HUMAN user",
    )
    parser.add_argument(
        "--display-name", default=None,
        help="optional display name for the HUMAN user",
    )
    parser.add_argument(
        "--rotate", action="store_true",
        help="supersede any existing active credential(s) for this identity",
    )
    args = parser.parse_args(argv)

    # Pre-flight: enforce the purpose ↔ principal_type ↔ role alignment.
    _validate_purpose_role_alignment(args)

    # Generate the raw key + hash BEFORE the transaction so the
    # operator's terminal capture is deterministic. If the DB write
    # rolls back, the raw key is still valid (the operator can
    # re-apply with --rotate to bind it).
    raw_key = generate_api_key()
    key_hash = hash_api_key(raw_key)
    purpose_db = PURPOSE_TO_DB_VALUE[args.purpose]

    factory = get_session_factory()
    db = factory()
    try:
        if args.principal_type == V1PrincipalType.HUMAN.value:
            role = _coerce_role(args.role)
            user, org, cred = _ensure_human_credential(
                db,
                workspace_name=args.workspace,
                username=args.username,
                role=role,
                raw_key=raw_key,
                key_hash=key_hash,
                display_name=args.display_name,
                telegram_user_id=args.telegram_user_id,
                rotate=args.rotate,
            )
        else:
            user, cred = _ensure_system_credential(
                db,
                raw_key=raw_key,
                key_hash=key_hash,
                purpose_db=purpose_db,
                rotate=args.rotate,
            )
            org = None

        if args.apply:
            db.commit()
            verb = "rotated" if args.rotate else "created"
            scope = (
                f"workspace={args.workspace!r} username={args.username!r} "
                f"role={args.role!r}"
                if args.principal_type == V1PrincipalType.HUMAN.value
                else f"principal_type=SYSTEM purpose={purpose_db!r}"
            )
            print(
                f"V1 credential {verb}: id={cred.id} ({scope}); "
                f"user_id={user.id}"
                + (f" org_id={org.id}" if org is not None else "")
            )
        else:
            db.rollback()
            print(
                "dry-run complete; transaction rolled back "
                "(use --apply to commit)"
            )
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
        reset_engine_cache()

    # ONE-TIME operator output. The raw key is emitted exactly ONCE
    # to stdout, in the canonical ``Authorization: Bearer <key>``
    # form. AGENTS.md §3: never log, never commit, never echo beyond
    # this single line. The Python ``raw_key`` binding is dropped when
    # ``main`` returns — there is no recovery path; operators MUST
    # capture the key from the terminal / redirect it into their secret
    # store before the script exits.
    print(f"Authorization: Bearer {raw_key}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
