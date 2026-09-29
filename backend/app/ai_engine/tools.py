"""The controlled AI tool layer.

Build spec section 6: strongly typed tools, backend-executed, every call recorded. The model
never touches the database and never runs a command; it may only *request* one of these tools,
and this registry is the single place that decides whether the request is legitimate.

Every invocation — successful or not — is written to ``ai_tool_calls`` with its arguments,
result, duration and the decision it informed. That ledger is what the AI Investigation page
renders, which is how the platform satisfies "show tool activity, do not expose hidden
chain-of-thought" (spec section 45).

Only tools that actually exist are exposed; the registry *is* the capability list.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Callable, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database.models import (
    AiToolCall,
    ConfigurationChange,
    Environment,
    Incident,
    IncidentEvidence,
    RemediationAction,
    Runbook,
    Service,
)
from app.detection.logs import aggregate_signatures
from app.detection.metrics import detect_anomaly
from app.detection.similarity import (
    IncidentFingerprint,
    compare_fingerprints,
    rank_neighbours,
)
from app.domain.incidents import (
    METRIC_LABELS,
    WATCHED_METRICS,
    build_what_changed,
    detect_anomalies,
    fingerprint_candidates,
    service_dependency_names,
)
from app.domain.remediation import list_actions, remediation_history_for_root_cause
from app.memory.service import MemoryService
from app.sim.engine import SimulationEngine, sim_now

logger = logging.getLogger(__name__)


@dataclass
class ToolContext:
    """Everything a tool is allowed to see. Tools are pure functions of this context."""

    db: Session
    engine: SimulationEngine
    incident: Incident
    service: Service
    environment: Environment
    memory: MemoryService
    investigation_id: int | None = None
    allowed_tools: set[str] | None = None

    def allows(self, name: str) -> bool:
        return self.allowed_tools is None or name in self.allowed_tools


@dataclass
class ToolResult:
    ok: bool
    data: dict[str, Any] = field(default_factory=dict)
    error_code: str | None = None
    error_message: str = ""
    duration_ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "data": self.data,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "duration_ms": round(self.duration_ms, 2),
        }


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[[ToolContext, dict[str, Any]], dict[str, Any]]

    def openai_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


# ---------------------------------------------------------------------------------------
# Individual tools
# ---------------------------------------------------------------------------------------
def _get_incident(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    incident = ctx.incident
    return {
        "incident_id": incident.id,
        "title": incident.title,
        "service": ctx.service.name,
        "environment": ctx.environment.name,
        "severity": incident.severity,
        "status": incident.status,
        "symptom": incident.symptom,
        "detected_at": incident.detected_at.isoformat(),
        "sim_time": sim_now().isoformat(),
        "detection": incident.detection_meta or {},
        "baseline": incident.baseline or {},
        "fingerprint": incident.fingerprint or {},
    }


def _get_current_metrics(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    metrics = args.get("metrics") or [m for m, _, _ in WATCHED_METRICS]
    window = float(args.get("window_seconds") or 900)
    series = ctx.engine.metric_series(service_name=ctx.service.name, metrics=metrics, seconds=window)
    summary = {}
    for metric, values in series.items():
        if not values:
            summary[metric] = {"samples": 0}
            continue
        summary[metric] = {
            "samples": len(values),
            "current": round(values[-1], 4),
            "min": round(min(values), 4),
            "max": round(max(values), 4),
            "mean": round(sum(values) / len(values), 4),
            "baseline_median": round(sorted(values[: max(1, len(values) // 3)])[max(0, len(values) // 6)], 4),
        }
    return {"window_seconds": window, "metrics": summary}


def _get_metric_anomalies(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    verdicts = detect_anomalies(ctx.engine, service_name=ctx.service.name)
    return {
        "detector_committee": "robust_z(MAD), rolling_median, ewma, cusum",
        "anomalies": {
            metric: {
                "anomalous": verdict.anomalous,
                "severity": verdict.severity,
                "direction": verdict.direction,
                "score": verdict.score,
                "current": verdict.current,
                "baseline": verdict.baseline,
                "relative_change": verdict.relative_change,
                "change_point_index": verdict.change_point_index,
                "detectors": [d.to_dict() for d in verdict.detectors],
                "reason": verdict.reason,
            }
            for metric, verdict in verdicts.items()
        },
    }


def _get_current_logs(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    window = float(args.get("window_seconds") or 900)
    level = args.get("level")
    limit = int(args.get("limit") or 40)
    events = ctx.engine.recent_logs(
        service_name=ctx.service.name, seconds=window, level=level, limit=max(limit * 3, 120)
    )
    error_count = ctx.engine.error_count(service_name=ctx.service.name, seconds=window)
    return {
        "window_seconds": window,
        "total_events_sampled": len(events),
        "error_events": error_count,
        "sample_lines": [f"{e['ts'][11:19]} {e['level']:5s} {e['message'][:220]}" for e in events[-limit:]],
    }


def _get_error_signatures(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    window = float(args.get("window_seconds") or 1800)
    signatures = ctx.engine.recent_signatures(service_name=ctx.service.name, seconds=window)
    return {
        "window_seconds": window,
        "note": (
            "Messages are normalised: variable values are replaced with typed placeholders, so "
            "recurring errors collapse into a single signature with a count."
        ),
        "signatures": [s.to_dict() for s in signatures[:15]],
        "new_signatures": [s.to_dict() for s in signatures if s.is_new][:8],
    }


def _get_recent_deployments(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    hours = float(args.get("hours") or 4)
    deployments = ctx.engine.recent_deployments(
        service_name=ctx.service.name, seconds=hours * 3600, limit=10
    )
    rows = []
    for deployment in deployments:
        lag = None
        if deployment.completed_at:
            lag = round((ctx.incident.detected_at - deployment.completed_at).total_seconds(), 1)
        rows.append(
            {
                "id": deployment.id,
                "version": deployment.version,
                "commit_sha": deployment.commit_sha,
                "commit_message": deployment.commit_message,
                "author": deployment.author,
                "status": deployment.status,
                "completed_at": deployment.completed_at.isoformat() if deployment.completed_at else None,
                "seconds_before_detection": lag,
                "is_suspected_cause": deployment.is_suspected_cause,
                "change_class": (deployment.meta or {}).get("change_class"),
                "risk_score": deployment.risk_score,
            }
        )
    return {"hours": hours, "deployments": rows}


def _get_configuration_changes(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    hours = float(args.get("hours") or 4)
    since = ctx.incident.detected_at - timedelta(hours=hours)
    rows = ctx.db.scalars(
        select(ConfigurationChange)
        .where(
            ConfigurationChange.service_id == ctx.service.id,
            ConfigurationChange.changed_at >= since,
        )
        .order_by(ConfigurationChange.changed_at.desc())
        .limit(30)
    ).all()
    return {
        "hours": hours,
        "changes": [
            {
                "id": row.id,
                "key": row.key,
                "old_value": row.old_value,
                "new_value": row.new_value,
                "change_class": row.change_class,
                "actor": row.actor,
                "changed_at": row.changed_at.isoformat(),
            }
            for row in rows
        ],
    }


def _get_service_dependencies(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    names = service_dependency_names(ctx.db, ctx.service)
    health = {}
    for name in names:
        try:
            health[name] = ctx.engine.gauges(name)
        except KeyError:
            health[name] = {"note": "dependency is not instrumented in this environment"}
    return {
        "service": ctx.service.name,
        "tier": ctx.service.tier,
        "dependencies": names,
        "dependency_health": health,
    }


def _get_service_health(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    return {
        "service": ctx.service.name,
        "environment": ctx.environment.name,
        "gauges": ctx.engine.gauges(ctx.service.name),
        "active_fault": (ctx.engine.active_fault(ctx.service.name).kind if ctx.engine.active_fault(ctx.service.name) else None),
    }


def _search_runbooks(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    query = (args.get("query") or "").strip()
    stmt = select(Runbook)
    rows = list(ctx.db.scalars(stmt.limit(200)).all())
    if query:
        tokens = [t for t in query.lower().split() if len(t) > 2]
        scored = []
        for row in rows:
            haystack = f"{row.title} {row.category} {' '.join(row.tags or [])} {row.content}".lower()
            score = sum(1 for token in tokens if token in haystack)
            if score:
                scored.append((score, row))
        scored.sort(key=lambda item: (item[0], item[1].times_effective), reverse=True)
        rows = [row for _, row in scored[:6]]
    else:
        rows = [r for r in rows if r.service_id in (None, ctx.service.id)][:6]
    return {
        "query": query,
        "runbooks": [
            {
                "id": row.id,
                "title": row.title,
                "category": row.category,
                "slug": row.slug,
                "summary": row.content[:600],
                "times_used": row.times_used,
                "times_effective": row.times_effective,
            }
            for row in rows
        ],
    }


def _get_incident_timeline(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    from app.domain.incidents import incident_timeline

    return {"timeline": incident_timeline(ctx.db, ctx.incident.id)}


def _get_incident_evidence(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    rows = ctx.db.scalars(
        select(IncidentEvidence)
        .where(IncidentEvidence.incident_id == ctx.incident.id)
        .order_by(IncidentEvidence.id)
    ).all()
    return {
        "evidence": [
            {
                "id": row.id,
                "kind": row.kind,
                "summary": row.summary,
                "strength": row.strength,
                "source": row.source,
                "retrieval": row.retrieval,
                "detail": row.detail,
            }
            for row in rows
        ]
    }


def _what_changed(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    verdicts = detect_anomalies(ctx.engine, service_name=ctx.service.name)
    deployments = ctx.engine.recent_deployments(service_name=ctx.service.name, seconds=7200, limit=6)
    changes = ctx.db.scalars(
        select(ConfigurationChange)
        .where(
            ConfigurationChange.service_id == ctx.service.id,
            ConfigurationChange.changed_at >= ctx.incident.detected_at - timedelta(hours=2),
        )
        .order_by(ConfigurationChange.changed_at.desc())
        .limit(20)
    ).all()
    timeline = build_what_changed(
        incident=ctx.incident, verdicts=verdicts, deployments=deployments, changes=changes
    )
    return {
        "note": "temporal relationships only; this tool never asserts causation",
        "timeline": timeline,
    }


def _search_similar_incidents(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    top_k = int(args.get("top_k") or 5)
    current = IncidentFingerprint.from_dict(ctx.incident.fingerprint)
    candidates = fingerprint_candidates(ctx.db, exclude_incident_id=ctx.incident.id)
    neighbours = rank_neighbours(current, candidates, top_k=top_k)
    return {
        "method": "weighted multi-feature similarity (service, environment, error signatures, metric pattern, root cause, deployment, symptom text)",
        "current_fingerprint": current.to_dict(),
        "neighbours": [n.to_dict() for n in neighbours],
    }


def _compare_incidents(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    other_id = int(args["incident_id"])
    other = ctx.db.get(Incident, other_id)
    if other is None:
        raise ValueError(f"incident {other_id} not found")
    other_service = ctx.db.get(Service, other.service_id)
    current = IncidentFingerprint.from_dict(ctx.incident.fingerprint)
    target = IncidentFingerprint.from_dict(other.fingerprint)
    result = compare_fingerprints(current, target)
    return {
        "historical_incident_id": other.id,
        "historical_title": other.title,
        "historical_root_cause": other.root_cause_summary,
        "historical_root_cause_category": other.root_cause_category,
        "historical_service": other_service.name if other_service else "unknown",
        "current_similarity": result.to_dict(),
        "how_it_was_resolved": _resolution_summary(ctx.db, other.id),
    }


def _resolution_summary(db: Session, incident_id: int) -> dict[str, Any]:
    from app.database.models import RemediationAttempt, VerificationRun

    attempts = db.scalars(
        select(RemediationAttempt)
        .where(RemediationAttempt.incident_id == incident_id)
        .order_by(RemediationAttempt.attempt_number)
    ).all()
    verifications = db.scalars(
        select(VerificationRun).where(VerificationRun.incident_id == incident_id)
    ).all()
    return {
        "attempts": [
            {
                "attempt_number": a.attempt_number,
                "action_code": a.action_code,
                "outcome": a.outcome,
                "failure_reason": a.failure_reason,
            }
            for a in attempts
        ],
        "verifications": [
            {
                "verdict": v.verdict,
                "improvement_pct": v.improvement_pct,
                "before": v.before_state,
                "after": v.after_state,
            }
            for v in verifications
        ],
    }


def _recall_historical_incidents(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """Recall from organizational memory (Hindsight TEMPR)."""
    fingerprint = IncidentFingerprint.from_dict(ctx.incident.fingerprint)
    extra = [args["focus"]] if args.get("focus") else []
    result = ctx.memory.recall_for_incident(
        fingerprint=fingerprint,
        db=ctx.db,
        incident_id=ctx.incident.id,
        extra_query=extra,
    )
    return {
        "backend": result.backend,
        "degraded": result.degraded,
        "note": result.note,
        "query": result.query,
        "strategy_hits": result.strategy_hits,
        "memory_count": len(result.hits),
        "memories": [hit.to_dict() for hit in result.hits],
    }


def _reflect_on_historical_experience(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    question = args.get("question") or "What has this organization learned about this class of incident?"
    result = ctx.memory.reflect(
        query=question,
        context=f"current incident #{ctx.incident.id} on {ctx.service.name} ({ctx.incident.severity})",
        db=ctx.db,
        incident_id=ctx.incident.id,
    )
    return {
        "question": question,
        "backend": result.backend,
        "degraded": result.degraded,
        "note": result.note,
        "answer": result.text,
        "citations": result.citations,
    }


def _get_remediation_history(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """Deterministic failure-memory lookup, independent of the memory backend.

    This exists so the platform can always answer "have we already tried this, and did it
    work?" even if organizational memory is unavailable — the answer lives in Postgres.
    """
    category = args.get("root_cause_category") or ctx.incident.root_cause_category or "unknown"
    attempts = remediation_history_for_root_cause(
        ctx.db, service_id=ctx.service.id, root_cause_category=category
    )
    failed = [a for a in attempts if a.outcome in {"failed", "partial"}]
    succeeded = [a for a in attempts if a.outcome == "succeeded"]
    summary: dict[str, Any] = {}
    for attempt in attempts:
        entry = summary.setdefault(attempt.action_code, {"outcome": attempt.outcome, "count": 0})
        entry["count"] += 1
    return {
        "root_cause_category": category,
        "scope": "postgres_remediation_attempts",
        "attempt_count": len(attempts),
        "failed_actions": sorted({a.action_code for a in failed}),
        "successful_actions": sorted({a.action_code for a in succeeded}),
        "history": [
            {
                "action_code": a.action_code,
                "attempt_number": a.attempt_number,
                "outcome": a.outcome,
                "failure_reason": a.failure_reason,
                "incident_id": a.incident_id,
                "created_at": a.created_at.isoformat(),
            }
            for a in attempts
        ],
        "by_action": summary,
    }


def _list_registered_actions(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    actions = list_actions(ctx.db)
    category = ctx.incident.root_cause_category or "unknown"
    return {
        "root_cause_category": category,
        "note": "the AI may only choose from this registry; no shell access exists",
        "actions": [
            {
                "code": a.code,
                "name": a.name,
                "description": a.description,
                "risk_level": a.risk_level,
                "required_approval": a.required_approval,
                "allowed_environments": a.allowed_environments,
                "applicable_to_current_root_cause": category in (a.applicable_categories or []),
                "historical_success": a.historical_success,
                "historical_failure": a.historical_failure,
                "executes_shell": a.executes_shell,
            }
            for a in actions
        ],
    }


# --- hypothesis testing -----------------------------------------------------------------
TEST_KINDS: dict[str, str] = {
    "check_active_connections": "Compare active pooled connections against pool capacity and the pre-incident baseline.",
    "check_pool_configuration": "Inspect pool size and any recent configuration change that affected it.",
    "check_deployment_impact": "Inspect the most recent deployments and the change classes they touched.",
    "check_resource_pressure": "Inspect CPU, memory and disk pressure on the instance.",
    "check_database_health": "Inspect database-facing latency and error signatures.",
    "check_dependency_health": "Inspect dependency availability and health.",
    "check_error_signatures": "Inspect normalised error signatures for new or spiking templates.",
    "check_historical_outcomes": "Inspect what happened when this class of incident was previously remediated.",
}


def _test_hypothesis(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """Run one concrete check and report supports/contradicts with the raw observation.

    The verdict is derived from the observation, never asserted: each check computes a
    predicate over real telemetry or real stored state.
    """
    kind = args.get("test") or "check_active_connections"
    if kind not in TEST_KINDS:
        raise ValueError(f"unknown test '{kind}'; available: {', '.join(sorted(TEST_KINDS))}")

    gauges = ctx.engine.gauges(ctx.service.name)
    baseline = ctx.incident.baseline or {}
    verdict = "inconclusive"
    observation: dict[str, Any] = {}

    if kind == "check_active_connections":
        saturation = gauges.get("connection_saturation", 0.0)
        baseline_sat = (baseline.get("connections", 0.0) / baseline.get("pool_size", 1.0)) if baseline.get("pool_size") else 0.0
        observation = {
            "active_connections": gauges.get("connections"),
            "pool_size": gauges.get("pool_size"),
            "saturation": saturation,
            "pre_incident_saturation": round(baseline_sat, 4),
        }
        verdict = "supports" if saturation >= 0.75 else ("contradicts" if saturation < 0.5 else "inconclusive")

    elif kind == "check_pool_configuration":
        changes = ctx.db.scalars(
            select(ConfigurationChange)
            .where(
                ConfigurationChange.service_id == ctx.service.id,
                ConfigurationChange.changed_at >= ctx.incident.detected_at - timedelta(hours=4),
            )
            .order_by(ConfigurationChange.changed_at.desc())
            .limit(20)
        ).all()
        pool_changes = [c for c in changes if "pool" in c.key.lower() or "config" in (c.change_class or "").lower()]
        observation = {
            "pool_size_now": gauges.get("pool_size"),
            "pool_size_pre_incident": baseline.get("pool_size"),
            "relevant_changes": [
                {"key": c.key, "old": c.old_value, "new": c.new_value, "class": c.change_class}
                for c in pool_changes[:8]
            ],
        }
        verdict = (
            "supports"
            if pool_changes or gauges.get("pool_size", 0) != baseline.get("pool_size")
            else "contradicts"
        )

    elif kind == "check_deployment_impact":
        deployments = ctx.engine.recent_deployments(service_name=ctx.service.name, seconds=7200, limit=5)
        recent = [
            d
            for d in deployments
            if d.completed_at and 0 <= (ctx.incident.detected_at - d.completed_at).total_seconds() <= 3600
        ]
        observation = {
            "deployments_in_window": [
                {
                    "version": d.version,
                    "seconds_before_detection": round(
                        (ctx.incident.detected_at - d.completed_at).total_seconds(), 1
                    )
                    if d.completed_at
                    else None,
                    "change_class": (d.meta or {}).get("change_class"),
                    "commit_message": d.commit_message,
                }
                for d in recent
            ],
            "total_deployments_considered": len(deployments),
        }
        verdict = "supports" if recent else "contradicts"

    elif kind == "check_resource_pressure":
        observation = {
            "cpu_pct": gauges.get("cpu_pct"),
            "mem_pct": gauges.get("mem_pct"),
            "disk_pct": gauges.get("disk_pct"),
            "baseline_cpu": baseline.get("cpu_pct"),
            "baseline_mem": baseline.get("mem_pct"),
            "baseline_disk": baseline.get("disk_pct"),
        }
        pressured = (
            gauges.get("cpu_pct", 0) > 88
            or gauges.get("mem_pct", 0) > 88
            or gauges.get("disk_pct", 0) > 92
        )
        verdict = "supports" if pressured else "contradicts"

    elif kind == "check_database_health":
        observation = {
            "latency_ms": gauges.get("latency_ms"),
            "baseline_latency_ms": baseline.get("latency_ms"),
            "error_rate": gauges.get("error_rate"),
            "relevant_signatures": [
                s.template
                for s in ctx.engine.recent_signatures(service_name=ctx.service.name, seconds=900)
                if "database" in s.template.lower() or "query" in s.template.lower()
            ][:5],
        }
        verdict = "supports" if observation["relevant_signatures"] else "contradicts"

    elif kind == "check_dependency_health":
        names = service_dependency_names(ctx.db, ctx.service)
        health: dict[str, Any] = {}
        any_degraded = False
        for name in names:
            try:
                dep = ctx.engine.gauges(name)
                health[name] = dep
                if dep.get("error_rate", 0) > 0.05 or dep.get("health_score", 1) < 0.7:
                    any_degraded = True
            except KeyError:
                health[name] = {"instrumented": False}
        observation = {"dependencies": names, "health": health, "any_dependency_degraded": any_degraded}
        verdict = "supports" if any_degraded else "contradicts"

    elif kind == "check_error_signatures":
        signatures = ctx.engine.recent_signatures(service_name=ctx.service.name, seconds=1800)
        new_sigs = [s for s in signatures if s.is_new and s.count >= 3]
        observation = {
            "total_signatures": len(signatures),
            "new_signatures": [{"template": s.template, "count": s.count, "level": s.level} for s in new_sigs[:6]],
            "top_signature": signatures[0].template if signatures else None,
        }
        verdict = "supports" if new_sigs else "inconclusive"

    elif kind == "check_historical_outcomes":
        history = _get_remediation_history(ctx, {"root_cause_category": args.get("root_cause_category")})
        observation = history
        verdict = "supports" if history["attempt_count"] else "inconclusive"

    return {
        "test": kind,
        "test_description": TEST_KINDS[kind],
        "observation": observation,
        "verdict": verdict,
        "interpretation": {
            "supports": "the observation is consistent with the hypothesis",
            "contradicts": "the observation is inconsistent with the hypothesis",
            "inconclusive": "the observation neither supports nor contradicts the hypothesis",
        }[verdict],
        "causality_note": "observations are correlational; causality is not asserted",
    }


def _get_latency_profile(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    series = ctx.engine.metric_series(
        service_name=ctx.service.name, metrics=["latency_ms", "error_rate", "rps"], seconds=1800
    )
    return {
        "series_length": {k: len(v) for k, v in series.items()},
        "latency_ms": {
            "first": series["latency_ms"][0] if series["latency_ms"] else None,
            "last": series["latency_ms"][-1] if series["latency_ms"] else None,
            "max": max(series["latency_ms"]) if series["latency_ms"] else None,
        },
        "error_rate": {
            "first": series["error_rate"][0] if series["error_rate"] else None,
            "last": series["error_rate"][-1] if series["error_rate"] else None,
            "max": max(series["error_rate"]) if series["error_rate"] else None,
        },
    }


# ---------------------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------------------
def _build_registry() -> dict[str, ToolSpec]:
    tools = [
        ToolSpec("get_incident", "Read the incident record, its detection metadata and baseline.", {"type": "object", "properties": {}, "required": []}, _get_incident),
        ToolSpec("get_current_metrics", "Read current and recent metric series for the affected service.", {"type": "object", "properties": {"metrics": {"type": "array", "items": {"type": "string"}}, "window_seconds": {"type": "number"}}, "required": []}, _get_current_metrics),
        ToolSpec("get_metric_anomalies", "Run the anomaly detector committee and read per-metric verdicts with detector detail.", {"type": "object", "properties": {}, "required": []}, _get_metric_anomalies),
        ToolSpec("get_current_logs", "Read a bounded sample of recent log lines (never the full log volume).", {"type": "object", "properties": {"window_seconds": {"type": "number"}, "level": {"type": "string"}, "limit": {"type": "integer"}}, "required": []}, _get_current_logs),
        ToolSpec("get_error_signatures", "Read normalised error signatures with counts, baselines and spike ratios.", {"type": "object", "properties": {"window_seconds": {"type": "number"}}, "required": []}, _get_error_signatures),
        ToolSpec("get_recent_deployments", "Read deployments in the window before detection, with lag in seconds.", {"type": "object", "properties": {"hours": {"type": "number"}}, "required": []}, _get_recent_deployments),
        ToolSpec("get_configuration_changes", "Read configuration changes in the window before detection.", {"type": "object", "properties": {"hours": {"type": "number"}}, "required": []}, _get_configuration_changes),
        ToolSpec("get_service_dependencies", "Read service topology and dependency health.", {"type": "object", "properties": {}, "required": []}, _get_service_dependencies),
        ToolSpec("get_service_health", "Read the current health snapshot including whether a fault is still active.", {"type": "object", "properties": {}, "required": []}, _get_service_health),
        ToolSpec("get_latency_profile", "Read latency/error/throughput series shape.", {"type": "object", "properties": {}, "required": []}, _get_latency_profile),
        ToolSpec("search_runbooks", "Search runbooks by free text or by service.", {"type": "object", "properties": {"query": {"type": "string"}}, "required": []}, _search_runbooks),
        ToolSpec("get_incident_timeline", "Read the persisted incident timeline.", {"type": "object", "properties": {}, "required": []}, _get_incident_timeline),
        ToolSpec("get_incident_evidence", "Read the evidence already collected for this incident.", {"type": "object", "properties": {}, "required": []}, _get_incident_evidence),
        ToolSpec("whats_changed", "Build the ordered change timeline with explicitly non-causal relationship labels.", {"type": "object", "properties": {}, "required": []}, _what_changed),
        ToolSpec("search_similar_incidents", "Rank historical incidents by explainable multi-feature similarity.", {"type": "object", "properties": {"top_k": {"type": "integer"}}, "required": []}, _search_similar_incidents),
        ToolSpec("compare_incidents", "Compare this incident with one historical incident and explain similarities and differences.", {"type": "object", "properties": {"incident_id": {"type": "integer"}}, "required": ["incident_id"]}, _compare_incidents),
        ToolSpec("recall_historical_incidents", "Recall organizational experience from Hindsight memory (TEMPR multi-arm retrieval).", {"type": "object", "properties": {"focus": {"type": "string"}}, "required": []}, _recall_historical_incidents),
        ToolSpec("reflect_on_historical_experience", "Ask organizational memory to synthesize what has been learned, with citations.", {"type": "object", "properties": {"question": {"type": "string"}}, "required": []}, _reflect_on_historical_experience),
        ToolSpec("get_remediation_history", "Read which remediation actions previously failed or succeeded for this root cause (Postgres ledger).", {"type": "object", "properties": {"root_cause_category": {"type": "string"}}, "required": []}, _get_remediation_history),
        ToolSpec("list_registered_actions", "List the remediation registry the AI is permitted to choose from.", {"type": "object", "properties": {}, "required": []}, _list_registered_actions),
        ToolSpec("test_hypothesis", "Run one concrete investigative check and read whether it supports or contradicts.", {"type": "object", "properties": {"test": {"type": "string", "enum": sorted(TEST_KINDS)}, "root_cause_category": {"type": "string"}}, "required": ["test"]}, _test_hypothesis),
    ]
    return {tool.name: tool for tool in tools}


REGISTRY: dict[str, ToolSpec] = _build_registry()


def openai_tool_schemas(names: Sequence[str] | None = None) -> list[dict[str, Any]]:
    selected = [REGISTRY[name] for name in names] if names else list(REGISTRY.values())
    return [tool.openai_schema() for tool in selected]


class ToolRegistry:
    """Executes tools and writes the audit ledger."""

    def __init__(self, *, db: Session) -> None:
        self.db = db
        self._seq = 0

    def execute(
        self,
        name: str,
        arguments: dict[str, Any] | None,
        ctx: ToolContext,
        *,
        round_index: int = 0,
        decision: str = "",
    ) -> ToolResult:
        self._seq += 1
        started = time.perf_counter()
        arguments = arguments or {}

        spec = REGISTRY.get(name)
        if spec is None:
            result = ToolResult(
                ok=False,
                error_code="TOOL_NOT_FOUND",
                error_message=f"'{name}' is not an exposed tool",
            )
        elif not ctx.allows(name):
            result = ToolResult(
                ok=False,
                error_code="TOOL_NOT_PERMITTED",
                error_message=f"'{name}' is not permitted in this phase",
            )
        else:
            try:
                data = spec.handler(ctx, arguments)
                result = ToolResult(ok=True, data=data)
            except Exception as exc:  # noqa: BLE001 - a failing tool must not kill the loop
                logger.warning("tool %s failed: %s", name, exc)
                result = ToolResult(
                    ok=False,
                    error_code="TOOL_FAILED",
                    error_message=f"{type(exc).__name__}: {exc}",
                )

        result.duration_ms = (time.perf_counter() - started) * 1000.0

        if ctx.investigation_id is not None:
            self.db.add(
                AiToolCall(
                    investigation_id=ctx.investigation_id,
                    seq=self._seq,
                    round_index=round_index,
                    tool_name=name,
                    arguments=arguments,
                    result=result.data if result.ok else {"error": result.error_message},
                    ok=result.ok,
                    error_code=result.error_code,
                    error_message=result.error_message,
                    duration_ms=result.duration_ms,
                    decision=decision,
                )
            )
            self.db.flush()
        return result

    @property
    def call_count(self) -> int:
        return self._seq


def available_tool_names() -> list[str]:
    return sorted(REGISTRY)


__all__ = [
    "ToolSpec",
    "ToolContext",
    "ToolResult",
    "ToolRegistry",
    "REGISTRY",
    "TEST_KINDS",
    "available_tool_names",
    "openai_tool_schemas",
    "aggregate_signatures",
    "detect_anomaly",
    "METRIC_LABELS",
    "IncidentFingerprint",
]
