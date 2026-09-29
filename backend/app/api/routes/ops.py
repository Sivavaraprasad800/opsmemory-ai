"""Operational routes: platform health, the dashboard overview, analytics, governance and the
simulator controls used to stage the demo."""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Query, Request
from sqlalchemy import select

from app.api.deps import DbDep, EngineDep, PrincipalDep, rate_limit_write
from app.api.serializers import (
    deployment_payload,
    incident_summary,
    investigation_payload,
    postmortem_payload,
    service_brief,
)
from app.core.config import settings
from app.core.errors import NotFoundError
from app.database.models import (
    AiInvestigation,
    AuditLog,
    Deployment,
    Incident,
    IntegrationConnection,
    LearningEvent,
    Postmortem,
    RemediationAction,
    Service,
)
from app.domain import postmortems as postmortem_domain
from app.domain.incidents import service_dependency_names
from app.ingest.service import latest_gauges
from app.domain.patterns import (
    detect_patterns,
    period_analysis,
    remediation_reliability,
    service_fragility,
)
from app.memory.service import get_memory_service
from app.schemas.api import (
    AdvanceRequest,
    DeployRequest,
    FaultRequest,
    PostmortemUpdateRequest,
)
from app.sim.engine import SimulationEngine, sim_now, world

router = APIRouter(prefix="/api", tags=["ops"])


# ---------------------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------------------
@router.get("/health")
def health(db: DbDep) -> dict:
    memory = get_memory_service()
    try:
        db.execute(select(1))
        database_ok = True
        database_detail = "connected"
    except Exception as exc:  # noqa: BLE001
        database_ok = False
        database_detail = str(exc)

    memory_status = memory.status()
    return {
        "status": "ok" if database_ok and not memory_status.degraded else "degraded",
        "sim_time": sim_now().isoformat(),
        "checks": {
            "database": {"ok": database_ok, "detail": database_detail},
            "memory": memory_status.to_dict(),
            "llm": {
                "configured": settings.llm_enabled,
                "model": settings.openai_model if settings.llm_enabled else None,
                "mode": "llm" if settings.llm_enabled else "offline-deterministic",
            },
        },
        "states": [
            state
            for state, active in (
                ("AI_UNAVAILABLE", not settings.llm_enabled),
                ("MEMORY_DEGRADED", memory_status.degraded),
            )
            if active
        ],
    }


@router.get("/system/overview")
def overview(principal: PrincipalDep, db: DbDep) -> dict:
    """Everything the Overview page needs, in one round trip."""
    principal.require("read")
    window_start = sim_now() - timedelta(hours=24)

    active = db.scalars(
        select(Incident)
        .where(Incident.status.notin_(["verified", "closed"]), Incident.is_historical.is_(False))
        .order_by(Incident.detected_at.desc())
        .limit(10)
    ).all()
    recent_incidents = db.scalars(
        select(Incident)
        .where(Incident.detected_at >= window_start)
        .order_by(Incident.detected_at.desc())
        .limit(8)
    ).all()
    investigations = db.scalars(
        select(AiInvestigation).order_by(AiInvestigation.id.desc()).limit(3)
    ).all()
    deployments = db.scalars(
        select(Deployment).order_by(Deployment.started_at.desc()).limit(8)
    ).all()
    learning = db.scalars(select(LearningEvent).order_by(LearningEvent.created_at.desc()).limit(8)).all()
    patterns = detect_patterns(db, days=30)

    services = db.scalars(select(Service).order_by(Service.name)).all()
    memory_status = get_memory_service().status()

    return {
        "sim_time": sim_now().isoformat(),
        "running": world().running,
        "status": {
            "active_incidents": len(active),
            "severity_counts": _counts(row.severity for row in active),
            "memory": memory_status.to_dict(),
            "reasoning_mode": "llm" if settings.llm_enabled else "offline",
            "autonomy_level": settings.max_autonomy_level,
        },
        "active_incidents": [incident_summary(db, row) for row in active],
        "recent_incidents": [incident_summary(db, row) for row in recent_incidents],
        "investigations": [investigation_payload(db, row) for row in investigations],
        "deployments": [deployment_payload(db, row) for row in deployments],
        "top_pattern": patterns[0].to_dict() if patterns else None,
        "learning_activity": [
            {
                "id": row.id,
                "kind": row.kind,
                "summary": row.summary,
                "incident_id": row.incident_id,
                "created_at": row.created_at.isoformat(),
            }
            for row in learning
        ],
        "services": [
            {
                **service_brief(db, service.id),
                "gauges": _safe_gauges(db, service.name),
            }
            for service in services
        ],
        "active_faults": {
            name: spec.kind for name, spec in SimulationEngine(db).active_faults().items()
        },
    }


def _safe_gauges(db, service_name: str) -> dict:
    try:
        if settings.real_clock:
            # Real-clock mode: report the connected system's own most recent sample. Deriving
            # the gauge from a synthetic runtime here would draw a healthy, plausible chart for
            # a service that has sent nothing at all - the one failure a monitoring dashboard
            # must never have.
            return latest_gauges(db, service_name)
        return SimulationEngine(db).gauges(service_name)
    except Exception:  # noqa: BLE001
        return {}


def _counts(values) -> dict[str, int]:
    out: dict[str, int] = {}
    for value in values:
        out[value] = out.get(value, 0) + 1
    return out


@router.get("/system/events")
def system_events(principal: PrincipalDep, db: DbDep, limit: int = Query(default=50, ge=1, le=500)) -> dict:
    principal.require("read")
    rows = db.scalars(select(LearningEvent).order_by(LearningEvent.created_at.desc()).limit(limit)).all()
    return {
        "events": [
            {
                "id": row.id,
                "kind": row.kind,
                "summary": row.summary,
                "incident_id": row.incident_id,
                "created_at": row.created_at.isoformat(),
            }
            for row in rows
        ]
    }


# ---------------------------------------------------------------------------------------
# Services and deployments
# ---------------------------------------------------------------------------------------
@router.get("/services")
def list_services(principal: PrincipalDep, db: DbDep) -> dict:
    principal.require("read")
    engine = SimulationEngine(db)
    services = db.scalars(select(Service).order_by(Service.name)).all()
    return {
        "services": [
            {
                **service_brief(db, service.id),
                "description": service.description,
                "language": service.language,
                "gauges": _safe_gauges(db, service.name),
                "active_fault": (engine.active_fault(service.name).kind if engine.active_fault(service.name) else None),
                "open_incidents": len(
                    db.scalars(
                        select(Incident).where(
                            Incident.service_id == service.id,
                            Incident.status.notin_(["verified", "closed"]),
                        )
                    ).all()
                ),
            }
            for service in services
        ]
    }


@router.get("/services/{service_name}")
def get_service(principal: PrincipalDep, db: DbDep, service_name: str) -> dict:
    principal.require("read")
    service = db.scalar(select(Service).where(Service.name == service_name))
    if service is None:
        raise NotFoundError(f"service '{service_name}' not found")
    engine = SimulationEngine(db)
    metric_windows = {
        metric: [round(v, 4) for _, v in engine.metric_window(service_name=service.name, metric=metric, seconds=1800)]
        for metric in ("error_rate", "latency_ms", "connection_saturation", "cpu_pct", "mem_pct", "disk_pct")
    }
    return {
        "service": {**service_brief(db, service.id), "description": service.description},
        "gauges": _safe_gauges(db, service.name),
        "metric_windows": metric_windows,
        "signatures": [s.to_dict() for s in engine.recent_signatures(service_name=service.name)][:10],
        "recent_deployments": [
            deployment_payload(db, row)
            for row in engine.recent_deployments(service_name=service.name, seconds=86400, limit=5)
        ],
        "incidents": [
            incident_summary(db, row)
            for row in db.scalars(
                select(Incident)
                .where(Incident.service_id == service.id)
                .order_by(Incident.detected_at.desc())
                .limit(10)
            ).all()
        ],
        "dependencies": service_dependency_names(db, service),
    }


@router.get("/deployments")
def list_deployments(
    principal: PrincipalDep, db: DbDep, limit: int = Query(default=50, ge=1, le=200)
) -> dict:
    principal.require("read")
    rows = db.scalars(select(Deployment).order_by(Deployment.started_at.desc()).limit(limit)).all()
    return {"deployments": [deployment_payload(db, row) for row in rows]}


# ---------------------------------------------------------------------------------------
# Analytics
# ---------------------------------------------------------------------------------------
@router.get("/analysis/period")
def analysis_period(principal: PrincipalDep, db: DbDep, days: int = Query(default=30, ge=1, le=365)) -> dict:
    principal.require("read")
    return period_analysis(db, days=days)


@router.get("/analysis/patterns")
def analysis_patterns(principal: PrincipalDep, db: DbDep, days: int = Query(default=30, ge=1, le=365)) -> dict:
    principal.require("read")
    patterns = detect_patterns(db, days=days)
    return {
        "window_days": days,
        "minimum_occurrences": 3,
        "count": len(patterns),
        "patterns": [p.to_dict() for p in patterns],
    }


@router.get("/analysis/reliability")
def analysis_reliability(principal: PrincipalDep, db: DbDep, days: int = Query(default=90, ge=1, le=365)) -> dict:
    principal.require("read")
    return {"window_days": days, "actions": remediation_reliability(db, days=days)}


@router.get("/analysis/fragility")
def analysis_fragility(principal: PrincipalDep, db: DbDep, days: int = Query(default=30, ge=1, le=365)) -> dict:
    principal.require("read")
    return {"window_days": days, "services": service_fragility(db, days=days)}


# ---------------------------------------------------------------------------------------
# Remediation registry
# ---------------------------------------------------------------------------------------
@router.get("/remediation/registry")
def remediation_registry(principal: PrincipalDep, db: DbDep) -> dict:
    principal.require("read")
    rows = db.scalars(select(RemediationAction).order_by(RemediationAction.risk_level, RemediationAction.code)).all()
    return {
        "actions": [
            {
                "id": row.id,
                "code": row.code,
                "name": row.name,
                "description": row.description,
                "risk_level": row.risk_level,
                "required_approval": row.required_approval,
                "allowed_environments": row.allowed_environments,
                "applicable_categories": row.applicable_categories,
                "timeout_seconds": row.timeout_seconds,
                "max_retries": row.max_retries,
                "is_enabled": row.is_enabled,
                "executes_shell": row.executes_shell,
                "historical_success": row.historical_success,
                "historical_failure": row.historical_failure,
                "execution_workflow": row.execution_workflow,
                "verification_workflow": row.verification_workflow,
                "rollback_workflow": row.rollback_workflow,
            }
            for row in rows
        ],
        "note": (
            "This registry is the complete set of actions the AI may choose from. Every entry has "
            "executes_shell = false; there is no shell execution path in the platform."
        ),
    }


# ---------------------------------------------------------------------------------------
# Postmortems
# ---------------------------------------------------------------------------------------
@router.get("/postmortems")
def list_postmortems(principal: PrincipalDep, db: DbDep, limit: int = Query(default=50, ge=1, le=200)) -> dict:
    principal.require("read")
    rows = db.scalars(select(Postmortem).order_by(Postmortem.updated_at.desc()).limit(limit)).all()
    return {"count": len(rows), "postmortems": [postmortem_payload(row) for row in rows]}


@router.get("/postmortems/{postmortem_id}")
def get_postmortem(principal: PrincipalDep, db: DbDep, postmortem_id: int) -> dict:
    principal.require("read")
    row = db.get(Postmortem, postmortem_id)
    if row is None:
        raise NotFoundError(f"postmortem {postmortem_id} not found")
    return {"postmortem": postmortem_payload(row), "incident": incident_summary(db, db.get(Incident, row.incident_id))}


@router.patch("/postmortems/{postmortem_id}")
def patch_postmortem(
    principal: PrincipalDep, db: DbDep, postmortem_id: int, payload: PostmortemUpdateRequest, request: Request
) -> dict:
    principal.require("edit_postmortem")
    rate_limit_write(principal, request)
    row = db.get(Postmortem, postmortem_id)
    if row is None:
        raise NotFoundError(f"postmortem {postmortem_id} not found")
    updated = postmortem_domain.update_postmortem(
        db, postmortem=row, actor=principal.actor, actor_role=principal.role, payload=payload.model_dump(exclude_none=True)
    )
    db.commit()
    return {"postmortem": postmortem_payload(updated)}


@router.post("/postmortems/{postmortem_id}/approve")
def approve_postmortem(principal: PrincipalDep, db: DbDep, postmortem_id: int, request: Request) -> dict:
    principal.require("finalize_postmortem")
    rate_limit_write(principal, request)
    row = db.get(Postmortem, postmortem_id)
    if row is None:
        raise NotFoundError(f"postmortem {postmortem_id} not found")
    result = postmortem_domain.approve_postmortem(
        db, postmortem=row, actor=principal.actor, actor_role=principal.role
    )
    db.commit()
    return result


# ---------------------------------------------------------------------------------------
# Investigations
# ---------------------------------------------------------------------------------------
@router.get("/investigations")
def list_investigations(
    principal: PrincipalDep,
    db: DbDep,
    status: str | None = Query(default=None, max_length=40),
    memory_used: bool | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=300),
) -> dict:
    """Every reasoning run, newest first, with the incident it belongs to.

    The per-incident route answers "what did the agent conclude about this incident". Reviewing a
    platform needs the other question: "what has the agent been doing, and how often did it have
    prior experience to work from".
    """
    principal.require("read")
    stmt = select(AiInvestigation)
    if status:
        stmt = stmt.where(AiInvestigation.status == status)
    if memory_used is not None:
        stmt = stmt.where(AiInvestigation.memory_recall_used.is_(memory_used))
    rows = db.scalars(stmt.order_by(AiInvestigation.id.desc()).limit(limit)).all()

    investigations: list[dict] = []
    for row in rows:
        incident = db.get(Incident, row.incident_id)
        investigations.append(
            {
                **investigation_payload(db, row),
                "incident": incident_summary(db, incident) if incident is not None else None,
            }
        )
    return {
        "count": len(investigations),
        "investigations": investigations,
    }


# ---------------------------------------------------------------------------------------
# Governance
# ---------------------------------------------------------------------------------------
@router.get("/audit")
def audit_log(
    principal: PrincipalDep,
    db: DbDep,
    action: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict:
    principal.require("read")
    stmt = select(AuditLog)
    if action:
        stmt = stmt.where(AuditLog.action == action)
    rows = db.scalars(stmt.order_by(AuditLog.created_at.desc()).limit(limit)).all()
    return {
        "count": len(rows),
        "entries": [
            {
                "id": row.id,
                "created_at": row.created_at.isoformat(),
                "actor": row.actor,
                "actor_role": row.actor_role,
                "action": row.action,
                "target_type": row.target_type,
                "target_id": row.target_id,
                "result": row.result,
                "reason": row.reason,
                "detail": row.detail,
                "ip_address": row.ip_address,
            }
            for row in rows
        ],
    }


@router.get("/integrations")
def integrations(principal: PrincipalDep, db: DbDep) -> dict:
    principal.require("read")
    rows = db.scalars(select(IntegrationConnection).order_by(IntegrationConnection.kind)).all()
    memory = get_memory_service()
    return {
        "integrations": [
            {
                "id": row.id,
                "kind": row.kind,
                "name": row.name,
                "base_url": row.base_url,
                "status": row.status,
                "has_secret": row.has_secret,
                "config": row.config,
                "last_checked_at": row.last_checked_at.isoformat() if row.last_checked_at else None,
                "last_error": row.last_error,
            }
            for row in rows
        ],
        "runtime": {
            "memory": memory.status().to_dict(),
            "llm_configured": settings.llm_enabled,
            "llm_model": settings.openai_model,
            "hindsight_configured": bool(settings.hindsight_base_url),
            "hindsight_bank": settings.hindsight_bank_id,
        },
        "secrets_exposed_to_browser": [],
    }


# ---------------------------------------------------------------------------------------
# Simulator control
# ---------------------------------------------------------------------------------------
@router.get("/sim/state")
def sim_state(principal: PrincipalDep, db: DbDep) -> dict:
    principal.require("read")
    engine = SimulationEngine(db)
    engine.load_catalogue()
    return engine.world_state()


@router.post("/sim/advance")
def sim_advance(principal: PrincipalDep, db: DbDep, engine: EngineDep, payload: AdvanceRequest, request: Request) -> dict:
    principal.require("simulate_fault")
    rate_limit_write(principal, request)
    report = engine.advance(payload.seconds)
    db.commit()
    return report


@router.post("/sim/fault")
def sim_fault(principal: PrincipalDep, db: DbDep, engine: EngineDep, payload: FaultRequest, request: Request) -> dict:
    principal.require("simulate_fault")
    rate_limit_write(principal, request)
    spec = engine.inject_fault(
        scenario=payload.scenario,
        service_name=payload.service,
        environment=payload.environment,
        severity=payload.severity,
    )
    db.commit()
    return {"fault": spec.to_dict()}


@router.post("/sim/deploy")
def sim_deploy(principal: PrincipalDep, db: DbDep, engine: EngineDep, payload: DeployRequest, request: Request) -> dict:
    principal.require("simulate_fault")
    rate_limit_write(principal, request)
    deployment = engine.inject_deployment(
        service_name=payload.service,
        environment=payload.environment,
        version=payload.version,
        commit_message=payload.commit_message,
        author=payload.author,
        change_class=payload.change_class,
        file_path=payload.file_path,
        with_scenario=payload.with_scenario,
    )
    db.commit()
    return {"deployment": deployment_payload(db, deployment)}


@router.post("/sim/pause")
def sim_pause(principal: PrincipalDep, db: DbDep) -> dict:
    principal.require("simulate_fault")
    world().running = False
    return {"running": False}


@router.post("/sim/resume")
def sim_resume(principal: PrincipalDep, db: DbDep) -> dict:
    principal.require("simulate_fault")
    world().running = True
    return {"running": True}


@router.get("/scenarios")
def scenarios(principal: PrincipalDep) -> dict:
    principal.require("read")
    from app.sim.faults import SCENARIOS

    return {
        "scenarios": [
            {
                "key": key,
                "kind": spec.kind,
                "root_cause_category": spec.root_cause_category,
                "severity": spec.severity,
                "description": spec.description,
                "canonical_actions": spec.canonical_actions,
                "symptoms": spec.symptoms,
            }
            for key, spec in SCENARIOS.items()
        ]
    }
