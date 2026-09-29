"""Verification: a command succeeding is not the same as an incident being fixed.

Build spec sections 23 and 24. Every figure in a verification report is a median over metric
rows the simulator produced, compared against a baseline captured *before* the incident
started. There is no code path that writes a "fixed" flag: the verdict is computed from
telemetry and the fault state, which is why a mitigation that only buys time is reported as
``VERIFICATION_FAILED`` rather than being quietly rounded up to success.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Sequence

from sqlalchemy.orm import Session

from app.core.config import settings
from sqlalchemy import select

from app.database.models import (
    AuditLog,
    Environment,
    Incident,
    IncidentEvent,
    RemediationAction,
    RemediationRun,
    Service,
    VerificationResult,
    VerificationRun,
)
from app.detection.logs import SignatureStat
from app.sim.engine import SimulationEngine, sim_now

logger = logging.getLogger(__name__)

# Which metric is the *primary* signal for each root cause. Verifying the wrong metric is how
# teams end up declaring victory on an incident that is still burning.
PRIMARY_METRIC_BY_CAUSE: dict[str, str] = {
    "connection_leak": "connection_saturation",
    "bad_deployment_config": "connection_saturation",
    "configuration": "connection_saturation",
    "memory_leak": "mem_pct",
    "database_overload": "latency_ms",
    "cpu_saturation": "cpu_pct",
    "disk_pressure": "disk_pct",
    "cache_misconfiguration": "error_rate",
    "expired_credentials": "error_rate",
    "network_issue": "latency_ms",
    "unknown": "error_rate",
}

TRACKED_METRICS = (
    "connection_saturation",
    "connections",
    "pool_size",
    "error_rate",
    "latency_ms",
    "cpu_pct",
    "mem_pct",
    "disk_pct",
    "health_score",
    "queue_depth",
)

TOLERANCE = 1.35
"""A metric may be up to 35% above its pre-incident baseline and still count as recovered."""

ABSOLUTE_TOLERANCE = {
    "error_rate": 0.02,
    "connection_saturation": 0.08,
    "cpu_pct": 6.0,
    "mem_pct": 6.0,
    "disk_pct": 3.0,
    "latency_ms": 60.0,
}

UNIT_BY_METRIC = {
    "connection_saturation": "ratio",
    "connections": "conns",
    "pool_size": "conns",
    "error_rate": "ratio",
    "latency_ms": "ms",
    "cpu_pct": "%",
    "mem_pct": "%",
    "disk_pct": "%",
    "health_score": "score",
    "queue_depth": "items",
}


@dataclass
class CheckOutcome:
    check_name: str
    metric: str
    before: float | None
    after: float | None
    baseline: float | None
    unit: str
    passed: bool
    is_primary: bool
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "check_name": self.check_name,
            "metric": self.metric,
            "before": self.before,
            "after": self.after,
            "baseline": self.baseline,
            "unit": self.unit,
            "passed": self.passed,
            "is_primary": self.is_primary,
            "detail": self.detail,
        }


@dataclass
class VerificationOutcome:
    verification_id: int
    verdict: str
    verdict_reason: str
    outcome: str
    improvement_pct: float
    before: dict[str, float]
    after: dict[str, float]
    baseline: dict[str, float]
    checks: list[CheckOutcome] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "verification_id": self.verification_id,
            "verdict": self.verdict,
            "verdict_reason": self.verdict_reason,
            "outcome": self.outcome,
            "improvement_pct": self.improvement_pct,
            "before": self.before,
            "after": self.after,
            "baseline": self.baseline,
            "checks": [c.to_dict() for c in self.checks],
        }


def _median(values: Sequence[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return float(ordered[len(ordered) // 2])


def capture_window(
    engine: SimulationEngine,
    *,
    service_name: str,
    seconds: float,
    end=None,
) -> dict[str, float]:
    """Median of each tracked metric over a trailing window."""
    state: dict[str, float] = {}
    for metric in TRACKED_METRICS:
        values = [v for _, v in engine.metric_window(service_name=service_name, metric=metric, seconds=seconds, end=end)]
        state[metric] = round(_median(values), 4)
    return state


def capture_state(engine: SimulationEngine, service_name: str) -> dict[str, float]:
    return {k: round(float(v), 4) for k, v in engine.gauges(service_name).items()}


def _threshold(metric: str, baseline: float) -> float:
    base = abs(baseline) * TOLERANCE
    absolute = ABSOLUTE_TOLERANCE.get(metric, 0.0)
    return max(base, abs(baseline) + absolute)


def _resolve_settle_seconds(db: Session, run: RemediationRun | None, *, explicit: int | None = None) -> int:
    """Settle time comes from the action's verification workflow, not from a global constant.

    Different remediations deserve different soak times, and encoding that in the registry keeps
    the reasoning auditable: a restart is verified against a soak because restarts mask
    cumulative faults, whereas a fix that removes the cause can be verified sooner.
    """
    if explicit is not None:
        return explicit
    if run is not None:
        action = db.scalar(select(RemediationAction).where(RemediationAction.code == run.action_code))
        workflow = (action.verification_workflow or {}) if action else {}
        value = workflow.get("settle_seconds")
        if isinstance(value, (int, float)) and value > 0:
            return int(value)
    return int(settings.verification_settle_seconds)


def verify(
    db: Session,
    *,
    engine: SimulationEngine,
    incident: Incident,
    service: Service,
    environment: Environment,
    remediation_run: RemediationRun | None,
    settle_seconds: int | None = None,
    actor: str = "verification_engine",
) -> VerificationOutcome:
    """Run the multi-signal verification and persist the full evidence."""
    settle = _resolve_settle_seconds(db, remediation_run, explicit=settle_seconds)
    started = sim_now()
    run = VerificationRun(
        incident_id=incident.id,
        remediation_run_id=remediation_run.id if remediation_run else None,
        status="running",
        settle_seconds=settle,
        started_at=started,
    )
    db.add(run)
    db.flush()

    # --- 1. "before" is measured from the window immediately preceding the remediation ----
    # Anchored to the *simulated* clock, because that is the clock the metric samples use. The
    # window ends now, and is read before the settle period is advanced, so it contains only
    # samples from before the action was applied.
    before_window = min(300.0, max(60.0, settle * 2))
    before_state = capture_window(engine, service_name=service.name, seconds=before_window)
    before_snapshot = capture_state(engine, service_name=service.name)

    # --- 2. settle, then measure "after" -------------------------------------------------
    if settle > 0:
        engine.advance(settle)
        db.flush()
    after_state = capture_window(engine, service_name=service.name, seconds=float(max(settle, 30)))
    after_snapshot = capture_state(engine, service_name=service.name)

    baseline = dict(incident.baseline or {})
    root_cause = incident.root_cause_category or "unknown"
    primary_metric = PRIMARY_METRIC_BY_CAUSE.get(root_cause, "error_rate")

    checks: list[CheckOutcome] = []

    # --- 3. primary signal for the diagnosed root cause ----------------------------------
    baseline_primary = baseline.get(primary_metric)
    before_primary = before_state.get(primary_metric)
    after_primary = after_state.get(primary_metric)
    if baseline_primary is None and remediation_run and remediation_run.result:
        changes = (remediation_run.result or {}).get("changes", {})
        baseline_primary = (changes.get("before") or {}).get(primary_metric)
    primary_threshold = _threshold(primary_metric, baseline_primary or 0.0)
    primary_passed = after_primary is not None and after_primary <= primary_threshold
    checks.append(
        CheckOutcome(
            check_name="primary_signal",
            metric=primary_metric,
            before=before_primary,
            after=after_primary,
            baseline=baseline_primary,
            unit=UNIT_BY_METRIC.get(primary_metric, ""),
            passed=primary_passed,
            is_primary=True,
            detail=(
                f"primary metric for root cause '{root_cause}': {primary_metric} "
                f"{before_primary} → {after_primary} (baseline {baseline_primary}, "
                f"threshold {primary_threshold:.4g})"
            ),
        )
    )

    # --- 4. error rate must return to normal --------------------------------------------
    baseline_error = baseline.get("error_rate", 0.004)
    error_threshold = max(baseline_error * 2.5, 0.02)
    after_error = after_state.get("error_rate", 0.0)
    error_passed = after_error <= error_threshold
    checks.append(
        CheckOutcome(
            check_name="error_rate_normalised",
            metric="error_rate",
            before=before_state.get("error_rate"),
            after=after_error,
            baseline=baseline_error,
            unit="ratio",
            passed=error_passed,
            is_primary=False,
            detail=(
                f"5xx ratio {before_state.get('error_rate')} → {after_error} "
                f"(threshold {error_threshold:.4g})"
            ),
        )
    )

    # --- 5. latency ----------------------------------------------------------------------
    baseline_latency = baseline.get("latency_ms", 200.0)
    latency_threshold = _threshold("latency_ms", baseline_latency)
    after_latency = after_state.get("latency_ms", 0.0)
    latency_passed = after_latency <= latency_threshold
    checks.append(
        CheckOutcome(
            check_name="latency_normalised",
            metric="latency_ms",
            before=before_state.get("latency_ms"),
            after=after_latency,
            baseline=baseline_latency,
            unit="ms",
            passed=latency_passed,
            is_primary=False,
            detail=f"p95 latency {before_state.get('latency_ms')} → {after_latency} (threshold {latency_threshold:.4g})",
        )
    )

    # --- 6. is the underlying fault actually gone? ---------------------------------------
    active_fault = engine.active_fault(service.name)
    fault_cleared = active_fault is None
    checks.append(
        CheckOutcome(
            check_name="root_cause_removed",
            metric="fault_state",
            before=1.0 if active_fault else 0.0,
            after=0.0 if fault_cleared else 1.0,
            baseline=0.0,
            unit="bool",
            passed=fault_cleared,
            is_primary=False,
            detail=(
                "the underlying fault is no longer active"
                if fault_cleared
                else f"the underlying fault '{active_fault.kind if active_fault else '?'}' is still active — "
                f"the remediation addressed symptoms only"
            ),
        )
    )

    # --- 7. no new critical log signatures appeared --------------------------------------
    signatures: list[SignatureStat] = engine.recent_signatures(
        service_name=service.name, seconds=float(max(settle, 60)), baseline_seconds=1800.0
    )
    critical = [s for s in signatures if s.severity == "critical" and s.level in {"ERROR", "FATAL"}]
    high_new = [s for s in signatures if s.is_new and s.count >= 5 and s.level in {"ERROR", "FATAL"}]
    logs_passed = not critical and not high_new
    checks.append(
        CheckOutcome(
            check_name="no_new_critical_signatures",
            metric="error_signatures",
            before=None,
            after=float(len(critical) + len(high_new)),
            baseline=0.0,
            unit="signatures",
            passed=logs_passed,
            is_primary=False,
            detail=(
                "no new critical error signatures in the verification window"
                if logs_passed
                else "new/worsening error signatures: "
                + "; ".join(s.template for s in (critical + high_new)[:3])[:400]
            ),
        )
    )

    # --- 8. availability probe -----------------------------------------------------------
    health_after = after_snapshot.get("health_score", 0.0)
    health_baseline = baseline.get("health_score", 1.0)
    availability_passed = health_after >= max(health_baseline * 0.9, 0.6)
    checks.append(
        CheckOutcome(
            check_name="service_healthy",
            metric="health_score",
            before=before_snapshot.get("health_score"),
            after=health_after,
            baseline=health_baseline,
            unit="score",
            passed=availability_passed,
            is_primary=False,
            detail=f"derived health score {before_snapshot.get('health_score')} → {health_after} (baseline {health_baseline})",
        )
    )

    # --- verdict -------------------------------------------------------------------------
    blocking = {"primary_signal", "error_rate_normalised", "root_cause_removed"}
    blocking_failures = [c for c in checks if c.check_name in blocking and not c.passed]
    all_passed = not blocking_failures

    before_primary_value = before_primary if before_primary is not None else 0.0
    improvement = 0.0
    denom = abs(before_primary_value) if abs(before_primary_value) > 1e-9 else 1.0
    improvement = ((before_primary_value - (after_primary or 0.0)) / denom) * 100.0

    if all_passed:
        verdict = "verified_success"
        outcome = "succeeded"
        reason = (
            f"all blocking checks passed: {primary_metric} returned from {before_primary} to "
            f"{after_primary} against a baseline of {baseline_primary}, and the underlying fault "
            f"is no longer active."
        )
    else:
        verdict = "verification_failed"
        outcome = (
            "partial"
            if improvement >= settings.verification_min_improvement_pct
            else "failed"
        )
        reason = "; ".join(c.detail for c in blocking_failures)

    for outcome_check in checks:
        db.add(
            VerificationResult(
                verification_run_id=run.id,
                check_name=outcome_check.check_name,
                metric=outcome_check.metric,
                before_value=outcome_check.before,
                after_value=outcome_check.after,
                baseline_value=outcome_check.baseline,
                unit=outcome_check.unit,
                passed=outcome_check.passed,
                is_primary=outcome_check.is_primary,
                detail=outcome_check.detail,
            )
        )

    run.status = "completed"
    run.verdict = verdict
    run.verdict_reason = reason
    run.completed_at = sim_now()
    run.before_state = {**before_state, "snapshot": before_snapshot, "primary_metric": primary_metric}
    run.after_state = {**after_state, "snapshot": after_snapshot}
    run.improvement_pct = round(improvement, 2)

    db.add(
        IncidentEvent(
            incident_id=incident.id,
            ts=sim_now(),
            kind="verification",
            title=f"Verification {verdict.replace('_', ' ')}",
            description=reason,
            actor=actor,
            source="verification_engine",
            payload={
                "verification_id": run.id,
                "outcome": outcome,
                "improvement_pct": run.improvement_pct,
                "before": before_state,
                "after": after_state,
            },
        )
    )
    db.add(
        AuditLog(
            action="verification.completed",
            actor=actor,
            actor_role="system",
            target_type="incident",
            target_id=str(incident.id),
            result="success" if all_passed else "failed",
            reason=reason[:800],
            detail={
                "verdict": verdict,
                "outcome": outcome,
                "improvement_pct": run.improvement_pct,
                "checks_failed": [c.check_name for c in blocking_failures],
            },
        )
    )
    db.flush()

    return VerificationOutcome(
        verification_id=run.id,
        verdict=verdict,
        verdict_reason=reason,
        outcome=outcome,
        improvement_pct=run.improvement_pct,
        before=before_state,
        after=after_state,
        baseline=baseline,
        checks=checks,
    )
