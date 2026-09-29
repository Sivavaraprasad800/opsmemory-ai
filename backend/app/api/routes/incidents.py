"""Incident lifecycle routes."""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Query, Request
from sqlalchemy import select

from app.ai_engine.orchestrator import IncidentOrchestrator
from app.api.deps import DbDep, EngineDep, PrincipalDep, rate_limit_expensive, rate_limit_write
from app.api.serializers import (
    incident_detail,
    incident_summary,
    investigation_payload,
    remediation_run_payload,
)
from app.core.config import settings
from app.core.errors import ActionBlockedError, NotFoundError, ValidationError
from app.database.models import (
    AiInvestigation,
    Environment,
    Incident,
    RemediationRun,
    Service,
    utcnow,
)
from app.domain import remediation as remediation_domain
from app.domain.incidents import detect_and_open_incidents
from app.schemas.api import ApproveRequest, InvestigateRequest, RemediationRequest
from app.sim.engine import sim_now

router = APIRouter(prefix="/api/incidents", tags=["incidents"])


def _get_incident(db, incident_id: int) -> Incident:
    incident = db.get(Incident, incident_id)
    if incident is None:
        raise NotFoundError(f"incident {incident_id} not found")
    return incident


@router.get("")
def list_incidents(
    principal: PrincipalDep,
    db: DbDep,
    status: str | None = Query(default=None),
    service: str | None = Query(default=None),
    days: int = Query(default=30, ge=1, le=365),
    include_historical: bool = Query(default=False),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict:
    principal.require("read")
    stmt = select(Incident).where(Incident.detected_at >= utcnow() - timedelta(days=days))
    if status:
        stmt = stmt.where(Incident.status == status)
    if not include_historical:
        stmt = stmt.where(Incident.is_historical.is_(False))
    if service:
        service_row = db.scalar(select(Service).where(Service.name == service))
        if service_row is None:
            raise NotFoundError(f"service '{service}' not found")
        stmt = stmt.where(Incident.service_id == service_row.id)
    incidents = db.scalars(stmt.order_by(Incident.detected_at.desc()).limit(limit)).all()
    return {
        "count": len(incidents),
        "sim_time": sim_now().isoformat(),
        "incidents": [incident_summary(db, row) for row in incidents],
    }


@router.post("/detect")
def run_detection(principal: PrincipalDep, db: DbDep, engine: EngineDep, request: Request) -> dict:
    """Run one detection pass. Idempotent with respect to open incidents (they absorb repeats)."""
    principal.require("investigate")
    rate_limit_write(principal, request)
    opened = detect_and_open_incidents(db, engine)
    db.commit()
    return {
        "opened": [incident_summary(db, row) for row in opened],
        "opened_count": len(opened),
        "sim_time": sim_now().isoformat(),
    }


@router.get("/{incident_id}")
def get_incident(principal: PrincipalDep, db: DbDep, incident_id: int) -> dict:
    principal.require("read")
    return incident_detail(db, _get_incident(db, incident_id))


@router.get("/{incident_id}/timeline")
def get_timeline(principal: PrincipalDep, db: DbDep, incident_id: int) -> dict:
    principal.require("read")
    incident = _get_incident(db, incident_id)
    return {"incident_id": incident.id, "timeline": incident_detail(db, incident)["timeline"]}


@router.get("/{incident_id}/investigation")
def latest_investigation(principal: PrincipalDep, db: DbDep, incident_id: int) -> dict:
    principal.require("read")
    _get_incident(db, incident_id)
    investigation = db.scalar(
        select(AiInvestigation)
        .where(AiInvestigation.incident_id == incident_id)
        .order_by(AiInvestigation.id.desc())
    )
    if investigation is None:
        return {"incident_id": incident_id, "investigation": None}
    return {"incident_id": incident_id, "investigation": investigation_payload(db, investigation)}


@router.post("/{incident_id}/investigate")
def investigate(
    principal: PrincipalDep,
    db: DbDep,
    engine: EngineDep,
    incident_id: int,
    payload: InvestigateRequest,
    request: Request,
) -> dict:
    principal.require("investigate")
    rate_limit_expensive(principal, request)
    incident = _get_incident(db, incident_id)
    if incident.status in {"verified", "closed"}:
        raise ValidationError(
            "This incident is already verified; investigating again would overwrite a closed record",
            detail={"status": incident.status},
        )
    orchestrator = IncidentOrchestrator(
        db,
        engine,
        incident,
        autonomy_level=payload.autonomy_level,
        actor=principal.actor,
        actor_role=principal.role,
    )
    result = orchestrator.investigate(execute=payload.execute, auto_approve=payload.auto_approve)
    db.commit()
    return {"result": result.to_dict(), "incident": incident_summary(db, incident)}


@router.post("/{incident_id}/remediation")
def propose_remediation(
    principal: PrincipalDep,
    db: DbDep,
    engine: EngineDep,
    incident_id: int,
    payload: RemediationRequest,
    request: Request,
) -> dict:
    """Operator-initiated remediation.

    This is the human-assisted path: an engineer can choose any *registered* action, including
    one the AI advised against. If it fails verification, the failure is recorded and retained,
    which is precisely how the platform builds failure memory.
    """
    principal.require("propose_remediation")
    rate_limit_write(principal, request)
    incident = _get_incident(db, incident_id)
    service = db.get(Service, incident.service_id)
    environment = db.get(Environment, incident.environment_id)
    assert service is not None and environment is not None

    action = remediation_domain.get_action(db, payload.action_code)
    if action is None:
        raise ActionBlockedError(
            f"'{payload.action_code}' is not a registered remediation action",
            reasons=["ACTION_NOT_REGISTERED"],
        )

    run = remediation_domain.propose(
        db,
        incident=incident,
        service=service,
        environment=environment,
        action_code=payload.action_code,
        rationale=payload.rationale or f"initiated by {principal.actor}",
        parameters=payload.parameters,
        autonomy_level=payload.autonomy_level,
        actor=principal.actor,
        actor_role=principal.role,
    )
    db.commit()
    return {"run": remediation_run_payload(db, run)}


@router.post("/{incident_id}/remediation/{run_id}/approve")
def approve_remediation(
    principal: PrincipalDep,
    db: DbDep,
    incident_id: int,
    run_id: int,
    payload: ApproveRequest,
    request: Request,
) -> dict:
    principal.require("approve_remediation")
    rate_limit_write(principal, request)
    incident = _get_incident(db, incident_id)
    run = db.get(RemediationRun, run_id)
    if run is None or run.incident_id != incident.id:
        raise NotFoundError(f"remediation run {run_id} not found for incident {incident_id}")
    remediation_domain.approve(db, run=run, actor=principal.actor, actor_role=principal.role, reason=payload.reason)
    db.commit()
    return {"run": remediation_run_payload(db, run)}


@router.post("/{incident_id}/remediation/{run_id}/execute")
def execute_remediation(
    principal: PrincipalDep,
    db: DbDep,
    engine: EngineDep,
    incident_id: int,
    run_id: int,
    request: Request,
) -> dict:
    """Execute an approved run, then verify it and continue the learning loop."""
    principal.require("execute_remediation")
    rate_limit_expensive(principal, request)
    incident = _get_incident(db, incident_id)
    run = db.get(RemediationRun, run_id)
    if run is None or run.incident_id != incident.id:
        raise NotFoundError(f"remediation run {run_id} not found for incident {incident_id}")

    orchestrator = IncidentOrchestrator(
        db, engine, incident, actor=principal.actor, actor_role=principal.role
    )
    result = orchestrator.continue_after_approval(
        run_id=run.id, actor=principal.actor, actor_role=principal.role
    )
    db.commit()
    return {
        "result": result.to_dict(),
        "incident": incident_summary(db, incident),
        "run": remediation_run_payload(db, db.get(RemediationRun, run.id)),
    }


@router.get("/{incident_id}/similar")
def similar_incidents(
    principal: PrincipalDep, db: DbDep, incident_id: int, top_k: int = Query(default=5, ge=1, le=25)
) -> dict:
    """Explainable similarity ranking for one incident (used by the Memory and Investigation pages)."""
    principal.require("read")
    from app.detection.similarity import IncidentFingerprint, rank_neighbours
    from app.domain.incidents import fingerprint_candidates

    incident = _get_incident(db, incident_id)
    current = IncidentFingerprint.from_dict(incident.fingerprint)
    if not incident.fingerprint:
        raise ValidationError("This incident has no fingerprint yet; run detection or investigation first")
    candidates = fingerprint_candidates(db, exclude_incident_id=incident.id)
    neighbours = rank_neighbours(current, candidates, top_k=top_k)
    return {
        "incident_id": incident.id,
        "current_fingerprint": current.to_dict(),
        "neighbours": [n.to_dict() for n in neighbours],
    }
