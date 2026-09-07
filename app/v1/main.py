"""FastAPI app factory for the V1 rewrite.

This module is the **production FastAPI entrypoint** in the Cloudflare
Container. The Dockerfile CMD (``uvicorn app.v1.main:app``) runs this
app. The legacy ``app/main.py`` is no longer the production entrypoint
but is kept importable for the legacy test harness; the legacy
routers are now mounted INSIDE this V1 app under their original top-level
paths so the Cloudflare Worker / queue / container production surfaces
(``/telegram/webhook``, ``/internal/ingest``, ``/health``) keep working
without duplicating the PTB ingest / Worker integration code.

Mounts:

  * thin V1 routers under ``/api/v1`` (Issue #99 / PR #100)
  * V1 SYSTEM scheduled-job endpoints under ``/api/v1/operations``
    (``/operations/digest`` + ``/operations/quick/tasks``) — Issue #119
    P0 fix, registered BEFORE the HUMAN ``operations`` router so the
    literal paths take precedence over the ``/{operation_id}`` path
    parameter
  * legacy ``/telegram/webhook`` (PTB ingest, top-level so Telegram can
    deliver directly; auth via ``X-Telegram-Bot-Api-Secret-Token``)
  * legacy ``/internal/ingest`` (Cloudflare Container queue delivery;
    not public-internet-routable by design)
  * legacy ``/health`` snapshot endpoint (the watchdog probes this
    surface and expects the architecture / telegram_webhook
    sub-snapshots the legacy implementation exposes)

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
from app.v1.api.system_ops import router as system_ops_router
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
            "(Issue #99, PR #100). "
            "Production entrypoint per Issue #119 P0 fix."
        ),
    )

    # ── Top-level production runtime surfaces ─────────────────────────
    # Mount the legacy /telegram/webhook + /internal/ingest routers so
    # the Worker → Container queue contract stays unchanged. The
    # legacy code paths use the legacy ``users`` / ``principals`` /
    # ``api_credentials`` tables that DO NOT exist in the production
    # ``v1_*`` schema; that is fine for /telegram/webhook and
    # /internal/ingest which only need to enqueue / dispatch a Telegram
    # update and never authenticate against the legacy tables
    # (X-Telegram-Bot-Api-Secret-Token + PASAY_CONTAINER_INGEST_TOKEN
    # are the only auth checks those endpoints perform).
    try:
        from app.api.routers.telegram_webhook import router as telegram_webhook_router
        from app.api.routers.internal_ingest import router as internal_ingest_router
        # The /health snapshot helpers live in the legacy app.main
        # module (Issue #135's /health architecture snapshot). Import
        # them from there so the watchdog contract is preserved
        # byte-for-byte after the entrypoint switch.
        from app.main import _webhook_health_snapshot, _architecture_health_snapshot
    except Exception:  # noqa: BLE001 - keep V1 tests runnable in isolation
        telegram_webhook_router = None
        internal_ingest_router = None
        _webhook_health_snapshot = None
        _architecture_health_snapshot = None

    if telegram_webhook_router is not None:
        # Telegram delivers to the public hostname; path is top-level
        # so the secret-token + JSON parse + enqueue flow stays
        # byte-identical to the legacy production wiring.
        app.include_router(telegram_webhook_router)
    if internal_ingest_router is not None:
        # Container queue delivery; top-level path; not public-routable
        # by design (Cloudflare Container does not expose /internal/*).
        app.include_router(internal_ingest_router)

    if (
        _webhook_health_snapshot is not None
        and _architecture_health_snapshot is not None
    ):
        from fastapi import Depends
        from fastapi.responses import JSONResponse
        from sqlalchemy import text
        from sqlalchemy.orm import Session

        from app.config import settings  # type: ignore[import-not-found]
        from app.database import get_db  # type: ignore[import-not-found]

        @app.get("/health", summary="Health check (no auth)")
        def health(db: Session = Depends(get_db)) -> JSONResponse:
            """Legacy-compatible /health surface for the watchdog.

            Returns the same fields the legacy ``app/main.py::health``
            exposed (``status`` / ``telegram_webhook`` /
            ``architecture``) so the existing watchdog contract is
            preserved when the production entrypoint switches from
            ``app.main:app`` to ``app.v1.main:app``.
            """
            db_ok = True
            err_class: str | None = None
            try:
                db.execute(text("SELECT 1"))
            except Exception as exc:  # noqa: BLE001
                db_ok = False
                err_class = type(exc).__name__
            if not db_ok:
                return JSONResponse(
                    status_code=503,
                    content={"status": "unavailable", "db_error_type": err_class},
                )
            body: dict = {"status": "ok", "version": "1.0.0"}
            try:
                body["telegram_webhook"] = _webhook_health_snapshot(db)
            except Exception:  # noqa: BLE001
                body["telegram_webhook"] = {"error": "snapshot_unavailable"}
            try:
                body["architecture"] = _architecture_health_snapshot()
            except Exception:  # noqa: BLE001
                body["architecture"] = {"error": "snapshot_unavailable"}
            return JSONResponse(status_code=200, content=body)

    # ── /api/v1 V1 surface ────────────────────────────────────────────
    # SYSTEM scheduled-job endpoints MUST be registered BEFORE the
    # HUMAN ``operations`` router so the literal paths
    # (``/operations/digest`` and ``/operations/quick/tasks``) take
    # precedence over the ``/{operation_id}`` path parameter; otherwise
    # the legacy ``/operations/{operation_id}`` pattern would match
    # ``/operations/digest`` first and FastAPI would 422-parse
    # ``operation_id="digest"`` as an int.
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
    # Issue #119 P0: SYSTEM endpoints FIRST so the literal paths
    # ``/operations/digest`` and ``/operations/quick/tasks`` win over
    # the ``/{operation_id}`` parameter pattern below.
    app.include_router(system_ops_router, prefix="/api/v1")
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
