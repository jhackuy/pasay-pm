from pathlib import Path

from dotenv import load_dotenv

# Export .env keys to os.environ so BOTH pydantic Settings (config.py) and any
# raw os.getenv consumers (e.g. the C1 copilot llm.py provider_config) see them.
# The app runs with CWD=/opt/pasay-pm; fall back to the file's own dir otherwise.
load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=False)

from fastapi import FastAPI
from fastapi.responses import JSONResponse

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
    units,
    viewings,
)
from app.database import engine
from app.config import settings
from app.runtime.health import build_health_payload

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


@app.get("/health", summary="Health check (no auth)")
def health():
    status_code, payload = build_health_payload(settings=settings, engine=engine)
    if status_code != 200:
        return JSONResponse(status_code=status_code, content=payload)
    return payload
