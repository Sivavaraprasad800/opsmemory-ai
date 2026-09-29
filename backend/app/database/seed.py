"""Sample data.

Two distinct seeds, deliberately separate because the demo needs both and they must not
interfere:

``seed_catalogue``
    Organization, users, project, environments, services, dependencies, runbooks and the
    remediation registry. Idempotent. This is the *world*, and it is always present.

``seed_history``
    A corpus of ~60 historical incidents generated from a designed causal template, so that
    similarity relationships between them are meaningful rather than random (build spec
    section 39). Optionally retained into organizational memory.

``clean_room``
    Wipes everything dynamic — incidents, evidence, investigations, remediation history,
    learning events, memory references and the memory banks themselves — while preserving the
    catalogue. This is what makes the demo idempotent (build spec section 52): you can run the
    learning loop live, twice, from a genuinely cold start.
"""

from __future__ import annotations

import logging
import random
from datetime import timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.security import hash_password
from app.database.models import (
    AiDecision,
    AiInvestigation,
    AiToolCall,
    AuditLog,
    ConfigurationChange,
    Deployment,
    DeploymentChange,
    Environment,
    ErrorSignature,
    HypothesisTest,
    Incident,
    IncidentEvent,
    IncidentEvidence,
    IncidentHypothesis,
    IntegrationConnection,
    LearningEvent,
    LogEvent,
    MemoryReference,
    MetricSample,
    Organization,
    Postmortem,
    Project,
    RemediationAction,
    RemediationAttempt,
    RemediationPolicy,
    RemediationRun,
    Runbook,
    Service,
    ServiceDependency,
    SystemEvent,
    TestResult,
    TestRun,
    User,
    VerificationResult,
    VerificationRun,
    utcnow,
)
from app.detection.logs import build_signature
from app.detection.similarity import build_fingerprint
from app.domain.remediation import seed_registry
from app.memory.documents import AttemptRecord, IncidentExperience
from app.memory.service import MemoryService
from app.sim.engine import SimulationEngine, sim_now

logger = logging.getLogger(__name__)

ORG_SLUG = "acme-payments"

SERVICES: tuple[dict[str, str], ...] = (
    {
        "name": "payment-service",
        "tier": "critical",
        "kind": "api",
        "language": "python",
        "owner_team": "payments",
        "description": "Authorises and captures card payments. Owns the primary payments database connection pool.",
    },
    {
        "name": "checkout-service",
        "tier": "critical",
        "kind": "api",
        "language": "go",
        "owner_team": "commerce",
        "description": "Orchestrates the checkout journey and calls payment-service.",
    },
    {
        "name": "order-service",
        "tier": "high",
        "kind": "api",
        "language": "python",
        "owner_team": "commerce",
        "description": "Order lifecycle, fulfilment hand-off and status projection.",
    },
    {
        "name": "auth-service",
        "tier": "critical",
        "kind": "api",
        "language": "node",
        "owner_team": "identity",
        "description": "Token issuance, session validation and credential rotation.",
    },
    {
        "name": "inventory-service",
        "tier": "high",
        "kind": "api",
        "language": "python",
        "owner_team": "supply",
        "description": "Stock levels and reservation ledger.",
    },
    {
        "name": "search-service",
        "tier": "medium",
        "kind": "api",
        "language": "java",
        "owner_team": "discovery",
        "description": "Product search and ranking.",
    },
    {
        "name": "notification-service",
        "tier": "low",
        "kind": "worker",
        "language": "python",
        "owner_team": "platform",
        "description": "Email and push delivery, asynchronous.",
    },
    {
        "name": "cache-service",
        "tier": "high",
        "kind": "cache",
        "language": "rust",
        "owner_team": "platform",
        "description": "Shared cache cluster used by payment and checkout.",
    },
)

DEPENDENCIES: tuple[tuple[str, str, str], ...] = (
    ("checkout-service", "payment-service", "http"),
    ("checkout-service", "inventory-service", "http"),
    ("order-service", "payment-service", "http"),
    ("payment-service", "cache-service", "tcp"),
    ("checkout-service", "cache-service", "tcp"),
    ("auth-service", "cache-service", "tcp"),
    ("search-service", "cache-service", "tcp"),
)

RUNBOOKS: tuple[dict[str, object], ...] = (
    {
        "title": "Database connection pool exhaustion",
        "slug": "db-connection-pool-exhaustion",
        "category": "database",
        "tags": ["database", "connections", "leak", "payment"],
        "content": (
            "1. Confirm pool saturation with the connections and connection_saturation metrics.\n"
            "2. Check whether a deployment changed pool sizing or disabled the leak guard.\n"
            "3. Restarting the service clears the pool but does NOT remove a leak; it will refill.\n"
            "4. Apply the known-safe pool configuration and confirm the leak guard flag is enabled.\n"
            "5. Verify that connection saturation returns to baseline within one minute."
        ),
        "times_used": 14,
        "times_effective": 11,
    },
    {
        "title": "Rolling back a bad deployment",
        "slug": "rollback-bad-deployment",
        "category": "change-management",
        "tags": ["deployment", "rollback"],
        "content": (
            "1. Identify the release completed closest before detection.\n"
            "2. Confirm the change touched configuration or infrastructure.\n"
            "3. Freeze further deploys, then roll back to the previous release.\n"
            "4. Confirm error rate and latency return to the pre-release baseline."
        ),
        "times_used": 9,
        "times_effective": 9,
    },
    {
        "title": "Memory pressure and heap growth",
        "slug": "memory-pressure",
        "category": "runtime",
        "tags": ["memory", "leak", "gc"],
        "content": (
            "1. Confirm resident memory is growing linearly rather than plateauing.\n"
            "2. Correlate growth with a deployment or a traffic pattern change.\n"
            "3. Apply the known-safe configuration, which bounds the cache and size of the working set.\n"
            "4. Restart alone defers the incident rather than resolving it."
        ),
        "times_used": 6,
        "times_effective": 5,
    },
    {
        "title": "Database saturation response",
        "slug": "database-saturation",
        "category": "database",
        "tags": ["database", "latency"],
        "content": (
            "1. Check whether latency is elevated for every client of the database simultaneously.\n"
            "2. If yes, the database is the saturation point: increase capacity.\n"
            "3. If only one client is affected, the cause is in that service, not the database."
        ),
        "times_used": 4,
        "times_effective": 4,
    },
    {
        "title": "Disk pressure escalation",
        "slug": "disk-pressure",
        "category": "infrastructure",
        "tags": ["disk", "escalation"],
        "content": (
            "1. Identify the volume and the largest consumers.\n"
            "2. Reclaiming space is DESTRUCTIVE and deliberately not automated by this platform.\n"
            "3. Escalate to a human with the volume, current utilisation and growth rate."
        ),
        "times_used": 2,
        "times_effective": 2,
    },
)


# ---------------------------------------------------------------------------------------
# Catalogue
# ---------------------------------------------------------------------------------------
def seed_catalogue(db: Session, *, include_simulated_estate: bool = True) -> dict[str, object]:
    """Create the catalogue, users, runbooks, policies and integrations.

    ``include_simulated_estate=False`` skips the eight fabricated services and their dependency
    graph. That is what a real-clock deployment wants: users, policies and runbooks are required
    for the platform to function at all, but seeding eight pretend services next to a connected
    real project would put fiction and reality on the same dashboard.
    """
    organization = db.scalar(select(Organization).where(Organization.slug == ORG_SLUG))
    if organization is None:
        organization = Organization(
            name="Acme Payments",
            slug=ORG_SLUG,
            mission=(
                "Operate payments infrastructure with a bias toward evidence. Prefer remediations "
                "that have been verified end-to-end, and never repeat an approach that already "
                "failed verification."
            ),
        )
        db.add(organization)
        db.flush()

    project = db.scalar(select(Project).where(Project.slug == "payments-platform"))
    if project is None:
        project = Project(
            organization_id=organization.id,
            name="Payments Platform",
            slug="payments-platform",
            description="Customer-facing payments, checkout, identity and inventory services.",
        )
        db.add(project)
        db.flush()

    environments: dict[str, Environment] = {}
    for name, kind, is_production in (
        ("production", "production", True),
        ("staging", "staging", False),
        ("development", "development", False),
    ):
        environment = db.scalar(
            select(Environment).where(Environment.project_id == project.id, Environment.name == name)
        )
        if environment is None:
            environment = Environment(
                project_id=project.id, name=name, kind=kind, is_production=is_production
            )
            db.add(environment)
            db.flush()
        environments[name] = environment

    services: dict[str, Service] = {}
    for spec in SERVICES if include_simulated_estate else ():
        service = db.scalar(
            select(Service).where(Service.project_id == project.id, Service.name == spec["name"])
        )
        if service is None:
            service = Service(
                project_id=project.id,
                name=spec["name"],
                tier=spec["tier"],
                kind=spec["kind"],
                language=spec["language"],
                owner_team=spec["owner_team"],
                description=spec["description"],
            )
            db.add(service)
            db.flush()
        services[spec["name"]] = service

    for source, target, kind in DEPENDENCIES if include_simulated_estate else ():
        existing = db.scalar(
            select(ServiceDependency).where(
                ServiceDependency.service_id == services[source].id,
                ServiceDependency.depends_on_service_id == services[target].id,
            )
        )
        if existing is None:
            db.add(
                ServiceDependency(
                    service_id=services[source].id,
                    depends_on_service_id=services[target].id,
                    kind=kind,
                )
            )

    for spec in RUNBOOKS:
        existing = db.scalar(select(Runbook).where(Runbook.slug == spec["slug"]))
        if existing is None:
            db.add(
                Runbook(
                    title=str(spec["title"]),
                    slug=str(spec["slug"]),
                    category=str(spec["category"]),
                    content=str(spec["content"]),
                    tags=list(spec["tags"]),  # type: ignore[arg-type]
                    times_used=int(spec["times_used"]),  # type: ignore[arg-type]
                    times_effective=int(spec["times_effective"]),  # type: ignore[arg-type]
                )
            )

    for email, name, role in (
        ("admin@acme.test", "Ada Admin", "admin"),
        ("sre@acme.test", "Sam SRE", "sre"),
        ("analyst@acme.test", "Alia Analyst", "analyst"),
        ("viewer@acme.test", "Vic Viewer", "viewer"),
    ):
        existing = db.scalar(
            select(User).where(User.organization_id == organization.id, User.email == email)
        )
        if existing is None:
            db.add(
                User(
                    organization_id=organization.id,
                    email=email,
                    full_name=name,
                    role=role,
                    password_hash=hash_password("password123"),
                )
            )

    policies = 0
    for action_code, kind, requires_approval, blocked, note in (
        ("restart_service", "staging", False, False, "safe mitigation in staging"),
        ("restart_service", "production", False, False, "permitted but verification decides"),
        ("rollback_deployment", "production", True, False, "requires SRE approval in production"),
        ("update_known_safe_configuration", "production", True, False, "requires SRE approval in production"),
        ("discard_and_wipe_volume", "production", True, True, "destructive: deliberately unsupported"),
    ):
        existing = db.scalar(
            select(RemediationPolicy).where(
                RemediationPolicy.project_id == project.id,
                RemediationPolicy.action_code == action_code,
                RemediationPolicy.environment_kind == kind,
            )
        )
        if existing is None:
            db.add(
                RemediationPolicy(
                    project_id=project.id,
                    action_code=action_code,
                    environment_kind=kind,
                    max_autonomy_level=4,
                    requires_approval=requires_approval,
                    is_blocked=blocked,
                    note=note,
                )
            )
            policies += 1

    for kind, name, base_url, status in (
        ("memory", "Hindsight organizational memory", "", "not_configured"),
        ("reasoning", "OpenAI reasoning layer", "https://api.openai.com", "not_configured"),
        ("vcs", "GitHub", "https://api.github.com", "not_configured"),
        ("self_hosted", "OpsMemory internal database", "", "healthy"),
    ):
        existing = db.scalar(
            select(IntegrationConnection).where(
                IntegrationConnection.organization_id == organization.id,
                IntegrationConnection.kind == kind,
            )
        )
        if existing is None:
            db.add(
                IntegrationConnection(
                    organization_id=organization.id,
                    kind=kind,
                    name=name,
                    base_url=base_url,
                    status=status,
                    config={"note": "configure via environment variables; secrets never reach the browser"},
                )
            )

    actions_created = seed_registry(db)
    db.flush()
    return {
        "organization_id": organization.id,
        "project_id": project.id,
        "services": {name: service.id for name, service in services.items()},
        "environments": {name: environment.id for name, environment in environments.items()},
        "remediation_actions_created": actions_created,
        "policies_created": policies,
    }


# ---------------------------------------------------------------------------------------
# Historical corpus
# ---------------------------------------------------------------------------------------
HISTORY_TEMPLATE: tuple[dict[str, object], ...] = (
    {
        "weight": 16,
        "service": "order-service",
        "category": "memory_leak",
        "title": "order-service: memory utilisation anomaly in production",
        "symptom": "resident memory grew steadily until the runtime spent most of its time collecting garbage",
        "failed": ["restart_service"],
        "successful": "update_known_safe_configuration",
        "signatures": ["heap usage high resident={mem} gc_pause={duration}", "memory limit approaching resident={mem} limit=2048MB"],
        "deployment_related": True,
        "metrics": {"mem_pct": 6.4, "latency_ms": 2.1, "error_rate": 0.9, "cpu_pct": 1.2},
    },
    {
        "weight": 14,
        "service": "search-service",
        "category": "bad_deployment_config",
        "title": "search-service: latency anomaly in production",
        "symptom": "a release removed the request timeout guard and latency collapsed throughput",
        "failed": [],
        "successful": "rollback_deployment",
        "signatures": ["GET /v1/{path} slow duration={duration} p95_breach=true"],
        "deployment_related": True,
        "metrics": {"latency_ms": 4.2, "error_rate": 1.4, "cpu_pct": 2.2},
    },
    {
        "weight": 12,
        "service": "auth-service",
        "category": "expired_credentials",
        "title": "auth-service: error rate anomaly in production",
        "symptom": "credential rotation completed but the previous token expired before the rollout finished",
        "failed": ["restart_service"],
        "successful": "rotate_expired_token",
        "signatures": ["authentication failed calling dependency: token expired"],
        "deployment_related": False,
        "metrics": {"error_rate": 5.8, "latency_ms": 0.6},
    },
    {
        "weight": 10,
        "service": "inventory-service",
        "category": "cpu_saturation",
        "title": "inventory-service: CPU utilisation anomaly in production",
        "symptom": "a stock-recalculation job saturated every instance in the pool",
        "failed": [],
        "successful": "scale_service",
        "signatures": ["GET /v1/{path} slow duration={duration} p95_breach=true"],
        "deployment_related": False,
        "metrics": {"cpu_pct": 5.1, "latency_ms": 3.4, "queue_depth": 4.0},
    },
    {
        "weight": 8,
        "service": "checkout-service",
        "category": "database_overload",
        "title": "checkout-service: latency anomaly in production",
        "symptom": "every client of the database saw elevated query latency simultaneously",
        "failed": [],
        "successful": "increase_database_capacity",
        "signatures": ["database query slow duration={duration}", "database query timeout after {duration}"],
        "deployment_related": False,
        "metrics": {"latency_ms": 6.1, "error_rate": 2.3, "cpu_pct": 3.1},
    },
    {
        "weight": 6,
        "service": "payment-service",
        "category": "cache_misconfiguration",
        "title": "payment-service: error rate anomaly in production",
        "symptom": "a cache namespace change was deployed without invalidation, serving stale entries",
        "failed": ["restart_service"],
        "successful": "clear_safe_cache",
        "signatures": ["cache served stale entry namespace=v7 key=<HASH>"],
        "deployment_related": True,
        "metrics": {"error_rate": 3.2, "latency_ms": 1.1},
    },
    {
        "weight": 5,
        "service": "notification-service",
        "category": "network_issue",
        "title": "notification-service: latency anomaly in staging",
        "symptom": "a network policy shipped with a release dropped packets to the mail relay",
        "failed": [],
        "successful": "rollback_deployment",
        "signatures": ["upstream call timed out after {duration} host=<HOST>"],
        "deployment_related": True,
        "metrics": {"latency_ms": 4.8, "error_rate": 1.9},
    },
)

"""Deliberately designed, not random (build spec section 39).

Note what is *absent*: there is no ``connection_leak`` on ``payment-service`` anywhere in this
corpus. That omission is intentional and is what makes the demo honest — incident 1 genuinely
has no precedent for its exact root-cause/service combination, while still having *related*
history for the similarity engine to surface."""

RUNBOOK_FOR_CATEGORY = {
    "memory_leak": "memory-pressure",
    "bad_deployment_config": "rollback-bad-deployment",
    "expired_credentials": "db-connection-pool-exhaustion",
    "cpu_saturation": "database-saturation",
    "database_overload": "database-saturation",
    "cache_misconfiguration": "rollback-bad-deployment",
    "network_issue": "rollback-bad-deployment",
    "connection_leak": "db-connection-pool-exhaustion",
    "disk_pressure": "disk-pressure",
}


def _weighted_choice(rng: random.Random) -> dict[str, object]:
    total = sum(int(t["weight"]) for t in HISTORY_TEMPLATE)
    pick = rng.uniform(0, total)
    cumulative = 0.0
    for template in HISTORY_TEMPLATE:
        cumulative += int(template["weight"])
        if pick <= cumulative:
            return template
    return HISTORY_TEMPLATE[0]


def seed_history(
    db: Session,
    *,
    count: int = 60,
    days: int = 45,
    retain_to_memory: bool = False,
    memory: MemoryService | None = None,
) -> dict[str, object]:
    """Generate a designed historical incident corpus.

    Records are interconnected on purpose: service, environment, error signatures, metric
    shape, root-cause category, deployment relationship and *remediation outcome* are all
    consistent within a template, so the similarity engine and the pattern analyser produce
    relationships that mean something.
    """
    catalogue = seed_catalogue(db)
    project_id = int(catalogue["project_id"])
    service_ids: dict[str, int] = catalogue["services"]  # type: ignore[assignment]
    environment_ids: dict[str, int] = catalogue["environments"]  # type: ignore[assignment]

    rng = random.Random(20260928)
    now = sim_now()
    created: list[int] = []
    attempts_created = 0

    for index in range(count):
        template = _weighted_choice(rng)
        service_name = str(template["service"])
        category = str(template["category"])
        environment_name = "production" if "staging" not in str(template["title"]) else "staging"
        if index % 11 == 0:
            environment_name = "staging"
        detected_at = now - timedelta(
            seconds=rng.randint(3600, days * 86400), minutes=rng.randint(0, 59)
        )
        service_id = service_ids[service_name]
        environment_id = environment_ids[environment_name]
        severity = "critical" if category in {"database_overload", "expired_credentials"} else "high"

        signature_hashes = []
        for raw in template["signatures"]:  # type: ignore[union-attr]
            message = f"{service_name} " + str(raw).format(
                duration="1.20s", mem="91%", disk="96.0%", path="payments", version="v2.28.0"
            )
            digest, _template, _level = build_signature(message)
            signature_hashes.append(digest)

        metrics: dict[str, float] = template["metrics"]  # type: ignore[assignment]
        fingerprint = build_fingerprint(
            service=service_name,
            environment=environment_name,
            environment_kind="production" if environment_name == "production" else "staging",
            severity=severity,
            root_cause_category=category,
            error_signatures=signature_hashes,
            symptom=str(template["symptom"]),
            symptoms=[str(s) for s in template["signatures"]],  # type: ignore[union-attr]
            metric_zscores=metrics,
            deployment_related=bool(template["deployment_related"]),
            deployment_change_classes=["application_config"] if template["deployment_related"] else [],
            time_bucket=f"{detected_at.hour:02d}",
        )

        resolved_at = detected_at + timedelta(minutes=rng.randint(9, 95))
        incident = Incident(
            project_id=project_id,
            service_id=service_id,
            environment_id=environment_id,
            title=str(template["title"]),
            symptom=str(template["symptom"]),
            severity=severity,
            status="verified",
            root_cause_category=category,
            root_cause_summary=f"{category.replace('_', ' ')} on {service_name} ({environment_name})",
            detected_at=detected_at,
            resolved_at=resolved_at,
            verified_at=resolved_at,
            mttr_seconds=(resolved_at - detected_at).total_seconds(),
            fingerprint=fingerprint.to_dict(),
            baseline=_synthetic_baseline(category),
            detection_meta={
                "primary_metric": _primary_metric(category),
                "detector": "committee(mad-z, rolling-median, ewma, cusum)",
                "synthetic": True,
            },
            is_historical=True,
        )
        db.add(incident)
        db.flush()
        created.append(incident.id)

        db.add(
            IncidentEvent(
                incident_id=incident.id,
                ts=detected_at,
                kind="detected",
                title="Incident detected",
                description=f"Detected by the anomaly committee; primary signal was {_primary_metric(category)}.",
                actor="detection_engine",
                source="detection_engine",
            )
        )
        deployment_id = None
        if template["deployment_related"]:
            deployment = Deployment(
                service_id=service_id,
                environment_id=environment_id,
                version=f"v2.{rng.randint(10, 40)}.{rng.randint(0, 9)}",
                commit_sha=f"{rng.randrange(16**12):012x}",
                commit_message=f"change touching {category.replace('_', ' ')} handling",
                author=rng.choice(["dev-a@acme.test", "dev-b@acme.test", "dev-c@acme.test"]),
                status="succeeded",
                started_at=detected_at - timedelta(minutes=rng.randint(6, 55)),
                completed_at=detected_at - timedelta(minutes=rng.randint(2, 5)),
                is_suspected_cause=True,
                risk_score=round(rng.uniform(0.3, 0.8), 2),
                meta={"change_class": "application_config"},
            )
            db.add(deployment)
            db.flush()
            deployment_id = deployment.id
            incident.suspected_deployment_id = deployment.id
            db.add(
                DeploymentChange(
                    deployment_id=deployment.id,
                    file_path="deploy/service.yaml",
                    change_type="modify",
                    summary=deployment.commit_message,
                    risk_score=deployment.risk_score,
                    touches_config=True,
                    diff_excerpt="-  pool: {}\n+  pool: {{size: 8}}\n".format(category[:12]),
                )
            )

        db.add(
            IncidentEvidence(
                incident_id=incident.id,
                kind="metric_anomaly",
                summary=(
                    f"{_primary_metric(category)} moved {max(metrics.values()):.1f} sigma-equivalents "
                    f"from its rolling baseline"
                ),
                detail={"metric_z": metrics},
                source="metric_detector",
                collected_at=detected_at,
                strength="strong",
            )
        )
        if signature_hashes:
            db.add(
                IncidentEvidence(
                    incident_id=incident.id,
                    kind="error_signature",
                    summary=f"{len(signature_hashes)} error signature(s) associated with this incident",
                    detail={"hashes": signature_hashes},
                    source="log_pipeline",
                    collected_at=detected_at,
                    strength="strong",
                )
            )
        if deployment_id:
            db.add(
                IncidentEvidence(
                    incident_id=incident.id,
                    kind="deployment",
                    summary=f"deployment {deployment.version} completed before detection",
                    detail={"deployment_id": deployment_id, "relationship": "preceded_by"},
                    source="deployment_history",
                    collected_at=detected_at,
                    strength="strong",
                )
            )

        attempt_number = 0
        for failed_action in template["failed"]:  # type: ignore[union-attr]
            attempt_number += 1
            db.add(
                RemediationAttempt(
                    incident_id=incident.id,
                    service_id=service_id,
                    root_cause_category=category,
                    action_code=str(failed_action),
                    attempt_number=attempt_number,
                    outcome="failed",
                    failure_reason=(
                        "verification failed: the underlying fault remained active after execution"
                    ),
                    created_at=detected_at + timedelta(minutes=3 * attempt_number),
                )
            )
            attempts_created += 1
        attempt_number += 1
        db.add(
            RemediationAttempt(
                incident_id=incident.id,
                service_id=service_id,
                root_cause_category=category,
                action_code=str(template["successful"]),
                attempt_number=attempt_number,
                outcome="succeeded",
                failure_reason="",
                created_at=detected_at + timedelta(minutes=4 * attempt_number),
            )
        )
        attempts_created += 1

        verification = VerificationRun(
            incident_id=incident.id,
            status="completed",
            verdict="verified_success",
            verdict_reason="primary signal returned to its pre-incident baseline and the fault was removed",
            settle_seconds=45,
            started_at=resolved_at - timedelta(minutes=2),
            completed_at=resolved_at,
            before_state=_synthetic_before(category),
            after_state=_synthetic_after(category),
            improvement_pct=round(rng.uniform(62.0, 96.0), 1),
        )
        db.add(verification)

        postmortem = Postmortem(
            incident_id=incident.id,
            status="approved",
            summary=f"{incident.title}. Resolved via {template['successful']}.",
            impact=f"severity {severity} on a {_tier(service_name)}-tier service",
            detection=f"Anomaly committee detected a change in {_primary_metric(category)}.",
            root_cause=f"{category.replace('_', ' ')} — {template['symptom']}",
            timeline=[
                {"ts": detected_at.isoformat(), "kind": "detected", "label": "incident detected"},
                {"ts": resolved_at.isoformat(), "kind": "verified", "label": "verification succeeded"},
            ],
            failed_attempts=[
                {"action_code": str(a), "outcome": "failed", "note": "fault remained active"}
                for a in template["failed"]  # type: ignore[union-attr]
            ],
            successful_remediation=str(template["successful"]),
            verification="verified_success",
            deployment_relationship=(
                "deployment preceded detection (temporal relationship only)"
                if template["deployment_related"]
                else "not deployment-related"
            ),
            lessons=[
                f"{template['successful']} resolved this class of incident; "
                + (
                    f"{', '.join(str(a) for a in template['failed'])} did not."  # type: ignore[union-attr]
                    if template["failed"]  # type: ignore[truthy-function]
                    else "no failed attempt was recorded."
                )
            ],
            preventive_actions=[
                f"alert on {_primary_metric(category)} crossing its rolling baseline by 25% for two samples",
                "pin reviewed configuration values in the deployment manifest",
            ],
            created_at=resolved_at,
            updated_at=resolved_at,
        )
        db.add(postmortem)

        if retain_to_memory and memory is not None:
            experience = IncidentExperience(
                incident_id=incident.id,
                title=incident.title,
                service=service_name,
                environment=environment_name,
                severity=severity,
                detected_at=detected_at.isoformat(),
                trigger=_primary_metric(category),
                symptoms=[str(template["symptom"])],
                evidence=[f"{_primary_metric(category)} deviated {max(metrics.values()):.1f} sigma"],
                timeline=[f"{detected_at.isoformat()} detected", f"{resolved_at.isoformat()} verified"],
                root_cause_category=category,
                root_cause=f"{category.replace('_', ' ')}: {template['symptom']}",
                attempts=[
                    AttemptRecord(
                        attempt_number=i + 1,
                        action_code=str(action),
                        outcome="failed",
                        reason="verification failed: the underlying fault remained active",
                    )
                    for i, action in enumerate(template["failed"])  # type: ignore[arg-type]
                ]
                + [
                    AttemptRecord(
                        attempt_number=len(template["failed"]) + 1,  # type: ignore[arg-type]
                        action_code=str(template["successful"]),
                        outcome="succeeded",
                        reason="primary signal returned to baseline",
                    )
                ],
                successful_remediation=str(template["successful"]),
                verification="verified_success",
                lessons=[
                    f"{', '.join(str(a) for a in template['failed'])} failed; "  # type: ignore[union-attr]
                    f"{template['successful']} worked"
                ]
                if template["failed"]  # type: ignore[truthy-function]
                else [f"{template['successful']} worked"],
                preventive_actions=["pin configuration values in the deployment manifest"],
                deployment_relationship=(
                    "deployment preceded detection" if template["deployment_related"] else "not deployment-related"
                ),
                mttr_seconds=(resolved_at - detected_at).total_seconds(),
            )
            memory.retain_incident_experience(experience=experience, fingerprint=fingerprint, db=db)

    db.flush()
    return {
        "incidents_created": len(created),
        "incident_ids": created,
        "remediation_attempts": attempts_created,
        "retained_to_memory": retain_to_memory,
    }


def _tier(service_name: str) -> str:
    for spec in SERVICES:
        if spec["name"] == service_name:
            return spec["tier"]
    return "medium"


def _primary_metric(category: str) -> str:
    return {
        "memory_leak": "mem_pct",
        "bad_deployment_config": "latency_ms",
        "expired_credentials": "error_rate",
        "cpu_saturation": "cpu_pct",
        "database_overload": "latency_ms",
        "cache_misconfiguration": "error_rate",
        "network_issue": "latency_ms",
        "connection_leak": "connection_saturation",
        "disk_pressure": "disk_pct",
    }.get(category, "error_rate")


def _synthetic_baseline(category: str) -> dict[str, float]:
    base = {
        "error_rate": 0.004,
        "latency_ms": 128.0,
        "cpu_pct": 22.0,
        "mem_pct": 46.0,
        "disk_pct": 52.0,
        "connections": 6.0,
        "pool_size": 50.0,
        "health_score": 0.98,
        "connection_saturation": 0.12,
        "queue_depth": 3.0,
    }
    base["connection_saturation"] = 0.12
    return base


def _synthetic_before(category: str) -> dict[str, float]:
    return {
        "memory_leak": {"mem_pct": 93.2, "latency_ms": 1180.0, "error_rate": 0.31},
        "cpu_saturation": {"cpu_pct": 96.5, "latency_ms": 1420.0, "error_rate": 0.22},
        "database_overload": {"latency_ms": 4820.0, "error_rate": 0.19},
        "connection_leak": {"connection_saturation": 0.99, "error_rate": 0.34, "latency_ms": 4610.0},
        "disk_pressure": {"disk_pct": 99.1, "error_rate": 0.18},
    }.get(category, {"error_rate": 0.28, "latency_ms": 2210.0})


def _synthetic_after(category: str) -> dict[str, float]:
    return {
        "memory_leak": {"mem_pct": 48.1, "latency_ms": 205.0, "error_rate": 0.006},
        "cpu_saturation": {"cpu_pct": 31.0, "latency_ms": 240.0, "error_rate": 0.007},
        "database_overload": {"latency_ms": 290.0, "error_rate": 0.009},
        "connection_leak": {"connection_saturation": 0.13, "error_rate": 0.007, "latency_ms": 214.0},
        "disk_pressure": {"disk_pct": 64.0, "error_rate": 0.004},
    }.get(category, {"error_rate": 0.008, "latency_ms": 260.0})


# ---------------------------------------------------------------------------------------
# Clean room
# ---------------------------------------------------------------------------------------
DYNAMIC_TABLES = (
    AiToolCall,
    AiDecision,
    AiInvestigation,
    HypothesisTest,
    IncidentHypothesis,
    IncidentEvidence,
    IncidentEvent,
    VerificationResult,
    VerificationRun,
    RemediationAttempt,
    RemediationRun,
    Postmortem,
    MemoryReference,
    LearningEvent,
    ErrorSignature,
    Incident,
    ConfigurationChange,
    DeploymentChange,
    Deployment,
    TestResult,
    TestRun,
    LogEvent,
    MetricSample,
    SystemEvent,
    AuditLog,
)


def _wipe_order(models: tuple[type, ...]) -> list[type]:
    """Order the models so that children are deleted before the rows they reference.

    The order is derived from the real foreign-key graph rather than written by hand. A manual
    list is a trap: the moment someone adds a foreign key, the reset starts failing with an
    opaque ``FOREIGN KEY constraint failed`` and the demo's cold start silently breaks. Looking
    at the graph makes that impossible — and it caught exactly that bug: ``remediation_attempts``
    references ``verification_runs``, and ``error_signatures`` references ``incidents``.

    Only dependencies *inside* the wiped set matter; references to preserved catalogue tables
    (projects, services, environments, users) never constrain the delete order. Self-references
    are ignored for the same reason a single ``DELETE FROM t`` clears them in SQLite: the
    referencing rows are removed by the very same statement.
    """
    selected = {model.__tablename__: model for model in models}
    dependents: dict[str, set[str]] = {name: set() for name in selected}
    for name, model in selected.items():
        for foreign_key in model.__table__.foreign_keys:
            parent = foreign_key.target_fullname.split(".")[0]
            if parent in selected and parent != name:
                # `name` references `parent`, so `name` must be deleted first.
                dependents[parent].add(name)

    ordered: list[type] = []
    emitted: set[str] = set()
    while len(ordered) < len(selected):
        progressed = False
        for name in sorted(selected):
            if name in emitted:
                continue
            if dependents[name] <= emitted:
                ordered.append(selected[name])
                emitted.add(name)
                progressed = True
        if not progressed:  # a cycle: fall back to name order for the remainder
            for name in sorted(selected):
                if name not in emitted:
                    ordered.append(selected[name])
                    emitted.add(name)
            break
    return ordered


def clean_room(db: Session, *, memory: MemoryService | None = None) -> dict[str, object]:
    """Reset everything dynamic while preserving the catalogue. Idempotent demo reset."""
    removed: dict[str, int] = {}

    for model in _wipe_order(DYNAMIC_TABLES):
        result = db.execute(delete(model))
        removed[model.__tablename__] = result.rowcount or 0

    db.flush()

    if memory is not None:
        memory.reset()

    engine = SimulationEngine(db)
    engine.load_catalogue()
    engine.reset_world()
    engine.load_catalogue()
    db.flush()

    from app.domain.incidents import reset_detection_state

    reset_detection_state()

    return {"tables_cleared": removed, "memory_reset": memory is not None}


def seed_all(
    db: Session, *, with_history: bool = False, history_count: int = 60, memory: MemoryService | None = None
) -> dict[str, object]:
    catalogue = seed_catalogue(db)
    payload: dict[str, object] = {"catalogue": catalogue}
    if with_history:
        payload["history"] = seed_history(
            db, count=history_count, retain_to_memory=False, memory=memory
        )
    db.commit()
    return payload
