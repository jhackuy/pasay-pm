#!/usr/bin/env python3
"""Bootstrap Native Bot credentials, Telegram bindings and endpoints (dry-run default)."""
import argparse
import secrets
from datetime import datetime, timezone

from app.core.security import hash_api_key
from app.database import SessionLocal
from app.models.identity import (ApiCredential, CommunicationEndpoint, CredentialLifecycle,
    CredentialState, Principal, PrincipalType, TelegramIdentityBinding)
from app.models.user import User
from app.services.identity import eligible_human


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="commit changes (default rolls back)")
    parser.add_argument("--bot-key")
    parser.add_argument("--user-id", type=int)
    parser.add_argument("--telegram-user-id", type=int)
    parser.add_argument("--telegram-destination")
    args = parser.parse_args(argv)
    db = SessionLocal(); now = datetime.now(timezone.utc)
    try:
        bot = db.query(Principal).filter_by(name="native-bot", principal_type=PrincipalType.SERVICE).one()
        if args.bot_key:
            old = db.query(ApiCredential).filter_by(principal_id=bot.id, purpose="telegram_bot", state=CredentialState.ACTIVE).all()
            for item in old:
                item.state = CredentialState.REVOKED; item.revoked_at = now
                db.add(CredentialLifecycle(credential_id=item.id, state=CredentialState.REVOKED, occurred_at=now, reason="superseded"))
            credential = ApiCredential(principal_id=bot.id, key_hash=hash_api_key(args.bot_key), purpose="telegram_bot", state=CredentialState.ACTIVE,
                supersedes_id=old[-1].id if old else None)
            db.add(credential); db.flush(); db.add(CredentialLifecycle(credential_id=credential.id, state=CredentialState.ACTIVE, occurred_at=now, reason="bootstrap"))
        if args.user_id is not None:
            user = db.get(User, args.user_id)
            if user is None or not eligible_human(user): raise RuntimeError("user is not an eligible HUMAN identity")
            principal = db.query(Principal).filter_by(user_id=user.id, principal_type=PrincipalType.HUMAN).one()
            if args.telegram_user_id is not None:
                if args.telegram_user_id <= 0: raise RuntimeError("Telegram user id must be positive")
                db.add(TelegramIdentityBinding(external_user_id=args.telegram_user_id, human_principal_id=principal.id, verified_at=now))
            if args.telegram_destination:
                db.add(CommunicationEndpoint(human_principal_id=principal.id, channel="telegram", destination=args.telegram_destination, verified_at=now))
        db.flush()
        if args.apply: db.commit(); print("identity bootstrap applied")
        else: db.rollback(); print("dry-run complete; transaction rolled back (use --apply to commit)")
    finally: db.close()


if __name__ == "__main__": main()
