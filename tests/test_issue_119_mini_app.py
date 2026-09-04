"""Issue #119 Mini App production half — focused regression tests.

The Mini App's production-readiness surface for Issue #119:

  A) Cloudflare Pages publish plumbing
     - ``mini_app/wrangler.toml`` pins the Pages project name so the
       trusted-lane ``scripts/deploy_mini_app_pages.sh`` script can
       deploy `mini_app/dist` to ``https://pasay-mini-app.pages.dev``
     without requiring the operator to remember the project name.  A
     regression that drops the file would silently publish to a
     personal Pages sandbox.
     - ``mini_app/public/_redirects`` ships the SPA fallback for any
     deep refresh; Vite's build must copy it into ``dist/_redirects``
     (the JSDOM smoke asserts both halves — this module just pins the
     contract).

  B) InitData -> bearer exchange endpoint
     - ``POST /api/v1/webapp/auth`` accepts a signed initData string,
     verifies the HMAC against the configured bot token, resolves the
     Telegram user id to an OWNER-only backend User, and issues a
     short-lived API key.  Fail-closed for: missing token, malformed
     initData, bad signature, non-owner id, unknown Telegram id,
     SECRETARY role, missing membership, stale auth_date.

  C) MenuButton WebApp registration (`打开管理后台`)
     - The Telegram bot MUST publish a per-user MenuButton pointing at
     the Mini App URL so Owner has an obvious entry point.  The
     registration path is plumbed through ``build_application``; the
     helper that drives it is unit-tested so a regression that drops
     the call (or re-binds it to the wrong scope) is caught.

  D) Mini App -> backend auth flow
     - The Mini App opens via initData, exchanges it for a bearer, and
     calls ``GET /api/v1/dashboard/home`` and ``GET /api/v1/properties``
     against the SAME backend.  These tests exercise the canonical
     Home and Properties routes with the issued bearer to prove the
     Mini App's auth path works end-to-end against real FastAPI +
     real PostgreSQL.

All four surfaces are locked into CI via ``tests/test_issue_119_mini_app.py``;
running targeted suites only.  No workflow/secrets are touched.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import quote, unquote

import pytest

from tests.v1_support import seed_workspace, v1_session_ctx
from app.db.session import get_db, get_session_factory


REPO_ROOT = Path(__file__).resolve().parents[1]

# The MenuButton WebApp registration test imports pasay_bot, which
# lives under pasay-telegram-bot/ and is not on sys.path when pytest
# is invoked from the repository root.  Adding it here keeps the
# import local to the affected test (no sys.path mutation leaks into
# other test files).
_PASAY_BOT_DIR = REPO_ROOT / "pasay-telegram-bot"
if str(_PASAY_BOT_DIR) not in sys.path:
    sys.path.insert(0, str(_PASAY_BOT_DIR))


# ===========================================================================
# A) Cloudflare Pages publish plumbing
# ===========================================================================


def test_mini_app_pages_wrangler_config_pins_project_name():
    """``mini_app/wrangler.toml`` MUST pin the Pages project name and
    the build-output directory so the trusted-lane deploy script
    always publishes to the SAME ``pasay-mini-app`` project (no
    operator typo, no accidental personal sandbox)."""
    wrangler = REPO_ROOT / "mini_app" / "wrangler.toml"
    assert wrangler.exists(), (
        "mini_app/wrangler.toml missing — Issue #119 Pages publish "
        "config must be checked in"
    )
    text = wrangler.read_text(encoding="utf-8")
    assert 'name = "pasay-mini-app"' in text, (
        "Pages project name MUST be pinned to 'pasay-mini-app' so the "
        "trusted-lane deploy script publishes to the canonical URL "
        "(https://pasay-mini-app.pages.dev)."
    )
    assert 'pages_build_output_dir = "dist"' in text, (
        "Pages build output MUST point at 'mini_app/dist' (Vite's "
        "configured outDir)."
    )


def test_mini_app_public_redirects_ship_spa_fallback():
    """``mini_app/public/_redirects`` MUST contain the SPA fallback so
    Cloudflare Pages serves ``index.html`` for every non-asset path
    and the hash router takes over on refresh."""
    redirects = REPO_ROOT / "mini_app" / "public" / "_redirects"
    assert redirects.exists(), (
        "mini_app/public/_redirects missing — Pages SPA fallback will "
        "404 on Owner refresh."
    )
    text = redirects.read_text(encoding="utf-8")
    assert "/index.html" in text, (
        "_redirects MUST reference /index.html so the hash-routed SPA "
        "always lands on the canonical shell page."
    )


def test_mini_app_deploy_script_is_executable_and_self_contained():
    """``scripts/deploy_mini_app_pages.sh`` MUST exist, be executable,
    and gate every step (prereqs → build → smoke → deploy) so a broken
    bundle can never ship to production."""
    script = REPO_ROOT / "scripts" / "deploy_mini_app_pages.sh"
    assert script.exists(), (
        "scripts/deploy_mini_app_pages.sh missing — no trusted-lane "
        "deploy script for the Mini App."
    )
    assert os.access(script, os.X_OK), (
        "deploy_mini_app_pages.sh must be executable (chmod +x)."
    )
    text = script.read_text(encoding="utf-8")
    # Every gate must be explicit so a future regression that drops
    # one is caught immediately.
    for gate in (
        "CLOUDFLARE_API_TOKEN",
        "CLOUDFLARE_ACCOUNT_ID",
        "wrangler.toml",
        "public/_redirects",
        "npm run build",
        "npm run test:smoke",
        "pages deploy dist",
        "--project-name",
    ):
        assert gate in text, (
            f"deploy_mini_app_pages.sh missing gate: {gate!r} — a "
            f"regression in the script must not silently publish a "
            f"broken bundle to pasay-mini-app.pages.dev."
        )


def test_mini_app_pages_project_url_is_canonical():
    """The deploy script MUST surface the canonical Pages URL so the
    operator can wire it into ``PASAY_MINI_APP_URL`` and the Telegram
    MenuButton."""
    script = (REPO_ROOT / "scripts" / "deploy_mini_app_pages.sh").read_text(
        encoding="utf-8"
    )
    assert "https://pasay-mini-app.pages.dev" in script, (
        "deploy script MUST echo the canonical Pages URL — the "
        "operator wires this into PASAY_MINI_APP_URL + BotFather."
    )


# ===========================================================================
# B) /api/v1/webapp/auth — initData signature + Owner-only policy
# ===========================================================================


# Stable bot token + Telegram user ids used throughout the section.
TEST_BOT_TOKEN = "0000000000:TEST-BOT-TOKEN-DO-NOT-USE-IN-PROD"
TEST_OWNER_TELEGRAM_ID = 5177241442  # matches pasay-telegram-bot roles.py
TEST_SECRETARY_TELEGRAM_ID = 1083657401
TEST_UNKNOWN_TELEGRAM_ID = 9999999999


def _build_init_data(
    bot_token: str, *, telegram_user_id: int, auth_date: int | None = None
) -> str:
    """Construct a Telegram initData string with a valid HMAC for tests.

    Mirrors ``_check_init_data_signature`` server-side — keep these two
    implementations in sync (or the test passes while the production
    path rejects everything).

    Wire format: percent-encoded field values, joined by ``&``.
    HMAC input (Telegram data-check-string): the DECODED field values
    (sorted alphabetically by key, formatted as ``key=value``, joined
    by ``\n``).  This matches Telegram's documented validation
    semantics — see
    https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
    """
    auth_date = auth_date or int(time.time())
    user_json = (
        '{"id":' + str(telegram_user_id) +
        ',"first_name":"Test","username":"tester"}'
    )
    # urlencode quotes the user payload so the on-the-wire format
    # matches what Telegram delivers.
    from urllib.parse import quote
    user_encoded = quote(user_json, safe="")
    # Build the wire-format pairs in dict form so we can compute the
    # data-check-string from DECODED values (per Telegram spec).
    raw_pairs: dict[str, str] = {
        "query_id": "AAEh",
        "user": user_encoded,
        "auth_date": str(auth_date),
    }
    secret_key = hmac.new(
        b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256
    ).digest()
    # Build the data-check-string from DECODED values, sorted by key,
    # formatted ``key=value``, joined by ``\n``.  This mirrors
    # server-side ``_build_check_string`` exactly.
    decoded_pairs: dict[str, str] = {
        key: (unquote(value) if value else value) for key, value in raw_pairs.items()
    }
    check_string = "\n".join(
        f"{k}={v}" for k, v in sorted(decoded_pairs.items())
    )
    expected_hash = hmac.new(
        secret_key, check_string.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    raw_pairs["hash"] = expected_hash
    return "&".join(f"{k}={v}" for k, v in raw_pairs.items())


@pytest.fixture
def owner_workspace():
    """Seed a workspace whose OWNER User is bound to TEST_OWNER_TELEGRAM_ID.

    This matches the canonical ``pasay-telegram-bot/pasay_bot/roles.py``
    mapping so a single allowlist value covers both the bot side and
    the API side.
    """
    from app.v1.models.foundation import User

    with v1_session_ctx() as session:
        workspace = seed_workspace(session, name="MiniAppWS")
        # Bind the OWNER User's telegram_user_id so the webapp_auth
        # endpoint can resolve initData -> User.  The bootstrap owner
        # has telegram_user_id=None by default (seed_workspace doesn't
        # populate it); patch it here for the Mini App test path.
        owner_user = (
            session.query(User).filter_by(id=workspace.owner_user_id).one()
        )
        owner_user.telegram_user_id = TEST_OWNER_TELEGRAM_ID
        session.commit()
        # Also bind the secretary id so the SECRETARY-rejection case
        # has a known Telegram mapping.
        sec_user = (
            session.query(User).filter_by(id=workspace.secretary_user_id).one()
        )
        sec_user.telegram_user_id = TEST_SECRETARY_TELEGRAM_ID
        session.commit()
        yield workspace


@pytest.fixture
def webapp_client(monkeypatch, owner_workspace):
    """Yield (client, workspace, session) with the OWNER_TELEGRAM_USER_IDS
    env var bound and the bot token injected via pydantic settings
    override.

    Mirrors the runtime contract exactly: /webapp/auth reads both
    ``settings.telegram_bot_token`` and
    ``settings.pasay_owner_telegram_user_ids`` at request time so
    monkeypatching is safe.
    """
    from fastapi.testclient import TestClient

    from app import config as app_config
    from app.v1.main import app as v1_app

    monkeypatch.setenv("PASAY_OWNER_TELEGRAM_USER_IDS", str(TEST_OWNER_TELEGRAM_ID))
    monkeypatch.setattr(
        app_config.settings,
        "telegram_bot_token",
        TEST_BOT_TOKEN,
        raising=False,
    )
    monkeypatch.setattr(
        app_config.settings,
        "pasay_owner_telegram_user_ids",
        str(TEST_OWNER_TELEGRAM_ID),
        raising=False,
    )

    session = get_session_factory()()
    try:
        def _override_get_db():
            yield session

        v1_app.dependency_overrides[get_db] = _override_get_db
        try:
            with TestClient(v1_app) as client:
                yield client, owner_workspace, session
        finally:
            v1_app.dependency_overrides.clear()
    finally:
        session.close()


def test_webapp_auth_owner_only_issues_bearer(webapp_client):
    """A correctly-signed initData for the OWNER Telegram user MUST
    yield a 200 + bearer session pointing at the same org."""
    from app.core.security import hash_api_key as _hash
    from app.v1.models.foundation import ApiCredential

    client, workspace, session = webapp_client
    init_data = _build_init_data(TEST_BOT_TOKEN, telegram_user_id=TEST_OWNER_TELEGRAM_ID)
    response = client.post("/api/v1/webapp/auth", json={"init_data": init_data})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["role"] == "OWNER"
    assert body["org_id"] == workspace.org_id
    assert body["user_id"] == workspace.owner_user_id
    # API key must hash-match the canonical security helper.
    cred = (
        session.query(ApiCredential)
        .filter_by(user_id=body["user_id"], key_hash=_hash(body["api_key"]))
        .one_or_none()
    )
    assert cred is not None, (
        "issued api_key MUST be persisted in v1_api_credentials so the "
        "subsequent /api/v1/properties + /api/v1/dashboard/home calls "
        "succeed (they share the get_current_principal auth path)."
    )


def test_webapp_auth_bad_signature_returns_401(webapp_client):
    """An initData with a forged hash MUST be rejected with 401 — the
    Mini App can NEVER exchange a leaked / fabricated string for a
    bearer session."""
    client, _, _ = webapp_client
    init_data = _build_init_data(TEST_BOT_TOKEN, telegram_user_id=TEST_OWNER_TELEGRAM_ID)
    # Tamper with the hash field.
    bad = init_data.replace(
        "hash=",
        "hash=0000000000000000000000000000000000000000000000000000000000000000",
    )
    response = client.post("/api/v1/webapp/auth", json={"init_data": bad})
    assert response.status_code == 401, response.text


def test_webapp_auth_non_owner_telegram_id_returns_403(webapp_client):
    """A SECRETARY Telegram id (bound to a non-OWNER Membership) MUST
    be rejected with 403 — the Owner-only policy is enforced at the
    user-id gate BEFORE the User lookup."""
    client, _, _ = webapp_client
    init_data = _build_init_data(TEST_BOT_TOKEN, telegram_user_id=TEST_SECRETARY_TELEGRAM_ID)
    response = client.post("/api/v1/webapp/auth", json={"init_data": init_data})
    assert response.status_code == 403, response.text


def test_webapp_auth_unknown_telegram_id_returns_403(webapp_client):
    """A Telegram id that is NOT in PASAY_OWNER_TELEGRAM_USER_IDS MUST
    be rejected with 403 — the SPA stays locked for everyone who is
    not explicitly allowlisted."""
    client, _, _ = webapp_client
    init_data = _build_init_data(TEST_BOT_TOKEN, telegram_user_id=TEST_UNKNOWN_TELEGRAM_ID)
    response = client.post("/api/v1/webapp/auth", json={"init_data": init_data})
    assert response.status_code == 403, response.text


def test_webapp_auth_empty_allowlist_returns_503(webapp_client, monkeypatch):
    """Empty PASAY_OWNER_TELEGRAM_USER_IDS MUST lock the SPA closed
    with 503 — the operator-deploy fail-closed contract (see AGENTS
    .md §4 / webapp_auth.py docstring)."""
    client, _, _ = webapp_client
    # Re-bind with an empty allowlist.
    from app import config as app_config

    monkeypatch.setattr(
        app_config.settings,
        "pasay_owner_telegram_user_ids",
        "",
        raising=False,
    )
    init_data = _build_init_data(TEST_BOT_TOKEN, telegram_user_id=TEST_OWNER_TELEGRAM_ID)
    response = client.post("/api/v1/webapp/auth", json={"init_data": init_data})
    assert response.status_code == 503, response.text
    assert "pasay_owner_telegram_user_ids" in response.text


def test_webapp_auth_stale_auth_date_returns_401(webapp_client):
    """initData older than 24 h MUST be rejected with 401 — closes the
    replay window for a leaked initData string."""
    client, _, _ = webapp_client
    stale_date = int(time.time()) - 48 * 3600
    init_data = _build_init_data(
        TEST_BOT_TOKEN,
        telegram_user_id=TEST_OWNER_TELEGRAM_ID,
        auth_date=stale_date,
    )
    response = client.post("/api/v1/webapp/auth", json={"init_data": init_data})
    assert response.status_code == 401, response.text


def test_webapp_auth_accepts_realistic_percent_encoded_user_json(webapp_client):
    """Regression guardrail for the documented Telegram data-check-string
    semantics (https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app).

    Real Telegram clients deliver ``user=`` as a percent-encoded JSON
    object.  The HMAC input MUST be built from the DECODED JSON, not
    from the raw percent-encoded chunk — the prior implementation
    rebuilt the check string from raw query chunks and accepted self-
    consistent test fixtures while rejecting real Telegram initData.

    This test:
      1. Constructs a realistic user JSON object (commas, quotes,
         unicode escape points, a colon, and a boolean — all the
         characters a real Telegram client percent-encodes);
      2. Percent-encodes it with ``quote(safe="")`` to mirror the
         on-the-wire form Telegram delivers;
      3. Independently computes the expected hash from the DECODED
         values (sorted alphabetically by key, ``key=value`` joined
         by ``\n``);
      4. Posts the resulting initData to /webapp/auth and asserts a
         200 + OWNER bearer — proving the verifier actually consumes
         decoded values.

    A regression that re-introduces the raw-chunk check-string builder
    fails step 4 with 401 (the wire-format hash no longer matches
    what the verifier computes from decoded fields).
    """
    client, workspace, _ = webapp_client

    # 1) Realistic user payload.  Includes a comma, both quote styles,
    #    nested escapes, a colon, and a boolean — every byte Telegram
    #    would percent-encode on the wire.
    user_obj = {
        "id": TEST_OWNER_TELEGRAM_ID,
        "first_name": "Test O'Owner",
        "last_name": "Doe, Jr.",
        "username": "tester.user",
        "language_code": "en-US",
        "is_premium": True,
        "photo_url": "https://t.me/i/avatar.png",
    }
    user_json = json.dumps(user_obj, separators=(",", ":"))
    # Sanity: confirm the raw JSON contains the characters that
    # percent-encoding must escape.  If a future change tightens the
    # payload, this assertion makes the regression visible.
    for must_contain in ('"', ":", ","):
        assert must_contain in user_json

    # 2) Percent-encode for the on-the-wire form (Telegram's wire
    #    format).  ``safe=""`` so ``/``, ``?``, ``:``, ``,``, ``"``,
    #    ``'`` and every other reserved character is escaped — same as
    #    what the Mini App's WebView produces.
    user_encoded = quote(user_json, safe="")
    assert "%" in user_encoded, (
        "user payload MUST be percent-encoded for the wire format "
        "to be a realistic Telegram initData; this regression would "
        "silently weaken the test by feeding the verifier unencoded "
        "JSON."
    )

    auth_date = int(time.time())
    raw_pairs: dict[str, str] = {
        "query_id": "AAHdF6MQAAAAAN0XmD",
        "user": user_encoded,
        "auth_date": str(auth_date),
    }

    # 3) Independently compute the expected hash from DECODED values
    #    (no shared helper — we want to prove the spec, not the test
    #    mirror, is what the verifier obeys).
    secret_key = hmac.new(
        b"WebAppData", TEST_BOT_TOKEN.encode("utf-8"), hashlib.sha256,
    ).digest()
    decoded_pairs: dict[str, str] = {
        key: unquote(value) for key, value in raw_pairs.items()
    }
    check_string = "\n".join(
        f"{k}={v}" for k, v in sorted(decoded_pairs.items())
    )
    expected_hash = hmac.new(
        secret_key, check_string.encode("utf-8"), hashlib.sha256,
    ).hexdigest()
    # Sanity: independently recompute the hash over the RAW
    # percent-encoded chunks.  This MUST differ from the decoded hash
    # (a regression where the verifier uses raw chunks would silently
    # match this second hash and break real Telegram initData).
    raw_check_string = "\n".join(
        f"{k}={v}" for k, v in sorted(raw_pairs.items())
    )
    raw_hash = hmac.new(
        secret_key, raw_check_string.encode("utf-8"), hashlib.sha256,
    ).hexdigest()
    assert expected_hash != raw_hash, (
        "decoded-hash and raw-hash MUST differ for a percent-encoded "
        "user JSON; if they ever match, this test no longer exercises "
        "the Telegram semantics regression."
    )

    raw_pairs["hash"] = expected_hash
    init_data = "&".join(f"{k}={v}" for k, v in raw_pairs.items())

    # 4) Verifier must accept the decoded-value hash and reject the
    #    raw-chunk hash.  We exercise the accepted path; the rejected
    #    path is covered by test_webapp_auth_bad_signature_returns_401
    #    and the negative test below.
    response = client.post("/api/v1/webapp/auth", json={"init_data": init_data})
    assert response.status_code == 200, (
        f"verifier MUST accept an initData whose hash was computed "
        f"from URL-decoded parsed fields (Telegram spec); got "
        f"{response.status_code} {response.text}"
    )
    body = response.json()
    assert body["role"] == "OWNER"
    assert body["org_id"] == workspace.org_id
    assert body["user_id"] == workspace.owner_user_id


def test_webapp_auth_rejects_raw_percent_encoded_hash(webapp_client):
    """Companion negative test for the regression guardrail above.

    Forging the hash from the RAW percent-encoded chunks (the prior
    implementation's semantics) MUST be rejected with 401 — the
    verifier signs the decoded fields, not the wire bytes.  Without
    this test, a future refactor could silently flip the verifier
    back to the raw-chunk builder and ``test_webapp_auth_..._issu
    es_bearer`` would still pass (because ``_build_init_data`` is
    rewritten to match whichever side is wrong).
    """
    client, _, _ = webapp_client

    # Same realistic payload as the positive case.
    user_obj = {
        "id": TEST_OWNER_TELEGRAM_ID,
        "first_name": "Test O'Owner",
        "last_name": "Doe, Jr.",
        "username": "tester.user",
        "language_code": "en-US",
        "is_premium": True,
    }
    user_json = json.dumps(user_obj, separators=(",", ":"))
    user_encoded = quote(user_json, safe="")
    auth_date = int(time.time())
    raw_pairs: dict[str, str] = {
        "query_id": "AAHdF6MQAAAAAN0XmD",
        "user": user_encoded,
        "auth_date": str(auth_date),
    }

    # Forged hash from RAW percent-encoded chunks — this is what the
    # prior (buggy) verifier computed.  The fixed verifier MUST
    # reject it because its own hash input is the decoded fields.
    secret_key = hmac.new(
        b"WebAppData", TEST_BOT_TOKEN.encode("utf-8"), hashlib.sha256,
    ).digest()
    raw_check_string = "\n".join(
        f"{k}={v}" for k, v in sorted(raw_pairs.items())
    )
    forged_hash = hmac.new(
        secret_key, raw_check_string.encode("utf-8"), hashlib.sha256,
    ).hexdigest()
    raw_pairs["hash"] = forged_hash
    init_data = "&".join(f"{k}={v}" for k, v in raw_pairs.items())

    response = client.post("/api/v1/webapp/auth", json={"init_data": init_data})
    assert response.status_code == 401, (
        f"verifier MUST reject an initData whose hash was forged over "
        f"raw percent-encoded chunks; got {response.status_code} "
        f"{response.text}"
    )


# ===========================================================================
# D) Mini App issued bearer -> Home + Properties endpoints
# ===========================================================================


def test_webapp_bearer_can_list_properties(webapp_client):
    """The bearer issued by /webapp/auth MUST work against
    ``GET /api/v1/properties`` (the second Owner-acceptance entry the
    issue enumerates)."""
    client, workspace, _ = webapp_client
    init_data = _build_init_data(TEST_BOT_TOKEN, telegram_user_id=TEST_OWNER_TELEGRAM_ID)
    auth = client.post("/api/v1/webapp/auth", json={"init_data": init_data}).json()
    # Now hit /properties with the issued bearer.
    response = client.get(
        f"/api/v1/properties?org_id={auth['org_id']}",
        headers={"Authorization": f"Bearer {auth['api_key']}"},
    )
    assert response.status_code == 200, response.text
    items = response.json()
    # seed_workspace creates exactly one Property.
    assert isinstance(items, list)
    assert len(items) >= 1
    names = [item["name"] for item in items]
    assert any("MiniAppWS" in (name or "") for name in names), (
        "expected the seeded property name to surface in the response"
    )


def test_webapp_bearer_can_call_dashboard_home(webapp_client):
    """The bearer issued by /webapp/auth MUST work against
    ``GET /api/v1/dashboard/home`` (the first Owner-acceptance entry
    the issue enumerates)."""
    client, _, _ = webapp_client
    init_data = _build_init_data(TEST_BOT_TOKEN, telegram_user_id=TEST_OWNER_TELEGRAM_ID)
    auth = client.post("/api/v1/webapp/auth", json={"init_data": init_data}).json()
    response = client.get(
        f"/api/v1/dashboard/home?org_id={auth['org_id']}",
        headers={"Authorization": f"Bearer {auth['api_key']}"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    # DashboardHome contract: every required field is present and the
    # shape matches what the Mini App renders.
    for key in (
        "open_operations",
        "overdue_rent",
        "overdue_rent_count",
        "open_repairs",
        "open_repairs_count",
        "pending_renewals",
        "pending_renewals_count",
        "open_move_outs",
        "open_move_outs_count",
        "pending_expense_claims",
        "open_tasks",
        "generated_at",
    ):
        assert key in body, (
            f"/dashboard/home MUST return {key!r} for the Mini App to "
            f"render the Home KPIs without a follow-up API call."
        )


def test_webapp_bearer_missing_returns_401(webapp_client):
    """A request to /properties WITHOUT the issued bearer MUST be
    rejected with 401 — the Mini App is the ONLY client that can mint
    these short-lived keys, so a missing header is the only signal of
    a tampered SPA session."""
    client, _, _ = webapp_client
    init_data = _build_init_data(TEST_BOT_TOKEN, telegram_user_id=TEST_OWNER_TELEGRAM_ID)
    auth = client.post("/api/v1/webapp/auth", json={"init_data": init_data}).json()
    response = client.get(
        f"/api/v1/properties?org_id={auth['org_id']}",
        # No Authorization header.
    )
    assert response.status_code == 401, response.text


# ===========================================================================
# C) MenuButton WebApp registration (`打开管理后台`)
# ===========================================================================


def test_bot_menu_button_registers_owner_mini_app(monkeypatch):
    """The bot's ``_set_owner_mini_app_menu_button`` post-init hook
    MUST call ``set_chat_menu_button`` once per bound Telegram user id
    (default: the OWNER role id) with a MenuButtonWebApp pointing at
    ``PASAY_MINI_APP_URL`` and the text "打开管理后台".

    Regression guardrail: a future PR that drops the menu-button
    registration (or re-binds it to the wrong scope) is caught here
    rather than only at Owner acceptance.
    """
    import asyncio
    from unittest import mock

    from pasay_bot.main import build_application
    from pasay_bot.config import Settings
    from pasay_bot.api_client import PasayApiClient
    from pasay_bot.state.store import StateStore

    settings = Settings(
        pasay_tg_bot_token="0:DUMMY",
        pasay_api_base="http://127.0.0.1:1/api/v1",
        pasay_api_key="dummy",
        pasay_mini_app_url="https://pasay-mini-app.pages.dev/",
        pasay_mini_app_owner_telegram_ids=str(TEST_OWNER_TELEGRAM_ID),
    )
    api_client = PasayApiClient(
        settings.pasay_api_base, settings.pasay_api_key,
    )
    store = StateStore(":memory:")
    # Bot identity is irrelevant for this assertion — substitute a
    # fake Bot with a recording set_chat_menu_button.
    captured: list[dict] = []

    class _FakeBot:
        async def set_chat_menu_button(self, menu_button, scope):
            captured.append({
                "menu_button": menu_button,
                "scope": scope,
            })

    app = build_application(settings, api_client, store, bot=_FakeBot())
    # post_init is bound to the wrapped function — run it explicitly.
    post_init = app.post_init
    assert post_init is not None
    asyncio.run(post_init(app))
    # Exactly one call, scoped to the OWNER chat id.
    assert len(captured) == 1, (
        f"expected one set_chat_menu_button call for the OWNER id, "
        f"got {len(captured)}: {captured!r}"
    )
    call = captured[0]
    from telegram import BotCommandScopeChat

    assert isinstance(call["scope"], BotCommandScopeChat)
    assert call["scope"].chat_id == TEST_OWNER_TELEGRAM_ID
    # The button MUST be a WebApp button labelled "打开管理后台" with
    # the configured URL — these are the Owner-visible markers.
    menu_button = call["menu_button"]
    assert getattr(menu_button, "text", None) == "打开管理后台"
    web_app = getattr(menu_button, "web_app", None)
    assert web_app is not None, (
        "MenuButtonWebApp MUST carry a web_app attribute pointing at the "
        "Mini App URL (PTB MenuButtonWebApp API contract)."
    )
    assert getattr(web_app, "url", None) == "https://pasay-mini-app.pages.dev/"


def test_bot_menu_button_skipped_when_mini_app_url_unset(monkeypatch):
    """If PASAY_MINI_APP_URL is unset, the bot MUST NOT call
    set_chat_menu_button at all — the persistent Reply Keyboard
    remains the only Owner entry point, and no orphaned WebApp button
    can appear in the bot's chat."""
    import asyncio

    from pasay_bot.main import build_application
    from pasay_bot.config import Settings
    from pasay_bot.api_client import PasayApiClient
    from pasay_bot.state.store import StateStore

    settings = Settings(
        pasay_tg_bot_token="0:DUMMY",
        pasay_api_base="http://127.0.0.1:1/api/v1",
        pasay_api_key="dummy",
        # pasay_mini_app_url intentionally empty
    )
    api_client = PasayApiClient(
        settings.pasay_api_base, settings.pasay_api_key,
    )
    store = StateStore(":memory:")

    captured: list[dict] = []

    class _FakeBot:
        async def set_chat_menu_button(self, menu_button, scope):
            captured.append({"menu_button": menu_button, "scope": scope})

    app = build_application(settings, api_client, store, bot=_FakeBot())
    asyncio.run(app.post_init(app))
    assert captured == [], (
        f"set_chat_menu_button MUST be skipped when PASAY_MINI_APP_URL "
        f"is empty (persistent Reply Keyboard is the only entry "
        f"point); got {captured!r}"
    )


def test_bot_menu_button_rejects_non_https_url(monkeypatch):
    """A non-https PASAY_MINI_APP_URL MUST be skipped — Telegram's
    WebView refuses http:// origins.  A regression that lets an http URL
    through would silently break the Owner entry point at runtime."""
    import asyncio

    from pasay_bot.main import build_application
    from pasay_bot.config import Settings
    from pasay_bot.api_client import PasayApiClient
    from pasay_bot.state.store import StateStore

    settings = Settings(
        pasay_tg_bot_token="0:DUMMY",
        pasay_api_base="http://127.0.0.1:1/api/v1",
        pasay_api_key="dummy",
        pasay_mini_app_url="http://insecure.example.com/app/",
    )
    api_client = PasayApiClient(
        settings.pasay_api_base, settings.pasay_api_key,
    )
    store = StateStore(":memory:")

    captured: list[dict] = []

    class _FakeBot:
        async def set_chat_menu_button(self, menu_button, scope):
            captured.append({"menu_button": menu_button, "scope": scope})

    app = build_application(settings, api_client, store, bot=_FakeBot())
    asyncio.run(app.post_init(app))
    assert captured == [], (
        f"set_chat_menu_button MUST be skipped for non-https URLs "
        f"(Telegram WebView refuses http://); got {captured!r}"
    )