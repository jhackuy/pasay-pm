from __future__ import annotations

from typing import Any

from sqlalchemy import text

from app.infrastructure.postgres import build_postgres_runtime_boundary


def build_health_payload(*, settings, engine, include_db_connectivity: bool = True) -> tuple[int, dict[str, Any]]:
    boundary = build_postgres_runtime_boundary(settings)
    payload: dict[str, Any] = {
        "status": "ok",
        "runtime": {
            "platform": "fastapi",
            "alive": True,
        },
        "application": {
            "boot": "ok",
        },
        "database": {
            "configured": boundary.config_available,
            "provider": boundary.provider,
            "application_connection_mode": boundary.application_connection_mode,
            "migration_connection_mode": boundary.migration_connection_mode,
            "connectivity_checked": include_db_connectivity,
            "reachable": None,
        },
    }

    if not include_db_connectivity:
        return 200, payload

    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception:
        payload["status"] = "unavailable"
        payload["database"]["reachable"] = False
        return 503, payload

    payload["database"]["reachable"] = True
    return 200, payload
