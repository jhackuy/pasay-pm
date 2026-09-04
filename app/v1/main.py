"""FastAPI app factory for the V1 rewrite.

Mounts thin routers under `/api/v1`. Bootstrap is dev/test only.
The legacy `app/main.py` is untouched.

Optional Mini App static serving (Issue #99 / Spec Kit T-066):
  When `PASAY_MINIAPP_DIST` points to a built `mini_app/dist/` directory,
  `mount_miniapp(app)` mounts the static assets under `/assets/` and
  registers a single-file fallback that returns `index.html` for any
  non-API path (the Mini App uses hash routing, so this only kicks in for
  refresh of root paths). No business truth ever lives in the static
  assets; they are the compiled Vite bundle.
"""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.v1.api.audit import router as audit_router
from app.v1.api.bootstrap import router as bootstrap_router
from app.v1.api.dashboard import router as dashboard_router
from app.v1.api.expenses import router as expenses_router
from app.v1.api.leases import router as leases_router
from app.v1.api.move_outs import router as move_outs_router
from app.v1.api.operations import router as operations_router
from app.v1.api.properties import router as properties_router
from app.v1.api.rent_payments import router as rent_payments_router
from app.v1.api.renewals import router as renewals_router
from app.v1.api.repairs import router as repairs_router
from app.v1.api.tenants import router as tenants_router
from app.v1.api.webapp_auth import router as webapp_auth_router
from app.v1.api.workspaces import router as workspaces_router


def mount_miniapp(app: FastAPI, dist_dir: Path | None = None) -> bool:
    """Mount the Mini App `dist/` under `/` with SPA fallback to `index.html`.

    Returns True if the mount succeeded, False if `dist_dir` does not exist
    or contains no `index.html`. Never raises — the API surface stays
    usable for the rewrite even when the static bundle is unavailable.
    """
    if dist_dir is None:
        env = os.environ.get("PASAY_MINIAPP_DIST")
        if not env:
            return False
        dist_dir = Path(env)
    if not dist_dir.is_dir():
        return False
    index_html = dist_dir / "index.html"
    if not index_html.is_file():
        return False
    assets_dir = dist_dir / "assets"
    if assets_dir.is_dir():
        app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="miniapp-assets")

    @app.get("/", include_in_schema=False)
    @app.get("/index.html", include_in_schema=False)
    async def _serve_index() -> FileResponse:
        return FileResponse(str(index_html))

    @app.get("/{full_path:path}", include_in_schema=False)
    async def _serve_spa(full_path: str, request: Request) -> FileResponse:
        # Any request that didn't match an API route falls through to the
        # Mini App shell (hash routing on the client takes over after that).
        candidate = dist_dir / full_path
        if (
            full_path
            and not full_path.startswith("api/")
            and not full_path.startswith("assets/")
            and candidate.is_file()
        ):
            return FileResponse(str(candidate))
        return FileResponse(str(index_html))

    return True


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
    app.include_router(renewals_router, prefix="/api/v1")
    app.include_router(move_outs_router, prefix="/api/v1")
    app.include_router(operations_router, prefix="/api/v1")
    app.include_router(dashboard_router, prefix="/api/v1")
    app.include_router(audit_router, prefix="/api/v1")
    # Issue #119 Mini App — exchange signed Telegram initData for a
    # bearer session.  Owner-only by policy (see webapp_auth.py docstring).
    app.include_router(webapp_auth_router, prefix="/api/v1")

    # Optional Mini App static mount (used by the Playwright browser smoke
    # and by container deployments that serve the SPA from the API host).
    mount_miniapp(app)
    return app


app = create_v1_app()
