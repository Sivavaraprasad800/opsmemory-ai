"""The connector API — how a real project hands its telemetry to the platform.

Two ways in, because they serve different situations:

* **A shared connector token** (``X-Ingest-Token``). This is what an agent or sidecar running
  inside someone's infrastructure uses. It needs no user account and no JWT refresh, so a
  long-running connector is a one-line curl rather than an auth dance.
* **A signed-in user** with the ``ingest_telemetry`` permission. This is what a person uses to
  test the connection from a terminal.

When ``INGEST_TOKEN`` is empty the token path is disabled and authentication is always required
— an unauthenticated write endpoint that accepts arbitrary telemetry would let anyone fabricate
an incident, so the default is the safe one.
"""

from __future__ import annotations

import hmac
import logging
from typing import Annotated, Any

from fastapi import APIRouter, Header, Query, Request
from pydantic import BaseModel, Field

from app.api.deps import DbDep, PrincipalDep, rate_limit_write
from app.core.config import settings
from app.core.errors import UnauthorizedError, ValidationError
from app.domain.incidents import WATCHED_METRICS
from app.ingest.service import (
    ensure_scope,
    ingest_logs,
    ingest_metrics,
    ingestion_overview,
)

WATCHED_METRIC_NAMES = sorted(metric for metric, _, _ in WATCHED_METRICS)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/ingest", tags=["ingest"])


# ---------------------------------------------------------------------------------------
# auth
# ---------------------------------------------------------------------------------------
def _authorise(principal, token: str | None) -> str:
    """Return the actor label for this batch, or raise."""
    if settings.ingest_token:
        if token and hmac.compare_digest(token, settings.ingest_token):
            return "connector-token"
        if token is None and principal.is_anonymous:
            raise UnauthorizedError(
                "This deployment requires the connector token in the X-Ingest-Token header"
            )
    principal.require("ingest_telemetry")
    return principal.actor


# ---------------------------------------------------------------------------------------
# schemas
# ---------------------------------------------------------------------------------------
class RegisterRequest(BaseModel):
    service: str = Field(min_length=1, max_length=120)
    environment: str = Field(default="production", max_length=60)
    project: str | None = Field(default=None, max_length=160)
    tier: str = "medium"
    kind: str = "api"
    language: str = ""


class MetricSampleIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    value: float
    ts: str | float | None = None
    labels: dict[str, Any] | None = None


class MetricBatchIn(BaseModel):
    service: str = Field(min_length=1, max_length=120)
    environment: str = Field(default="production", max_length=60)
    project: str | None = Field(default=None, max_length=160)
    kind: str = "api"
    language: str = ""
    samples: list[MetricSampleIn] = Field(min_length=1)


class LogLineIn(BaseModel):
    message: str = Field(min_length=1, max_length=8000)
    level: str | None = None
    ts: str | float | None = None


class LogBatchIn(BaseModel):
    service: str = Field(min_length=1, max_length=120)
    environment: str = Field(default="production", max_length=60)
    project: str | None = Field(default=None, max_length=160)
    lines: list[LogLineIn] = Field(min_length=1)


# ---------------------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------------------
@router.post("/register")
def register(
    payload: RegisterRequest,
    principal: PrincipalDep,
    db: DbDep,
    request: Request,
    x_ingest_token: Annotated[str | None, Header()] = None,
) -> dict:
    """Create the catalogue rows for a project before any telemetry arrives.

    Optional: the first sample from an unknown service registers it automatically. This exists
    so an operator can pre-create the rows, set a tier, and confirm connectivity before wiring
    up their log shipper.
    """
    actor = _authorise(principal, x_ingest_token)
    rate_limit_write(principal, request)
    try:
        scope = ensure_scope(
            db,
            service=payload.service,
            environment=payload.environment,
            project=payload.project,
            tier=payload.tier,
            kind=payload.kind,
            language=payload.language,
        )
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc
    db.commit()
    logger.info("ingest: %s registered %s", actor, scope.to_dict())
    return {
        "registered": scope.to_dict(),
        "created": scope.created,
        "actor": actor,
        "clock_mode": settings.clock_mode,
        "next": "POST /api/ingest/metrics and /api/ingest/logs with the same service/environment",
    }


@router.post("/metrics")
def push_metrics(
    payload: MetricBatchIn,
    principal: PrincipalDep,
    db: DbDep,
    request: Request,
    x_ingest_token: Annotated[str | None, Header()] = None,
) -> dict:
    """Accept a batch of metric samples from a connected project."""
    actor = _authorise(principal, x_ingest_token)
    rate_limit_write(principal, request)
    try:
        scope = ensure_scope(
            db,
            service=payload.service,
            environment=payload.environment,
            project=payload.project,
            kind=payload.kind,
            language=payload.language,
        )
        outcome = ingest_metrics(
            db, scope=scope, samples=[sample.model_dump() for sample in payload.samples]
        )
    except ValueError as exc:
        db.rollback()
        raise ValidationError(str(exc)) from exc
    db.commit()
    result = outcome.to_dict()
    result["actor"] = actor
    # An unwatched metric is a common first-run confusion: the data was stored, but nothing will
    # ever open an incident from it. Say so here rather than leaving it to be discovered.
    if result["unwatched_metrics"]:
        result["hint"] = (
            "These metric names are stored but are not watched by the detection committee: "
            + ", ".join(result["unwatched_metrics"])
            + ". Watched names: "
            + ", ".join(WATCHED_METRIC_NAMES)
        )
    return result


@router.post("/logs")
def push_logs(
    payload: LogBatchIn,
    principal: PrincipalDep,
    db: DbDep,
    request: Request,
    x_ingest_token: Annotated[str | None, Header()] = None,
) -> dict:
    """Accept a batch of log lines from a connected project."""
    actor = _authorise(principal, x_ingest_token)
    rate_limit_write(principal, request)
    try:
        scope = ensure_scope(
            db,
            service=payload.service,
            environment=payload.environment,
            project=payload.project,
        )
        outcome = ingest_logs(db, scope=scope, lines=[line.model_dump() for line in payload.lines])
    except ValueError as exc:
        db.rollback()
        raise ValidationError(str(exc)) from exc
    db.commit()
    result = outcome.to_dict()
    result["actor"] = actor
    return result


@router.get("/status")
def status(principal: PrincipalDep, db: DbDep) -> dict:
    """What is currently arriving, per service — used by the Connect page."""
    principal.require("read")
    return ingestion_overview(db)


@router.get("/schema")
def schema(principal: PrincipalDep) -> dict:
    """The exact payload shapes, so a connector can be written without reading the source."""
    principal.require("read")
    return {
        "metrics": {
            "method": "POST",
            "path": "/api/ingest/metrics",
            "body": {
                "service": "my-api",
                "environment": "production",
                "project": "my-project",
                "samples": [{"name": "error_rate", "value": 0.031, "ts": "2026-09-29T14:02:00Z"}],
            },
        },
        "logs": {
            "method": "POST",
            "path": "/api/ingest/logs",
            "body": {
                "service": "my-api",
                "environment": "production",
                "lines": [
                    {"message": "connection pool exhausted active=50 max=50", "level": "ERROR"}
                ],
            },
        },
        "register": {
            "method": "POST",
            "path": "/api/ingest/register",
            "body": {"service": "my-api", "environment": "production", "tier": "critical"},
        },
        "auth": "Send X-Ingest-Token, or a bearer token for a user with ingest_telemetry",
        "watched_metrics": WATCHED_METRIC_NAMES,
        "limits": {
            "max_batch": settings.ingest_max_batch,
            "rows_per_minute_per_service": settings.ingest_services_per_minute,
            "max_future_skew_seconds": settings.ingest_max_skew_seconds,
            "retention_minutes": settings.sim_retention_minutes,
        },
    }
