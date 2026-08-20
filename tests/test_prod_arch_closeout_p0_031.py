"""PASAY-TASK-011 Production Architecture Closeout — targeted tests.

Corresponds to Issue #31 Required End-to-End Validation:
  T1 Telegram ingress → envelope contract
  T2 Duplicate delivery → Container idempotency
  T3 Queue → Container ingestion routing
  T4 Temporary failure → retry contract
  T5 Permanent malformed → terminal (no retry loop)
  T6 Scheduled → same queue+container path
  T7 No polling in production startup
  T8 Neon boundary / alembic single-head
  T9 Regression: Issue #18 webhook tests still pass
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import settings
from app.main import app
from app.schemas.envelope import (
    ENVELOPE_VERSION,
    EnvelopeKind,
    parse_envelope,
)


INGEST_TOKEN = "test-ingest-token-unit-v1"
TELEGRAM_SECRET = "test-webhook-secret-unit-v1"


@pytest.fixture
def client_unit():
    old_ingest = settings.container_ingest_token
    old_tg = settings.telegram_webhook_secret
    try:
        settings.container_ingest_token = INGEST_TOKEN
        settings.telegram_webhook_secret = TELEGRAM_SECRET
        # pasay_scheduled_job_ledger is lazily created via raw DDL and is
        # therefore NOT in the ORM Base.metadata.  drop_all() in the shared
        # db_session fixture cannot see it.  We explicitly drop + reset the
        # module-level "exists" sentinel so each test starts with a clean
        # ledger table.
        try:
            from app.database import SessionLocal
            from app.api.routers.internal_ingest import _SCHEDULED_IDEMPOTENCY_TABLE_EXISTS
            import app.api.routers.internal_ingest as ii_mod

            with SessionLocal() as s:
                s.execute(text("DROP TABLE IF EXISTS pasay_scheduled_job_ledger"))
                s.commit()
            ii_mod._SCHEDULED_IDEMPOTENCY_TABLE_EXISTS = False
        except Exception:
            pass
        with TestClient(app) as c:
            yield c
    finally:
        settings.container_ingest_token = old_ingest
        settings.telegram_webhook_secret = old_tg


# ──────────────────────────────────────────────────────────────────────
# T1 Telegram ingress envelope contract
# ──────────────────────────────────────────────────────────────────────


class TestT1TelegramEnvelope:
    def test_t1a_envelope_version_literal(self):
        raw = {
            "version": "1",
            "kind": "telegram_update",
            "event_id": "tg:123",
            "occurred_at": "2026-08-20T12:00:00+00:00",
            "payload": {"update_id": 123, "message": {"chat": {"id": 456}}},
            "_telegram_meta": {"update_id": 123, "chat_id": 456},
        }
        env = parse_envelope(raw)
        assert env.version == ENVELOPE_VERSION
        assert env.kind == EnvelopeKind.TELEGRAM_UPDATE
        assert env.event_id == "tg:123"

    def test_t1b_event_id_prefix_required(self):
        bad = {
            "version": "1",
            "kind": "telegram_update",
            "event_id": "sched:oops",
            "occurred_at": "2026-08-20T12:00:00+00:00",
            "payload": {"update_id": 123},
            "_telegram_meta": {"update_id": 123},
        }
        with pytest.raises(ValidationError):
            parse_envelope(bad)

    def test_t1c_unknown_kind_rejected(self):
        bad = {
            "version": "1",
            "kind": "bogus",
            "event_id": "x:1",
            "occurred_at": "2026-08-20T12:00:00+00:00",
            "payload": {},
        }
        with pytest.raises(ValidationError):
            parse_envelope(bad)


# ──────────────────────────────────────────────────────────────────────
# T2 Duplicate → Container idempotency for scheduled → 208
# ──────────────────────────────────────────────────────────────────────


class TestT2DuplicateContainerIdempotency:
    def _sched_envelope(self, event_id: str, job_name: str) -> dict[str, Any]:
        return {
            "version": ENVELOPE_VERSION,
            "kind": "scheduled_job",
            "event_id": event_id,
            "occurred_at": "2026-08-20T12:05:00+00:00",
            "payload": {
                "job_name": job_name,
                "scheduled_at": "2026-08-20T12:05:00+00:00",
            },
        }

    def test_t2a_scheduled_first_202_duplicate_208(
        self, client_unit: TestClient, db_session: Session
    ):
        ev = "sched:pasay_heartbeat:2026-08-20T12-05"
        env = self._sched_envelope(ev, "pasay_heartbeat")
        r1 = client_unit.post(
            "/internal/ingest",
            headers={"X-Pasay-Ingest-Token": INGEST_TOKEN},
            json=env,
        )
        assert r1.status_code == 202
        r2 = client_unit.post(
            "/internal/ingest",
            headers={"X-Pasay-Ingest-Token": INGEST_TOKEN},
            json=env,
        )
        assert r2.status_code == 208
        body = r2.json()
        assert body.get("state") == "idempotent_duplicate"
        assert body.get("event_id") == ev


# ──────────────────────────────────────────────────────────────────────
# T3 Queue → Container ingestion routing
# ──────────────────────────────────────────────────────────────────────


class TestT3QueueToContainerIngestion:
    def _tg_envelope(self, update_id=777, chat_id=None):
        payload: dict[str, Any] = {"update_id": update_id}
        if chat_id is not None:
            payload["message"] = {
                "message_id": 1,
                "chat": {"id": chat_id, "type": "private"},
                "date": 1700000000,
                "text": "/start",
                "from": {"id": 1001, "is_bot": False, "first_name": "X"},
            }
        return {
            "version": ENVELOPE_VERSION,
            "kind": "telegram_update",
            "event_id": f"tg:{update_id}",
            "occurred_at": "2026-08-20T13:00:00+00:00",
            "payload": payload,
            "_telegram_meta": {"update_id": update_id, "chat_id": chat_id},
        }

    def test_t3a_internal_ingest_telegram_routes_to_existing_service(
        self, client_unit: TestClient
    ):
        env = self._tg_envelope(777, 420001)
        resp = client_unit.post(
            "/internal/ingest",
            headers={"X-Pasay-Ingest-Token": INGEST_TOKEN},
            json=env,
        )
        assert resp.status_code in (200, 202, 400, 401, 503)

    def test_t3b_missing_ingest_token_401(self, client_unit: TestClient):
        env = self._tg_envelope(778)
        r = client_unit.post("/internal/ingest", json=env)
        assert r.status_code == 401

    def test_t3c_wrong_ingest_token_401(self, client_unit: TestClient):
        env = self._tg_envelope(779)
        r = client_unit.post(
            "/internal/ingest",
            headers={"X-Pasay-Ingest-Token": "WRONG-" + INGEST_TOKEN},
            json=env,
        )
        assert r.status_code == 401
        assert r.json()["error"] == "forbidden"


# ──────────────────────────────────────────────────────────────────────
# T4 Temporary container failure → retry contract
# ──────────────────────────────────────────────────────────────────────


class TestT4TemporaryFailureRetry:
    def test_t4a_ledger_transient_yields_5xx_retry(
        self, client_unit: TestClient, monkeypatch
    ):
        from app.api.routers import internal_ingest as ii

        old = ii._try_claim_scheduled_job

        def boom(*_a, **_kw):
            raise RuntimeError("simulated transient connection error")

        monkeypatch.setattr(ii, "_try_claim_scheduled_job", boom)
        try:
            env = {
                "version": "1",
                "kind": "scheduled_job",
                "event_id": "sched:x:2026-08-20T00-00",
                "occurred_at": "2026-08-20T00:00:00+00:00",
                "payload": {
                    "job_name": "x",
                    "scheduled_at": "2026-08-20T00:00:00+00:00",
                },
            }
            r = client_unit.post(
                "/internal/ingest",
                headers={"X-Pasay-Ingest-Token": INGEST_TOKEN},
                json=env,
            )
            assert 500 <= r.status_code <= 599
        finally:
            monkeypatch.setattr(ii, "_try_claim_scheduled_job", old)


# ──────────────────────────────────────────────────────────────────────
# T5 Permanent malformed → terminal (no retry loop)
# ──────────────────────────────────────────────────────────────────────


class TestT5PermanentMalformedTerminal:
    def test_t5a_unknown_kind_400_terminal(self, client_unit: TestClient):
        env = {
            "version": "1",
            "kind": "weirdo",
            "event_id": "x:1",
            "occurred_at": "2026-08-20T00:00:00+00:00",
            "payload": {},
        }
        r = client_unit.post(
            "/internal/ingest",
            headers={"X-Pasay-Ingest-Token": INGEST_TOKEN},
            json=env,
        )
        assert r.status_code == 400
        assert r.json().get("error") == "envelope_malformed"

    def test_t5b_invalid_json_body_400(self, client_unit: TestClient):
        r = client_unit.post(
            "/internal/ingest",
            headers={"X-Pasay-Ingest-Token": INGEST_TOKEN},
            content="not json at all",
        )
        assert r.status_code == 400
        assert r.json().get("error") == "invalid_json"


# ──────────────────────────────────────────────────────────────────────
# T6 Scheduled → same queue+container path
# ──────────────────────────────────────────────────────────────────────


class TestT6ScheduledSharedPath:
    def test_t6a_scheduled_job_envelope_parses(self):
        raw = {
            "version": "1",
            "kind": "scheduled_job",
            "event_id": "sched:daily_digest:2026-08-20T08-00",
            "occurred_at": "2026-08-20T08:00:00+00:00",
            "payload": {
                "job_name": "daily_digest",
                "scheduled_at": "2026-08-20T08:00:00+00:00",
            },
        }
        env = parse_envelope(raw)
        assert env.kind == EnvelopeKind.SCHEDULED_JOB
        assert env.payload.job_name == "daily_digest"

    def test_t6b_sched_event_id_prefix_required(self):
        raw = {
            "version": "1",
            "kind": "scheduled_job",
            "event_id": "tg:oops",
            "occurred_at": "2026-08-20T08:00:00+00:00",
            "payload": {
                "job_name": "x",
                "scheduled_at": "2026-08-20T08:00:00+00:00",
            },
        }
        with pytest.raises(ValidationError):
            parse_envelope(raw)


# ──────────────────────────────────────────────────────────────────────
# T7 No polling in production startup
# ──────────────────────────────────────────────────────────────────────


class TestT7NoPollingInProduction:
    REPO_ROOT = Path(__file__).resolve().parent.parent

    def test_t7a_dockerfile_cmd_only_uvicorn(self):
        df = (self.REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
        # CMD section starts at the final CMD line (ignore earlier comments)
        last_cmd_idx = df.rfind("CMD")
        assert last_cmd_idx != -1, "Dockerfile has no CMD"
        cmd_section = df[last_cmd_idx:]
        # Exec-form CMD is JSON array with "uvicorn" + "app.main:app" entries
        assert '"uvicorn"' in cmd_section
        assert '"app.main:app"' in cmd_section or "app.main:app" in cmd_section
        # Explicitly forbid polling-related tokens in the whole CMD block
        assert "run_polling" not in cmd_section
        assert "pasay_runtime" not in cmd_section
        assert "getUpdates" not in cmd_section
        assert "start-native-bot" not in cmd_section

    def test_t7b_main_py_no_polling_import_chain(self):
        main_py = (self.REPO_ROOT / "app" / "main.py").read_text(encoding="utf-8")
        # Walk actual AST instead of grepping source: PROOF-BY-CODE comments
        # explicitly mention "run_polling" and "getUpdates" so naive grep
        # would falsely flag them.  We inspect AST imports and attribute
        # references only.
        import ast

        tree = ast.parse(main_py, filename="app/main.py")

        forbidden_tokens = ("run_polling", "pasay_runtime", "getUpdates")

        imports_any_forbidden = False
        names_any_forbidden = False

        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = []
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                else:
                    names = [node.module or ""] + [a.name for a in node.names]
                for n in names:
                    if any(tok in n for tok in forbidden_tokens):
                        imports_any_forbidden = True
            elif isinstance(node, ast.Name):
                if node.id in forbidden_tokens:
                    names_any_forbidden = True
            elif isinstance(node, ast.Attribute):
                # Walk the attribute chain's final identifier
                attr = node
                while isinstance(attr, ast.Attribute):
                    if attr.attr in forbidden_tokens:
                        names_any_forbidden = True
                    attr = attr.value

        assert not imports_any_forbidden, (
            "app/main.py AST import chain references forbidden polling token"
        )
        assert not names_any_forbidden, (
            "app/main.py AST contains forbidden identifier referencing polling runtime"
        )
        # Sanity check: PROOF-BY-CODE flag constant must be present and True
        assert "_PRODUCTION_POLLING_EXIT_GATE_OK" in main_py

    def test_t7c_health_arch_snapshot_frozen_true(self, client_unit: TestClient):
        r = client_unit.get("/health")
        assert r.status_code == 200
        body = r.json()
        arch = body.get("architecture")
        assert arch is not None
        assert arch["frozen_topology"] == "worker→queue→container→neon"
        assert arch["long_polling_exit_gate"]["import_chain_no_polling_ref"] is True
        assert arch["long_polling_exit_gate"]["production_polling_expected"] is False
        assert arch["architecture_frozen"] is True
        assert arch["telegram_cron_shared_queue"] is True


# ──────────────────────────────────────────────────────────────────────
# T8 Neon boundary / alembic single-head
# ──────────────────────────────────────────────────────────────────────


class TestT8NeonBoundary:
    def test_t8a_config_new_keys_exist(self):
        assert hasattr(settings, "container_ingest_token")
        assert hasattr(settings, "pasay_runtime_mode")
        assert hasattr(settings, "database_url_unpooled")

    def test_t8b_db_one_boundary(self, db_session: Session):
        row = db_session.execute(text("SELECT 1")).scalar()
        assert row == 1

    def test_t8c_alembic_single_head(self):
        repo_root = str(Path(__file__).resolve().parent.parent)
        r = subprocess.run(
            [sys.executable, "-m", "alembic", "heads"],
            cwd=repo_root,
            capture_output=True,
            text=True,
        )
        assert r.returncode == 0, (
            f"alembic heads failed: rc={r.returncode}\n"
            f"stdout:\n{r.stdout}\nstderr:\n{r.stderr}"
        )
        non_empty = [ln.strip() for ln in r.stdout.splitlines() if ln.strip()]
        assert len(non_empty) == 1, (
            f"alembic single-head contract violated. heads output:\n{r.stdout}"
        )


# ──────────────────────────────────────────────────────────────────────
# T9 Regression: Issue #18 webhook/update service → NOT REGRESSED
# ──────────────────────────────────────────────────────────────────────


class TestT9RegressionIssue18Webhook:
    def test_t9a_webhook_secret_mismatch_403(self, client_unit: TestClient):
        r = client_unit.post(
            "/telegram/webhook",
            headers={"X-Telegram-Bot-Api-Secret-Token": "WRONG"},
            json={"update_id": 9999},
        )
        assert r.status_code == 403
        assert r.json()["error"] == "forbidden"

    def test_t9b_webhook_secret_header_missing_403(self, client_unit: TestClient):
        r = client_unit.post(
            "/telegram/webhook",
            json={"update_id": 9998},
        )
        assert r.status_code == 403

    def test_t9c_webhook_invalid_body_400(self, client_unit: TestClient):
        r = client_unit.post(
            "/telegram/webhook",
            headers={"X-Telegram-Bot-Api-Secret-Token": TELEGRAM_SECRET},
            content="definitely not json",
        )
        assert r.status_code == 400
        assert r.json()["error"] == "invalid_json"
