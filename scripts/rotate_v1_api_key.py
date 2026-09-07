#!/usr/bin/env python3
"""Rotate an existing V1 PASAY API credential.

Issue #119 P0 v1_api_credential_bootstrap: production-side rotation
helper. The companion to ``scripts/create_v1_api_key.py``: when an
operator needs to replace an existing API key WITHOUT changing the
identity (HUMAN username or SYSTEM purpose), use this script.

Behavior:
  * Looks up the identity's active credential(s) and marks them
    ``is_active=False`` (the previous raw key stops authenticating
    immediately on commit).
  * Generates a fresh 256-bit URL-safe raw key, stores its SHA-256
    hash on a NEW ApiCredential row, prints the new raw key ONCE.
  * Dry-run by default; ``--apply`` is required to commit.

Identity selection (mutually exclusive — pass exactly one):

  * HUMAN: ``--username`` — rotates the active HUMAN credential for
    that username. The Org is unaffected; only the bearer is rotated.
  * SYSTEM: ``--principal-type SYSTEM --purpose internal:scheduler`` —
    rotates the active SYSTEM credential for the canonical scheduler
    purpose. There is exactly one SYSTEM credential per purpose at a
    time.

Security invariants (AGENTS.md §3 + §4):

  * The raw key is generated ONCE via ``secrets.token_urlsafe(32)``
    (256 bits of entropy), printed to the operator's stdout ONCE, and
    NEVER persisted: only the SHA-256 hex is stored.
  * The script never logs, prints, or echoes the raw key beyond the
    one-time operator output line.
  * A system credential can NEVER be downgraded to HUMAN or vice
    versa — identity is bound to the credential's principal_type +
    purpose at creation time. Rotating a SYSTEM credential produces a
    SYSTEM credential; rotating a HUMAN credential produces a HUMAN
    credential.
  * Fail closed on ambiguity: if the requested identity has no active
    credential, the script refuses (does not silently create one).

Usage:

    # Rotate the interactive admin (Owner) credential:
    python scripts/rotate_v1_api_key.py --username pasay-admin

    # Rotate the scheduled-job SYSTEM credential:
    python scripts/rotate_v1_api_key.py \\
        --principal-type SYSTEM \\
        --purpose internal:scheduler

    # After capturing the new raw key into the operator's secret store:
    python scripts/rotate_v1_api_key.py \\
        --username pasay-admin \\
        --apply
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Make the repository importable when run from any CWD.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy.orm import Session

from app.core.security import generate_api_key, hash_api_key
from app.db.session import get_session_factory, reset_engine_cache
from app.v1.models.base import V1PrincipalType
from app.v1.models.foundation import ApiCredential, User
from scripts.create_v1_api_key import SYSTEM_SENTINEL_USERNAME


def _validate_args(args: argparse.Namespace) -> None:
    """Mutually-exclusive identity selector + minimum invariants."""
    if args.username and args.principal_type == V1PrincipalType.SYSTEM.value:
        raise SystemExit(
            "--username and --principal-type=SYSTEM are mutually exclusive; "
            "rotate a HUMAN credential with --username, a SYSTEM credential "
            "with --principal-type SYSTEM --purpose <purpose>"
        )
    if not args.username and args.principal_type != V1PrincipalType.SYSTEM.value:
        raise SystemExit(
            "must pass either --username (HUMAN) or "
            "--principal-type SYSTEM --purpose <purpose>"
        )
    if args.principal_type == V1PrincipalType.SYSTEM.value and not args.purpose:
        raise SystemExit(
            "--principal-type=SYSTEM requires --purpose (e.g. internal:scheduler)"
        )


def _resolve_identity(
    db: Session, *, username: str | None, purpose: str | None,
) -> tuple[User, list[ApiCredential], str, str | None, int | None]:
    """Resolve (user, actives, principal_type, purpose, trusted_org_id) for the rotation.

    Raises SystemExit on any ambiguity. Never returns an empty list of
    actives — the rotation is a no-op without something to supersede.

    The ``trusted_org_id`` is the canonical org binding for SYSTEM
    credentials. The rotation helper carries the existing binding
    forward so the new credential is bound to the SAME org as the
    one it replaces (Issue #119 P0 follow-up — the binding is
    immutable on rotation; re-binding requires a fresh create).
    """
    if username:
        user = db.query(User).filter(User.username == username).one_or_none()
        if user is None:
            raise SystemExit(
                f"no User row with username={username!r}; nothing to rotate"
            )
        actives = (
            db.query(ApiCredential)
            .filter(
                ApiCredential.user_id == user.id,
                ApiCredential.is_active.is_(True),
                ApiCredential.principal_type == V1PrincipalType.HUMAN.value,
            )
            .order_by(ApiCredential.id.asc())
            .all()
        )
        if not actives:
            raise SystemExit(
                f"no active HUMAN credential for username={username!r}; "
                f"use scripts/create_v1_api_key.py --apply to create one"
            )
        return user, actives, V1PrincipalType.HUMAN.value, None, None

    user = (
        db.query(User)
        .filter(User.username == SYSTEM_SENTINEL_USERNAME)
        .one_or_none()
    )
    if user is None:
        raise SystemExit(
            f"no SYSTEM sentinel User row (username={SYSTEM_SENTINEL_USERNAME!r}); "
            f"use scripts/create_v1_api_key.py --apply to create one"
        )
    actives = (
        db.query(ApiCredential)
        .filter(
            ApiCredential.user_id == user.id,
            ApiCredential.is_active.is_(True),
            ApiCredential.principal_type == V1PrincipalType.SYSTEM.value,
            ApiCredential.purpose == purpose,
        )
        .order_by(ApiCredential.id.asc())
        .all()
    )
    if not actives:
        raise SystemExit(
            f"no active SYSTEM credential with purpose={purpose!r}; "
            f"use scripts/create_v1_api_key.py --apply to create one"
        )
    # All SYSTEM actives for the same purpose must share a single
    # trusted_organization_id binding; if a prior rotation left
    # stragglers with mismatched bindings, refuse to rotate rather
    # than silently pick one.
    bindings = {c.trusted_organization_id for c in actives}
    if None in bindings:
        raise SystemExit(
            "active SYSTEM credential(s) exist with NULL "
            "trusted_organization_id; run scripts/create_v1_api_key.py "
            "--principal-type SYSTEM --purpose <purpose> --workspace <name> "
            "to re-create the binding before rotation"
        )
    if len(bindings) > 1:
        raise SystemExit(
            f"active SYSTEM credentials are bound to multiple orgs "
            f"{sorted(bindings)}; refusing to rotate"
        )
    return user, actives, V1PrincipalType.SYSTEM.value, purpose, next(iter(bindings))


def _supersede(
    db: Session,
    *,
    user: User,
    actives: list[ApiCredential],
    principal_type: str,
    purpose: str | None,
    trusted_organization_id: int | None,
) -> tuple[ApiCredential, str]:
    """Create a new active credential + deactivate the old ones. Returns (cred, raw_key)."""
    raw_key = generate_api_key()
    cred = ApiCredential(
        user_id=user.id,
        key_hash=hash_api_key(raw_key),
        is_active=True,
        principal_type=principal_type,
        purpose=purpose,
        trusted_organization_id=(
            int(trusted_organization_id)
            if trusted_organization_id is not None else None
        ),
    )
    db.add(cred)
    db.flush()
    for old in actives:
        old.is_active = False
    return cred, raw_key


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
        "--username", default=None,
        help="HUMAN username whose active credential to rotate",
    )
    parser.add_argument(
        "--principal-type", default=None,
        choices=[p.value for p in V1PrincipalType],
        help="principal type (SYSTEM only — HUMAN rotation uses --username)",
    )
    parser.add_argument(
        "--purpose", default=None,
        help="SYSTEM purpose (e.g. internal:scheduler) — required when rotating a SYSTEM credential",
    )
    args = parser.parse_args(argv)
    _validate_args(args)

    factory = get_session_factory()
    db = factory()
    try:
        user, actives, pt, purpose, trusted_org_id = _resolve_identity(
            db, username=args.username, purpose=args.purpose,
        )
        cred, raw_key = _supersede(
            db, user=user, actives=actives,
            principal_type=pt, purpose=purpose,
            trusted_organization_id=trusted_org_id,
        )
        if args.username:
            scope = f"username={args.username!r}"
        else:
            scope = (
                f"principal_type=SYSTEM purpose={args.purpose!r} "
                f"trusted_organization_id={trusted_org_id}"
            )

        if args.apply:
            db.commit()
            print(f"V1 credential rotated: id={cred.id} ({scope})")
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

    # ONE-TIME operator output. The raw key is shown on stdout exactly
    # once and then dropped from memory.
    print(f"API key: {raw_key}")
    print(f"Authorization: Bearer {raw_key}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
