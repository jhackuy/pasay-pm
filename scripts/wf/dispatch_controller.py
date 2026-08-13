"""BRIDGE-ROUTER-002: deterministic Router dispatch controller.

Program First, LLM Last. Pure stdlib (Python 3.9+), pure functions, no IO, no
network, no SSH, no LLM: a RouteResult maps to the same execution plan every
time, and plan execution only happens through injected hermes / max / approval
functions (fakes in tests, real subprocess wrappers in the runner).

Plan mapping (RouteResult.route -> stages, each stage has a call limit and a
fixed order):

  DIRECT_MAX                 -> [MAX]
  HERMES_THEN_MAX            -> [HERMES_PLAN, MAX]
  MAX_THEN_FUGUI_ACCEPTANCE  -> [MAX]                (acceptance_target=FUGUI)
  OWNER_APPROVAL_REQUIRED    -> [] + MANUAL_APPROVAL_REQUIRED block
  HERMES_TRIAGE              -> [HERMES_TRIAGE, REROUTE] + final route stages

Hard constraints enforced here:
  * one execution chain per task: HERMES_PLAN, HERMES_TRIAGE and MAX each run
    at most once per execute_plan call (per-stage max_calls + role counters);
  * OWNER_APPROVAL_REQUIRED starts Hermes/Max exactly 0 times;
  * Hermes triage may only influence type/risk/constraints - never executor,
    route or acceptance (classification sanitized before rerouting);
  * a reroute that stays HERMES_TRIAGE fails closed with
    FAILED_TRIAGE_NO_PROGRESS.

Standalone: python scripts/wf/dispatch_controller.py  (smoke assertions only)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

import bridge_router as br

# Stage names.
HERMES_PLAN = "HERMES_PLAN"
HERMES_TRIAGE = "HERMES_TRIAGE"
MAX = "MAX"
REROUTE = "REROUTE"

# Status values.
SUCCESS = "SUCCESS"
MANUAL_APPROVAL_REQUIRED = "MANUAL_APPROVAL_REQUIRED"
FAILED_TRIAGE_NO_PROGRESS = "FAILED_TRIAGE_NO_PROGRESS"
DISPATCH_LIMIT_VIOLATION = "DISPATCH_LIMIT_VIOLATION"

# Hermes triage may only influence these bridge fields. Anything else the LLM
# emits (executor / planner / acceptance / route ...) is deliberately ignored.
TRIAGE_KEYS = ("type", "risk", "constraints")


@dataclass(frozen=True)
class Stage:
    """One execution step: a role plus its per-call limit (default 1)."""

    name: str
    max_calls: int = 1

    def __post_init__(self) -> None:
        if self.name not in (HERMES_PLAN, HERMES_TRIAGE, MAX, REROUTE):
            raise ValueError("unsupported stage: %s" % self.name)
        if self.max_calls < 0:
            raise ValueError("max_calls must be >= 0")


@dataclass(frozen=True)
class DispatchPlan:
    """Deterministic execution plan derived from a RouteResult."""

    route: str
    stages: Tuple[Stage, ...]
    acceptance_target: str
    status: Optional[str] = None  # MANUAL_APPROVAL_REQUIRED when approval blocks

    def stage_names(self) -> List[str]:
        return [s.name for s in self.stages]

    def as_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "route": self.route,
            "stages": self.stage_names(),
            "acceptance_target": self.acceptance_target,
        }
        if self.status:
            out["status"] = self.status
        return out


@dataclass(frozen=True)
class ExecutionReport:
    """Call counts and outcome of one deterministic execution pass."""

    plan: DispatchPlan
    status: str
    hermes_plan_calls: int
    hermes_triage_calls: int
    max_calls: int
    approval_called: bool
    final_route: Optional[str]
    acceptance_target: str
    stages_executed: Tuple[str, ...]

    @property
    def hermes_started(self) -> int:
        """Total Hermes invocations (planner + triage; each role <= 1)."""
        return self.hermes_plan_calls + self.hermes_triage_calls

    @property
    def max_started(self) -> int:
        return self.max_calls

    def as_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "status": self.status,
            "route": self.plan.route,
            "hermes_started": self.hermes_started,
            "max_started": self.max_started,
            "hermes_plan_calls": self.hermes_plan_calls,
            "hermes_triage_calls": self.hermes_triage_calls,
            "max_calls": self.max_calls,
            "approval_called": self.approval_called,
            "acceptance_target": self.acceptance_target,
            "stages_executed": list(self.stages_executed),
        }
        if self.final_route is not None:
            out["final_route"] = self.final_route
        return out


def plan_for_route(route_result: br.RouteResult) -> DispatchPlan:
    """Map a RouteResult to a deterministic DispatchPlan (pure)."""
    if not isinstance(route_result, br.RouteResult):
        raise TypeError("plan_for_route expects a RouteResult")
    route = route_result.route
    if route == br.DIRECT_MAX:
        stages = (Stage(MAX),)
        status = None
    elif route == br.HERMES_THEN_MAX:
        stages = (Stage(HERMES_PLAN), Stage(MAX))
        status = None
    elif route == br.MAX_THEN_FUGUI_ACCEPTANCE:
        stages = (Stage(MAX),)
        status = None
    elif route == br.OWNER_APPROVAL_REQUIRED:
        stages = ()
        status = MANUAL_APPROVAL_REQUIRED
    elif route == br.HERMES_TRIAGE:
        stages = (Stage(HERMES_TRIAGE), Stage(REROUTE))
        status = None
    else:
        raise ValueError("unsupported route: %s" % route)
    return DispatchPlan(
        route=route,
        stages=stages,
        acceptance_target=route_result.acceptance,
        status=status,
    )


def planned_counts(plan: DispatchPlan, final_plan: Optional[DispatchPlan] = None) -> Dict[str, int]:
    """Deterministic planned start counts (no LLM involved)."""
    hermes = sum(1 for s in plan.stages if s.name in (HERMES_PLAN, HERMES_TRIAGE))
    max_calls = sum(1 for s in plan.stages if s.name == MAX)
    if final_plan is not None:
        hermes += sum(1 for s in final_plan.stages if s.name in (HERMES_PLAN, HERMES_TRIAGE))
        max_calls += sum(1 for s in final_plan.stages if s.name == MAX)
    return {"hermes_started": hermes, "max_started": max_calls}


def sanitize_classification(classification: Any) -> Dict[str, Any]:
    """Keep only type/risk/constraints from a Hermes triage classification.

    Hermes may never specify an executor, planner, route or acceptance target;
    any such field is stripped before the Router re-decides.
    """
    if not isinstance(classification, dict):
        return {}
    out: Dict[str, Any] = {}
    for key in TRIAGE_KEYS:
        value = classification.get(key)
        if value in (None, "", []):
            continue
        out[key] = value
    return out


def apply_classification(task: Dict[str, Any], classification: Any) -> Dict[str, Any]:
    """Merge a sanitized Hermes classification into the bridge task (pure)."""
    merged = dict(task)
    for key, value in sanitize_classification(classification).items():
        merged[key] = value
    return merged


def execute_plan(
    plan: DispatchPlan,
    *,
    hermes_plan: Optional[Callable[[], Any]] = None,
    hermes_triage: Optional[Callable[[], Any]] = None,
    max_exec: Optional[Callable[[], Any]] = None,
    approval: Optional[Callable[[DispatchPlan], Any]] = None,
    reroute: Optional[Callable[[Dict[str, Any]], br.RouteResult]] = None,
) -> ExecutionReport:
    """Execute a plan through injected functions; returns a call-count report.

    Contract:
      * hermes_plan()  - starts the Hermes planner (returns anything; ignored).
      * hermes_triage()- starts Hermes triage; MUST return a raw classification
                         dict (only type/risk/constraints are honored).
      * max_exec()     - starts the Max executor (returns anything; ignored).
      * approval(plan) - invoked only when the plan blocks on Owner approval.
      * reroute(cls)   - returns the final RouteResult after triage.

    Pure orchestration: this function never touches the network, filesystem or
    an LLM itself; side effects only happen inside the injected callables.
    """
    if not isinstance(plan, DispatchPlan):
        raise TypeError("execute_plan expects a DispatchPlan")

    counts = {HERMES_PLAN: 0, HERMES_TRIAGE: 0, MAX: 0}
    executed: List[str] = []
    final_route: Optional[str] = plan.route
    acceptance_target = plan.acceptance_target
    approval_called = False

    if plan.status == MANUAL_APPROVAL_REQUIRED:
        if approval is not None:
            approval(plan)
            approval_called = True
        return ExecutionReport(
            plan=plan,
            status=MANUAL_APPROVAL_REQUIRED,
            hermes_plan_calls=0,
            hermes_triage_calls=0,
            max_calls=0,
            approval_called=approval_called,
            final_route=None,
            acceptance_target=acceptance_target,
            stages_executed=(),
        )

    def run(stage: Stage, role: str) -> bool:
        """Run one stage; returns False on a dispatch-limit violation."""
        if counts[role] >= stage.max_calls:
            return False
        if role == HERMES_PLAN:
            if hermes_plan is None:
                raise ValueError("hermes_plan function required for HERMES_PLAN")
            hermes_plan()
        elif role == MAX:
            if max_exec is None:
                raise ValueError("max_exec function required for MAX")
            max_exec()
        counts[role] += 1
        executed.append(role)
        return True

    classification: Dict[str, Any] = {}
    for stage in plan.stages:
        if stage.name == HERMES_TRIAGE:
            if hermes_triage is None:
                raise ValueError("hermes_triage function required for HERMES_TRIAGE")
            classification = hermes_triage() or {}
            counts[HERMES_TRIAGE] += 1
            executed.append(HERMES_TRIAGE)
        elif stage.name == REROUTE:
            if reroute is None:
                raise ValueError("reroute function required for REROUTE")
            final_result = reroute(classification)
            final_route = final_result.route
            acceptance_target = final_result.acceptance
            executed.append(REROUTE)
            if final_route == br.HERMES_TRIAGE:
                return ExecutionReport(
                    plan=plan,
                    status=FAILED_TRIAGE_NO_PROGRESS,
                    hermes_plan_calls=counts[HERMES_PLAN],
                    hermes_triage_calls=counts[HERMES_TRIAGE],
                    max_calls=counts[MAX],
                    approval_called=False,
                    final_route=final_route,
                    acceptance_target=acceptance_target,
                    stages_executed=tuple(executed),
                )
            if final_route == br.OWNER_APPROVAL_REQUIRED:
                if approval is not None:
                    approval(plan)
                    approval_called = True
                return ExecutionReport(
                    plan=plan,
                    status=MANUAL_APPROVAL_REQUIRED,
                    hermes_plan_calls=counts[HERMES_PLAN],
                    hermes_triage_calls=counts[HERMES_TRIAGE],
                    max_calls=counts[MAX],
                    approval_called=approval_called,
                    final_route=final_route,
                    acceptance_target=acceptance_target,
                    stages_executed=tuple(executed),
                )
            for final_stage in plan_for_route(final_result).stages:
                if not run(final_stage, final_stage.name):
                    return ExecutionReport(
                        plan=plan,
                        status=DISPATCH_LIMIT_VIOLATION,
                        hermes_plan_calls=counts[HERMES_PLAN],
                        hermes_triage_calls=counts[HERMES_TRIAGE],
                        max_calls=counts[MAX],
                        approval_called=False,
                        final_route=final_route,
                        acceptance_target=acceptance_target,
                        stages_executed=tuple(executed),
                    )
        else:
            if not run(stage, stage.name):
                return ExecutionReport(
                    plan=plan,
                    status=DISPATCH_LIMIT_VIOLATION,
                    hermes_plan_calls=counts[HERMES_PLAN],
                    hermes_triage_calls=counts[HERMES_TRIAGE],
                    max_calls=counts[MAX],
                    approval_called=False,
                    final_route=final_route,
                    acceptance_target=acceptance_target,
                    stages_executed=tuple(executed),
                )

    return ExecutionReport(
        plan=plan,
        status=SUCCESS,
        hermes_plan_calls=counts[HERMES_PLAN],
        hermes_triage_calls=counts[HERMES_TRIAGE],
        max_calls=counts[MAX],
        approval_called=approval_called,
        final_route=final_route,
        acceptance_target=acceptance_target,
        stages_executed=tuple(executed),
    )


if __name__ == "__main__":  # pragma: no cover - smoke assertions only
    import json

    task = {
        "task_id": "DC-SMOKE-001",
        "type": "code",
        "risk": "LOW",
        "capabilities": ["code"],
        "constraints": [],
        "objective": "smoke",
        "acceptance": "pass",
    }
    result = br.route_task(task)
    plan = plan_for_route(result)
    print(json.dumps({"route": result.route, "plan": plan.as_dict(),
                      "counts": planned_counts(plan)}, ensure_ascii=False, indent=2))
