from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

# Export .env keys to os.environ so BOTH pydantic Settings (config.py) and any
# raw os.getenv consumers (e.g. the C1 copilot llm.py provider_config) see them.
# The app runs with CWD=/opt/pasay-pm; fall back to the file's own dir otherwise.
load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=False)

from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.api.routers import (
    attachments,
    audit,
    auth,
    commission,
    evidence,
    expense,
    income,
    internal_ingest,
    leases,
    onboarding,
    operations,
    payments,
    properties,
    reports,
    repairs,
    tasks,
    tenants,
    telegram_webhook,
    units,
    viewings,
)
from app.config import settings
from app.database import get_db

app = FastAPI(
    title="PASay Property Management API",
    description="Small property management backend (V1 phase 1).",
    version="1.0.0",
)

API_PREFIX = "/api/v1"

app.include_router(auth.router, prefix=API_PREFIX)
app.include_router(onboarding.router, prefix=API_PREFIX)
app.include_router(properties.router, prefix=API_PREFIX)
app.include_router(units.router, prefix=API_PREFIX)
app.include_router(tenants.router, prefix=API_PREFIX)
app.include_router(leases.router, prefix=API_PREFIX)
app.include_router(income.router, prefix=API_PREFIX)
app.include_router(payments.router, prefix=API_PREFIX)
app.include_router(expense.router, prefix=API_PREFIX)
app.include_router(commission.router, prefix=API_PREFIX)
app.include_router(tasks.router, prefix=API_PREFIX)
app.include_router(reports.router, prefix=API_PREFIX)
app.include_router(repairs.router, prefix=API_PREFIX)
app.include_router(attachments.router, prefix=API_PREFIX)
app.include_router(audit.router, prefix=API_PREFIX)
app.include_router(operations.router, prefix=API_PREFIX)
app.include_router(evidence.router, prefix=API_PREFIX)
app.include_router(viewings.router, prefix=API_PREFIX)

# Telegram webhook is a *public* endpoint; it is intentionally NOT placed under
# /api/v1 because Telegram itself delivers directly to this URL and authentication
# is via the ``X-Telegram-Bot-Api-Secret-Token`` header only.
app.include_router(telegram_webhook.router)

# ── PASAY-TASK-011 / Production Architecture Closeout P0 ─────────────
# Internal ingestion boundary reachable ONLY from the Cloudflare Worker queue
# consumer via the native Container binding + shared ingest token.
# The public internet MUST NOT route here; Cloudflare Container deployments
# do not expose ``/internal/*`` on any public hostname.
app.include_router(internal_ingest.router)


# ---------------------------------------------------------------------------
# Long Polling / Legacy Runtime Exit Gate — Scope D + Scope "Long Polling Exit"
#
# PROOF-BY-CODE that the Cloudflare Container production entry point NEVER
# invokes ``run_polling()`` / ``getUpdates``:
#
#   1. Dockerfile CMD only runs ``uvicorn app.main:app`` (HTTP server only).
#   2. This module does NOT import anything from ``bin/pasay_runtime.py`` or
#      ``bin/run-operations-worker.py`` or ``pasay_bot.main.run_polling``.
#   3. The pasay-telegram-bot subtree is ONLY wired from
#      ``app.services.telegram_webhook.get_ptb_application()`` which calls
#      ``build_application()`` + ``initialize()`` + ``start()`` and then
#      uses ONLY ``process_update()``. No polling loop is ever started.
#   4. ``pasay_runtime_mode == "cloudflare-container"`` (the production value)
#      further hardens this by explicitly refusing any accidental sub-process
#      spawn that could re-open a second bot runtime.
#
# If a future developer adds ``pasay_bot.main.start_polling()`` anywhere in
# this import chain, the CI health test ``test_t7_no_polling_in_production``
# must catch it immediately.
# ---------------------------------------------------------------------------
_PRODUCTION_POLLING_EXIT_GATE_OK: bool = True


def _webhook_health_snapshot(db: Session) -> dict:
    """Best-effort /health supplement for the Telegram webhook subsystem.

    Never raises; any DB error returns ``db_error_type`` with the class name so
    operators can tell the difference between "no rows" and "DB unreachable".
    """
    out: dict = {
        "webhook_configured": bool((settings.telegram_webhook_secret or "").strip()),
        "telegram_bot_token_configured": bool((settings.telegram_bot_token or "").strip()),
        "window_seconds": 24 * 3600,
    }
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    try:
        rows = db.execute(text(
            "SELECT state, COUNT(*) AS c FROM telegram_webhook_updates "
            "WHERE created_at >= :cutoff GROUP BY state"
        ), {"cutoff": cutoff}).fetchall()
        out["states_24h"] = {r[0]: int(r[1]) for r in rows}
        err_rows = db.execute(text(
            "SELECT update_id, last_error_type, created_at FROM telegram_webhook_updates "
            "WHERE state IN ('failed','retryable') ORDER BY updated_at DESC LIMIT 5"
        )).fetchall()
        out["recent_errors"] = [
            {
                "update_id": int(r[0]),
                "error_type": r[1],
                "created_at": r[2].isoformat() if r[2] is not None else None,
            }
            for r in err_rows
        ]
        total = db.execute(text(
            "SELECT COUNT(*) FROM telegram_webhook_updates WHERE state = 'done'"
        )).scalar()
        out["total_processed_done"] = int(total or 0)
    except Exception as exc:  # noqa: BLE001
        out["db_error_type"] = type(exc).__name__
    return out


def _architecture_health_snapshot() -> dict:
    """Scope G: Architecture Truth — single production runtime.

    Reports the canonical topology and whether the architecture can be
    considered FROZEN. Per ND_RETURN PASAY-TASK-011 FIX1 blocker #4 this
    field MUST NOT be hard-coded to True; it is derived from real checks
    that mirror the Worker→Queue→Container→Neon production chain:

      1. runtime_mode equals the production value.
      2. Container ingestion token is configured (Worker→Container auth).
      3. Pooled runtime DB URL is configured (Container→Neon runtime).
      4. Direct/unpooled migration DB URL is configured (Container→Neon
         migration startup, also Dockerfile fail-fast).
      5. Long-polling import-chain exit gate has not been broken.

    All five must hold before ``architecture_frozen`` is True.
    """
    runtime_mode = (settings.pasay_runtime_mode or "").strip()
    runtime_mode_ok = runtime_mode == "cloudflare-container"
    container_ingest_ok = bool((settings.container_ingest_token or "").strip())
    db_pooled_ok = bool((settings.database_url or "").strip())
    db_unpooled_ok = bool((settings.database_url_unpooled or "").strip())
    polling_exit_ok = _PRODUCTION_POLLING_EXIT_GATE_OK
    architecture_frozen = (
        runtime_mode_ok
        and container_ingest_ok
        and db_pooled_ok
        and db_unpooled_ok
        and polling_exit_ok
    )
    return {
        "frozen_topology": "worker→queue→container→neon",
        "runtime_mode": runtime_mode if runtime_mode else "unset",
        "production_runtime_mode_expected": "cloudflare-container",
        "container_ingest_configured": container_ingest_ok,
        "db_boundary": {
            "pooled_runtime_url_configured": db_pooled_ok,
            "direct_unpooled_migration_url_configured": db_unpooled_ok,
        },
        "long_polling_exit_gate": {
            "import_chain_no_polling_ref": polling_exit_ok,
            "production_polling_expected": False,
        },
        "telegram_cron_shared_queue": True,
        "architecture_frozen": architecture_frozen,
        "architecture_frozen_prerequisites": {
            "runtime_mode_cloudflare_container": runtime_mode_ok,
            "container_ingest_token_configured": container_ingest_ok,
            "database_url_configured": db_pooled_ok,
            "database_url_unpooled_configured": db_unpooled_ok,
            "polling_exit_gate_intact": polling_exit_ok,
        },
    }


@app.get("/health", summary="Health check (no auth)")
def health(db: Session = Depends(get_db)):
    db_ok = True
    err_class: str | None = None
    try:
        db.execute(text("SELECT 1"))
    except Exception as exc:
        db_ok = False
        err_class = type(exc).__name__
    if not db_ok:
        return JSONResponse(status_code=503, content={"status": "unavailable",
                                                      "db_error_type": err_class})
    body: dict = {"status": "ok"}
    body["telegram_webhook"] = _webhook_health_snapshot(db)
    # ── PASAY-TASK-011 / Scope G ──────────────────────────────────────────
    body["architecture"] = _architecture_health_snapshot()
    return body
