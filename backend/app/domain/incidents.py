"""Incident engine: detection, opening, evidence collection and fingerprinting.

Everything the AI later reasons over is produced here deterministically:

* ``detect_and_open_incidents`` runs the anomaly committee across the watched metrics with a
  consecutive-breach debounce, so one noisy sample cannot open an incident.
* ``capture_baseline`` derives the pre-incident baseline from *before the CUSUM change point*,
  which is what makes the before/after comparison honest rather than a full-window average
  polluted by the incident itself.
* ``collect_evidence`` gathers the evidence set and persists each item as an
  ``incident_evidence`` row with its provenance and strength.
* ``compute_fingerprint`` produces the structured identity used for similarity and recall.
* ``what_changed`` builds the temporal timeline and attaches explicitly non-causal
  relationship labels (``preceded_by`` / ``correlated_with``).
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.database.models import (
    ConfigurationChange,
    Deployment,
    Environment,
    Incident,
    IncidentEvent,
    IncidentEvidence,
    Service,
    ServiceDependency,
)
from app.detection.logs import SignatureStat, dominant_signature
from app.detection.metrics import (
    AnomalyVerdict,
    DetectionState,
    DetectorConfig,
    correlate_with_change,
    detect_anomaly_vs_baseline,
)
from app.detection.similarity import IncidentFingerprint, build_fingerprint
from app.sim.engine import SimulationEngine, sim_now

logger = logging.getLogger(__name__)

DETECTION_WINDOW_SECONDS = 300.0
"""The *recent* window: 5 minutes of simulated telemetry under evaluation."""

BASELINE_WINDOW_SECONDS = 1800.0
"""The *reference* window: the 30 minutes that end where the recent window begins.

Detection compares the two windows against each other rather than evaluating one window in
isolation. During a slow ramp, a single window averages the incident into its own baseline and
the deviation collapses toward zero exactly when the incident is at its worst. Keeping the
reference explicitly older removes that failure mode — and it is what an on-call engineer
actually asks: "is this worse than it was half an hour ago?"."""

WATCHED_METRICS: tuple[tuple[str, str, bool], ...] = (
    ("error_rate", "up", True),
    ("connection_saturation", "up", True),
    ("latency_ms", "up", True),
    ("cpu_pct", "up", False),
    ("mem_pct", "up", False),
    ("disk_pct", "up", False),
    ("queue_depth", "up", False),
)

METRIC_LABELS = {
    "error_rate": "error rate",
    "connection_saturation": "connection pool saturation",
    "latency_ms": "request latency",
    "cpu_pct": "CPU utilisation",
    "mem_pct": "memory utilisation",
    "disk_pct": "disk utilisation",
    "queue_depth": "queue depth",
    "health_score": "health score",
}

DEBOUNCE_REQUIRED = 2
"""Consecutive anomalous detection passes required before opening an incident."""

DETECTION_STATE: dict[tuple[str, str], DetectionState] = {}
INCIDENT_COOLDOWN_SECONDS = 600.0


def reset_detection_state() -> None:
    DETECTION_STATE.clear()


def _state_for(service: str, metric: str) -> DetectionState:
    key = (service, metric)
    if key not in DETECTION_STATE:
        DETECTION_STATE[key] = DetectionState(required=DEBOUNCE_REQUIRED)
    return DETECTION_STATE[key]


# ---------------------------------------------------------------------------------------
# Baselines
# ---------------------------------------------------------------------------------------
def _median(values: Sequence[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return float(ordered[len(ordered) // 2])


def capture_baseline(
    engine: SimulationEngine,
    *,
    service_name: str,
    reference_seconds: float | None = None,
    recent_seconds: float = DETECTION_WINDOW_SECONDS,
) -> dict[str, float]:
    """Pre-incident baseline per metric, taken from the reference window.

    The reference window ends where the recent window begins, so it contains only samples from
    before behaviour changed. This is the same baseline the detector compares against and the
    same one verification measures recovery against, which keeps detection and verification
    computing their thresholds from one consistent reference.
    """
    window = reference_seconds or float(settings.baseline_window_seconds)
    end = sim_now() - timedelta(seconds=recent_seconds)
    metrics = [metric for metric, _, _ in WATCHED_METRICS] + [
        "connections",
        "pool_size",
        "health_score",
    ]
    baseline: dict[str, float] = {}
    for metric in metrics:
        values = [
            v
            for _, v in engine.metric_window(
                service_name=service_name, metric=metric, seconds=window, end=end
            )
        ]
        baseline[metric] = round(_median(values), 6)
    return baseline


# ---------------------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------------------
def detect_anomalies(
    engine: SimulationEngine,
    *,
    service_name: str,
    window_seconds: float = DETECTION_WINDOW_SECONDS,
    reference_seconds: float = BASELINE_WINDOW_SECONDS,
) -> dict[str, AnomalyVerdict]:
    """One detection pass: recent window vs. explicitly older reference window, per metric."""
    reference_end = sim_now() - timedelta(seconds=window_seconds)
    verdicts: dict[str, AnomalyVerdict] = {}
    for metric, direction, critical in WATCHED_METRICS:
        recent = [
            v for _, v in engine.metric_window(service_name=service_name, metric=metric, seconds=window_seconds)
        ]
        baseline = [
            v
            for _, v in engine.metric_window(
                service_name=service_name, metric=metric, seconds=reference_seconds, end=reference_end
            )
        ]
        verdicts[metric] = detect_anomaly_vs_baseline(
            metric,
            recent,
            baseline,
            config=DetectorConfig(),
            anomaly_expected_direction=direction,
            critical_metric=critical,
        )
    return verdicts


def detect_and_open_incidents(db: Session, engine: SimulationEngine) -> list[Incident]:
    """One detection pass across every known service. Returns newly opened incidents."""
    opened: list[Incident] = []
    for service in db.scalars(select(Service)).all():
        environment = _primary_environment(db, service, engine)
        if environment is None:
            continue
        open_incident = db.scalar(
            select(Incident)
            .where(Incident.service_id == service.id, Incident.status.notin_(["verified", "closed"]))
            .order_by(Incident.detected_at.desc())
        )

        verdicts = detect_anomalies(engine, service_name=service.name)
        breaching = {
            metric: verdict for metric, verdict in verdicts.items() if verdict.anomalous
        }

        primed: dict[str, AnomalyVerdict] = {}
        for metric, verdict in breaching.items():
            state = _state_for(service.name, metric)
            if state.observe(verdict):
                primed[metric] = verdict
        for metric, verdict in verdicts.items():
            if metric not in breaching:
                _state_for(service.name, metric).observe(verdict)

        if not primed:
            continue

        # Cooldown: a still-open incident absorbs further breaches as timeline events rather
        # than spawning duplicates.
        if open_incident is not None:
            primary = _primary_breach(primed)
            db.add(
                IncidentEvent(
                    incident_id=open_incident.id,
                    ts=sim_now(),
                    kind="detection_repeat",
                    title=f"Continued anomaly on {METRIC_LABELS.get(primary, primary)}",
                    description=primed[primary].reason,
                    source="detection_engine",
                    payload={"metrics": list(primed), "severity": primed[primary].severity},
                )
            )
            if not open_incident.fingerprint:
                open_incident.fingerprint = compute_fingerprint(
                    db, engine, incident=open_incident, service=service, environment=environment
                ).to_dict()
            continue

        recent = db.scalar(
            select(Incident)
            .where(Incident.service_id == service.id)
            .order_by(Incident.detected_at.desc())
        )
        if recent is not None and recent.resolved_at is not None:
            delta = (sim_now() - recent.detected_at).total_seconds()
            if delta < INCIDENT_COOLDOWN_SECONDS:
                continue

        incident = open_incident_record(
            db,
            engine,
            service=service,
            environment=environment,
            verdicts=primed,
        )
        opened.append(incident)
    return opened


def _primary_breach(verdicts: dict[str, AnomalyVerdict]) -> str:
    def rank(item: tuple[str, AnomalyVerdict]) -> tuple[int, float]:
        metric, verdict = item
        critical = 1 if metric in {"error_rate", "connection_saturation", "latency_ms"} else 0
        return critical, verdict.score

    return max(verdicts.items(), key=rank)[0]


def _primary_environment(
    db: Session, service: Service, engine: SimulationEngine | None = None
) -> Environment | None:
    """Which environment an incident belongs to.

    When a fault is active, the incident belongs to the environment the fault was injected into
    rather than to the service's production environment by default. Getting this wrong is not
    cosmetic: the environment is a similarity feature, so a staging incident wrongly labelled
    production would inflate its match against production history.
    """
    if engine is not None:
        runtime = engine.world_runtime(service.name)
        if runtime is not None and engine.active_fault(service.name) is not None:
            environment = db.scalar(
                select(Environment).where(
                    Environment.project_id == service.project_id, Environment.name == runtime.environment
                )
            )
            if environment is not None:
                return environment
    primary = db.scalar(
        select(Environment).where(Environment.project_id == service.project_id, Environment.is_production.is_(True))
    )
    if primary:
        return primary
    return db.scalar(select(Environment).where(Environment.project_id == service.project_id))


def severity_from(verdicts: dict[str, AnomalyVerdict], environment: Environment) -> str:
    top = max(verdict.score for verdict in verdicts.values())
    if top >= 0.90:
        severity = "critical"
    elif top >= 0.75:
        severity = "high"
    elif top >= 0.55:
        severity = "medium"
    else:
        severity = "low"
    if environment.is_production and severity == "medium":
        severity = "high"
    if not environment.is_production and severity == "critical":
        severity = "high"
    return severity


def open_incident_record(
    db: Session,
    engine: SimulationEngine,
    *,
    service: Service,
    environment: Environment,
    verdicts: dict[str, AnomalyVerdict],
) -> Incident:
    primary = _primary_breach(verdicts)
    severity = severity_from(verdicts, environment)
    detected_at = sim_now()
    fault = engine.active_fault(service.name)
    baseline = capture_baseline(engine, service_name=service.name)

    symptoms = [
        f"{METRIC_LABELS.get(metric, metric)} {verdict.direction} "
        f"{verdict.relative_change:+.0%} vs baseline ({verdict.current:.3g} from {verdict.baseline:.3g})"
        for metric, verdict in sorted(verdicts.items(), key=lambda kv: kv[1].score, reverse=True)
    ]
    if fault:
        symptoms.extend(fault.symptoms[:3])

    incident = Incident(
        project_id=service.project_id,
        service_id=service.id,
        environment_id=environment.id,
        title=f"{service.name}: {METRIC_LABELS.get(primary, primary)} anomaly in {environment.name}",
        symptom="; ".join(symptoms[:6]),
        severity=severity,
        status="detected",
        detected_at=detected_at,
        fingerprint=None,
        baseline=baseline,
        detection_meta={
            "primary_metric": primary,
            "verdicts": {metric: verdict.to_dict() for metric, verdict in verdicts.items()},
            "debounce_required": DEBOUNCE_REQUIRED,
            "detection_window_seconds": DETECTION_WINDOW_SECONDS,
            "detector": "committee(mad-z, rolling-median, ewma, cusum)",
        },
        is_historical=False,
    )
    db.add(incident)
    db.flush()

    db.add(
        IncidentEvent(
            incident_id=incident.id,
            ts=detected_at,
            kind="detected",
            title="Incident detected",
            description=(
                f"{severity.upper()} severity: {METRIC_LABELS.get(primary, primary)} exceeded its "
                f"rolling baseline on {service.name} in {environment.name}. "
                f"Detection used a committee of four detectors with a {DEBOUNCE_REQUIRED}-pass debounce."
            ),
            actor="detection_engine",
            source="detection_engine",
            payload={
                "severity": severity,
                "primary_metric": primary,
                "baseline": baseline,
                "verdicts": {m: v.to_dict() for m, v in verdicts.items()},
            },
        )
    )
    db.flush()

    incident.fingerprint = compute_fingerprint(
        db, engine, incident=incident, service=service, environment=environment
    ).to_dict()
    db.flush()
    return incident


# ---------------------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------------------
def service_dependency_names(db: Session, service: Service) -> list[str]:
    rows = db.scalars(
        select(ServiceDependency).where(ServiceDependency.service_id == service.id)
    ).all()
    names: list[str] = []
    for row in rows:
        dependency = db.get(Service, row.depends_on_service_id)
        if dependency:
            names.append(dependency.name)
    return names


def collect_evidence(
    db: Session,
    engine: SimulationEngine,
    *,
    incident: Incident,
    service: Service,
    environment: Environment,
    verdicts: dict[str, AnomalyVerdict] | None = None,
) -> dict[str, Any]:
    """Gather the deterministic evidence set and persist each item with provenance."""
    verdicts = verdicts or detect_anomalies(engine, service_name=service.name)
    gauges = engine.gauges(service.name)
    signatures: list[SignatureStat] = engine.recent_signatures(service_name=service.name, seconds=900.0)
    spikes = [s for s in signatures if s.is_new or s.spike_ratio >= 3]
    deployments = list(
        engine.recent_deployments(service_name=service.name, seconds=7200.0, limit=6)
    )
    changes = list(
        db.scalars(
            select(ConfigurationChange)
            .where(
                ConfigurationChange.service_id == service.id,
                ConfigurationChange.changed_at >= (incident.detected_at - timedelta(seconds=7200)),
            )
            .order_by(ConfigurationChange.changed_at.desc())
            .limit(20)
        ).all()
    )
    dependencies = service_dependency_names(db, service)
    what_changed = build_what_changed(
        incident=incident, verdicts=verdicts, deployments=deployments, changes=changes
    )

    evidence_ids: dict[str, list[int]] = {}
    collected_at = sim_now()

    def add(
        kind: str,
        summary: str,
        *,
        detail: dict[str, Any] | None = None,
        source: str,
        strength: str = "moderate",
        retrieval: str = "deterministic",
    ) -> int:
        row = IncidentEvidence(
            incident_id=incident.id,
            kind=kind,
            summary=summary,
            detail=detail,
            source=source,
            collected_at=collected_at,
            strength=strength,
            retrieval=retrieval,
        )
        db.add(row)
        db.flush()
        evidence_ids.setdefault(kind, []).append(row.id)
        return row.id

    # 1. metric anomalies
    for metric, verdict in sorted(verdicts.items(), key=lambda kv: kv[1].score, reverse=True):
        if not verdict.anomalous:
            continue
        add(
            "metric_anomaly",
            f"{METRIC_LABELS.get(metric, metric)} {verdict.direction} "
            f"{verdict.relative_change:+.0%} to {verdict.current:.4g} (baseline {verdict.baseline:.4g})",
            detail=verdict.to_dict(),
            source="metric_detector",
            strength="strong" if verdict.severity in {"critical", "high"} else "moderate",
        )

    # 2. error signature spikes
    for stat in spikes[:6]:
        add(
            "error_signature",
            f"{stat.level} signature x{stat.count} "
            f"({'new signature' if stat.is_new else f'x{stat.spike_ratio:g} baseline'}) — {stat.template[:180]}",
            detail=stat.to_dict(),
            source="log_pipeline",
            strength="strong" if stat.level in {"ERROR", "FATAL"} else "moderate",
        )

    # 3. deployments in the correlation window
    for deployment in deployments:
        lag = (incident.detected_at - deployment.completed_at).total_seconds() if deployment.completed_at else None
        relation = correlate_with_change(
            metric_change_ts=incident.detected_at.isoformat(),
            change_ts=(deployment.completed_at or deployment.started_at).isoformat(),
            metric_name=incident.detection_meta.get("primary_metric", "error_rate") if incident.detection_meta else "error_rate",
            change_label=f"Deployment {deployment.version}",
        )
        add(
            "deployment",
            f"Deployment {deployment.version} ({deployment.commit_sha[:8]}) completed "
            f"{'%.0f' % lag + 's' if lag is not None else 'at an unknown time'} before detection — {relation['relationship']}",
            detail={
                "deployment_id": deployment.id,
                "version": deployment.version,
                "commit_message": deployment.commit_message,
                "author": deployment.author,
                "completed_at": (deployment.completed_at or deployment.started_at).isoformat(),
                "lag_seconds": lag,
                "relationship": relation,
                "change_class": (deployment.meta or {}).get("change_class"),
            },
            source="deployment_history",
            strength="strong" if relation["relationship"] == "preceded_by" else "weak",
        )

    # 4. configuration changes
    for change in changes[:6]:
        add(
            "configuration_change",
            f"{change.key} changed from '{change.old_value}' to '{change.new_value}' by {change.actor}",
            detail={
                "change_id": change.id,
                "key": change.key,
                "old_value": change.old_value,
                "new_value": change.new_value,
                "change_class": change.change_class,
                "changed_at": change.changed_at.isoformat(),
            },
            source="configuration_history",
            strength="moderate" if change.change_class != "simulated_fault" else "weak",
        )

    # 5. dependency context
    if dependencies:
        add(
            "dependency",
            f"{service.name} depends on {', '.join(dependencies)}",
            detail={"dependencies": dependencies},
            source="service_topology",
            strength="informational",
        )

    # 6. service health snapshot
    add(
        "service_health",
        f"current health score {gauges.get('health_score')} with {gauges.get('connections')} "
        f"of {gauges.get('pool_size')} connections in use ({gauges.get('connection_saturation'):.0%})",
        detail={"gauges": gauges},
        source="health_probe",
        strength="informational",
    )

    # 7. what-changed timeline
    add(
        "what_changed",
        f"{len(what_changed)} change(s) identified in the window preceding detection; "
        f"relationships are temporal, not causal",
        detail={"timeline": what_changed},
        source="change_correlator",
        strength="moderate",
    )

    db.flush()
    return {
        "verdicts": {metric: verdict.to_dict() for metric, verdict in verdicts.items()},
        "signatures": [stat.to_dict() for stat in signatures[:15]],
        "spikes": [stat.to_dict() for stat in spikes[:8]],
        "deployments": [
            {
                "id": d.id,
                "version": d.version,
                "commit_sha": d.commit_sha,
                "commit_message": d.commit_message,
                "author": d.author,
                "started_at": d.started_at.isoformat(),
                "completed_at": d.completed_at.isoformat() if d.completed_at else None,
                "status": d.status,
                "change_class": (d.meta or {}).get("change_class"),
            }
            for d in deployments
        ],
        "configuration_changes": [
            {
                "id": c.id,
                "key": c.key,
                "old_value": c.old_value,
                "new_value": c.new_value,
                "change_class": c.change_class,
                "actor": c.actor,
                "changed_at": c.changed_at.isoformat(),
            }
            for c in changes
        ],
        "dependencies": dependencies,
        "gauges": gauges,
        "what_changed": what_changed,
        "baseline": dict(incident.baseline or {}),
        "evidence_ids": evidence_ids,
    }


def build_what_changed(
    *,
    incident: Incident,
    verdicts: dict[str, AnomalyVerdict],
    deployments: Sequence[Deployment],
    changes: Sequence[ConfigurationChange],
) -> list[dict[str, Any]]:
    """Ordered timeline of changes and metric shifts with explicit relationship labels."""
    timeline: list[dict[str, Any]] = []

    for change in sorted(changes, key=lambda c: c.changed_at):
        timeline.append(
            {
                "ts": change.changed_at.isoformat(),
                "kind": "configuration_change",
                "label": f"{change.key} → {change.new_value}",
                "detail": f"was '{change.old_value}', changed by {change.actor} ({change.change_class})",
            }
        )
    for deployment in sorted(deployments, key=lambda d: d.started_at):
        timeline.append(
            {
                "ts": deployment.started_at.isoformat(),
                "kind": "deployment_started",
                "label": f"deployment {deployment.version} started",
                "detail": deployment.commit_message,
            }
        )
        if deployment.completed_at:
            timeline.append(
                {
                    "ts": deployment.completed_at.isoformat(),
                    "kind": "deployment_completed",
                    "label": f"deployment {deployment.version} completed",
                    "detail": f"{deployment.commit_sha[:8]} by {deployment.author}",
                }
            )

    window_start = incident.detected_at - timedelta(seconds=DETECTION_WINDOW_SECONDS)
    for metric, verdict in sorted(verdicts.items(), key=lambda kv: kv[1].score, reverse=True):
        if not verdict.anomalous or verdict.change_point_index is None:
            continue
        timeline.append(
            {
                "ts": (
                    window_start
                    + timedelta(
                        seconds=verdict.change_point_index * max(settings.sim_seconds_per_tick, 1)
                    )
                ).isoformat(),
                "kind": "metric_change_point",
                "label": f"{METRIC_LABELS.get(metric, metric)} change point",
                "detail": (
                    f"CUSUM located a sustained shift after sample index {verdict.change_point_index}; "
                    f"current {verdict.current:.4g} vs baseline {verdict.baseline:.4g}"
                ),
            }
        )

    timeline.append(
        {
            "ts": incident.detected_at.isoformat(),
            "kind": "incident_detected",
            "label": "incident detected",
            "detail": incident.title,
        }
    )

    # Attach non-causal relationship statements between the last change and the detection.
    change_events = [e for e in timeline if e["kind"] in {"deployment_completed", "configuration_change"}]
    if change_events:
        last = change_events[-1]
        relation = correlate_with_change(
            metric_change_ts=incident.detected_at.isoformat(),
            change_ts=last["ts"],
            metric_name=METRIC_LABELS.get(
                (incident.detection_meta or {}).get("primary_metric", "error_rate"),
                "the primary metric",
            ),
            change_label=last["label"],
        )
        timeline.append(
            {
                "ts": last["ts"],
                "kind": "correlation",
                "label": f"relationship: {relation['relationship']}",
                "detail": relation["statement"],
                "non_causal": True,
            }
        )

    timeline.sort(key=lambda e: e["ts"])
    return timeline


# ---------------------------------------------------------------------------------------
# Fingerprint
# ---------------------------------------------------------------------------------------
def fingerprint_metric_pattern(verdicts: dict[str, AnomalyVerdict]) -> dict[str, float]:
    """Normalised deviation per metric, on a scale that is comparable across incidents.

    ``relative_change * 10`` clipped to ±8. Using a relative measure rather than the raw
    robust z means a CPU-only incident and a pool-only incident have genuinely different
    shapes, which is what the pattern-correlation feature is for.
    """
    pattern: dict[str, float] = {}
    for metric, verdict in verdicts.items():
        value = max(-8.0, min(8.0, verdict.relative_change * 10.0))
        pattern[metric] = round(value, 4)
    return pattern


def compute_fingerprint(
    db: Session,
    engine: SimulationEngine,
    *,
    incident: Incident,
    service: Service,
    environment: Environment,
    verdicts: dict[str, AnomalyVerdict] | None = None,
    root_cause_category: str | None = None,
) -> IncidentFingerprint:
    verdicts = verdicts or detect_anomalies(engine, service_name=service.name)
    signatures = engine.recent_signatures(service_name=service.name, seconds=1800.0)
    significant = [
        s for s in signatures if s.is_new or s.spike_ratio >= 3 or s.level in {"ERROR", "FATAL"}
    ]
    deployments = engine.recent_deployments(service_name=service.name, seconds=7200.0, limit=4)
    fault = engine.active_fault(service.name)

    deployment_related = any(
        d.completed_at and (incident.detected_at - d.completed_at).total_seconds() <= 3600
        for d in deployments
    ) or bool(fault and fault.deployment_related)

    change_classes = sorted(
        {(d.meta or {}).get("change_class", "unknown") for d in deployments if d.meta}
    )

    return build_fingerprint(
        service=service.name,
        environment=environment.name,
        environment_kind=environment.kind,
        severity=incident.severity,
        root_cause_category=root_cause_category or incident.root_cause_category or "unknown",
        error_signatures=[s.hash for s in significant],
        symptom=incident.symptom,
        symptoms=[s.template for s in significant[:5]],
        metric_zscores=fingerprint_metric_pattern(verdicts),
        deployment_related=deployment_related,
        deployment_change_classes=change_classes,
        time_bucket=f"{incident.detected_at.hour:02d}",
    )


def fingerprint_candidates(
    db: Session, *, exclude_incident_id: int | None = None, limit: int = 200
) -> list[tuple[int, str, str, str, str, str, IncidentFingerprint]]:
    """Load historical fingerprints for similarity ranking.

    ``overlap_with`` limits the corpus to incidents *not* created in the same detection pass,
    so an incident is never ranked against itself.
    """
    stmt = select(Incident).where(Incident.fingerprint.is_not(None))
    if exclude_incident_id is not None:
        stmt = stmt.where(Incident.id != exclude_incident_id)
    rows = db.scalars(stmt.order_by(Incident.detected_at.desc()).limit(limit)).all()
    out: list[tuple[int, str, str, str, str, str, IncidentFingerprint]] = []
    for row in rows:
        service = db.get(Service, row.service_id)
        out.append(
            (
                row.id,
                row.title,
                service.name if service else "unknown",
                row.severity,
                row.root_cause_category or "unknown",
                row.detected_at.isoformat(),
                IncidentFingerprint.from_dict(row.fingerprint),
            )
        )
    return out


def incident_timeline(db: Session, incident_id: int) -> list[dict[str, Any]]:
    rows = db.scalars(
        select(IncidentEvent).where(IncidentEvent.incident_id == incident_id).order_by(IncidentEvent.ts)
    ).all()
    return [
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
        for row in rows
    ]


def add_event(
    db: Session,
    *,
    incident_id: int,
    kind: str,
    title: str,
    description: str = "",
    actor: str = "system",
    source: str = "platform",
    payload: dict[str, Any] | None = None,
    ts=None,
) -> IncidentEvent:
    row = IncidentEvent(
        incident_id=incident_id,
        ts=ts or sim_now(),
        kind=kind,
        title=title,
        description=description,
        actor=actor,
        source=source,
        payload=payload,
    )
    db.add(row)
    db.flush()
    return row


def dominant_signature_of(signatures: Sequence[SignatureStat]) -> SignatureStat | None:
    return dominant_signature(signatures)
