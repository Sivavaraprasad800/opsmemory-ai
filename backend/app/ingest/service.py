"""Validated ingestion of telemetry from an arbitrary external project.

Design notes worth stating, because the validation is the point:

**Anything can connect.** A project does not have to be in the catalogue beforehand. The first
sample for an unknown project/environment/service creates them, so connecting a real system is
one command rather than a database migration. ``INGEST_AUTOREGISTER=false`` turns this off for
deployments that want an explicit allow-list instead.

**Bad data is rejected, not absorbed.** A connector with a wrong clock, a swapped field or a
runaway loop is the most likely way this goes wrong in practice, and every one of those
failures is silent in a schema this permissive: a future timestamp simply never appears in any
window, and a NaN poisons every median it touches. So samples outside the clock skew window are
dropped with a reason, non-finite values are refused, and the per-service rate ceiling rejects a
flood rather than letting it become a fake mass incident.

**Telemetry is bounded.** The simulator prunes its own history as it advances. In real-clock
mode nothing advances the simulator, so pruning happens here instead - otherwise a long-running
connection grows the database forever.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Sequence

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.database.models import (
    Environment,
    LogEvent,
    MetricSample,
    Organization,
    Project,
    Service,
)
from app.detection.logs import build_signature
from app.domain.incidents import WATCHED_METRICS
from app.sim.engine import sim_now

logger = logging.getLogger(__name__)

# The metrics the detection committee actually votes on. Anything else is stored and shown, but
# it will not open an incident on its own - worth telling the connector, because "I sent
# metrics and nothing happened" is otherwise a confusing first experience.
WATCHED_METRIC_NAMES: frozenset[str] = frozenset(m for m, _, _ in WATCHED_METRICS)

# Log levels normalised on the way in, so an ingesting system's own vocabulary ("warning",
# "err", "critical") lands in the same column as the simulator's.
_LEVEL_ALIASES: dict[str, str] = {
    "WARN": "WARN",
    "WARNING": "WARN",
    "ERR": "ERROR",
    "CRITICAL": "FATAL",
    "CRIT": "FATAL",
    "SEVERE": "ERROR",
    "NOTICE": "INFO",
    "TRACE": "DEBUG",
}

ALLOWED_LEVELS: frozenset[str] = frozenset({"DEBUG", "INFO", "WARN", "ERROR", "FATAL"})

PRODUCTION_NAMES: frozenset[str] = frozenset({"prod", "production", "live", "prd"})


@dataclass
class Scope:
    """The catalogue rows a batch of telemetry belongs to."""

    organization: Organization
    project: Project
    environment: Environment
    service: Service
    created: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "organization": self.organization.slug,
            "project": self.project.slug,
            "environment": self.environment.name,
            "service": self.service.name,
            "created": list(self.created),
        }


@dataclass
class IngestOutcome:
    accepted: int = 0
    rejected: int = 0
    issues: list[str] = field(default_factory=list)
    accepted_metrics: list[str] = field(default_factory=list)
    unwatched_metrics: list[str] = field(default_factory=list)
    new_signatures: int = 0
    scope: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "rejected": self.rejected,
            "issues": self.issues[:20],
            "accepted_metrics": sorted(set(self.accepted_metrics))[:40],
            "unwatched_metrics": sorted(set(self.unwatched_metrics))[:40],
            "new_signatures": self.new_signatures,
            "scope": self.scope,
        }


# ---------------------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------------------
def _slugify(value: str, *, fallback: str = "connected") -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in value.strip().lower())
    cleaned = "-".join(part for part in cleaned.split("-") if part)
    return (cleaned or fallback)[:80]


def _clamp_text(value: str, limit: int) -> str:
    text = (value or "").strip()
    return text[:limit]


def parse_timestamp(value: Any) -> datetime | None:
    """Accept ISO-8601, a unix epoch in seconds or milliseconds, or nothing.

    Returns ``None`` when the value cannot be understood, so the caller can decide between
    "use now" and "reject this sample". A timestamp that silently becomes *now* is a much worse
    outcome than one that is refused, because it makes a late batch look like a live incident.

    Always returns **timezone-aware UTC**, which is the convention ``sim_now()`` already uses.
    Returning a naive datetime here would make every comparison against a window boundary raise
    "can't subtract offset-naive and offset-aware datetimes" - the sort of failure that only
    appears in the one code path the tests do not cover.
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, (int, float)):
        seconds = float(value)
        # Milliseconds are the common mistake; 10^11 seconds is year 5138, so this is safe.
        if seconds > 1e11:
            seconds = seconds / 1000.0
        try:
            parsed = datetime.fromtimestamp(seconds, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    else:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    if parsed.tzinfo is None:
        # A bare timestamp with no offset is ambiguous. UTC is the only assumption that is not
        # silently wrong for a distributed system, and it is stated in the docs.
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _coerce_float(value: Any) -> float | None:
    """Return a finite float, or None. NaN and infinity are refused rather than stored."""
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _normalise_level(value: Any, message: str) -> str:
    if value is None or str(value).strip() == "":
        return build_signature(message)[2]
    token = str(value).strip().upper()
    token = _LEVEL_ALIASES.get(token, token)
    return token if token in ALLOWED_LEVELS else "INFO"


# ---------------------------------------------------------------------------------------
# scope resolution
# ---------------------------------------------------------------------------------------
def _default_organization(db: Session) -> tuple[Organization, bool]:
    org = db.scalar(select(Organization).order_by(Organization.id))
    if org is not None:
        return org, False
    org = Organization(
        name="Connected Projects",
        slug="connected-projects",
        mission="Systems connected to OpsMemory AI by their operators.",
    )
    db.add(org)
    db.flush()
    return org, True


def ensure_scope(
    db: Session,
    *,
    service: str,
    environment: str = "production",
    project: str | None = None,
    tier: str = "medium",
    kind: str = "api",
    language: str = "",
) -> Scope:
    """Resolve (and, when allowed, create) the catalogue rows for an incoming batch.

    Raises ``ValueError`` when a row is missing and auto-registration is disabled, so the
    connector gets an actionable message rather than a silent drop.
    """
    service_name = _clamp_text(service, 120)
    if not service_name:
        raise ValueError("a service name is required")

    environment_name = _clamp_text(environment or "production", 60) or "production"
    project_slug = _slugify(project or service_name)
    organisation, org_created = _default_organization(db)
    created: list[str] = []
    if org_created:
        created.append(f"organization:{organisation.slug}")

    project_row = db.scalar(
        select(Project).where(
            Project.organization_id == organisation.id, Project.slug == project_slug
        )
    )
    if project_row is None:
        if not settings.ingest_autoregister:
            raise ValueError(
                f"project '{project_slug}' is not registered and INGEST_AUTOREGISTER is off"
            )
        project_row = Project(
            organization_id=organisation.id,
            name=project or service_name,
            slug=project_slug,
            description="Connected by an operator through the ingestion API.",
        )
        db.add(project_row)
        db.flush()
        created.append(f"project:{project_slug}")

    environment_row = db.scalar(
        select(Environment).where(
            Environment.project_id == project_row.id, Environment.name == environment_name
        )
    )
    if environment_row is None:
        if not settings.ingest_autoregister:
            raise ValueError(
                f"environment '{environment_name}' is not registered and INGEST_AUTOREGISTER is off"
            )
        is_prod = environment_name.lower() in PRODUCTION_NAMES
        environment_row = Environment(
            project_id=project_row.id,
            name=environment_name,
            kind="production" if is_prod else ("development" if "dev" in environment_name.lower() else "staging"),
            is_production=is_prod,
        )
        db.add(environment_row)
        db.flush()
        created.append(f"environment:{environment_name}")

    service_row = db.scalar(
        select(Service).where(Service.project_id == project_row.id, Service.name == service_name)
    )
    if service_row is None:
        if not settings.ingest_autoregister:
            raise ValueError(
                f"service '{service_name}' is not registered and INGEST_AUTOREGISTER is off"
            )
        service_row = Service(
            project_id=project_row.id,
            name=service_name,
            tier=tier if tier in {"critical", "high", "medium", "low"} else "medium",
            kind=_clamp_text(kind, 40) or "api",
            language=_clamp_text(language, 40),
            owner_team="connected",
            description="Connected by an operator through the ingestion API.",
        )
        db.add(service_row)
        db.flush()
        created.append(f"service:{service_name}")

    return Scope(
        organization=organisation,
        project=project_row,
        environment=environment_row,
        service=service_row,
        created=created,
    )


# ---------------------------------------------------------------------------------------
# rate limiting and retention
# ---------------------------------------------------------------------------------------
def _recent_row_count(db: Session, service_id: int) -> int:
    since = sim_now() - timedelta(seconds=60)
    metrics = db.scalar(
        select(func.count())
        .select_from(MetricSample)
        .where(MetricSample.service_id == service_id, MetricSample.ts >= since)
    )
    logs = db.scalar(
        select(func.count())
        .select_from(LogEvent)
        .where(LogEvent.service_id == service_id, LogEvent.ts >= since)
    )
    return int(metrics or 0) + int(logs or 0)


def _prune(db: Session, service_id: int) -> None:
    """Bound the history the way the simulator bounds its own.

    Only in real-clock mode: in simulated mode ``SimulationEngine.advance`` already prunes, and
    doing it twice would delete rows from underneath the demo.
    """
    if not settings.real_clock or settings.sim_retention_minutes <= 0:
        return
    cutoff = sim_now() - timedelta(minutes=settings.sim_retention_minutes)
    db.execute(delete(MetricSample).where(MetricSample.service_id == service_id, MetricSample.ts < cutoff))
    db.execute(delete(LogEvent).where(LogEvent.service_id == service_id, LogEvent.ts < cutoff))


# ---------------------------------------------------------------------------------------
# ingestion
# ---------------------------------------------------------------------------------------
def _check_rate(db: Session, scope: Scope, incoming: int) -> None:
    ceiling = max(1, settings.ingest_services_per_minute)
    if _recent_row_count(db, scope.service.id) + incoming > ceiling:
        raise ValueError(
            f"rate ceiling reached for '{scope.service.name}': more than {ceiling} telemetry "
            f"rows in the last 60s. Reduce the connector's flush rate, or raise "
            f"INGEST_SERVICES_PER_MINUTE."
        )


def ingest_metrics(
    db: Session,
    *,
    scope: Scope,
    samples: Sequence[dict[str, Any]],
) -> IngestOutcome:
    """Insert metric samples.

    Each sample is ``{"name": str, "value": number, "ts": optional, "labels": optional}``.
    A batch-level ``ts`` is not used: per-sample timestamps are what make an out-of-order
    backfill work, and dropping them silently would misdate the whole history.
    """
    outcome = IngestOutcome(scope=scope.to_dict()["service"])
    if len(samples) > settings.ingest_max_batch:
        raise ValueError(
            f"batch of {len(samples)} exceeds INGEST_MAX_BATCH ({settings.ingest_max_batch}); "
            f"split it into smaller requests"
        )
    _check_rate(db, scope, len(samples))

    now = sim_now()
    skew = timedelta(seconds=max(0, settings.ingest_max_skew_seconds))
    rows: list[dict[str, Any]] = []

    for index, sample in enumerate(samples):
        name = _clamp_text(str(sample.get("name") or ""), 60)
        value = _coerce_float(sample.get("value"))
        if not name:
            outcome.rejected += 1
            outcome.issues.append(f"sample {index}: missing metric name")
            continue
        if value is None:
            outcome.rejected += 1
            outcome.issues.append(f"sample {index} ({name}): value is not a finite number")
            continue
        ts = parse_timestamp(sample.get("ts")) or now
        if ts > now + skew:
            outcome.rejected += 1
            outcome.issues.append(
                f"sample {index} ({name}): timestamp {ts.isoformat()}Z is more than "
                f"{settings.ingest_max_skew_seconds}s in the future - check the sender's clock"
            )
            continue
        labels = sample.get("labels")
        rows.append(
            {
                "service_id": scope.service.id,
                "ts": ts,
                "name": name,
                "value": value,
                "labels": labels if isinstance(labels, dict) else None,
            }
        )
        outcome.accepted_metrics.append(name)
        if name not in WATCHED_METRIC_NAMES:
            outcome.unwatched_metrics.append(name)

    if rows:
        db.execute(MetricSample.__table__.insert(), rows)
        db.flush()
        outcome.accepted = len(rows)
        _prune(db, scope.service.id)

    return outcome


def ingest_logs(
    db: Session,
    *,
    scope: Scope,
    lines: Sequence[dict[str, Any]],
) -> IngestOutcome:
    """Insert log lines.

    Each line is ``{"message": str, "level": optional, "ts": optional}``. The message is stored
    verbatim; normalisation, masking and template hashing happen in the same code the simulator
    feeds, so an ingested line aggregates into the same signatures and spikes exactly like a
    simulated one.
    """
    outcome = IngestOutcome(scope=scope.to_dict()["service"])
    if len(lines) > settings.ingest_max_batch:
        raise ValueError(
            f"batch of {len(lines)} exceeds INGEST_MAX_BATCH ({settings.ingest_max_batch}); "
            f"split it into smaller requests"
        )
    _check_rate(db, scope, len(lines))

    now = sim_now()
    skew = timedelta(seconds=max(0, settings.ingest_max_skew_seconds))
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()

    for index, line in enumerate(lines):
        message = str(line.get("message") or "").strip()
        if not message:
            outcome.rejected += 1
            outcome.issues.append(f"line {index}: message is empty")
            continue
        ts = parse_timestamp(line.get("ts")) or now
        if ts > now + skew:
            outcome.rejected += 1
            outcome.issues.append(
                f"line {index}: timestamp is more than {settings.ingest_max_skew_seconds}s in the "
                f"future - check the sender's clock"
            )
            continue
        digest, _template, _level = build_signature(message)
        level = _normalise_level(line.get("level"), message)
        rows.append(
            {
                "service_id": scope.service.id,
                "ts": ts,
                "level": level,
                "message": message[:8000],
                "signature_hash": digest,
                "deployment_id": None,
            }
        )
        seen.add(digest)

    if rows:
        db.execute(LogEvent.__table__.insert(), rows)
        db.flush()
        outcome.accepted = len(rows)
        outcome.new_signatures = len(seen)
        _prune(db, scope.service.id)

    return outcome


# ---------------------------------------------------------------------------------------
# read model
# ---------------------------------------------------------------------------------------
def latest_gauges(db: Session, service_name: str, *, lookback_minutes: int = 30) -> dict[str, float]:
    """Most recent value per metric for a service.

    Used in real-clock mode so the dashboard shows the connected system's own numbers rather
    than the simulator's synthetic baseline for that service name. Falling back to invented
    values here would make a dashboard that looks fine while describing a system that does not
    exist.
    """
    service = db.scalar(select(Service).where(Service.name == service_name))
    if service is None:
        return {}
    since = sim_now() - timedelta(minutes=lookback_minutes)
    rows = db.execute(
        select(MetricSample.name, MetricSample.value)
        .where(MetricSample.service_id == service.id, MetricSample.ts >= since)
        .order_by(MetricSample.ts.desc())
        .limit(4000)
    ).all()
    latest: dict[str, float] = {}
    for name, value in rows:
        if name not in latest:
            latest[name] = round(float(value), 4)
    return latest


def ingestion_overview(db: Session) -> dict[str, Any]:
    """What is currently arriving, per connected service. Drives the connect page."""
    now = sim_now()
    window = now - timedelta(minutes=15)
    services: list[dict[str, Any]] = []

    for service in db.scalars(select(Service).order_by(Service.name)).all():
        environment = db.scalar(
            select(Environment).where(Environment.project_id == service.project_id).order_by(Environment.id)
        )
        metric_count = db.scalar(
            select(func.count())
            .select_from(MetricSample)
            .where(MetricSample.service_id == service.id, MetricSample.ts >= window)
        )
        log_count = db.scalar(
            select(func.count())
            .select_from(LogEvent)
            .where(LogEvent.service_id == service.id, LogEvent.ts >= window)
        )
        last_metric_ts = db.scalar(
            select(func.max(MetricSample.ts)).where(MetricSample.service_id == service.id)
        )
        last_log_ts = db.scalar(
            select(func.max(LogEvent.ts)).where(LogEvent.service_id == service.id)
        )
        error_count = db.scalar(
            select(func.count())
            .select_from(LogEvent)
            .where(
                LogEvent.service_id == service.id,
                LogEvent.ts >= window,
                LogEvent.level.in_(["ERROR", "FATAL"]),
            )
        )
        latest = last_metric_ts or last_log_ts
        services.append(
            {
                "service": service.name,
                "project": service.project.slug if service.project else None,
                "environment": environment.name if environment else None,
                "metrics_15m": int(metric_count or 0),
                "logs_15m": int(log_count or 0),
                "errors_15m": int(error_count or 0),
                "last_metric_at": last_metric_ts.isoformat() if last_metric_ts else None,
                "last_log_at": last_log_ts.isoformat() if last_log_ts else None,
                "seconds_since_last": int((now - latest).total_seconds()) if latest else None,
                "gauges": latest_gauges(db, service.name),
            }
        )

    return {
        "clock_mode": settings.clock_mode,
        "autoregister": settings.ingest_autoregister,
        "retention_minutes": settings.sim_retention_minutes,
        "rate_ceiling_per_minute": settings.ingest_services_per_minute,
        "watched_metrics": sorted(WATCHED_METRIC_NAMES),
        "services": services,
        "now": now.isoformat() + "Z",
    }
