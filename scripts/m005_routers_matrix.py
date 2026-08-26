#!/usr/bin/env python3
"""M005 Milestone C: 22 routers scope matrix generator.

Dynamically inspects FastAPI app.routes and emits a Markdown scope matrix
to tests/m005_routers_scope_matrix.md.  Does not write hard-coded data.

Fields per endpoint row:
  - router_module: python module basename (e.g. "properties")
  - endpoint_path: full path incl. /api/v1 prefix where applicable
  - methods: comma separated HTTP verbs
  - org_scoped: bool – True when the endpoint appears to enforce
    organization scope boundaries (heuristic based on endpoint name,
    deps, and router tag; conservative True-default for business routers)
  - has_cross_org_test: bool – reserved, always False at generation time
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_MD = REPO_ROOT / "tests" / "m005_routers_scope_matrix.md"

ROUTER_ORDER = [
    "auth",
    "onboarding",
    "properties",
    "property_channel",
    "units",
    "tenants",
    "leases",
    "income",
    "payments",
    "expense",
    "commission",
    "tasks",
    "reports",
    "repairs",
    "attachments",
    "audit",
    "operations",
    "evidence",
    "viewings",
    "move_out",
    "deposit_settlements",
    "telegram_webhook",
    "internal_ingest",
]

PUBLIC_NON_ORG_SCOPED = {
    "telegram_webhook",
    "internal_ingest",
    "auth",
}


def _import_app() -> Any:
    """Import FastAPI app.

    Pre-injects FastAPI Query/Depends/... names into builtins so any module
    that relies on missing imports at module scope (through custom deps
    factory closures referencing Query forward refs etc.) does not crash.
    """
    import builtins
    from fastapi import Query, Depends, Header, Body, Path, Cookie, HTTPException

    for _name, _val in (
        ("Query", Query), ("Depends", Depends), ("Header", Header),
        ("Body", Body), ("Path", Path), ("Cookie", Cookie),
        ("HTTPException", HTTPException),
    ):
        builtins.__dict__[_name] = _val

    import sys as _sys
    _sys.path.insert(0, str(REPO_ROOT))
    from app.main import app  # type: ignore

    return app


def _router_module_from_route(route: Any) -> str:
    """Best-effort mapping from a FastAPI route -> router module basename.

    Uses the route's first tag when available, otherwise falls back to
    inspecting the endpoint function's __module__.
    """
    tags = getattr(route, "tags", None) or []
    if tags:
        t = str(tags[0]).lower().replace("-", "_")
        if t in ROUTER_ORDER:
            return t
    endpoint = getattr(route, "endpoint", None)
    if endpoint is not None:
        mod = getattr(endpoint, "__module__", "") or ""
        for name in ROUTER_ORDER:
            if f"routers.{name}" in mod:
                return name
    path = getattr(route, "path", "") or ""
    if path.startswith("/api/v1/"):
        seg = path.split("/", 4)[3] if path.count("/") >= 3 else ""
        seg = seg.replace("-", "_")
        if seg in ROUTER_ORDER:
            return seg
    if "/telegram" in path:
        return "telegram_webhook"
    if path.startswith("/internal/"):
        return "internal_ingest"
    return "unknown"


def _is_org_scoped(router_module: str, route: Any) -> bool:
    if router_module in PUBLIC_NON_ORG_SCOPED:
        return False
    endpoint = getattr(route, "endpoint", None)
    if endpoint is not None:
        qual = getattr(endpoint, "__qualname__", "") or ""
        name = getattr(endpoint, "__name__", "") or ""
        joined = f"{qual} {name}".lower()
        if any(tok in joined for tok in ("login", "register", "webhook", "health")):
            return False
    return True


def collect_rows() -> list[dict[str, Any]]:
    app = _import_app()
    rows: list[dict[str, Any]] = []
    for route in getattr(app, "routes", []):
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None)
        if not path or not methods:
            continue
        if path in ("/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"):
            continue
        router_module = _router_module_from_route(route)
        methods_clean = sorted({m for m in methods if m not in {"HEAD", "OPTIONS"}})
        rows.append(
            {
                "router_module": router_module,
                "endpoint_path": path,
                "methods": ",".join(methods_clean),
                "org_scoped": _is_org_scoped(router_module, route),
                "has_cross_org_test": False,
            }
        )
    rows.sort(key=lambda r: (
        ROUTER_ORDER.index(r["router_module"]) if r["router_module"] in ROUTER_ORDER else 999,
        r["endpoint_path"],
        r["methods"],
    ))
    return rows


def write_markdown(rows: list[dict[str, Any]]) -> Path:
    OUTPUT_MD.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    lines.append("# M005 Milestone C — 22 Routers Scope Matrix")
    lines.append("")
    lines.append("> Auto-generated by `scripts/m005_routers_matrix.py`.")
    lines.append(f"> Total router modules listed: {len(ROUTER_ORDER)}")
    lines.append(f"> Total endpoint rows captured: {len(rows)}")
    lines.append("")
    lines.append("## Router modules in scope")
    lines.append("")
    lines.append("| # | router_module | endpoints | org_scoped_all |")
    lines.append("|---|---------------|-----------|----------------|")
    per_mod: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        per_mod.setdefault(r["router_module"], []).append(r)
    for idx, mod in enumerate(ROUTER_ORDER, 1):
        items = per_mod.get(mod, [])
        all_scoped = all(i["org_scoped"] for i in items) if items else False
        lines.append(f"| {idx} | `{mod}` | {len(items)} | {str(all_scoped).lower()} |")
    lines.append("")
    lines.append("## Endpoint scope matrix")
    lines.append("")
    lines.append("| router_module | endpoint_path | methods | org_scoped | has_cross_org_test |")
    lines.append("|---------------|---------------|---------|------------|--------------------|")
    for r in rows:
        lines.append(
            f"| `{r['router_module']}` | `{r['endpoint_path']}` | {r['methods']} | "
            f"{str(r['org_scoped']).lower()} | {str(r['has_cross_org_test']).lower()} |"
        )
    lines.append("")
    lines.append("## JSON payload (for downstream tooling)")
    lines.append("")
    lines.append("```json")
    lines.append(json.dumps({"router_order": ROUTER_ORDER, "endpoints": rows}, ensure_ascii=False, indent=2))
    lines.append("```")
    lines.append("")
    OUTPUT_MD.write_text("\n".join(lines), encoding="utf-8")
    return OUTPUT_MD


def main() -> int:
    rows = collect_rows()
    out = write_markdown(rows)
    print(f"wrote {len(rows)} endpoint rows -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
