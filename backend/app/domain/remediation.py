"""The remediation registry, the safety gate and the executor.

Build spec sections 20-22, enforced in code rather than in a prompt:

* The AI may **only** choose an action that exists as an enabled row in
  ``remediation_actions``. There is no code path that accepts a free-form command, and
  ``executes_shell`` is ``False`` on every registered action.
* Before anything runs, the safety gate evaluates seven independent conditions. If any one
  fails, the action is **blocked**, the reasons are recorded, and the incident escalates to a
  human instead of the platform improvising.
* Autonomy level 5 ("automatically execute bounded proven remediation") is deliberately
  narrow: it requires low risk, a non-production environment, at least three verified
  historical successes and zero historical failures for that exact action.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.errors import ActionBlockedError
from app.core.security import require_permission
from app.database.models import (
    AuditLog,
    Environment,
    Incident,
    RemediationAction,
    RemediationAttempt,
    RemediationPolicy,
    RemediationRun,
    Service,
)
from app.sim.engine import SimulationEngine, sim_now
from app.sim.faults import ActionResult

logger = logging.getLogger(__name__)

RISK_ORDER = {"low": 1, "medium": 2, "high": 3, "critical": 4}


# ---------------------------------------------------------------------------------------
# Registry definition
# ---------------------------------------------------------------------------------------
@dataclass
class ActionSpec:
    code: str
    name: str
    description: str
    risk_level: str
    required_approval: bool
    allowed_environments: list[str]
    applicable_categories: list[str]
    timeout_seconds: int
    max_retries: int
    execution_workflow: dict[str, Any]
    verification_workflow: dict[str, Any]
    rollback_workflow: dict[str, Any]


REGISTRY: tuple[ActionSpec, ...] = (
    ActionSpec(
        code="restart_service",
        name="Restart service",
        description=(
            "Perform a rolling restart of the service. Clears the connection pool and releases "
            "resident memory. This is a mitigation, not a fix: it does not remove a leak."
        ),
        risk_level="low",
        required_approval=False,
        allowed_environments=["production", "staging", "development"],
        applicable_categories=["connection_leak", "memory_leak", "cpu_saturation", "hung_process"],
        timeout_seconds=180,
        max_retries=2,
        execution_workflow={"steps": ["drain", "restart", "await_ready"], "serial": True},
        verification_workflow={
            "checks": ["health_score", "error_rate", "connections", "latency_ms"],
            # A restart must survive a *soak* window before it counts as a fix. This is not an
            # arbitrary number: a restart resets counters, so a short window will always look
            # healthy. Five minutes is long enough for a resource leak to re-manifest, which is
            # precisely the failure this platform exists to remember.
            "settle_seconds": 300,
            "settle_rationale": (
                "restarts mask cumulative faults, so they are verified against a 5-minute soak"
            ),
            "primary_metric": "connection_saturation",
        },
        rollback_workflow={"steps": ["restart previous replica set"], "is_reversible": True},
    ),
    ActionSpec(
        code="rollback_deployment",
        name="Roll back deployment",
        description=(
            "Revert the most recent release for this service and environment, restoring the "
            "previous known-good configuration and artefacts."
        ),
        risk_level="high",
        required_approval=True,
        allowed_environments=["production", "staging", "development"],
        applicable_categories=["bad_deployment_config", "network_issue", "change_induced"],
        timeout_seconds=420,
        max_retries=1,
        execution_workflow={"steps": ["freeze", "revert release", "await_ready"], "serial": True},
        verification_workflow={
            "checks": ["error_rate", "latency_ms", "health_score"],
            "settle_seconds": 60,
            "primary_metric": "error_rate",
        },
        rollback_workflow={"steps": ["redeploy reverted release"], "is_reversible": True},
    ),
    ActionSpec(
        code="scale_service",
        name="Scale service horizontally",
        description=(
            "Add replicas and pool headroom. Buys capacity and relieves CPU pressure, but does "
            "not remove an underlying resource leak."
        ),
        risk_level="medium",
        required_approval=False,
        allowed_environments=["production", "staging", "development"],
        applicable_categories=["cpu_saturation", "capacity", "traffic_spike"],
        timeout_seconds=300,
        max_retries=2,
        execution_workflow={"steps": ["increase replicas", "await_ready"], "serial": True},
        verification_workflow={
            "checks": ["cpu_pct", "latency_ms", "error_rate"],
            "settle_seconds": 45,
            "primary_metric": "cpu_pct",
        },
        rollback_workflow={"steps": ["scale back to previous replica count"], "is_reversible": True},
    ),
    ActionSpec(
        code="update_known_safe_configuration",
        name="Apply known-safe configuration",
        description=(
            "Apply the reviewed, version-controlled known-safe configuration for this service "
            "(pool sizing, leak guard flags, timeouts) and reload it without a redeploy."
        ),
        risk_level="medium",
        required_approval=True,
        allowed_environments=["production", "staging", "development"],
        applicable_categories=[
            "connection_leak",
            "bad_deployment_config",
            "memory_leak",
            "cache_misconfiguration",
            "configuration",
        ],
        timeout_seconds=300,
        max_retries=2,
        execution_workflow={"steps": ["validate config", "apply", "reload", "await_ready"], "serial": True},
        verification_workflow={
            "checks": ["connection_saturation", "connections", "error_rate", "latency_ms"],
            "settle_seconds": 45,
            "primary_metric": "connection_saturation",
        },
        rollback_workflow={"steps": ["re-apply previous configuration revision"], "is_reversible": True},
    ),
    ActionSpec(
        code="clear_safe_cache",
        name="Flush cache safely",
        description=(
            "Invalidate the service cache through the supported API. Safe and reversible; only "
            "valid where stale cache entries are the cause."
        ),
        risk_level="low",
        # Low risk, but flushing a shared cache during peak traffic is disruptive enough that a
        # human confirms it. This is also the action that makes bounded level-5 automation
        # reachable: with a clean record it may auto-execute outside production, and one recorded
        # failure takes that privilege away again.
        required_approval=True,
        allowed_environments=["production", "staging", "development"],
        applicable_categories=["cache_misconfiguration", "configuration"],
        timeout_seconds=120,
        max_retries=1,
        execution_workflow={"steps": ["flush namespaces", "warm", "await_ready"], "serial": True},
        verification_workflow={
            "checks": ["error_rate", "health_score"],
            "settle_seconds": 30,
            "primary_metric": "error_rate",
        },
        rollback_workflow={"steps": ["no-op: caches repopulate"], "is_reversible": True},
    ),
    ActionSpec(
        code="rotate_expired_token",
        name="Rotate expired credential",
        description=(
            "Rotate the expired service credential in the secret store and roll the deployment "
            "so the new value is picked up."
        ),
        risk_level="medium",
        required_approval=True,
        allowed_environments=["production", "staging", "development"],
        applicable_categories=["expired_credentials", "configuration"],
        timeout_seconds=300,
        max_retries=1,
        execution_workflow={"steps": ["rotate secret", "roll deployment", "await_ready"], "serial": True},
        verification_workflow={
            "checks": ["error_rate", "health_score"],
            "settle_seconds": 45,
            "primary_metric": "error_rate",
        },
        rollback_workflow={"steps": ["restore previous secret version"], "is_reversible": True},
    ),
    ActionSpec(
        code="increase_database_capacity",
        name="Increase database capacity",
        description=(
            "Increase database compute and connection limits. Only appropriate when the "
            "database itself is the saturation point."
        ),
        risk_level="high",
        required_approval=True,
        allowed_environments=["production", "staging", "development"],
        applicable_categories=["database_overload"],
        timeout_seconds=600,
        max_retries=1,
        execution_workflow={"steps": ["resize instance", "wait for failover", "await_ready"], "serial": False},
        verification_workflow={
            "checks": ["latency_ms", "error_rate", "connections"],
            "settle_seconds": 60,
            "primary_metric": "latency_ms",
        },
        rollback_workflow={"steps": ["resize back to previous class"], "is_reversible": True},
    ),
)


def seed_registry(db: Session) -> int:
    """Idempotently install the registry. Returns the number of actions created."""
    created = 0
    for spec in REGISTRY:
        existing = db.scalar(select(RemediationAction).where(RemediationAction.code == spec.code))
        if existing:
            continue
        db.add(
            RemediationAction(
                code=spec.code,
                name=spec.name,
                description=spec.description,
                risk_level=spec.risk_level,
                required_approval=spec.required_approval,
                allowed_environments=spec.allowed_environments,
                applicable_categories=spec.applicable_categories,
                execution_workflow=spec.execution_workflow,
                verification_workflow=spec.verification_workflow,
                rollback_workflow=spec.rollback_workflow,
                timeout_seconds=spec.timeout_seconds,
                max_retries=spec.max_retries,
                is_enabled=True,
                executes_shell=False,
            )
        )
        created += 1
    db.flush()
    return created


def list_actions(db: Session, *, enabled_only: bool = True) -> list[RemediationAction]:
    stmt = select(RemediationAction)
    if enabled_only:
        stmt = stmt.where(RemediationAction.is_enabled.is_(True))
    return list(db.scalars(stmt.order_by(RemediationAction.risk_level, RemediationAction.code)).all())


def get_action(db: Session, code: str) -> RemediationAction | None:
    return db.scalar(select(RemediationAction).where(RemediationAction.code == code))


def actions_for_root_cause(db: Session, category: str) -> list[RemediationAction]:
    return [
        action
        for action in list_actions(db)
        if category in (action.applicable_categories or [])
    ]


# ---------------------------------------------------------------------------------------
# Safety gate
# ---------------------------------------------------------------------------------------
@dataclass
class SafetyCheck:
    name: str
    passed: bool
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "passed": self.passed, "detail": self.detail}


@dataclass
class SafetyVerdict:
    allowed: bool
    checks: list[SafetyCheck] = field(default_factory=list)
    checks_passed: list[str] = field(default_factory=list)
    checks_failed: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    requires_approval: bool = True
    approval_satisfied: bool = False
    effective_autonomy: int = 4
    risk_level: str = "medium"

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "requires_approval": self.requires_approval,
            "approval_satisfied": self.approval_satisfied,
            "effective_autonomy": self.effective_autonomy,
            "risk_level": self.risk_level,
            "checks": [c.to_dict() for c in self.checks],
            "checks_passed": self.checks_passed,
            "checks_failed": self.checks_failed,
            "reasons": self.reasons,
        }


def _role_allows(role: str, permission: str) -> tuple[bool, str]:
    try:
        require_permission(role, permission)
        return True, f"role '{role}' holds '{permission}'"
    except Exception as exc:  # noqa: BLE001 - normalise to a gate failure
        return False, str(exc)


def evaluate_safety_gate(
    db: Session,
    *,
    action_code: str,
    root_cause_category: str,
    environment: Environment,
    requested_autonomy: int | None = None,
    actor_role: str = "viewer",
    approved: bool = False,
    approved_by: str | None = None,
) -> SafetyVerdict:
    """The seven-condition gate from build spec section 22.

    Ordering matters: registration is checked first because nothing else is meaningful if the
    action does not exist.
    """
    checks: list[SafetyCheck] = []
    autonomy = min(requested_autonomy if requested_autonomy is not None else settings.max_autonomy_level, settings.max_autonomy_level)

    # 1. Is the action registered and enabled?
    action = get_action(db, action_code)
    registered = action is not None and action.is_enabled
    checks.append(
        SafetyCheck(
            "action_registered",
            registered,
            f"'{action_code}' is registered in the remediation registry"
            if registered
            else f"'{action_code}' is not a registered, enabled remediation action",
        )
    )
    if not registered:
        return SafetyVerdict(
            allowed=False,
            checks=checks,
            checks_failed=["action_registered"],
            checks_passed=[],
            reasons=[f"ACTION_BLOCKED: '{action_code}' is not registered and enabled"],
            requires_approval=True,
            risk_level="unknown",
            effective_autonomy=autonomy,
        )

    assert action is not None
    risk_level = action.risk_level

    # 2. Is this environment allowed for this action?
    allowed_environments = action.allowed_environments or []
    env_allowed = environment.name in allowed_environments or environment.kind in allowed_environments
    checks.append(
        SafetyCheck(
            "environment_allowed",
            env_allowed,
            f"'{action_code}' is permitted in {environment.name}"
            if env_allowed
            else f"'{action_code}' is not permitted in {environment.name} (allowed: {', '.join(allowed_environments)})",
        )
    )

    # 3. Is the risk acceptable at the requested autonomy level?
    risk_ok = RISK_ORDER.get(risk_level, 4) <= RISK_ORDER.get(settings.approval_required_above_risk, 2) or autonomy >= 4
    checks.append(
        SafetyCheck(
            "risk_acceptable",
            risk_ok,
            f"risk '{risk_level}' is within policy at autonomy level {autonomy}"
            if risk_ok
            else f"risk '{risk_level}' exceeds the policy ceiling and autonomy {autonomy} cannot waive it",
        )
    )

    # 4. Does the action actually address the diagnosed root cause?
    applicable = root_cause_category in (action.applicable_categories or [])
    checks.append(
        SafetyCheck(
            "addresses_root_cause",
            applicable,
            f"'{action_code}' is applicable to root cause '{root_cause_category}'"
            if applicable
            else f"'{action_code}' does not list root cause '{root_cause_category}' as applicable",
        )
    )

    # 5. Is approval required, and is it satisfied?
    policy = db.scalar(
        select(RemediationPolicy).where(
            RemediationPolicy.action_code == action_code,
            RemediationPolicy.environment_kind == environment.kind,
        )
    )
    if policy and policy.is_blocked:
        checks.append(
            SafetyCheck(
                "policy_allows",
                False,
                f"policy explicitly blocks '{action_code}' in {environment.kind}: {policy.note}",
            )
        )
    else:
        checks.append(SafetyCheck("policy_allows", True, "no blocking policy for this action/environment"))

    requires_approval = action.required_approval or (
        RISK_ORDER.get(risk_level, 4) > RISK_ORDER.get(settings.approval_required_above_risk, 2)
    )

    # Bounded level-5 automation: proven, low-risk, non-production only.
    level5_eligible = (
        autonomy >= 5
        and risk_level == "low"
        and not environment.is_production
        and action.historical_success >= 3
        and action.historical_failure == 0
    )
    if requires_approval and level5_eligible:
        requires_approval = False

    approval_satisfied = (not requires_approval) or approved
    # Approval is reported as its own state rather than as a gate *failure*. Conflating the two
    # would mark every remediation that legitimately needs a human as "blocked", which is both
    # wrong and would stop the platform at the approval step instead of pausing at it.
    checks.append(
        SafetyCheck(
            "approval_satisfied",
            True if not requires_approval else approval_satisfied,
            (
                "no approval required"
                if not requires_approval
                else (f"approved by {approved_by}" if approved else "approval pending: a human decision is required")
            ),
        )
    )

    # 6. Does the requesting role have permission to execute?
    permission_needed = "execute_remediation" if approval_satisfied else "propose_remediation"
    role_ok, role_detail = _role_allows(actor_role, permission_needed)
    checks.append(
        SafetyCheck(
            "role_permitted",
            role_ok,
            role_detail if role_ok else f"{role_detail} (needs '{permission_needed}')",
        )
    )

    # 7. Is a rollback defined and are verification steps defined?
    rollback_ok = bool(action.rollback_workflow)
    verification_ok = bool((action.verification_workflow or {}).get("checks"))
    checks.append(
        SafetyCheck(
            "rollback_available",
            rollback_ok,
            "a rollback workflow is defined" if rollback_ok else "no rollback workflow defined",
        )
    )
    checks.append(
        SafetyCheck(
            "verification_defined",
            verification_ok,
            "verification checks are defined" if verification_ok else "no verification checks defined",
        )
    )

    # Autonomy floor: levels 0-3 may never execute.
    autonomy_ok = autonomy >= 4 or (autonomy >= 5 and level5_eligible)
    if autonomy < 4:
        enforced = (not requires_approval) and level5_eligible and autonomy >= 5
        autonomy_ok = enforced
    checks.append(
        SafetyCheck(
            "autonomy_sufficient",
            autonomy_ok,
            f"autonomy level {autonomy} permits execution" if autonomy_ok else f"autonomy level {autonomy} permits proposal only",
        )
    )

    # `allowed` means "may proceed once approval is satisfied", not "may execute now".
    failed = [c.name for c in checks if not c.passed and c.name != "approval_satisfied"]
    reasons: list[str] = []
    for check in checks:
        if not check.passed and check.name != "approval_satisfied":
            reasons.append(f"ACTION_BLOCKED: {check.detail}")
    if requires_approval and not approval_satisfied:
        reasons.append("APPROVAL_REQUIRED: this action requires a human decision before execution")

    return SafetyVerdict(
        allowed=not failed,
        checks=checks,
        checks_passed=[c.name for c in checks if c.passed],
        checks_failed=failed,
        reasons=reasons,
        requires_approval=requires_approval,
        approval_satisfied=approval_satisfied,
        effective_autonomy=autonomy,
        risk_level=risk_level,
    )


# ---------------------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------------------
@dataclass
class ExecutionOutcome:
    run_id: int
    status: str
    action_code: str
    attempts: int
    result: dict[str, Any]
    safety: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "action_code": self.action_code,
            "attempts": self.attempts,
            "result": self.result,
            "safety": self.safety,
        }


def propose(
    db: Session,
    *,
    incident: Incident,
    service: Service,
    environment: Environment,
    action_code: str,
    rationale: str,
    parameters: dict[str, Any] | None = None,
    autonomy_level: int | None = None,
    actor: str = "ai",
    actor_role: str = "sre",
) -> RemediationRun:
    """Record a proposal and run the safety gate. Never executes anything."""
    verdict = evaluate_safety_gate(
        db,
        action_code=action_code,
        root_cause_category=incident.root_cause_category or "unknown",
        environment=environment,
        requested_autonomy=autonomy_level,
        actor_role=actor_role,
    )
    run = RemediationRun(
        incident_id=incident.id,
        action_code=action_code,
        environment_id=environment.id,
        autonomy_level=verdict.effective_autonomy,
        status="proposed" if verdict.allowed else "blocked",
        attempt=1,
        proposed_by=actor,
        rationale=rationale,
        parameters=parameters or {},
        blocked_reasons=verdict.reasons or None,
        safety_checks=[c.to_dict() for c in verdict.checks],
    )
    db.add(run)
    db.flush()
    db.add(
        AuditLog(
            action="remediation.proposed",
            actor=actor,
            actor_role=actor_role,
            target_type="incident",
            target_id=str(incident.id),
            result="blocked" if not verdict.allowed else "success",
            reason=rationale[:500],
            detail={
                "action_code": action_code,
                "safety_allowed": verdict.allowed,
                "blocked_reasons": verdict.reasons,
                "requires_approval": verdict.requires_approval,
            },
        )
    )
    db.flush()
    return run


def approve(
    db: Session,
    *,
    run: RemediationRun,
    actor: str,
    actor_role: str,
    reason: str = "",
) -> RemediationRun:
    require_permission(actor_role, "approve_remediation")
    run.approved_by = actor
    run.approved_at = sim_now()
    run.status = "approved"
    db.add(
        AuditLog(
            action="remediation.approved",
            actor=actor,
            actor_role=actor_role,
            target_type="remediation_run",
            target_id=str(run.id),
            result="success",
            reason=reason or f"approved {run.action_code}",
            detail={"action_code": run.action_code, "incident_id": run.incident_id},
        )
    )
    db.flush()
    return run


def execute(
    db: Session,
    *,
    engine: SimulationEngine,
    run: RemediationRun,
    actor: str,
    actor_role: str,
    service: Service,
    environment: Environment,
    incident: Incident,
) -> ExecutionOutcome:
    """Execute an approved run through the simulated environment and record the attempt.

    Re-runs the safety gate immediately before executing: approval does not create a
    permanent bypass, and a policy change between proposal and execution must still win.
    """
    verdict = evaluate_safety_gate(
        db,
        action_code=run.action_code,
        root_cause_category=incident.root_cause_category or "unknown",
        environment=environment,
        requested_autonomy=run.autonomy_level,
        actor_role=actor_role,
        approved=run.status in {"approved", "executed"},
        approved_by=run.approved_by,
    )
    # The gate is re-evaluated at execution time. Approval does not create a permanent bypass,
    # and an unsatisfied approval requirement blocks execution *here*, at the last moment.
    if not verdict.allowed or (verdict.requires_approval and not verdict.approval_satisfied):
        run.status = "blocked"
        run.blocked_reasons = verdict.reasons
        run.safety_checks = [c.to_dict() for c in verdict.checks]
        db.flush()
        db.add(
            AuditLog(
                action="remediation.blocked",
                actor=actor,
                actor_role=actor_role,
                target_type="remediation_run",
                target_id=str(run.id),
                result="blocked",
                reason="; ".join(verdict.reasons),
                detail={
                    "checks_failed": verdict.checks_failed,
                    "requires_approval": verdict.requires_approval,
                    "approval_satisfied": verdict.approval_satisfied,
                },
            )
        )
        db.flush()
        raise ActionBlockedError("; ".join(verdict.reasons), reasons=verdict.reasons)

    result: ActionResult = engine.apply_remediation(
        service_name=service.name, action_code=run.action_code, params=run.parameters or {}
    )
    run.status = "executed"
    run.executed_at = sim_now()
    run.completed_at = sim_now()
    run.result = result.to_dict()

    db.add(
        RemediationAttempt(
            incident_id=incident.id,
            service_id=service.id,
            root_cause_category=incident.root_cause_category or "unknown",
            action_code=run.action_code,
            attempt_number=run.attempt,
            outcome="applied",
            failure_reason="",
        )
    )
    db.add(
        AuditLog(
            action="remediation.executed",
            actor=actor,
            actor_role=actor_role,
            target_type="remediation_run",
            target_id=str(run.id),
            result="success",
            reason=result.note,
            detail={
                "action_code": run.action_code,
                "resolved_cause": result.resolved_cause,
                "incident_id": incident.id,
            },
        )
    )
    db.flush()
    return ExecutionOutcome(
        run_id=run.id,
        status=run.status,
        action_code=run.action_code,
        attempts=run.attempt,
        result=result.to_dict(),
        safety=verdict.to_dict(),
    )


def record_attempt_outcome(
    db: Session,
    *,
    run: RemediationRun,
    outcome: str,
    failure_reason: str = "",
    verification_id: int | None = None,
) -> None:
    """Close the loop on an attempt: update the ledger, the registry statistics and the run."""
    run.outcome = outcome
    run.status = "verified" if outcome == "succeeded" else "failed"
    attempt = db.scalar(
        select(RemediationAttempt)
        .where(
            RemediationAttempt.incident_id == run.incident_id,
            RemediationAttempt.action_code == run.action_code,
        )
        .order_by(RemediationAttempt.id.desc())
    )
    if attempt:
        attempt.outcome = outcome
        attempt.failure_reason = failure_reason
        attempt.verification_id = verification_id

    action = get_action(db, run.action_code)
    if action:
        if outcome == "succeeded":
            action.historical_success += 1
        elif outcome == "failed":
            action.historical_failure += 1
    db.flush()


def remediation_history_for_root_cause(
    db: Session, *, service_id: int, root_cause_category: str, limit: int = 20
) -> list[RemediationAttempt]:
    """Deterministic failure-memory lookup: what did we already try for this exact cause?"""
    return list(
        db.scalars(
            select(RemediationAttempt)
            .where(
                RemediationAttempt.service_id == service_id,
                RemediationAttempt.root_cause_category == root_cause_category,
            )
            .order_by(RemediationAttempt.created_at.desc())
            .limit(limit)
        ).all()
    )
