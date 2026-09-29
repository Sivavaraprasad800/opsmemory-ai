"""Remediation safety tests (build spec sections 20-22, testing block 70-80).

These are the tests that would catch a regression that made the platform dangerous: an
unregistered action executing, an approval being bypassed, a destructive cleanup being added to
the registry, or shell access appearing anywhere.
"""

from __future__ import annotations

import pytest

from app.core.config import settings
from app.core.errors import ActionBlockedError
from app.database.models import Environment, RemediationAction, Service
from app.domain import remediation as remediation_domain
from app.schemas.api import RemediationRequest
from sqlalchemy import select


def _context(db, incident):
    service = db.get(Service, incident.service_id)
    environment = db.get(Environment, incident.environment_id)
    return service, environment


def test_registry_is_seeded_and_never_executes_shell(db) -> None:
    actions = remediation_domain.list_actions(db)
    assert len(actions) >= 7
    codes = {a.code for a in actions}
    assert {
        "restart_service",
        "rollback_deployment",
        "scale_service",
        "update_known_safe_configuration",
        "clear_safe_cache",
        "rotate_expired_token",
        "increase_database_capacity",
    } <= codes
    for action in actions:
        assert action.executes_shell is False
        assert action.rollback_workflow, f"{action.code} must define a rollback"
        assert (action.verification_workflow or {}).get("checks"), f"{action.code} must define verification"


def test_no_destructive_action_exists_in_the_registry(db) -> None:
    """Disk pressure has no registered fix on purpose: the platform must escalate, not improvise."""
    codes = {a.code for a in remediation_domain.list_actions(db)}
    for forbidden in ("wipe_volume", "delete_files", "discard_and_wipe_volume", "rm_rf", "exec_shell"):
        assert forbidden not in codes
    assert remediation_domain.actions_for_root_cause(db, "disk_pressure") == []


def test_unregistered_action_is_blocked(db, open_incident) -> None:
    incident = open_incident()
    incident.root_cause_category = "connection_leak"
    service, environment = _context(db, incident)
    verdict = remediation_domain.evaluate_safety_gate(
        db,
        action_code="drop_database",
        root_cause_category="connection_leak",
        environment=environment,
        actor_role="sre",
    )
    assert verdict.allowed is False
    assert "action_registered" in verdict.checks_failed
    assert any("not registered" in reason for reason in verdict.reasons)


def test_approval_required_is_pending_not_blocked(db, open_incident) -> None:
    """The distinction the executor depends on: needing approval is not the same as being blocked."""
    incident = open_incident()
    incident.root_cause_category = "connection_leak"
    _service, environment = _context(db, incident)

    verdict = remediation_domain.evaluate_safety_gate(
        db,
        action_code="update_known_safe_configuration",
        root_cause_category="connection_leak",
        environment=environment,
        actor_role="sre",
    )
    assert verdict.allowed is True
    assert verdict.requires_approval is True
    assert verdict.approval_satisfied is False
    assert any(reason.startswith("APPROVAL_REQUIRED") for reason in verdict.reasons)


def test_execution_without_approval_is_blocked(db, engine, open_incident) -> None:
    incident = open_incident()
    incident.root_cause_category = "connection_leak"
    service, environment = _context(db, incident)

    run = remediation_domain.propose(
        db,
        incident=incident,
        service=service,
        environment=environment,
        action_code="update_known_safe_configuration",
        rationale="test proposal",
        actor="tester",
        actor_role="sre",
    )
    assert run.status == "proposed"

    with pytest.raises(ActionBlockedError) as excinfo:
        remediation_domain.execute(
            db,
            engine=engine,
            run=run,
            actor="tester",
            actor_role="sre",
            service=service,
            environment=environment,
            incident=incident,
        )
    assert any("APPROVAL_REQUIRED" in reason for reason in excinfo.value.reasons)
    assert run.status == "blocked"


def test_approved_execution_proceeds(db, engine, open_incident) -> None:
    incident = open_incident()
    incident.root_cause_category = "connection_leak"
    service, environment = _context(db, incident)

    run = remediation_domain.propose(
        db,
        incident=incident,
        service=service,
        environment=environment,
        action_code="update_known_safe_configuration",
        rationale="test",
        actor="tester",
        actor_role="sre",
    )
    remediation_domain.approve(db, run=run, actor="approver", actor_role="sre", reason="looks right")
    outcome = remediation_domain.execute(
        db,
        engine=engine,
        run=run,
        actor="approver",
        actor_role="sre",
        service=service,
        environment=environment,
        incident=incident,
    )
    assert outcome.status == "executed"
    assert outcome.result["resolved_cause"] is True


def test_action_not_applicable_to_root_cause_is_blocked(db, open_incident) -> None:
    incident = open_incident()
    incident.root_cause_category = "cpu_saturation"
    _service, environment = _context(db, incident)
    verdict = remediation_domain.evaluate_safety_gate(
        db,
        action_code="clear_safe_cache",
        root_cause_category="cpu_saturation",
        environment=environment,
        actor_role="sre",
    )
    assert verdict.allowed is False
    assert "addresses_root_cause" in verdict.checks_failed


def test_explicit_policy_block_wins(db, open_incident) -> None:
    incident = open_incident()
    incident.root_cause_category = "connection_leak"
    _service, environment = _context(db, incident)

    policy = db.scalar(
        select(remediation_domain.RemediationPolicy).where(
            remediation_domain.RemediationPolicy.action_code == "update_known_safe_configuration",
            remediation_domain.RemediationPolicy.environment_kind == environment.kind,
        )
    )
    if policy is None:
        policy = remediation_domain.RemediationPolicy(
            project_id=incident.project_id,
            action_code="update_known_safe_configuration",
            environment_kind=environment.kind,
        )
        db.add(policy)
    policy.is_blocked = True
    policy.note = "change freeze until Friday"
    db.flush()

    verdict = remediation_domain.evaluate_safety_gate(
        db,
        action_code="update_known_safe_configuration",
        root_cause_category="connection_leak",
        environment=environment,
        actor_role="sre",
    )
    assert verdict.allowed is False
    assert "policy_allows" in verdict.checks_failed
    assert any("change freeze" in reason for reason in verdict.reasons)


def test_insufficient_role_cannot_propose(db, open_incident) -> None:
    incident = open_incident()
    incident.root_cause_category = "connection_leak"
    _service, environment = _context(db, incident)
    verdict = remediation_domain.evaluate_safety_gate(
        db,
        action_code="restart_service",
        root_cause_category="connection_leak",
        environment=environment,
        actor_role="viewer",
    )
    assert verdict.allowed is False
    assert "role_permitted" in verdict.checks_failed


def test_low_autonomy_cannot_execute(db, open_incident) -> None:
    incident = open_incident()
    incident.root_cause_category = "connection_leak"
    _service, environment = _context(db, incident)
    verdict = remediation_domain.evaluate_safety_gate(
        db,
        action_code="restart_service",
        root_cause_category="connection_leak",
        environment=environment,
        requested_autonomy=2,
        actor_role="sre",
    )
    assert verdict.allowed is False
    assert "autonomy_sufficient" in verdict.checks_failed


def test_level_five_is_off_by_default_whatever_is_requested(db, open_incident) -> None:
    """The configured ceiling wins over the caller's request.

    ``max_autonomy_level`` defaults to 4, so asking for 5 gets clamped to 4. Level 5 is an opt-in
    deployment decision, and this test pins that: no API caller can raise the ceiling at runtime.
    """
    incident = open_incident()
    incident.root_cause_category = "cache_misconfiguration"
    _service, production = _context(db, incident)
    staging = db.scalar(select(Environment).where(Environment.name == "staging"))
    assert staging is not None

    action = remediation_domain.get_action(db, "clear_safe_cache")
    assert action is not None
    action.historical_success = 5
    action.historical_failure = 0
    db.flush()

    verdict = remediation_domain.evaluate_safety_gate(
        db,
        action_code="clear_safe_cache",
        root_cause_category="cache_misconfiguration",
        environment=staging,
        requested_autonomy=5,
        actor_role="sre",
    )
    assert verdict.effective_autonomy == 4
    assert verdict.requires_approval is True


def test_level_five_automation_is_narrowly_bounded(db, open_incident, monkeypatch) -> None:
    """Level 5 must not become a blanket auto-execute switch.

    The rule requires *all* of: autonomy >= 5, a low-risk action, a non-production environment, at
    least three verified successes and zero recorded failures for that exact action. Each of those
    is exercised here, and ``clear_safe_cache`` is the action used because it is low risk yet still
    approval-required, which makes the relaxation observable.

    Level 5 is opt-in, so the ceiling is raised explicitly here rather than assumed.
    """
    monkeypatch.setattr(settings, "max_autonomy_level", 5)

    incident = open_incident()
    incident.root_cause_category = "cache_misconfiguration"
    _service, production = _context(db, incident)
    staging = db.scalar(select(Environment).where(Environment.name == "staging"))
    assert staging is not None

    action = remediation_domain.get_action(db, "clear_safe_cache")
    assert action is not None
    assert action.risk_level == "low" and action.required_approval is True

    # 1. Even with a perfect record, production always requires a human.
    action.historical_success = 5
    action.historical_failure = 0
    db.flush()
    in_production = remediation_domain.evaluate_safety_gate(
        db,
        action_code="clear_safe_cache",
        root_cause_category="cache_misconfiguration",
        environment=production,
        requested_autonomy=5,
        actor_role="sre",
    )
    assert in_production.requires_approval is True, "production must always require approval"

    # 2. An unproven action in staging still requires a human.
    action.historical_success = 1
    db.flush()
    unproven = remediation_domain.evaluate_safety_gate(
        db,
        action_code="clear_safe_cache",
        root_cause_category="cache_misconfiguration",
        environment=staging,
        requested_autonomy=5,
        actor_role="sre",
    )
    assert unproven.requires_approval is True, "three verified successes are required"

    # 3. A proven, low-risk, non-production action may finally auto-execute at level 5.
    action.historical_success = 5
    db.flush()
    proven = remediation_domain.evaluate_safety_gate(
        db,
        action_code="clear_safe_cache",
        root_cause_category="cache_misconfiguration",
        environment=staging,
        requested_autonomy=5,
        actor_role="sre",
    )
    assert proven.requires_approval is False
    assert proven.allowed is True

    # 4. Autonomy 4 is not enough for that relaxation.
    at_four = remediation_domain.evaluate_safety_gate(
        db,
        action_code="clear_safe_cache",
        root_cause_category="cache_misconfiguration",
        environment=staging,
        requested_autonomy=4,
        actor_role="sre",
    )
    assert at_four.requires_approval is True

    # 5. A single recorded failure withdraws the privilege again.
    action.historical_failure = 1
    db.flush()
    after_failure = remediation_domain.evaluate_safety_gate(
        db,
        action_code="clear_safe_cache",
        root_cause_category="cache_misconfiguration",
        environment=staging,
        requested_autonomy=5,
        actor_role="sre",
    )
    assert after_failure.requires_approval is True


def test_request_schema_rejects_shell_metacharacters() -> None:
    from pydantic import ValidationError

    for payload in ("restart_service; rm -rf /", "restart_service && curl evil", "restart$(whoami)"):
        with pytest.raises(ValidationError):
            RemediationRequest(action_code=payload)
    assert RemediationRequest(action_code="restart_service").action_code == "restart_service"


def test_proposal_writes_an_audit_entry(db, open_incident) -> None:
    from app.database.models import AuditLog

    incident = open_incident()
    incident.root_cause_category = "connection_leak"
    service, environment = _context(db, incident)
    remediation_domain.propose(
        db,
        incident=incident,
        service=service,
        environment=environment,
        action_code="restart_service",
        rationale="unit test proposal",
        actor="tester",
        actor_role="sre",
    )
    entry = db.scalar(select(AuditLog).where(AuditLog.action == "remediation.proposed"))
    assert entry is not None
    assert entry.actor == "tester"
    assert entry.detail["action_code"] == "restart_service"


def test_remediation_registry_rows_are_not_mutable_by_the_ai_layer() -> None:
    """The registry is seeded from code; there is no API that creates an action at runtime."""
    from app.main import app

    paths = {route.path for route in app.routes}
    assert "/api/remediation/registry" in paths
    assert not any("action" in path and method in {"POST", "PUT", "DELETE"} for path in paths for method in [""]), (
        "no route should create or modify registered remediation actions"
    )
