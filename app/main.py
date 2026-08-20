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
    leases,
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
    return body
