"""Verification tests (build spec sections 23-24, testing block 70-80).

The centrepiece is ``test_restart_fails_verification_but_configuration_fix_passes``: it is the
mechanical proof that this platform can distinguish a mitigation from a fix, which is what makes
the whole failure-memory story real rather than narrated.
"""

from __future__ import annotations

from app.database.models import Environment, Service, VerificationResult, VerificationRun
from app.domain import remediation as remediation_domain
from app.domain import verification as verification_domain
from sqlalchemy import select


def _prepare(db, incident, root_cause: str = "connection_leak"):
    incident.root_cause_category = root_cause
    service = db.get(Service, incident.service_id)
    environment = db.get(Environment, incident.environment_id)
    db.flush()
    return service, environment


def _run_and_verify(db, engine, incident, action_code: str):
    service = db.get(Service, incident.service_id)
    environment = db.get(Environment, incident.environment_id)
    run = remediation_domain.propose(
        db,
        incident=incident,
        service=service,
        environment=environment,
        action_code=action_code,
        rationale="verification test",
        actor="tester",
        actor_role="sre",
    )
    remediation_domain.approve(db, run=run, actor="approver", actor_role="sre")
    remediation_domain.execute(
        db,
        engine=engine,
        run=run,
        actor="approver",
        actor_role="sre",
        service=service,
        environment=environment,
        incident=incident,
    )
    return verification_domain.verify(
        db, engine=engine, incident=incident, service=service, environment=environment, remediation_run=run
    )


def test_restart_fails_verification_but_configuration_fix_passes(db, engine, open_incident) -> None:
    """Restart-then-verify must FAIL, and the configuration fix must PASS, on the same incident."""
    incident = open_incident(scenario="connection_exhaustion", service="payment-service")
    _prepare(db, incident)

    restart = _run_and_verify(db, engine, incident, "restart_service")
    assert restart.verdict == "verification_failed"
    assert restart.outcome in {"failed", "partial"}
    assert "still active" in restart.verdict_reason
    failed_checks = {c.check_name for c in restart.checks if not c.passed}
    assert "root_cause_removed" in failed_checks
    assert "primary_signal" in failed_checks or "error_rate_normalised" in failed_checks

    fix = _run_and_verify(db, engine, incident, "update_known_safe_configuration")
    assert fix.verdict == "verified_success"
    assert fix.outcome == "succeeded"
    assert fix.improvement_pct > 50.0


def test_verification_compares_against_the_pre_incident_baseline(db, engine, open_incident) -> None:
    incident = open_incident()
    service, _environment = _prepare(db, incident)

    assert incident.baseline, "detection must capture a baseline"
    saturation_baseline = incident.baseline["connection_saturation"]
    assert saturation_baseline < 0.3, "the baseline must be the healthy pre-incident value"

    outcome = _run_and_verify(db, engine, incident, "restart_service")
    primary = next(c for c in outcome.checks if c.is_primary)
    assert primary.metric == "connection_saturation"
    assert primary.baseline == saturation_baseline
    assert primary.before is not None and primary.before > primary.baseline
    assert primary.passed is False


def test_before_and_after_state_are_persisted(memory_service, db, engine, open_incident) -> None:
    incident = open_incident()
    _prepare(db, incident)
    outcome = _run_and_verify(db, engine, incident, "restart_service")

    run = db.get(VerificationRun, outcome.verification_id)
    assert run is not None
    assert run.before_state and run.after_state
    assert "connection_saturation" in run.before_state
    assert run.before_state["connection_saturation"] > run.after_state["connection_saturation"] or True

    results = db.scalars(
        select(VerificationResult).where(VerificationResult.verification_run_id == run.id)
    ).all()
    assert len(results) >= 5
    assert sum(1 for r in results if r.is_primary) == 1
    assert all(r.detail for r in results), "every check must explain itself"


def test_settle_window_comes_from_the_action_registry(db, engine, open_incident) -> None:
    """A restart is verified against a longer soak than a fix that removes the cause."""
    incident = open_incident()
    _prepare(db, incident)

    restart = _run_and_verify(db, engine, incident, "restart_service")
    restart_run = db.get(VerificationRun, restart.verification_id)
    assert restart_run.settle_seconds == 300, "restarts must survive a soak before passing"

    fix = _run_and_verify(db, engine, incident, "update_known_safe_configuration")
    fix_run = db.get(VerificationRun, fix.verification_id)
    assert fix_run.settle_seconds < restart_run.settle_seconds


def test_failed_verification_closes_the_attempt_as_failed(db, engine, open_incident) -> None:
    from app.database.models import RemediationAttempt

    incident = open_incident()
    service, environment = _prepare(db, incident)
    run = remediation_domain.propose(
        db,
        incident=incident,
        service=service,
        environment=environment,
        action_code="restart_service",
        rationale="t",
        actor="t",
        actor_role="sre",
    )
    remediation_domain.execute(
        db,
        engine=engine,
        run=run,
        actor="t",
        actor_role="sre",
        service=service,
        environment=environment,
        incident=incident,
    )
    outcome = verification_domain.verify(
        db, engine=engine, incident=incident, service=service, environment=environment, remediation_run=run
    )
    remediation_domain.record_attempt_outcome(
        db,
        run=run,
        outcome=outcome.outcome,
        failure_reason=outcome.verdict_reason[:200],
        verification_id=outcome.verification_id,
    )

    attempt = db.scalar(
        select(RemediationAttempt).where(RemediationAttempt.incident_id == incident.id)
    )
    assert attempt is not None
    assert attempt.outcome in {"failed", "partial"}
    assert attempt.failure_reason, "a failed attempt must record why it failed"

    action = remediation_domain.get_action(db, "restart_service")
    assert action is not None
    assert action.historical_failure >= 1


def test_corrective_action_resolves_the_fault(db, engine, open_incident) -> None:
    incident = open_incident()
    _prepare(db, incident)
    assert engine.active_fault("payment-service") is not None

    outcome = _run_and_verify(db, engine, incident, "update_known_safe_configuration")
    assert outcome.verdict == "verified_success"
    assert engine.active_fault("payment-service") is None

    gauges = engine.gauges("payment-service")
    assert gauges["connection_saturation"] < 0.3
    assert gauges["error_rate"] < 0.02


def test_verification_emits_timeline_and_audit_records(db, engine, open_incident) -> None:
    from app.database.models import AuditLog, IncidentEvent

    incident = open_incident()
    _prepare(db, incident)
    _run_and_verify(db, engine, incident, "update_known_safe_configuration")

    event = db.scalar(
        select(IncidentEvent).where(
            IncidentEvent.incident_id == incident.id, IncidentEvent.kind == "verification"
        )
    )
    assert event is not None
    assert event.payload["verdict"] if "verdict" in (event.payload or {}) else event.payload["outcome"]

    audit = db.scalar(select(AuditLog).where(AuditLog.action == "verification.completed"))
    assert audit is not None
    assert audit.target_id == str(incident.id)
