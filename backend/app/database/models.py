"""SQLAlchemy 2.0 models for the complete OpsMemory AI schema (build spec section 35).

Design rules applied throughout:

* **Timezone-safe timestamps.**  A ``TZDateTime`` type decorator stores naive UTC and always
  hands back timezone-aware UTC. This removes an entire class of "naive vs aware" bugs that
  would otherwise surface only when the analytics layer compares detector timestamps.
* **Portable DDL.**  Enums are stored as constrained strings and structured payloads as JSON,
  so the same schema runs on SQLite (zero-infrastructure demo) and PostgreSQL (production)
  without a migration fork.
* **Evidence is first-class.**  ``incident_evidence``, ``ai_tool_calls`` and
  ``verification_results`` are real tables, not log lines, because spec section 49 requires
  every claim made by the AI to be traceable to a stored row.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class TZDateTime(TypeDecorator):
    """Store naive UTC, always return timezone-aware UTC."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect) -> datetime | None:  # noqa: ANN001
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect) -> datetime | None:  # noqa: ANN001
        if value is None:
            return None
        return value.replace(tzinfo=timezone.utc)


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSON, list[str]: JSON, datetime: TZDateTime}


def _ts(**kwargs) -> Mapped[datetime]:
    return mapped_column(TZDateTime, **kwargs)


# ---------------------------------------------------------------------------------------
# Tenancy and topology
# ---------------------------------------------------------------------------------------
class Organization(Base):
    __tablename__ = "organizations"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    slug: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    mission: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = _ts(default=utcnow)

    users: Mapped[list["User"]] = relationship(back_populates="organization")
    projects: Mapped[list["Project"]] = relationship(back_populates="organization")


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    email: Mapped[str] = mapped_column(String(200), nullable=False)
    full_name: Mapped[str] = mapped_column(String(160), default="")
    role: Mapped[str] = mapped_column(String(20), default="viewer")
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = _ts(default=utcnow)

    organization: Mapped[Organization] = relationship(back_populates="users")

    __table_args__ = (UniqueConstraint("organization_id", "email", name="uq_user_email"),)


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    slug: Mapped[str] = mapped_column(String(80), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = _ts(default=utcnow)

    organization: Mapped[Organization] = relationship(back_populates="projects")
    services: Mapped[list["Service"]] = relationship(back_populates="project")
    environments: Mapped[list["Environment"]] = relationship(back_populates="project")


class Environment(Base):
    __tablename__ = "environments"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    name: Mapped[str] = mapped_column(String(60), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), default="staging")
    is_production: Mapped[bool] = mapped_column(Boolean, default=False)

    project: Mapped[Project] = relationship(back_populates="environments")

    __table_args__ = (UniqueConstraint("project_id", "name", name="uq_env_name"),)


class Service(Base):
    __tablename__ = "services"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    tier: Mapped[str] = mapped_column(String(20), default="medium")
    kind: Mapped[str] = mapped_column(String(40), default="api")
    language: Mapped[str] = mapped_column(String(40), default="python")
    owner_team: Mapped[str] = mapped_column(String(120), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    fragility_score: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = _ts(default=utcnow)

    project: Mapped[Project] = relationship(back_populates="services")

    __table_args__ = (UniqueConstraint("project_id", "name", name="uq_service_name"),)


class ServiceDependency(Base):
    __tablename__ = "service_dependencies"

    id: Mapped[int] = mapped_column(primary_key=True)
    service_id: Mapped[int] = mapped_column(ForeignKey("services.id"), index=True)
    depends_on_service_id: Mapped[int] = mapped_column(ForeignKey("services.id"), index=True)
    kind: Mapped[str] = mapped_column(String(40), default="http")
    critical: Mapped[bool] = mapped_column(Boolean, default=True)

    __table_args__ = (
        UniqueConstraint("service_id", "depends_on_service_id", name="uq_service_dep"),
    )


# ---------------------------------------------------------------------------------------
# Telemetry (the evidence substrate produced by the simulated environment)
# ---------------------------------------------------------------------------------------
class MetricSample(Base):
    """Narrow time-series table: one row per (service, metric, timestamp)."""

    __tablename__ = "metric_samples"

    id: Mapped[int] = mapped_column(primary_key=True)
    service_id: Mapped[int] = mapped_column(ForeignKey("services.id"), index=True)
    ts: Mapped[datetime] = _ts(index=True)
    name: Mapped[str] = mapped_column(String(60), index=True)
    value: Mapped[float] = mapped_column(Float)
    labels: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)

    __table_args__ = (Index("ix_metric_service_name_ts", "service_id", "name", "ts"),)


class LogEvent(Base):
    __tablename__ = "log_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    service_id: Mapped[int] = mapped_column(ForeignKey("services.id"), index=True)
    ts: Mapped[datetime] = _ts(index=True)
    level: Mapped[str] = mapped_column(String(12), default="INFO")
    message: Mapped[str] = mapped_column(Text)
    signature_hash: Mapped[str | None] = mapped_column(String(40), index=True, default=None)
    deployment_id: Mapped[int | None] = mapped_column(
        ForeignKey("deployments.id"), nullable=True, default=None
    )

    __table_args__ = (Index("ix_log_service_ts", "service_id", "ts"),)


class ErrorSignature(Base):
    """Normalised, counted error template (spec section 10)."""

    __tablename__ = "error_signatures"

    id: Mapped[int] = mapped_column(primary_key=True)
    service_id: Mapped[int] = mapped_column(ForeignKey("services.id"), index=True)
    incident_id: Mapped[int | None] = mapped_column(
        ForeignKey("incidents.id"), nullable=True, index=True, default=None
    )
    deployment_id: Mapped[int | None] = mapped_column(
        ForeignKey("deployments.id"), nullable=True, default=None
    )
    hash: Mapped[str] = mapped_column(String(40), index=True)
    template: Mapped[str] = mapped_column(Text)
    sample: Mapped[str] = mapped_column(Text, default="")
    level: Mapped[str] = mapped_column(String(12), default="ERROR")
    count: Mapped[int] = mapped_column(Integer, default=0)
    baseline_rate: Mapped[float] = mapped_column(Float, default=0.0)
    observed_rate: Mapped[float] = mapped_column(Float, default=0.0)
    severity: Mapped[str] = mapped_column(String(20), default="medium")
    first_seen: Mapped[datetime] = _ts(default=utcnow)
    last_seen: Mapped[datetime] = _ts(default=utcnow)

    __table_args__ = (
        UniqueConstraint("service_id", "hash", "incident_id", name="uq_signature_scope"),
    )


# ---------------------------------------------------------------------------------------
# Change management
# ---------------------------------------------------------------------------------------
class Deployment(Base):
    __tablename__ = "deployments"

    id: Mapped[int] = mapped_column(primary_key=True)
    service_id: Mapped[int] = mapped_column(ForeignKey("services.id"), index=True)
    environment_id: Mapped[int] = mapped_column(ForeignKey("environments.id"), index=True)
    version: Mapped[str] = mapped_column(String(60))
    commit_sha: Mapped[str] = mapped_column(String(40), default="")
    commit_message: Mapped[str] = mapped_column(Text, default="")
    author: Mapped[str] = mapped_column(String(160), default="")
    status: Mapped[str] = mapped_column(String(24), default="succeeded")
    strategy: Mapped[str] = mapped_column(String(24), default="rolling")
    started_at: Mapped[datetime] = _ts(default=utcnow)
    completed_at: Mapped[datetime | None] = _ts(default=None, nullable=True)
    rollback_of_id: Mapped[int | None] = mapped_column(
        ForeignKey("deployments.id"), nullable=True, default=None
    )
    is_suspected_cause: Mapped[bool] = mapped_column(Boolean, default=False)
    risk_score: Mapped[float] = mapped_column(Float, default=0.0)
    meta: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)

    changes: Mapped[list["DeploymentChange"]] = relationship(back_populates="deployment")

    __table_args__ = (Index("ix_deploy_service_started", "service_id", "started_at"),)


class DeploymentChange(Base):
    __tablename__ = "deployment_changes"

    id: Mapped[int] = mapped_column(primary_key=True)
    deployment_id: Mapped[int] = mapped_column(ForeignKey("deployments.id"), index=True)
    file_path: Mapped[str] = mapped_column(String(300))
    change_type: Mapped[str] = mapped_column(String(40), default="modify")
    summary: Mapped[str] = mapped_column(Text, default="")
    risk_score: Mapped[float] = mapped_column(Float, default=0.0)
    touches_config: Mapped[bool] = mapped_column(Boolean, default=False)
    diff_excerpt: Mapped[str] = mapped_column(Text, default="")

    deployment: Mapped[Deployment] = relationship(back_populates="changes")


class ConfigurationChange(Base):
    """Non-deployment change (spec section 18 "what changed?"): config, infra, database."""

    __tablename__ = "configuration_changes"

    id: Mapped[int] = mapped_column(primary_key=True)
    service_id: Mapped[int | None] = mapped_column(
        ForeignKey("services.id"), nullable=True, index=True, default=None
    )
    environment_id: Mapped[int | None] = mapped_column(
        ForeignKey("environments.id"), nullable=True, default=None
    )
    key: Mapped[str] = mapped_column(String(160))
    old_value: Mapped[str] = mapped_column(Text, default="")
    new_value: Mapped[str] = mapped_column(Text, default="")
    change_class: Mapped[str] = mapped_column(String(40), default="application_config")
    actor: Mapped[str] = mapped_column(String(160), default="system")
    changed_at: Mapped[datetime] = _ts(default=utcnow, index=True)


class TestRun(Base):
    __tablename__ = "test_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    deployment_id: Mapped[int | None] = mapped_column(
        ForeignKey("deployments.id"), nullable=True, default=None
    )
    branch: Mapped[str] = mapped_column(String(160), default="main")
    commit_sha: Mapped[str] = mapped_column(String(40), default="")
    status: Mapped[str] = mapped_column(String(24), default="passed")
    started_at: Mapped[datetime] = _ts(default=utcnow)
    completed_at: Mapped[datetime | None] = _ts(default=None, nullable=True)
    total: Mapped[int] = mapped_column(Integer, default=0)
    passed: Mapped[int] = mapped_column(Integer, default=0)
    failed: Mapped[int] = mapped_column(Integer, default=0)
    skipped: Mapped[int] = mapped_column(Integer, default=0)
    coverage_pct: Mapped[float] = mapped_column(Float, default=0.0)
    log_excerpt: Mapped[str] = mapped_column(Text, default="")

    results: Mapped[list["TestResult"]] = relationship(back_populates="run")


class TestResult(Base):
    __tablename__ = "test_results"

    id: Mapped[int] = mapped_column(primary_key=True)
    test_run_id: Mapped[int] = mapped_column(ForeignKey("test_runs.id"), index=True)
    suite: Mapped[str] = mapped_column(String(120), default="")
    name: Mapped[str] = mapped_column(String(240))
    status: Mapped[str] = mapped_column(String(20), default="passed")
    duration_ms: Mapped[float] = mapped_column(Float, default=0.0)
    file_path: Mapped[str] = mapped_column(String(300), default="")
    failure_message: Mapped[str] = mapped_column(Text, default="")
    stack_trace: Mapped[str] = mapped_column(Text, default="")
    ai_diagnosis: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    ai_patch_attempts: Mapped[int] = mapped_column(Integer, default=0)

    run: Mapped[TestRun] = relationship(back_populates="results")


class Runbook(Base):
    __tablename__ = "runbooks"

    id: Mapped[int] = mapped_column(primary_key=True)
    service_id: Mapped[int | None] = mapped_column(
        ForeignKey("services.id"), nullable=True, index=True, default=None
    )
    title: Mapped[str] = mapped_column(String(240))
    slug: Mapped[str] = mapped_column(String(120), unique=True)
    category: Mapped[str] = mapped_column(String(80), default="general")
    content: Mapped[str] = mapped_column(Text, default="")
    tags: Mapped[list[str] | None] = mapped_column(JSON, default=None)
    times_used: Mapped[int] = mapped_column(Integer, default=0)
    times_effective: Mapped[int] = mapped_column(Integer, default=0)
    last_used_at: Mapped[datetime | None] = _ts(default=None, nullable=True)


# ---------------------------------------------------------------------------------------
# Incidents and the investigation record
# ---------------------------------------------------------------------------------------
class Incident(Base):
    __tablename__ = "incidents"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    service_id: Mapped[int] = mapped_column(ForeignKey("services.id"), index=True)
    environment_id: Mapped[int] = mapped_column(ForeignKey("environments.id"), index=True)
    title: Mapped[str] = mapped_column(String(240))
    symptom: Mapped[str] = mapped_column(Text, default="")
    severity: Mapped[str] = mapped_column(String(20), default="medium")
    status: Mapped[str] = mapped_column(String(30), default="detected", index=True)
    root_cause_category: Mapped[str | None] = mapped_column(String(80), default=None, index=True)
    root_cause_summary: Mapped[str] = mapped_column(Text, default="")
    detected_at: Mapped[datetime] = _ts(default=utcnow, index=True)
    acknowledged_at: Mapped[datetime | None] = _ts(default=None, nullable=True)
    resolved_at: Mapped[datetime | None] = _ts(default=None, nullable=True)
    verified_at: Mapped[datetime | None] = _ts(default=None, nullable=True)
    mttr_seconds: Mapped[float | None] = mapped_column(Float, default=None)
    suspected_deployment_id: Mapped[int | None] = mapped_column(
        ForeignKey("deployments.id"), nullable=True, default=None
    )
    fingerprint: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    baseline: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    impact: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    detection_meta: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    is_historical: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    created_at: Mapped[datetime] = _ts(default=utcnow)

    events: Mapped[list["IncidentEvent"]] = relationship(
        back_populates="incident", order_by="IncidentEvent.ts"
    )
    evidence: Mapped[list["IncidentEvidence"]] = relationship(back_populates="incident")
    hypotheses: Mapped[list["IncidentHypothesis"]] = relationship(back_populates="incident")

    __table_args__ = (Index("ix_incident_service_detected", "service_id", "detected_at"),)


class IncidentEvent(Base):
    """The incident timeline (spec section 18 / 31)."""

    __tablename__ = "incident_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    incident_id: Mapped[int] = mapped_column(ForeignKey("incidents.id"), index=True)
    ts: Mapped[datetime] = _ts(default=utcnow, index=True)
    kind: Mapped[str] = mapped_column(String(40))
    title: Mapped[str] = mapped_column(String(240))
    description: Mapped[str] = mapped_column(Text, default="")
    actor: Mapped[str] = mapped_column(String(120), default="system")
    source: Mapped[str] = mapped_column(String(80), default="platform")
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)

    incident: Mapped[Incident] = relationship(back_populates="events")


class IncidentEvidence(Base):
    """A single, immutable piece of evidence with provenance (spec section 7)."""

    __tablename__ = "incident_evidence"

    id: Mapped[int] = mapped_column(primary_key=True)
    incident_id: Mapped[int] = mapped_column(ForeignKey("incidents.id"), index=True)
    kind: Mapped[str] = mapped_column(String(50), index=True)
    summary: Mapped[str] = mapped_column(Text)
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    source: Mapped[str] = mapped_column(String(120), default="")
    collected_at: Mapped[datetime] = _ts(default=utcnow)
    strength: Mapped[str] = mapped_column(String(20), default="moderate")
    supports: Mapped[list[str] | None] = mapped_column(JSON, default=None)
    contradicts: Mapped[list[str] | None] = mapped_column(JSON, default=None)
    retrieval: Mapped[str] = mapped_column(String(40), default="deterministic")

    incident: Mapped[Incident] = relationship(back_populates="evidence")


class IncidentHypothesis(Base):
    __tablename__ = "incident_hypotheses"

    id: Mapped[int] = mapped_column(primary_key=True)
    incident_id: Mapped[int] = mapped_column(ForeignKey("incidents.id"), index=True)
    code: Mapped[str] = mapped_column(String(40))
    statement: Mapped[str] = mapped_column(Text)
    category: Mapped[str] = mapped_column(String(80), default="unknown")
    status: Mapped[str] = mapped_column(String(30), default="needs_more_evidence")
    ranking: Mapped[int] = mapped_column(Integer, default=0)
    supporting: Mapped[list[str] | None] = mapped_column(JSON, default=None)
    contradicting: Mapped[list[str] | None] = mapped_column(JSON, default=None)
    missing: Mapped[list[str] | None] = mapped_column(JSON, default=None)
    historical_support: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    created_by: Mapped[str] = mapped_column(String(40), default="agent")
    created_at: Mapped[datetime] = _ts(default=utcnow)

    incident: Mapped[Incident] = relationship(back_populates="hypotheses")


class HypothesisTest(Base):
    """An active test performed against a hypothesis (spec section 17)."""

    __tablename__ = "hypothesis_tests"

    id: Mapped[int] = mapped_column(primary_key=True)
    hypothesis_id: Mapped[int] = mapped_column(ForeignKey("incident_hypotheses.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    tool: Mapped[str] = mapped_column(String(80))
    arguments: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    verdict: Mapped[str] = mapped_column(String(40), default="inconclusive")
    explanation: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = _ts(default=utcnow)


class AiInvestigation(Base):
    __tablename__ = "ai_investigations"

    id: Mapped[int] = mapped_column(primary_key=True)
    incident_id: Mapped[int] = mapped_column(ForeignKey("incidents.id"), index=True)
    status: Mapped[str] = mapped_column(String(30), default="running")
    mode: Mapped[str] = mapped_column(String(20), default="offline")
    model: Mapped[str] = mapped_column(String(80), default="")
    autonomy_level: Mapped[int] = mapped_column(Integer, default=4)
    round_count: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[datetime] = _ts(default=utcnow)
    completed_at: Mapped[datetime | None] = _ts(default=None, nullable=True)
    duration_ms: Mapped[float] = mapped_column(Float, default=0.0)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    degraded: Mapped[list[str] | None] = mapped_column(JSON, default=None)
    summary: Mapped[str] = mapped_column(Text, default="")
    root_cause_category: Mapped[str | None] = mapped_column(String(80), default=None)
    root_cause_statement: Mapped[str] = mapped_column(Text, default="")
    alternatives_ruled_out: Mapped[list[str] | None] = mapped_column(JSON, default=None)
    recommended_remediation: Mapped[str | None] = mapped_column(String(80), default=None)
    recommendation_reason: Mapped[str] = mapped_column(Text, default="")
    memory_recall_used: Mapped[bool] = mapped_column(Boolean, default=False)
    memory_hits: Mapped[int] = mapped_column(Integer, default=0)


class AiToolCall(Base):
    """The auditable tool ledger (spec section 45). Nothing hidden, nothing fabricated."""

    __tablename__ = "ai_tool_calls"

    id: Mapped[int] = mapped_column(primary_key=True)
    investigation_id: Mapped[int] = mapped_column(ForeignKey("ai_investigations.id"), index=True)
    seq: Mapped[int] = mapped_column(Integer, default=0)
    round_index: Mapped[int] = mapped_column(Integer, default=0)
    tool_name: Mapped[str] = mapped_column(String(80), index=True)
    arguments: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    error_code: Mapped[str | None] = mapped_column(String(40), default=None)
    error_message: Mapped[str] = mapped_column(Text, default="")
    duration_ms: Mapped[float] = mapped_column(Float, default=0.0)
    decision: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = _ts(default=utcnow)


class AiDecision(Base):
    __tablename__ = "ai_decisions"

    id: Mapped[int] = mapped_column(primary_key=True)
    investigation_id: Mapped[int] = mapped_column(ForeignKey("ai_investigations.id"), index=True)
    stage: Mapped[str] = mapped_column(String(40))
    decision: Mapped[str] = mapped_column(Text)
    rationale: Mapped[str] = mapped_column(Text, default="")
    evidence_ids: Mapped[list[int] | None] = mapped_column(JSON, default=None)
    memory_ids: Mapped[list[str] | None] = mapped_column(JSON, default=None)
    status_label: Mapped[str] = mapped_column(String(30), default="supported")
    created_at: Mapped[datetime] = _ts(default=utcnow)


# ---------------------------------------------------------------------------------------
# Remediation and verification
# ---------------------------------------------------------------------------------------
class RemediationAction(Base):
    """The allow-list registry. The AI may only ever choose from these rows (spec section 20)."""

    __tablename__ = "remediation_actions"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(160))
    description: Mapped[str] = mapped_column(Text, default="")
    risk_level: Mapped[str] = mapped_column(String(20), default="medium")
    required_approval: Mapped[bool] = mapped_column(Boolean, default=True)
    allowed_environments: Mapped[list[str] | None] = mapped_column(JSON, default=None)
    applicable_categories: Mapped[list[str] | None] = mapped_column(JSON, default=None)
    execution_workflow: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    verification_workflow: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    rollback_workflow: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    timeout_seconds: Mapped[int] = mapped_column(Integer, default=120)
    max_retries: Mapped[int] = mapped_column(Integer, default=2)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    executes_shell: Mapped[bool] = mapped_column(Boolean, default=False)
    historical_success: Mapped[int] = mapped_column(Integer, default=0)
    historical_failure: Mapped[int] = mapped_column(Integer, default=0)


class RemediationRun(Base):
    __tablename__ = "remediation_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    incident_id: Mapped[int] = mapped_column(ForeignKey("incidents.id"), index=True)
    action_code: Mapped[str] = mapped_column(String(80), index=True)
    environment_id: Mapped[int] = mapped_column(ForeignKey("environments.id"))
    autonomy_level: Mapped[int] = mapped_column(Integer, default=4)
    status: Mapped[str] = mapped_column(String(30), default="proposed", index=True)
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    proposed_by: Mapped[str] = mapped_column(String(120), default="ai")
    proposed_at: Mapped[datetime] = _ts(default=utcnow)
    rationale: Mapped[str] = mapped_column(Text, default="")
    parameters: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    blocked_reasons: Mapped[list[str] | None] = mapped_column(JSON, default=None)
    safety_checks: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, default=None)
    approved_by: Mapped[str | None] = mapped_column(String(120), default=None)
    approved_at: Mapped[datetime | None] = _ts(default=None, nullable=True)
    executed_at: Mapped[datetime | None] = _ts(default=None, nullable=True)
    completed_at: Mapped[datetime | None] = _ts(default=None, nullable=True)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    outcome: Mapped[str | None] = mapped_column(String(30), default=None)
    rollback_of_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("remediation_runs.id"), nullable=True, default=None
    )

    verifications: Mapped[list["VerificationRun"]] = relationship(back_populates="remediation_run")


class RemediationPolicy(Base):
    __tablename__ = "remediation_policies"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    action_code: Mapped[str] = mapped_column(String(80))
    environment_kind: Mapped[str] = mapped_column(String(20), default="staging")
    max_autonomy_level: Mapped[int] = mapped_column(Integer, default=4)
    requires_approval: Mapped[bool] = mapped_column(Boolean, default=True)
    is_blocked: Mapped[bool] = mapped_column(Boolean, default=False)
    note: Mapped[str] = mapped_column(Text, default="")

    __table_args__ = (
        UniqueConstraint("project_id", "action_code", "environment_kind", name="uq_policy"),
    )


class VerificationRun(Base):
    __tablename__ = "verification_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    incident_id: Mapped[int] = mapped_column(ForeignKey("incidents.id"), index=True)
    remediation_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("remediation_runs.id"), nullable=True, index=True, default=None
    )
    status: Mapped[str] = mapped_column(String(30), default="running")
    verdict: Mapped[str | None] = mapped_column(String(30), default=None)
    verdict_reason: Mapped[str] = mapped_column(Text, default="")
    settle_seconds: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[datetime] = _ts(default=utcnow)
    completed_at: Mapped[datetime | None] = _ts(default=None, nullable=True)
    before_state: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    after_state: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    improvement_pct: Mapped[float] = mapped_column(Float, default=0.0)

    remediation_run: Mapped[RemediationRun | None] = relationship(back_populates="verifications")
    results: Mapped[list["VerificationResult"]] = relationship(back_populates="run")


class VerificationResult(Base):
    """One named check with an explicit expected/observed pair (spec section 23/24)."""

    __tablename__ = "verification_results"

    id: Mapped[int] = mapped_column(primary_key=True)
    verification_run_id: Mapped[int] = mapped_column(ForeignKey("verification_runs.id"), index=True)
    check_name: Mapped[str] = mapped_column(String(80))
    metric: Mapped[str] = mapped_column(String(80), default="")
    before_value: Mapped[float | None] = mapped_column(Float, default=None)
    after_value: Mapped[float | None] = mapped_column(Float, default=None)
    baseline_value: Mapped[float | None] = mapped_column(Float, default=None)
    unit: Mapped[str] = mapped_column(String(20), default="")
    passed: Mapped[bool] = mapped_column(Boolean, default=False)
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False)
    detail: Mapped[str] = mapped_column(Text, default="")

    run: Mapped[VerificationRun] = relationship(back_populates="results")


class Postmortem(Base):
    __tablename__ = "postmortems"

    id: Mapped[int] = mapped_column(primary_key=True)
    incident_id: Mapped[int] = mapped_column(ForeignKey("incidents.id"), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(20), default="draft")
    summary: Mapped[str] = mapped_column(Text, default="")
    impact: Mapped[str] = mapped_column(Text, default="")
    detection: Mapped[str] = mapped_column(Text, default="")
    root_cause: Mapped[str] = mapped_column(Text, default="")
    timeline: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, default=None)
    evidence: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, default=None)
    hypotheses: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, default=None)
    failed_attempts: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, default=None)
    successful_remediation: Mapped[str] = mapped_column(Text, default="")
    verification: Mapped[str] = mapped_column(Text, default="")
    deployment_relationship: Mapped[str] = mapped_column(Text, default="")
    lessons: Mapped[list[str] | None] = mapped_column(JSON, default=None)
    preventive_actions: Mapped[list[str] | None] = mapped_column(JSON, default=None)
    authored_by: Mapped[str] = mapped_column(String(120), default="ai")
    approved_by: Mapped[str | None] = mapped_column(String(120), default=None)
    approved_at: Mapped[datetime | None] = _ts(default=None, nullable=True)
    created_at: Mapped[datetime] = _ts(default=utcnow)
    updated_at: Mapped[datetime] = _ts(default=utcnow)


# ---------------------------------------------------------------------------------------
# Memory + learning
# ---------------------------------------------------------------------------------------
class MemoryReference(Base):
    """PostgreSQL remembers *which* Hindsight memory belongs to which incident (spec 36)."""

    __tablename__ = "memory_references"

    id: Mapped[int] = mapped_column(primary_key=True)
    incident_id: Mapped[int | None] = mapped_column(
        ForeignKey("incidents.id"), nullable=True, index=True, default=None
    )
    bank_id: Mapped[str] = mapped_column(String(120), index=True)
    memory_type: Mapped[str] = mapped_column(String(30), default="experience")
    hindsight_memory_id: Mapped[str] = mapped_column(String(120), index=True)
    document_id: Mapped[str] = mapped_column(String(120), default="", index=True)
    scope: Mapped[str] = mapped_column(String(40), default="incident_experience")
    content_digest: Mapped[str] = mapped_column(String(64), default="")
    content_preview: Mapped[str] = mapped_column(Text, default="")
    backend: Mapped[str] = mapped_column(String(20), default="hindsight")
    retained_at: Mapped[datetime] = _ts(default=utcnow)
    retained_by: Mapped[str] = mapped_column(String(120), default="learning_agent")
    recall_count: Mapped[int] = mapped_column(Integer, default=0)

    __table_args__ = (Index("ix_memref_incident_scope", "incident_id", "scope"),)


class LearningEvent(Base):
    __tablename__ = "learning_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    incident_id: Mapped[int | None] = mapped_column(
        ForeignKey("incidents.id"), nullable=True, index=True, default=None
    )
    kind: Mapped[str] = mapped_column(String(40), index=True)
    summary: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    memory_ids: Mapped[list[str] | None] = mapped_column(JSON, default=None)
    created_at: Mapped[datetime] = _ts(default=utcnow, index=True)


class RemediationAttempt(Base):
    """Compact per-attempt ledger for fast failure-memory recall (spec section 29)."""

    __tablename__ = "remediation_attempts"

    id: Mapped[int] = mapped_column(primary_key=True)
    incident_id: Mapped[int] = mapped_column(ForeignKey("incidents.id"), index=True)
    service_id: Mapped[int] = mapped_column(ForeignKey("services.id"), index=True)
    root_cause_category: Mapped[str] = mapped_column(String(80), default="unknown", index=True)
    action_code: Mapped[str] = mapped_column(String(80), index=True)
    attempt_number: Mapped[int] = mapped_column(Integer, default=1)
    outcome: Mapped[str] = mapped_column(String(30), default="unknown")
    failure_reason: Mapped[str] = mapped_column(Text, default="")
    verification_id: Mapped[int | None] = mapped_column(
        ForeignKey("verification_runs.id"), nullable=True, default=None
    )
    created_at: Mapped[datetime] = _ts(default=utcnow)


# ---------------------------------------------------------------------------------------
# Governance
# ---------------------------------------------------------------------------------------
class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int | None] = mapped_column(
        ForeignKey("organizations.id"), nullable=True, default=None
    )
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True, default=None
    )
    actor: Mapped[str] = mapped_column(String(160), default="system")
    actor_role: Mapped[str] = mapped_column(String(20), default="system")
    action: Mapped[str] = mapped_column(String(120), index=True)
    target_type: Mapped[str] = mapped_column(String(60), default="")
    target_id: Mapped[str] = mapped_column(String(60), default="")
    result: Mapped[str] = mapped_column(String(30), default="success")
    reason: Mapped[str] = mapped_column(Text, default="")
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    ip_address: Mapped[str] = mapped_column(String(60), default="")
    user_agent: Mapped[str] = mapped_column(String(300), default="")
    created_at: Mapped[datetime] = _ts(default=utcnow, index=True)


class IntegrationConnection(Base):
    __tablename__ = "integration_connections"

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    kind: Mapped[str] = mapped_column(String(40))
    name: Mapped[str] = mapped_column(String(120))
    base_url: Mapped[str] = mapped_column(String(300), default="")
    status: Mapped[str] = mapped_column(String(30), default="not_configured")
    has_secret: Mapped[bool] = mapped_column(Boolean, default=False)
    config: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    last_checked_at: Mapped[datetime | None] = _ts(default=None, nullable=True)
    last_error: Mapped[str] = mapped_column(Text, default="")


class SystemEvent(Base):
    """Platform-level status feed ('what is happening right now', spec section 43)."""

    __tablename__ = "system_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = _ts(default=utcnow, index=True)
    kind: Mapped[str] = mapped_column(String(40), index=True)
    severity: Mapped[str] = mapped_column(String(20), default="info")
    title: Mapped[str] = mapped_column(String(240))
    detail: Mapped[str] = mapped_column(Text, default="")
    ref_type: Mapped[str] = mapped_column(String(40), default="")
    ref_id: Mapped[int | None] = mapped_column(Integer, default=None)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)


__all__ = [
    "Base",
    "utcnow",
    "TZDateTime",
    "Organization",
    "User",
    "Project",
    "Environment",
    "Service",
    "ServiceDependency",
    "MetricSample",
    "LogEvent",
    "ErrorSignature",
    "Deployment",
    "DeploymentChange",
    "ConfigurationChange",
    "TestRun",
    "TestResult",
    "Runbook",
    "Incident",
    "IncidentEvent",
    "IncidentEvidence",
    "IncidentHypothesis",
    "HypothesisTest",
    "AiInvestigation",
    "AiToolCall",
    "AiDecision",
    "RemediationAction",
    "RemediationRun",
    "RemediationPolicy",
    "VerificationRun",
    "VerificationResult",
    "Postmortem",
    "MemoryReference",
    "LearningEvent",
    "RemediationAttempt",
    "AuditLog",
    "IntegrationConnection",
    "SystemEvent",
]
