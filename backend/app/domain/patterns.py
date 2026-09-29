"""Pattern detection, remediation reliability and period analysis (build spec sections 30-34).

Section 33 says: *never claim a pattern with insufficient evidence*. So every pattern carries
an explicit occurrence count and an evidence-strength label, and the API refuses to describe
anything below the minimum threshold as a pattern at all — it is reported as an observation
with the count, or not at all.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import timedelta
from typing import Any, Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database.models import (
    Deployment,
    Incident,
    RemediationAttempt,
    Service,
    VerificationRun,
    utcnow,
)

MIN_OCCURRENCES_FOR_PATTERN = 3
"""Below this, an occurrence cluster is reported as a single incident, never as a pattern."""


@dataclass
class Pattern:
    category: str
    occurrences: int
    window_days: int
    services: list[str]
    incident_ids: list[int]
    failed_actions: list[str]
    successful_actions: list[str]
    deployment_related: int
    median_mttr_seconds: float | None
    evidence_strength: str
    statement: str
    retention_advice: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _window_start(days: int):
    return utcnow() - timedelta(days=days)


def detect_patterns(db: Session, *, days: int = 30, include_historical: bool = True) -> list[Pattern]:
    start = _window_start(days)
    stmt = select(Incident).where(Incident.detected_at >= start)
    if not include_historical:
        stmt = stmt.where(Incident.is_historical.is_(False))
    incidents = list(db.scalars(stmt).all())

    grouped: dict[str, list[Incident]] = {}
    for incident in incidents:
        category = incident.root_cause_category or "unknown"
        if category == "unknown":
            continue
        grouped.setdefault(category, []).append(incident)

    patterns: list[Pattern] = []
    for category, group in grouped.items():
        if len(group) < MIN_OCCURRENCES_FOR_PATTERN:
            continue
        service_names: list[str] = []
        for incident in group:
            service = db.get(Service, incident.service_id)
            if service and service.name not in service_names:
                service_names.append(service.name)

        attempts = list(
            db.scalars(
                select(RemediationAttempt).where(
                    RemediationAttempt.incident_id.in_([i.id for i in group])
                )
            ).all()
        )
        # Action reliability is only claimed with a minimum sample, same rule as the pattern.
        failed_counts: dict[str, int] = {}
        success_counts: dict[str, int] = {}
        for attempt in attempts:
            if attempt.outcome in {"failed", "partial"}:
                failed_counts[attempt.action_code] = failed_counts.get(attempt.action_code, 0) + 1
            elif attempt.outcome == "succeeded":
                success_counts[attempt.action_code] = success_counts.get(attempt.action_code, 0) + 1

        failed_actions = sorted(a for a, n in failed_counts.items() if n >= 1)
        successful_actions = sorted(a for a, n in success_counts.items() if n >= 1)

        deployment_related = 0
        for incident in group:
            fingerprint = incident.fingerprint or {}
            if fingerprint.get("deployment_related"):
                deployment_related += 1

        mttrs = [i.mttr_seconds for i in group if i.mttr_seconds is not None]
        median_mttr = sorted(mttrs)[len(mttrs) // 2] if mttrs else None

        strength = "strong" if len(group) >= 6 else ("moderate" if len(group) >= 4 else "weak")

        statement = (
            f"'{category}' has occurred {len(group)} times in {days} days across "
            f"{len(service_names)} service(s)"
        )
        if deployment_related:
            statement += f", and {deployment_related} of those followed a deployment within the hour"
        statement += "."

        advice = ""
        if failed_actions:
            advice = (
                f"Do not lead with {', '.join(failed_actions)} for this pattern: "
                f"{len(failed_actions)} action(s) recorded as failed in this window."
            )
        if successful_actions:
            advice += (
                f" Verified effective: {', '.join(successful_actions)}."
                if advice
                else f"Verified effective: {', '.join(successful_actions)}."
            )

        patterns.append(
            Pattern(
                category=category,
                occurrences=len(group),
                window_days=days,
                services=service_names[:8],
                incident_ids=[i.id for i in group][:50],
                failed_actions=failed_actions,
                successful_actions=successful_actions,
                deployment_related=deployment_related,
                median_mttr_seconds=median_mttr,
                evidence_strength=strength,
                statement=statement,
                retention_advice=advice,
            )
        )

    patterns.sort(key=lambda p: (p.occurrences, p.deployment_related), reverse=True)
    return patterns


def remediation_reliability(db: Session, *, days: int = 90) -> list[dict[str, Any]]:
    start = _window_start(days)
    rows = db.execute(
        select(
            RemediationAttempt.action_code,
            RemediationAttempt.outcome,
            func.count(),
        )
        .where(RemediationAttempt.created_at >= start)
        .group_by(RemediationAttempt.action_code, RemediationAttempt.outcome)
    ).all()

    table: dict[str, dict[str, Any]] = {}
    for action_code, outcome, count in rows:
        entry = table.setdefault(
            action_code,
            {"action_code": action_code, "succeeded": 0, "failed": 0, "partial": 0, "applied": 0, "total": 0},
        )
        if outcome in entry:
            entry[outcome] = count
        entry["total"] += count
    for entry in table.values():
        attempts = entry["succeeded"] + entry["failed"] + entry["partial"]
        entry["success_rate"] = round(entry["succeeded"] / attempts, 3) if attempts else None
        entry["reliability_label"] = (
            "reliable"
            if attempts >= 3 and entry["succeeded"] >= attempts * 0.7
            else ("unreliable" if attempts >= 3 and entry["succeeded"] <= attempts * 0.34 else "insufficient_evidence")
        )
    return sorted(table.values(), key=lambda e: e["total"], reverse=True)


def service_fragility(db: Session, *, days: int = 30) -> list[dict[str, Any]]:
    start = _window_start(days)
    rows = db.execute(
        select(Incident.service_id, func.count(), func.avg(Incident.mttr_seconds))
        .where(Incident.detected_at >= start)
        .group_by(Incident.service_id)
    ).all()
    out: list[dict[str, Any]] = []
    for service_id, count, avg_mttr in rows:
        service = db.get(Service, service_id)
        out.append(
            {
                "service_id": service_id,
                "service": service.name if service else "unknown",
                "tier": service.tier if service else "unknown",
                "incidents": count,
                "mean_mttr_seconds": round(float(avg_mttr), 1) if avg_mttr else None,
            }
        )
    out.sort(key=lambda row: row["incidents"], reverse=True)
    return out


def period_analysis(db: Session, *, days: int = 30) -> dict[str, Any]:
    start = _window_start(days)
    incidents = list(db.scalars(select(Incident).where(Incident.detected_at >= start)).all())
    attempts = list(
        db.scalars(select(RemediationAttempt).where(RemediationAttempt.created_at >= start)).all()
    )
    verifications = list(
        db.scalars(select(VerificationRun).where(VerificationRun.started_at >= start)).all()
    )
    deployments = list(db.scalars(select(Deployment).where(Deployment.started_at >= start)).all())

    by_category: dict[str, int] = {}
    by_severity: dict[str, int] = {}
    for incident in incidents:
        by_category[incident.root_cause_category or "unknown"] = (
            by_category.get(incident.root_cause_category or "unknown", 0) + 1
        )
        by_severity[incident.severity] = by_severity.get(incident.severity, 0) + 1

    resolved = [i for i in incidents if i.resolved_at]
    mttrs = sorted(i.mttr_seconds for i in resolved if i.mttr_seconds is not None)
    verified_success = sum(1 for v in verifications if v.verdict == "verified_success")
    failed = sum(1 for v in verifications if v.verdict == "verification_failed")

    return {
        "window_days": days,
        "window_start": start.isoformat(),
        "incidents": len(incidents),
        "open_incidents": sum(1 for i in incidents if i.status not in {"verified", "closed"}),
        "by_root_cause": dict(sorted(by_category.items(), key=lambda kv: kv[1], reverse=True)),
        "by_severity": by_severity,
        "median_mttr_seconds": mttrs[len(mttrs) // 2] if mttrs else None,
        "remediation_attempts": len(attempts),
        "failed_remediation_actions": sorted(
            {a.action_code for a in attempts if a.outcome in {"failed", "partial"}}
        ),
        "successful_remediation_actions": sorted(
            {a.action_code for a in attempts if a.outcome == "succeeded"}
        ),
        "verifications": len(verifications),
        "verified_success": verified_success,
        "verification_failed": failed,
        "remediation_success_rate": (
            round(verified_success / (verified_success + failed), 3)
            if (verified_success + failed)
            else None
        ),
        "deployments": len(deployments),
        "deployment_related_incidents": sum(
            1 for i in incidents if (i.fingerprint or {}).get("deployment_related")
        ),
    }


def insights_for_memory(db: Session, *, days: int = 30) -> list[dict[str, Any]]:
    """Patterns strong enough that retaining them as knowledge-level memory is justified."""
    out: list[dict[str, Any]] = []
    for pattern in detect_patterns(db, days=days):
        if pattern.evidence_strength in {"moderate", "strong"} and (
            pattern.failed_actions or pattern.successful_actions
        ):
            out.append(pattern.to_dict())
    return out
