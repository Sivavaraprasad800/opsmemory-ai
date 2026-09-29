"""The simulated production environment.

This is the *evidence substrate*. Every metric sample and every log line the AI reasons about
is produced here by stepping service state forward in time, and the state itself is mutated by
fault dynamics and remediation effects (``app.sim.faults``). Nothing is hand-written into a
telemetry table, so build spec section 24 ("do not fake these numbers") holds by construction:
the before/after figures in a verification report are rows this engine generated.

The world is a process-global singleton (``_WORLD``) because service state must be shared
across requests; ``SimulationEngine`` is the thin, session-bound operator over that world.
"""

from __future__ import annotations

import logging
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Sequence

from sqlalchemy import delete, func, insert, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.database.models import (
    ConfigurationChange,
    Deployment,
    DeploymentChange,
    Environment,
    LogEvent,
    MetricSample,
    Service,
    utcnow,
)
from app.detection.logs import SignatureStat, aggregate_signatures, classify_level, signature_hash
from app.sim.faults import (
    FaultSpec,
    ServiceRuntime,
    ActionResult,
    activate_fault,
    apply_remediation as apply_effect,
    clear_fault,
    resolve_scenario,
    service_profile,
)

logger = logging.getLogger(__name__)


def _build_runtime(service_name: str, kind: str = "api") -> ServiceRuntime:
    """Create a healthy runtime with *this* service's own baseline resources.

    Without a profile every service starts at the same defaults and a healthy estate renders as
    a row of identical clones - which looks fake, and destroys the cross-service contrast that
    makes "this one is behaving unusually" a meaningful statement.
    """
    profile = service_profile(service_name, kind=kind)
    pool = int(profile["pool_size"])
    runtime = ServiceRuntime(
        name=service_name,
        environment="production",
        base_pool_size=pool,
        pool_size=pool,
        known_good_pool_size=pool,
        base_connections=profile["connections"],
        active_connections=profile["connections"],
        base_latency_ms=profile["latency_ms"],
        latency_ms=profile["latency_ms"],
        base_rps=profile["rps"],
        rps=profile["rps"],
        cpu_pct=profile["cpu_pct"],
        base_cpu_pct=profile["cpu_pct"],
        mem_pct=profile["mem_pct"],
        base_mem_pct=profile["mem_pct"],
        disk_pct=profile["disk_pct"],
        base_disk_pct=profile["disk_pct"],
    )
    runtime.recompute()
    return runtime

METRIC_NAMES = (
    "connections",
    "pool_size",
    "connection_saturation",
    "latency_ms",
    "error_rate",
    "cpu_pct",
    "mem_pct",
    "disk_pct",
    "queue_depth",
    "rps",
    "health_score",
)

LOG_SIGNATURE_PATTERNS: tuple[tuple[str, str, str], ...] = (
    (
        "pool_exhausted",
        "ERROR",
        "connection acquisition timeout: pool exhausted active={active} max={pool} conn_id=<ID>",
    ),
    ("pool_high", "WARN", "connection pool utilisation high active={active} max={pool}"),
    ("leak_guard", "WARN", "connection not released after request completed path=/v1/{path} conn_id=<ID>"),
    ("http_5xx", "ERROR", "GET /v1/{path} returned 503 upstream failure duration={duration}"),
    ("http_latency", "WARN", "GET /v1/{path} slow duration={duration} p95_breach=true"),
    ("db_timeout", "ERROR", "database query timeout after {duration} query=select%20payments"),
    ("db_slow", "WARN", "database query slow duration={duration}"),
    ("gc_pressure", "WARN", "heap usage high resident={mem} gc_pause={duration}"),
    ("oom_warning", "ERROR", "memory limit approaching resident={mem} limit=2048MB"),
    ("disk_write", "ERROR", "write failed: no space left on device path=/var/log/app"),
    ("disk_high", "WARN", "disk utilisation high used={disk}"),
    ("auth_failed", "ERROR", "authentication failed calling dependency: token expired"),
    ("cache_stale", "WARN", "cache served stale entry namespace=v7 key=<HASH>"),
    ("net_timeout", "ERROR", "upstream call timed out after {duration} host=<HOST>"),
    ("deploy_started", "INFO", "deployment started version={version}"),
    ("deploy_completed", "INFO", "deployment completed version={version} in {duration}"),
    ("health_ok", "INFO", "healthcheck ok latency={duration}"),
    ("request_ok", "INFO", "GET /v1/{path} 200 duration={duration}"),
)


@dataclass
class SimWorld:
    """Process-global simulated world."""

    clock: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    runtimes: dict[str, ServiceRuntime] = field(default_factory=dict)
    faults: dict[str, FaultSpec] = field(default_factory=dict)
    service_ids: dict[str, int] = field(default_factory=dict)
    environment_ids: dict[str, int] = field(default_factory=dict)
    lock: threading.RLock = field(default_factory=threading.RLock)
    running: bool = True
    tick_count: int = 0

    def reset(self) -> None:
        with self.lock:
            self.runtimes.clear()
            self.faults.clear()
            self.clock = datetime.now(timezone.utc)
            self.tick_count = 0
            self.running = True


_WORLD = SimWorld()


def world() -> SimWorld:
    return _WORLD


def sim_now() -> datetime:
    """The platform's single notion of "now".

    Every detection window, baseline capture and verification comparison is relative to this
    value, which is why ingestion does not need its own detector: stamp your real telemetry with
    wall-clock time, run the platform with ``CLOCK_MODE=real``, and the same windows apply.

    Keeping this as the *only* clock is what makes the simulated and real modes share one code
    path rather than two parallel implementations that drift apart.
    """
    if settings.clock_mode == "real":
        return datetime.now(timezone.utc)
    with _WORLD.lock:
        return _WORLD.clock


# ---------------------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------------------
class SimulationEngine:
    """Session-bound operations over the simulated world."""

    def __init__(self, db: Session) -> None:
        self.db = db

    # -- catalogue ---------------------------------------------------------------------
    def load_catalogue(self, *, only: Sequence[str] | None = None) -> None:
        """Register known services and environments in the world.

        ``only`` restricts which services get a runtime. Tests use it to simulate a handful of
        services instead of the whole estate, which keeps telemetry generation cheap without
        changing any behaviour under test.
        """
        allowed = set(only) if only else None
        with _WORLD.lock:
            for service in self.db.scalars(select(Service)).all():
                if allowed is not None and service.name not in allowed:
                    continue
                _WORLD.service_ids[service.name] = service.id
                if service.name not in _WORLD.runtimes:
                    _WORLD.runtimes[service.name] = _build_runtime(
                        service.name, kind=getattr(service, "kind", "api") or "api"
                    )
            for environment in self.db.scalars(select(Environment)).all():
                _WORLD.environment_ids[environment.name] = environment.id

    def service(self, name: str) -> Service:
        service = self.db.scalar(select(Service).where(Service.name == name))
        if service is None:
            raise KeyError(f"unknown service '{name}'")
        return service

    def environment_id(self, name: str) -> int:
        environment = self.db.scalar(select(Environment).where(Environment.name == name))
        if environment is None:
            raise KeyError(f"unknown environment '{name}'")
        return environment.id

    def world_runtime(self, service_name: str) -> ServiceRuntime | None:
        """The runtime for a service if it exists, without creating one as a side effect."""
        with _WORLD.lock:
            return _WORLD.runtimes.get(service_name)

    def runtime(self, service_name: str) -> ServiceRuntime:
        with _WORLD.lock:
            rt = _WORLD.runtimes.get(service_name)
            if rt is None:
                rt = _build_runtime(service_name)
                _WORLD.runtimes[service_name] = rt
            return rt

    # -- time --------------------------------------------------------------------------
    def advance(self, seconds: float, *, max_ticks: int = 240) -> dict[str, Any]:
        """Step the world forward by ``seconds`` of simulated time.

        Large jumps are down-sampled so a "fast-forward 30 minutes" call stays fast while
        preserving the shape of the curve. This matters: the demo needs to move from
        "healthy" to "INC-1 resolved" to "INC-2 recall" in seconds, not minutes.
        """
        if seconds <= 0:
            return {"advanced_seconds": 0, "ticks": 0}

        target_ticks = max(1, int(seconds / max(settings.sim_seconds_per_tick, 1)))
        ticks = min(target_ticks, max_ticks)
        dt = seconds / ticks

        created_metrics = 0
        created_logs = 0
        with _WORLD.lock:
            # Rows are buffered across the whole call and written with one executemany per table.
            # A 35-minute warm-up emits ~33k telemetry rows; adding them one at a time through
            # the ORM cost tens of seconds, which is the difference between a responsive demo
            # and a test fixture that times the suite out. Buffering is order-preserving:
            # within a tick metrics are emitted before logs, so ordering by (ts, id) is
            # identical to the row-at-a-time version.
            metric_rows: list[dict[str, Any]] = []
            log_rows: list[dict[str, Any]] = []
            for _ in range(ticks):
                _WORLD.clock = _WORLD.clock + timedelta(seconds=dt)
                _WORLD.tick_count += 1
                for name, rt in _WORLD.runtimes.items():
                    rt.step(dt)
                    created_metrics += self._record_metrics(name, rt, metric_rows)
                created_logs += self._record_logs(dt, log_rows)
            if metric_rows:
                self.db.execute(insert(MetricSample.__table__), metric_rows)
            if log_rows:
                self.db.execute(insert(LogEvent.__table__), log_rows)
            clock = _WORLD.clock

        if settings.sim_retention_minutes > 0 and ticks > 8:
            self._prune()

        self.db.flush()
        return {
            "advanced_seconds": seconds,
            "ticks": ticks,
            "dt_per_tick": round(dt, 3),
            "sim_time": clock.isoformat(),
            "metric_rows": created_metrics,
            "log_rows": created_logs,
        }

    def _record_metrics(
        self, service_name: str, rt: ServiceRuntime, rows: list[dict[str, Any]]
    ) -> int:
        service_id = _WORLD.service_ids.get(service_name)
        if service_id is None:
            return 0
        gauges = rt.gauges()
        ts = _WORLD.clock
        for metric, value in gauges.items():
            rows.append(
                {
                    "service_id": service_id,
                    "ts": ts,
                    "name": metric,
                    "value": float(value),
                    "labels": None,
                }
            )
        return len(gauges)

    def _record_logs(self, dt: float, rows: list[dict[str, Any]]) -> int:
        """Generate log lines with probabilities derived from service state.

        Log volume is proportional to error rate, so a spike in errors produces a spike in log
        volume *and* in the frequency of specific error signatures — which is exactly what the
        deterministic log pipeline is built to detect.
        """
        created = 0
        ts = _WORLD.clock
        for service_name, rt in _WORLD.runtimes.items():
            service_id = _WORLD.service_ids.get(service_name)
            if service_id is None:
                continue
            saturation = rt.active_connections / max(rt.pool_size, 1)
            for _ in range(self._log_volume(rt, dt)):
                level, template = self._pick_template(rt, saturation)
                message = self._render(template, rt, service_name, saturation)
                rows.append(
                    {
                        "service_id": service_id,
                        "ts": ts,
                        "level": level,
                        "message": message,
                        "signature_hash": signature_hash(message),
                        "deployment_id": None,
                    }
                )
                created += 1
        return created

    @staticmethod
    def _log_volume(rt: ServiceRuntime, dt: float) -> int:
        base = max(1, int(4 * dt))
        if rt.error_rate > 0.25:
            base += int(12 * dt)
        elif rt.error_rate > 0.05:
            base += int(5 * dt)
        if rt.disk_pct > 90:
            base += int(4 * dt)
        return min(base, 40)

    @staticmethod
    def _pick_template(rt: ServiceRuntime, saturation: float) -> tuple[str, str]:
        import random

        weighted: list[tuple[float, str]] = [
            (2.0, "request_ok"),
            (0.6, "health_ok"),
        ]
        if saturation > 0.95:
            weighted.append((6.0 * saturation, "pool_exhausted"))
        elif saturation > 0.75:
            weighted.append((3.0, "pool_high"))
        if rt.leak_rate > 0:
            weighted.append((2.4, "leak_guard"))
        if rt.error_rate > 0.30:
            weighted.append((7.0 * rt.error_rate, "http_5xx"))
        elif rt.error_rate > 0.05:
            weighted.append((3.0, "http_latency"))
        if rt.db_latency_add_ms > 500:
            weighted.append((5.0, "db_timeout"))
        elif rt.db_latency_add_ms > 0:
            weighted.append((3.0, "db_slow"))
        if rt.mem_growth_per_sec > 0:
            weighted.append((2.2 if rt.mem_pct > 90 else 1.0, "gc_pressure"))
            if rt.mem_pct > 92:
                weighted.append((3.0, "oom_warning"))
        if rt.disk_growth_per_sec > 0:
            if rt.disk_pct > 95:
                weighted.append((5.0, "disk_write"))
            weighted.append((1.6, "disk_high"))
        if rt.credentials_expired:
            weighted.append((6.0, "auth_failed"))
        if rt.cache_stale:
            weighted.append((2.5, "cache_stale"))
        if rt.latency_add_ms > 300 and rt.fault_kind == "network_issue":
            weighted.append((4.0, "net_timeout"))

        total = sum(w for w, _ in weighted)
        pick = random.random() * total
        cumulative = 0.0
        for weight, key in weighted:
            cumulative += weight
            if pick <= cumulative:
                return _template_for(key)
        return _template_for("request_ok")

    @staticmethod
    def _render(template: str, rt: ServiceRuntime, service_name: str, saturation: float) -> str:
        import random

        paths = ["payments", "checkout", "orders", "refunds", "invoices", "balance"]
        duration = (
            f"{random.uniform(0.05, 0.4):.2f}s"
            if rt.latency_ms < 500
            else f"{rt.latency_ms / 1000 * random.uniform(0.8, 1.3):.2f}s"
        )
        return (
            f"{service_name} "
            + template.format(
                active=int(rt.active_connections),
                pool=rt.pool_size,
                duration=duration,
                mem=f"{int(rt.mem_pct)}%",
                disk=f"{rt.disk_pct:.1f}%",
                path=random.choice(paths),
                version=random.choice(["v2.31.0", "v2.30.4", "v2.29.1"]),
            )
        )

    def has_telemetry(self) -> bool:
        """True when the database already holds simulated telemetry.

        Startup uses this to decide whether the world needs warming up. Without the check, every
        API restart replays the warm-up window on top of existing data, which both wastes time
        and silently shifts the simulated timeline forward.
        """
        return self.db.scalar(select(MetricSample.id).limit(1)) is not None

    def _prune(self) -> None:
        cutoff = sim_now() - timedelta(minutes=settings.sim_retention_minutes)
        self.db.execute(delete(MetricSample).where(MetricSample.ts < cutoff))
        self.db.execute(delete(LogEvent).where(LogEvent.ts < cutoff))

    # -- faults ------------------------------------------------------------------------
    def inject_fault(
        self,
        *,
        scenario: str,
        service_name: str,
        environment: str = "production",
        severity: str | None = None,
        activate: bool = True,
    ) -> FaultSpec:
        spec = resolve_scenario(scenario)
        if severity:
            spec.severity = severity
        rt = self.runtime(service_name)
        rt.environment = environment
        if spec.pool_size_override:
            rt.pool_size = spec.pool_size_override
        if activate:
            activate_fault(rt, spec)
        with _WORLD.lock:
            _WORLD.faults[service_name] = spec
        self._record_change(
            service_name=service_name,
            environment=environment,
            key="fault.injection",
            old_value="none",
            new_value=spec.kind,
            change_class="simulated_fault",
            actor="simulator",
        )
        return spec

    def clear_fault(self, service_name: str, *, restore: bool = True) -> None:
        rt = self.runtime(service_name)
        if restore:
            rt.pool_size = rt.known_good_pool_size or rt.base_pool_size
        clear_fault(rt)
        with _WORLD.lock:
            _WORLD.faults.pop(service_name, None)

    def active_fault(self, service_name: str) -> FaultSpec | None:
        with _WORLD.lock:
            return _WORLD.faults.get(service_name)

    def active_faults(self) -> dict[str, FaultSpec]:
        with _WORLD.lock:
            return dict(_WORLD.faults)

    # -- deployments and configuration -------------------------------------------------
    def inject_deployment(
        self,
        *,
        service_name: str,
        environment: str,
        version: str,
        commit_message: str,
        author: str,
        change_class: str = "application_config",
        file_path: str = "deploy/service.yaml",
        diff_excerpt: str = "",
        risk_score: float = 0.4,
        with_scenario: str | None = None,
        activate_fault_flag: bool = True,
    ) -> Deployment:
        service = self.service(service_name)
        environment_id = self.environment_id(environment)
        started = _WORLD.clock
        deployment = Deployment(
            service_id=service.id,
            environment_id=environment_id,
            version=version,
            commit_sha=uuid.uuid4().hex[:12],
            commit_message=commit_message,
            author=author,
            status="succeeded",
            started_at=started,
            completed_at=started + timedelta(seconds=90),
            risk_score=risk_score,
            meta={"change_class": change_class},
        )
        self.db.add(deployment)
        self.db.flush()
        self.db.add(
            DeploymentChange(
                deployment_id=deployment.id,
                file_path=file_path,
                change_type="modify",
                summary=commit_message,
                risk_score=risk_score,
                touches_config=change_class in {"application_config", "infrastructure"},
                diff_excerpt=diff_excerpt,
            )
        )
        self._record_change(
            service_name=service_name,
            environment=environment,
            key=file_path,
            old_value="previous release",
            new_value=version,
            change_class=change_class,
            actor=author,
        )
        if with_scenario:
            spec = self.inject_fault(
                scenario=with_scenario,
                service_name=service_name,
                environment=environment,
                activate=activate_fault_flag,
            )
            deployment.is_suspected_cause = True
            spec.deployment_related = True
        self.db.flush()
        return deployment

    def _record_change(
        self,
        *,
        service_name: str,
        environment: str,
        key: str,
        old_value: str,
        new_value: str,
        change_class: str,
        actor: str,
    ) -> ConfigurationChange:
        service_id = _WORLD.service_ids.get(service_name)
        if service_id is None:
            existing = self.db.scalar(select(Service).where(Service.name == service_name))
            service_id = existing.id if existing else None
        environment_id = _WORLD.environment_ids.get(environment)
        change = ConfigurationChange(
            service_id=service_id,
            environment_id=environment_id,
            key=key,
            old_value=old_value,
            new_value=new_value,
            change_class=change_class,
            actor=actor,
            changed_at=_WORLD.clock,
        )
        self.db.add(change)
        self.db.flush()
        return change

    # -- remediation -------------------------------------------------------------------
    def apply_remediation(
        self, *, service_name: str, action_code: str, params: dict[str, Any] | None = None
    ) -> ActionResult:
        rt = self.runtime(service_name)
        result = apply_effect(rt, action_code, params or {})
        if result.resolved_cause:
            with _WORLD.lock:
                _WORLD.faults.pop(service_name, None)
        else:
            # A failed remediation is still a *change to the system*; record it so the
            # "what changed?" engine sees it, and let the fault dynamics continue.
            self._record_change(
                service_name=service_name,
                environment=rt.environment,
                key=f"remediation.{action_code}",
                old_value="none",
                new_value=action_code,
                change_class="remediation_attempt",
                actor="remediation_executor",
            )
        self.db.flush()
        return result

    # -- read model --------------------------------------------------------------------
    def gauges(self, service_name: str) -> dict[str, float]:
        return self.runtime(service_name).gauges()

    def metric_window(
        self, *, service_name: str, metric: str, seconds: float, end: datetime | None = None
    ) -> list[tuple[datetime, float]]:
        service = self.service(service_name)
        end = end or sim_now()
        start = end - timedelta(seconds=seconds)
        rows = self.db.execute(
            select(MetricSample.ts, MetricSample.value)
            .where(
                MetricSample.service_id == service.id,
                MetricSample.name == metric,
                MetricSample.ts >= start,
                MetricSample.ts <= end,
            )
            .order_by(MetricSample.ts)
        ).all()
        return [(row[0], float(row[1])) for row in rows]

    def metric_series(
        self, *, service_name: str, metrics: Sequence[str], seconds: float, end: datetime | None = None
    ) -> dict[str, list[float]]:
        return {
            metric: [value for _, value in self.metric_window(service_name=service_name, metric=metric, seconds=seconds, end=end)]
            for metric in metrics
        }

    def baseline_stats(
        self, *, service_name: str, metric: str, seconds: float, end: datetime | None = None
    ) -> dict[str, float]:
        """Median/MAD baseline captured *before* an incident — the comparator for verification."""
        values = [v for _, v in self.metric_window(service_name=service_name, metric=metric, seconds=seconds, end=end)]
        if not values:
            return {"count": 0, "median": 0.0, "p95": 0.0, "min": 0.0, "max": 0.0}
        ordered = sorted(values)
        p95_index = min(len(ordered) - 1, int(len(ordered) * 0.95))
        return {
            "count": len(values),
            "median": float(ordered[len(ordered) // 2]),
            "p95": float(ordered[p95_index]),
            "min": float(ordered[0]),
            "max": float(ordered[-1]),
            "mean": float(sum(values) / len(values)),
        }

    def recent_logs(
        self,
        *,
        service_name: str,
        seconds: float,
        level: str | None = None,
        limit: int = 400,
        end: datetime | None = None,
    ) -> list[dict[str, Any]]:
        service = self.service(service_name)
        end = end or sim_now()
        start = end - timedelta(seconds=seconds)
        stmt = select(LogEvent).where(
            LogEvent.service_id == service.id,
            LogEvent.ts >= start,
            LogEvent.ts <= end,
        )
        if level:
            stmt = stmt.where(LogEvent.level == level.upper())
        rows = self.db.scalars(stmt.order_by(LogEvent.ts.desc()).limit(limit)).all()
        return [
            {
                "id": row.id,
                "ts": row.ts.isoformat(),
                "level": row.level,
                "message": row.message,
                "signature_hash": row.signature_hash,
                "service_name": service_name,
                "service_id": service.id,
            }
            for row in reversed(rows)
        ]

    def recent_signatures(
        self, *, service_name: str, seconds: float = 600, baseline_seconds: float = 3600
    ) -> list[SignatureStat]:
        """Current error signatures with historical baseline rates attached."""
        current = self.recent_logs(service_name=service_name, seconds=seconds, limit=800)
        historical = self.recent_logs(
            service_name=service_name,
            seconds=baseline_seconds,
            end=sim_now() - timedelta(seconds=seconds),
            limit=3000,
        )
        baseline: dict[str, float] = {}
        if historical:
            hist_sigs = aggregate_signatures(historical, top_n=200)
            for stat in hist_sigs:
                baseline[stat.hash] = stat.observed_rate
        return aggregate_signatures(current, window_seconds=seconds, baseline=baseline, top_n=25)

    def error_count(self, *, service_name: str, seconds: float) -> int:
        service = self.service(service_name)
        start = sim_now() - timedelta(seconds=seconds)
        return int(
            self.db.scalar(
                select(func.count())
                .select_from(LogEvent)
                .where(
                    LogEvent.service_id == service.id,
                    LogEvent.ts >= start,
                    LogEvent.level.in_(["ERROR", "FATAL"]),
                )
            )
            or 0
        )

    def recent_deployments(
        self, *, service_name: str | None = None, seconds: float = 86400, limit: int = 10
    ) -> list[Deployment]:
        start = sim_now() - timedelta(seconds=seconds)
        stmt = select(Deployment).where(Deployment.started_at >= start)
        if service_name:
            stmt = stmt.where(Deployment.service_id == self.service(service_name).id)
        return list(self.db.scalars(stmt.order_by(Deployment.started_at.desc()).limit(limit)).all())

    def service_names(self) -> list[str]:
        return list(self.db.scalars(select(Service.name).order_by(Service.name)).all())

    def world_state(self) -> dict[str, Any]:
        with _WORLD.lock:
            return {
                "sim_time": _WORLD.clock.isoformat(),
                "running": _WORLD.running,
                "tick_count": _WORLD.tick_count,
                "services": {name: rt.gauges() for name, rt in _WORLD.runtimes.items()},
                "active_faults": {name: spec.kind for name, spec in _WORLD.faults.items()},
            }

    def pause(self) -> None:
        with _WORLD.lock:
            _WORLD.running = False

    def resume(self) -> None:
        with _WORLD.lock:
            _WORLD.running = True

    def reset_world(self) -> None:
        _WORLD.reset()
        self.load_catalogue()


def _template_for(key: str) -> tuple[str, str]:
    for pattern_key, level, template in LOG_SIGNATURE_PATTERNS:
        if pattern_key == key:
            return level, template
    return "INFO", "GET /v1/{path} 200 duration={duration}"


def current_sim_time() -> datetime:
    return sim_now()


def level_for(message: str) -> str:
    return classify_level(message)


__all__ = [
    "SimulationEngine",
    "METRIC_NAMES",
    "world",
    "sim_now",
    "current_sim_time",
    "SimWorld",
]
