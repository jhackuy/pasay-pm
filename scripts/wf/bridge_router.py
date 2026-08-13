"""BRIDGE-ROUTER-001: minimal deterministic Python task Router.

Program First, LLM Last. Pure stdlib (Python 3.9+), pure functions, no network,
no SSH, no IO, no LLM: the same task input always produces the same RouteResult.

Decision order (deterministic fields only; FAIL CLOSED, never guess):

1. OWNER_APPROVAL_REQUIRED (block first)
   - production_access=true, or constraints contain production_access /
     destructive / manual_approval / owner_approval.
   - Router only blocks and waits for Owner approval; no executor is dispatched.

2. HERMES_THEN_MAX
   - constraints contain architecture_change / rbac_change / financial_logic /
     db_migration, or db_migration=true, or risk=HIGH on a code task.
   - Hermes plans / high-level analysis / review when required; Max still executes.

3. MAX_THEN_FUGUI_ACCEPTANCE
   - real_device_test=true (or constraints contain real_device_test) and not
     high-risk. Executor stays MAX; only acceptance belongs to Fugui.

4. DIRECT_MAX
   - risk LOW or ordinary MEDIUM and type in code/fix/feature/refactor/api/
     bot_ux/test_authoring. No high-risk boundary involved.

5. HERMES_TRIAGE (fail closed)
   - Schema insufficient, unsupported constraint, or fields cannot be routed
     deterministically (e.g. unknown risk / unknown type). Hermes only performs
     semantic triage and returns a structured classification to the Router,
     which then decides the final execution chain.

Minimal task schema: task_id / type / risk / capabilities / constraints /
objective / acceptance. Existing wf_ops field names (task_type / risk_level /
acceptance_criteria) are accepted as aliases where the bridge field is absent.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Tuple

# ---------------------------------------------------------------------------
# Route names
# ---------------------------------------------------------------------------

DIRECT_MAX = "DIRECT_MAX"
HERMES_THEN_MAX = "HERMES_THEN_MAX"
MAX_THEN_FUGUI_ACCEPTANCE = "MAX_THEN_FUGUI_ACCEPTANCE"
OWNER_APPROVAL_REQUIRED = "OWNER_APPROVAL_REQUIRED"
HERMES_TRIAGE = "HERMES_TRIAGE"

ROUTES = frozenset({
    DIRECT_MAX,
    HERMES_THEN_MAX,
    MAX_THEN_FUGUI_ACCEPTANCE,
    OWNER_APPROVAL_REQUIRED,
    HERMES_TRIAGE,
})

# ---------------------------------------------------------------------------
# Minimal task schema
# ---------------------------------------------------------------------------

REQUIRED_SCALAR_FIELDS = ("task_id", "type", "risk", "objective", "acceptance")
LIST_FIELDS = ("capabilities", "constraints")

# Bridge field -> existing wf_ops field alias (schema reuse, minimal additions).
FIELD_ALIASES = {
    "type": "task_type",
    "risk": "risk_level",
    "acceptance": "acceptance_criteria",
}

SUPPORTED_CONSTRAINTS = frozenset({
    "db_migration",
    "production_access",
    "financial_logic",
    "rbac_change",
    "architecture_change",
    "real_device_test",
    "destructive",
    "manual_approval",
    "owner_approval",
})

# Constraints that force OWNER_APPROVAL_REQUIRED (block first).
APPROVAL_CONSTRAINTS = frozenset({
    "production_access",
    "destructive",
    "manual_approval",
    "owner_approval",
})

# Constraints that force HERMES_THEN_MAX (high-risk engineering).
HIGH_RISK_CONSTRAINTS = frozenset({
    "architecture_change",
    "rbac_change",
    "financial_logic",
    "db_migration",
})

CODE_TYPES = frozenset({
    "code",
    "fix",
    "feature",
    "refactor",
    "api",
    "bot_ux",
    "test_authoring",
})

DIRECT_MAX_RISKS = frozenset({"LOW", "MEDIUM"})


@dataclass(frozen=True)
class RouteResult:
    """Structured routing decision. Never a single-agent shortcut."""

    route: str
    planner: str
    executor: str
    reviewer: str
    acceptance: str
    approval: str
    reason_code: str
    reason: str

    def as_dict(self) -> Dict[str, str]:
        return {
            "route": self.route,
            "planner": self.planner,
            "executor": self.executor,
            "reviewer": self.reviewer,
            "acceptance": self.acceptance,
            "approval": self.approval,
            "reason_code": self.reason_code,
            "reason": self.reason,
        }


def _field(task: Dict[str, Any], name: str) -> Any:
    """Read a bridge-schema field, falling back to the existing wf_ops alias."""
    value = task.get(name)
    if value in (None, "", [], {}):
        alias = FIELD_ALIASES.get(name)
        if alias:
            value = task.get(alias)
    return value


def _constraints(task: Dict[str, Any]) -> List[str]:
    """Normalized, deduplicated constraint list (strings only)."""
    raw = task.get("constraints")
    if not isinstance(raw, list):
        return []
    out: List[str] = []
    for item in raw:
        if isinstance(item, str) and item.strip() and item.strip() not in out:
            out.append(item.strip())
    return out


def validate_task_schema(task: Any) -> Tuple[bool, List[str]]:
    """Minimal schema validation -> (ok, errors). FAIL CLOSED: insufficient
    fields are errors; the router never guesses a route from partial input.
    """
    errors: List[str] = []
    if not isinstance(task, dict):
        return False, ["task must be a dict"]
    for name in REQUIRED_SCALAR_FIELDS:
        value = _field(task, name)
        if value in (None, "", []):
            errors.append("missing field: %s" % name)
        elif name == "acceptance":
            if not isinstance(value, (str, list)):
                errors.append("invalid field type: %s" % name)
        elif not isinstance(value, str):
            errors.append("invalid field type: %s" % name)
    for name in LIST_FIELDS:
        value = task.get(name)
        if value is None:
            errors.append("missing field: %s" % name)
        elif not isinstance(value, list):
            errors.append("invalid field type: %s" % name)
        else:
            bad = [i for i, item in enumerate(value)
                   if not isinstance(item, str) or not item.strip()]
            if bad:
                errors.append("invalid entries in %s: %s" % (name, bad))
    return (not errors), errors


def _result(route, planner, executor, reviewer, acceptance, approval,
            reason_code, reason) -> RouteResult:
    return RouteResult(
        route=route,
        planner=planner,
        executor=executor,
        reviewer=reviewer,
        acceptance=acceptance,
        approval=approval,
        reason_code=reason_code,
        reason=reason,
    )


def route_task(task: Any) -> RouteResult:
    """Deterministic task routing -> RouteResult (one of the 5 supported routes).

    Pure function: decisions are computed from task fields only; no LLM, no IO,
    no network, no SSH. Identical input always yields an identical RouteResult.
    """
    ok, errors = validate_task_schema(task)
    if not ok:
        return _result(
            HERMES_TRIAGE, "HERMES", "ROUTER", "none", "ROUTER_REROUTE", "AUTO",
            "invalid_task_schema",
            "task schema validation failed; FAIL CLOSED: " + "; ".join(errors),
        )

    ttype = str(_field(task, "type") or "").strip().lower()
    risk = str(_field(task, "risk") or "").strip().upper()
    constraints = _constraints(task)

    unknown = sorted(set(constraints) - SUPPORTED_CONSTRAINTS)
    if unknown:
        return _result(
            HERMES_TRIAGE, "HERMES", "ROUTER", "none", "ROUTER_REROUTE", "AUTO",
            "unsupported_constraint",
            "unsupported constraints: %s; cannot route deterministically"
            % ", ".join(unknown),
        )

    # 1. Block first: Owner approval required (production / destructive /
    #    manual approval actions). Router blocks and waits; no executor runs.
    if task.get("production_access") is True or any(
            c in APPROVAL_CONSTRAINTS for c in constraints):
        return _result(
            OWNER_APPROVAL_REQUIRED, "none", "none", "none",
            "OWNER_APPROVAL", "OWNER", "owner_approval_required",
            "production/manual-approval action; blocked until Owner approval",
        )

    # 2. High-risk engineering: architecture / RBAC / finance / DB migration,
    #    or an explicitly HIGH-risk code task.
    high_risk = any(c in HIGH_RISK_CONSTRAINTS for c in constraints) or \
        task.get("db_migration") is True
    if high_risk or (risk == "HIGH" and ttype in CODE_TYPES):
        return _result(
            HERMES_THEN_MAX, "HERMES", "MAX", "HERMES when required",
            "automated tests", "AUTO", "hermes_then_max",
            "high-risk engineering task; Hermes plans, Max executes",
        )

    # 3. Real device / Windows / Telegram UX acceptance only. Fugui accepts;
    #    MAX remains the code executor (never FUGUI).
    if "real_device_test" in constraints or task.get("real_device_test") is True:
        return _result(
            MAX_THEN_FUGUI_ACCEPTANCE, "none", "MAX", "none",
            "FUGUI", "AUTO", "max_then_fugui_acceptance",
            "real_device_test=true; MAX executes code, Fugui only accepts "
            "Windows/Telegram UX",
        )

    # 4. Ordinary code task: risk LOW / normal MEDIUM.
    if risk in DIRECT_MAX_RISKS and ttype in CODE_TYPES:
        return _result(
            DIRECT_MAX, "none", "MAX", "none",
            "automated tests", "AUTO", "direct_max",
            "ordinary code task (risk=%s, type=%s); single deterministic Max "
            "session" % (risk, ttype),
        )

    # 5. Cannot route from deterministic fields -> Hermes semantic triage only;
    #    the Router decides the final execution chain afterwards.
    return _result(
        HERMES_TRIAGE, "HERMES", "ROUTER", "none", "ROUTER_REROUTE", "AUTO",
        "hermes_triage",
        "cannot route from deterministic fields (risk=%s, type=%s); Hermes "
        "classifies, Router re-decides" % (risk, ttype),
    )


if __name__ == "__main__":  # pragma: no cover - CLI smoke entry only
    import json
    import sys

    if len(sys.argv) != 2:
        sys.stderr.write("usage: python bridge_router.py '<task-json>'\n")
        sys.exit(2)
    try:
        task = json.loads(sys.argv[1])
    except json.JSONDecodeError as exc:
        sys.stderr.write("invalid task json: %s\n" % exc)
        sys.exit(2)
    print(json.dumps(route_task(task).as_dict(), ensure_ascii=False, indent=2))
