"""FastAPI app factory for the V1 rewrite.

Mounts thin routers under `/api/v1`. Bootstrap is dev/test only.
The legacy `app/main.py` is untouched.
"""
from __future__ import annotations

from fastapi import FastAPI

from app.v1.api.bootstrap import router as bootstrap_router
from app.v1.api.expenses import router as expenses_router
from app.v1.api.leases import router as leases_router
from app.v1.api.properties import router as properties_router
from app.v1.api.rent_payments import router as rent_payments_router
from app.v1.api.repairs import router as repairs_router
from app.v1.api.tenants import router as tenants_router
from app.v1.api.workspaces import router as workspaces_router


def create_v1_app() -> FastAPI:
    app = FastAPI(
        title="PASAY V1 API",
        version="1.0.0",
        description=(
            "Clean rewrite of PASAY property-management API "
            "(Issue #99, PR #100)."
        ),
    )

    @app.get("/health", tags=["health"])
    def health() -> dict[str, str]:
        return {"status": "ok", "version": "1.0.0"}

    app.include_router(bootstrap_router, prefix="/api/v1")
    app.include_router(workspaces_router, prefix="/api/v1")
    app.include_router(properties_router, prefix="/api/v1")
    app.include_router(tenants_router, prefix="/api/v1")
    app.include_router(leases_router, prefix="/api/v1")
    app.include_router(rent_payments_router, prefix="/api/v1")
    app.include_router(expenses_router, prefix="/api/v1")
    app.include_router(repairs_router, prefix="/api/v1")
    return app


app = create_v1_app()
