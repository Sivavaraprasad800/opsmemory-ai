"""Serializers. Explicit dicts rather than generic introspection, so it is always obvious what
a client receives and that no secret field can leak into a response by accident."""

from __future__ import annotations

from typing import Any, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database.models import (
    AiDecision,
    AiInvestigation,
    AiToolCall,
    Deployment,
    Environment,
    Incident,
    IncidentEvidence,
    IncidentHypothesis,
    IncidentEvent,
    LearningEvent,
    MemoryReference,
    Postmortem,
    RemediationAction,
    RemediationRun,
    Service,
    VerificationResult,
    VerificationRun,
)


def service_brief(db: Session, service_id: int) -> dict[str, Any]:
    service = db.get(Service, service_id)
    if service is None:
        return {"id": service_id, "name": "unknown"}
    return {
        "id": service.id,
        "name": service.name,
        "tier": service.tier,
        "kind": service.kind,
        "owner_team": service.owner_team,
        "fragility_score": service.fragility_score,
    }


def incident_summary(db: Session, incident: Incident) -> dict[str, Any]:
    service = db.get(Service, incident.service_id)
    environment = db.get(Environment, incident.environment_id)
    return {
        "id": incident.id,
        "title": incident.title,
        "service": service.name if service else "unknown",
        "service_id": incident.service_id,
        "environment": environment.name if environment else "unknown",
        "environment_id": incident.environment_id,
        "severity": incident.severity,
        "status": incident.status,
        "root_cause_category": incident.root_cause_category,
        "root_cause_summary": incident.root_cause_summary,
        "symptom": incident.symptom,
        "detected_at": incident.detected_at.isoformat(),
        "resolved_at": incident.resolved_at.isoformat() if incident.resolved_at else None,
        "verified_at": incident.verified_at.isoformat() if incident.verified_at else None,
        "mttr_seconds": incident.mttr_seconds,
        "is_historical": incident.is_historical,
        "suspected_deployment_id": incident.suspected_deployment_id,
        "has_fingerprint": bool(incident.fingerprint),
    }


def incident_detail(db: Session, incident: Incident) -> dict[str, Any]:
    payload = incident_summary(db, incident)
    payload.update(
        {
            "fingerprint": incident.fingerprint,
            "baseline": incident.baseline,
            "impact": incident.impact,
            "detection_meta": incident.detection_meta,
            "evidence": [
                {
                    "id": row.id,
                    "kind": row.kind,
                    "summary": row.summary,
                    "detail": row.detail,
                    "source": row.source,
                    "strength": row.strength,
                    "retrieval": row.retrieval,
                    "collected_at": row.collected_at.isoformat(),
                }
                for row in db.scalars(
                    select(IncidentEvidence)
                    .where(IncidentEvidence.incident_id == incident.id)
                    .order_by(IncidentEvidence.id)
                ).all()
            ],
            "hypotheses": [
                {
                    "id": row.id,
                    "code": row.code,
                    "statement": row.statement,
                    "category": row.category,
                    "status": row.status,
                    "ranking": row.ranking,
                    "supporting": row.supporting,
                    "contradicting": row.contradicting,
                    "missing": row.missing,
                    "tests": [
                        {
                            "name": test.name,
                            "verdict": test.verdict,
                            "explanation": test.explanation,
                            "result": test.result,
                        }
                        for test in row.__dict__.get("_tests", []) or []
                    ],
                }
                for row in db.scalars(
                    select(IncidentHypothesis)
                    .where(IncidentHypothesis.incident_id == incident.id)
                    .order_by(IncidentHypothesis.ranking)
                ).all()
            ],
            "timeline": [
                {
                    "id": row.id,
                    "ts": row.ts.isoformat(),
                    "kind": row.kind,
                    "title": row.title,
                    "description": row.description,
                    "actor": row.actor,
                    "source": row.source,
                    "payload": row.payload,
                }
                for row in db.scalars(
                    select(IncidentEvent)
                    .where(IncidentEvent.incident_id == incident.id)
                    .order_by(IncidentEvent.ts, IncidentEvent.id)
                ).all()
            ],
            "investigations": [
                investigation_payload(db, row)
                for row in db.scalars(
                    select(AiInvestigation)
                    .where(AiInvestigation.incident_id == incident.id)
                    .order_by(AiInvestigation.id)
                ).all()
            ],
            "remediation_runs": [
                remediation_run_payload(db, row)
                for row in db.scalars(
                    select(RemediationRun)
                    .where(RemediationRun.incident_id == incident.id)
                    .order_by(RemediationRun.id)
                ).all()
            ],
            "verifications": [
                verification_payload(db, row)
                for row in db.scalars(
                    select(VerificationRun)
                    .where(VerificationRun.incident_id == incident.id)
                    .order_by(VerificationRun.id)
                ).all()
            ],
            "memory_references": [
                {
                    "id": row.id,
                    "scope": row.scope,
                    "bank_id": row.bank_id,
                    "memory_id": row.hindsight_memory_id,
                    "document_id": row.document_id,
                    "backend": row.backend,
                    "recall_count": row.recall_count,
                    "retained_at": row.retained_at.isoformat(),
                    "preview": row.content_preview[:400],
                }
                for row in db.scalars(
                    select(MemoryReference).where(MemoryReference.incident_id == incident.id)
                ).all()
            ],
            "postmortem": postmortem_payload(
                db.scalar(select(Postmortem).where(Postmortem.incident_id == incident.id))
            ),
        }
    )
    return payload


def verification_payload(db: Session, run: VerificationRun) -> dict[str, Any]:
    return {
        "id": run.id,
        "remediation_run_id": run.remediation_run_id,
        "status": run.status,
        "verdict": run.verdict,
        "verdict_reason": run.verdict_reason,
        "settle_seconds": run.settle_seconds,
        "started_at": run.started_at.isoformat(),
        "completed_at": run.completed_at.isoformat() if run.completed_at else None,
        "improvement_pct": run.improvement_pct,
        "before": run.before_state,
        "after": run.after_state,
        "checks": [
            {
                "check_name": row.check_name,
                "metric": row.metric,
                "before": row.before_value,
                "after": row.after_value,
                "baseline": row.baseline_value,
                "unit": row.unit,
                "passed": row.passed,
                "is_primary": row.is_primary,
                "detail": row.detail,
            }
            for row in db.scalars(
                select(VerificationResult)
                .where(VerificationResult.verification_run_id == run.id)
                .order_by(VerificationResult.id)
            ).all()
        ],
    }


def remediation_run_payload(db: Session, run: RemediationRun) -> dict[str, Any]:
    action = db.scalar(select(RemediationAction).where(RemediationAction.code == run.action_code))
    return {
        "id": run.id,
        "incident_id": run.incident_id,
        "action_code": run.action_code,
        "action_name": action.name if action else run.action_code,
        "risk_level": action.risk_level if action else "unknown",
        "autonomy_level": run.autonomy_level,
        "status": run.status,
        "attempt": run.attempt,
        "proposed_by": run.proposed_by,
        "proposed_at": run.proposed_at.isoformat(),
        "rationale": run.rationale,
        "parameters": run.parameters,
        "blocked_reasons": run.blocked_reasons,
        "safety_checks": run.safety_checks,
        "approved_by": run.approved_by,
        "approved_at": run.approved_at.isoformat() if run.approved_at else None,
        "executed_at": run.executed_at.isoformat() if run.executed_at else None,
        "result": run.result,
        "outcome": run.outcome,
    }


def investigation_payload(db: Session, investigation: AiInvestigation) -> dict[str, Any]:
    tool_calls = db.scalars(
        select(AiToolCall)
        .where(AiToolCall.investigation_id == investigation.id)
        .order_by(AiToolCall.seq)
    ).all()
    decisions = db.scalars(
        select(AiDecision)
        .where(AiDecision.investigation_id == investigation.id)
        .order_by(AiDecision.id)
    ).all()
    return {
        "id": investigation.id,
        "incident_id": investigation.incident_id,
        "status": investigation.status,
        "mode": investigation.mode,
        "model": investigation.model,
        "autonomy_level": investigation.autonomy_level,
        "round_count": investigation.round_count,
        "started_at": investigation.started_at.isoformat(),
        "completed_at": investigation.completed_at.isoformat() if investigation.completed_at else None,
        "duration_ms": round(investigation.duration_ms, 1),
        "prompt_tokens": investigation.prompt_tokens,
        "completion_tokens": investigation.completion_tokens,
        "degraded": investigation.degraded,
        "summary": investigation.summary,
        "root_cause_category": investigation.root_cause_category,
        "root_cause_statement": investigation.root_cause_statement,
        "alternatives_ruled_out": investigation.alternatives_ruled_out,
        "recommended_remediation": investigation.recommended_remediation,
        "recommendation_reason": investigation.recommendation_reason,
        "memory_recall_used": investigation.memory_recall_used,
        "memory_hits": investigation.memory_hits,
        "tool_call_count": len(tool_calls),
        "tool_calls": [
            {
                "seq": row.seq,
                "round": row.round_index,
                "tool": row.tool_name,
                "arguments": row.arguments,
                "result": row.result,
                "ok": row.ok,
                "error_code": row.error_code,
                "error_message": row.error_message,
                "duration_ms": round(row.duration_ms, 2),
                "decision": row.decision,
            }
            for row in tool_calls
        ],
        "decisions": [
            {
                "id": row.id,
                "stage": row.stage,
                "decision": row.decision,
                "rationale": row.rationale,
                "evidence_ids": row.evidence_ids,
                "memory_ids": row.memory_ids,
                "status_label": row.status_label,
                "created_at": row.created_at.isoformat(),
            }
            for row in decisions
        ],
    }


def deployment_payload(db: Session, deployment: Deployment) -> dict[str, Any]:
    service = db.get(Service, deployment.service_id)
    environment = db.get(Environment, deployment.environment_id)
    return {
        "id": deployment.id,
        "service": service.name if service else "unknown",
        "environment": environment.name if environment else "unknown",
        "version": deployment.version,
        "commit_sha": deployment.commit_sha,
        "commit_message": deployment.commit_message,
        "author": deployment.author,
        "status": deployment.status,
        "strategy": deployment.strategy,
        "started_at": deployment.started_at.isoformat(),
        "completed_at": deployment.completed_at.isoformat() if deployment.completed_at else None,
        "is_suspected_cause": deployment.is_suspected_cause,
        "risk_score": deployment.risk_score,
        "change_class": (deployment.meta or {}).get("change_class"),
    }


def postmortem_payload(postmortem: Postmortem | None) -> dict[str, Any] | None:
    if postmortem is None:
        return None
    return {
        "id": postmortem.id,
        "incident_id": postmortem.incident_id,
        "status": postmortem.status,
        "summary": postmortem.summary,
        "impact": postmortem.impact,
        "detection": postmortem.detection,
        "root_cause": postmortem.root_cause,
        "timeline": postmortem.timeline,
        "evidence": postmortem.evidence,
        "hypotheses": postmortem.hypotheses,
        "failed_attempts": postmortem.failed_attempts,
        "successful_remediation": postmortem.successful_remediation,
        "verification": postmortem.verification,
        "deployment_relationship": postmortem.deployment_relationship,
        "lessons": postmortem.lessons,
        "preventive_actions": postmortem.preventive_actions,
        "authored_by": postmortem.authored_by,
        "approved_by": postmortem.approved_by,
        "approved_at": postmortem.approved_at.isoformat() if postmortem.approved_at else None,
        "created_at": postmortem.created_at.isoformat(),
        "updated_at": postmortem.updated_at.isoformat(),
    }


def learning_event_payload(row: LearningEvent) -> dict[str, Any]:
    return {
        "id": row.id,
        "incident_id": row.incident_id,
        "kind": row.kind,
        "summary": row.summary,
        "payload": row.payload,
        "memory_ids": row.memory_ids,
        "created_at": row.created_at.isoformat(),
    }


def hypothesis_payload(db: Session, row: IncidentHypothesis) -> dict[str, Any]:
    return {
        "id": row.id,
        "code": row.code,
        "statement": row.statement,
        "category": row.category,
        "status": row.status,
        "ranking": row.ranking,
        "supporting": row.supporting,
        "contradicting": row.contradicting,
        "missing": row.missing,
    }


def serialize_many(items: Sequence[Any], fn) -> list[dict[str, Any]]:  # pragma: no cover - helper
    return [fn(item) for item in items]
