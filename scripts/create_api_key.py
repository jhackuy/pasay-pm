#!/usr/bin/env python3
"""Create or rotate a PASay-PM API client key.

Usage:
    python scripts/create_api_key.py --username hermes --role admin
    docker compose exec api python scripts/create_api_key.py --username hermes --role admin

The key is printed once and must be stored by the caller. Only its SHA-256
hash is persisted in the database (users.api_key_hash).
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

    with SessionLocal() as db:
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
    print(f"Authorization: Bearer {api_key}")


if __name__ == "__main__":
    main()
