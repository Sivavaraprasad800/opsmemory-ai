"""Fault dynamics and the remediation-effect model — pure logic, no I/O.

This module is what makes the demo honest. Metrics on screen are **derived** from service
state, and service state is mutated by fault dynamics and by remediation effects. Nothing is
written by hand into a metrics table.

The critical property for build spec section 29 (*failure memory*) is causal, not cosmetic:

* the ``connection_leak`` fault keeps adding connections to the pool at ``leak_rate`` per
  second regardless of what we do;
* therefore ``restart_service`` resets the counter but the leak immediately resumes, and the
  leak is *still active* at verification time;
* therefore verification genuinely fails, and the failed attempt is genuinely retained.

That chain is why "the AI remembers that a restart already failed" is a real capability here
rather than a scripted line of dialogue.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

# ---------------------------------------------------------------------------------------
# Service runtime state
# ---------------------------------------------------------------------------------------
BASE_LATENCY_MS = 120.0


# ---------------------------------------------------------------------------------------
# Per-service baselines
# ---------------------------------------------------------------------------------------
# Every service used to be constructed from the same dataclass defaults, so a healthy estate
# rendered as eight identical rows - same latency, same saturation, same throughput. That looks
# fake on a dashboard, and it also removes the natural cross-service variation that makes
# per-service anomaly detection meaningful: when everything is identical, "this service is
# unusual" cannot be answered at all.
#
# The profile is derived from the service name, so a service keeps the same personality across
# restarts and across environments while still differing from its neighbours.
_KIND_PROFILES: dict[str, dict[str, float]] = {
    # throughput-heavy, latency-sensitive, small pools
    "cache": {"rps": 1.65, "latency": 0.35, "connections": 1.2, "pool": 0.6, "cpu": 0.8},
    # fan-out workers: low request rate, mid resources
    "worker": {"rps": 0.35, "latency": 1.4, "connections": 0.5, "pool": 0.7, "cpu": 1.15},
    # request-serving APIs: the default shape
    "api": {"rps": 1.0, "latency": 1.0, "connections": 1.0, "pool": 1.0, "cpu": 1.0},
}


def service_profile(service_name: str, kind: str = "api") -> dict[str, float]:
    """Deterministic baseline resources for one service.

    The spread is wide enough to look like a real estate (a cache at ~1.6x the request rate of
    an API, a worker at ~0.35x) and narrow enough that no service starts out looking unhealthy.
    """
    digest = hashlib.sha256(service_name.encode("utf-8")).digest()
    shape = _KIND_PROFILES.get(kind, _KIND_PROFILES["api"])

    def spread(index: int, low: float, high: float) -> float:
        """A stable value in [low, high) taken from the name's hash."""
        span = high - low
        return low + span * (digest[index] / 255.0)

    pool = int(round(spread(0, 40.0, 120.0) * shape["pool"]))
    if service_name.startswith("db-") or "database" in service_name:
        pool = int(round(spread(0, 150.0, 260.0)))
    pool = max(24, pool)

    connections = round(spread(1, 4.0, 16.0) * shape["connections"], 1)
    # A healthy service must sit comfortably below a quarter of its pool. This is a product
    # statement, not a cosmetic one: the estate has to start out unmistakably healthy so that
    # a real saturation incident stands out, and so "this pool is fine" is never ambiguous.
    connections = min(connections, round(pool * 0.22, 1))

    return {
        "pool_size": float(pool),
        "connections": max(2.0, connections),
        "latency_ms": round(BASE_LATENCY_MS * shape["latency"] * spread(2, 0.55, 1.5), 1),
        "rps": round(420.0 * shape["rps"] * spread(3, 0.55, 1.7), 2),
        "cpu_pct": round(spread(4, 14.0, 38.0) * shape["cpu"], 1),
        "mem_pct": round(spread(5, 36.0, 71.0), 1),
        "disk_pct": round(spread(6, 30.0, 64.0), 1),
    }



def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


@dataclass
class ServiceRuntime:
    """Live state of one simulated service. Metrics are derived from this, never invented."""

    name: str
    environment: str
    base_pool_size: int = 50
    base_connections: float = 6.0
    pool_size: int = 50
    active_connections: float = 6.0
    leak_rate: float = 0.0
    mem_growth_per_sec: float = 0.0
    disk_growth_per_sec: float = 0.0
    cpu_target: float = 0.0
    latency_add_ms: float = 0.0
    db_latency_add_ms: float = 0.0
    error_add: float = 0.0
    latency_ms: float = BASE_LATENCY_MS
    error_rate: float = 0.004
    cpu_pct: float = 22.0
    mem_pct: float = 46.0
    disk_pct: float = 51.0
    queue_depth: float = 3.0
    rps: float = 420.0
    base_mem_pct: float = 46.0
    base_disk_pct: float = 51.0
    base_cpu_pct: float = 22.0
    # Healthy-state references. Derived from the service profile so that `recompute` restores
    # each service to *its own* baseline rather than to one shared set of defaults.
    base_latency_ms: float = BASE_LATENCY_MS
    base_rps: float = 420.0
    fault_kind: str | None = None
    root_cause_category: str = "unknown"
    deployment_related: bool = False
    known_good_pool_size: int = 50
    cache_stale: bool = False
    credentials_expired: bool = False

    # -- derived metrics ---------------------------------------------------------------
    def recompute(self) -> None:
        saturation = self.active_connections / max(self.pool_size, 1)
        pool_pressure = clamp((saturation - 0.70) / 0.30, 0.0, 1.8)
        mem_pressure = clamp((self.mem_pct - 80.0) / 20.0, 0.0, 1.4)
        cpu_pressure = clamp((self.cpu_pct - 78.0) / 22.0, 0.0, 1.4)
        disk_pressure = clamp((self.disk_pct - 85.0) / 15.0, 0.0, 1.4)
        cache_penalty = 0.05 if self.cache_stale else 0.0
        credential_penalty = 0.35 if self.credentials_expired else 0.0

        self.error_rate = clamp(
            0.003
            + 0.42 * pool_pressure
            + 0.30 * mem_pressure
            + 0.24 * cpu_pressure
            + 0.18 * disk_pressure
            + cache_penalty
            + credential_penalty
            + self.error_add,
            0.0,
            0.98,
        )
        self.latency_ms = (
            self.base_latency_ms
            * (1.0 + 3.4 * pool_pressure + 2.6 * mem_pressure + 2.2 * cpu_pressure + 1.1 * disk_pressure)
            + self.latency_add_ms
            + self.db_latency_add_ms
            + 1.2 * self.queue_depth
        )
        self.queue_depth = clamp(2.0 + 40.0 * pool_pressure + 12.0 * cpu_pressure, 0.0, 400.0)
        # A per-service throughput ceiling: a cache cluster and a notification worker must not
        # report the same requests per second just because they share a simulator.
        self.rps = clamp(self.base_rps * (1.0 - 0.45 * self.error_rate), 20.0, 4000.0)

    # -- one simulation step -----------------------------------------------------------
    def step(self, dt: float) -> None:
        """Advance by ``dt`` simulated seconds."""
        if self.leak_rate > 0:
            self.active_connections += self.leak_rate * dt
        elif not self.credentials_expired:
            # Connections relax back toward the working set when nothing is leaking.
            drift = (self.base_connections - self.active_connections) * min(dt * 0.05, 1.0)
            self.active_connections += drift
        self.active_connections = clamp(self.active_connections, 0.0, float(self.pool_size))

        if self.mem_growth_per_sec > 0:
            self.mem_pct = clamp(self.mem_pct + self.mem_growth_per_sec * dt, 0.0, 99.5)
        else:
            self.mem_pct = clamp(
                self.mem_pct + (self.base_mem_pct - self.mem_pct) * min(dt * 0.02, 1.0), 0.0, 99.5
            )

        if self.disk_growth_per_sec > 0:
            self.disk_pct = clamp(self.disk_pct + self.disk_growth_per_sec * dt, 0.0, 99.9)
        else:
            self.disk_pct = clamp(
                self.disk_pct + (self.base_disk_pct - self.disk_pct) * min(dt * 0.01, 1.0), 0.0, 99.9
            )

        target_cpu = max(self.base_cpu_pct, self.cpu_target)
        self.cpu_pct = clamp(self.cpu_pct + (target_cpu - self.cpu_pct) * min(dt * 0.25, 1.0), 0.0, 99.5)

        self.recompute()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def gauges(self) -> dict[str, float]:
        return {
            "connections": round(self.active_connections, 2),
            "pool_size": float(self.pool_size),
            "connection_saturation": round(self.active_connections / max(self.pool_size, 1), 4),
            "latency_ms": round(self.latency_ms, 2),
            "error_rate": round(self.error_rate, 4),
            "cpu_pct": round(self.cpu_pct, 2),
            "mem_pct": round(self.mem_pct, 2),
            "disk_pct": round(self.disk_pct, 2),
            "queue_depth": round(self.queue_depth, 2),
            "rps": round(self.rps, 2),
            "health_score": round(max(0.0, 1.0 - self.error_rate * 2 - self.latency_ms / 4000.0), 4),
        }


# ---------------------------------------------------------------------------------------
# Faults
# ---------------------------------------------------------------------------------------
@dataclass
class FaultSpec:
    """A fault and its causal dynamics."""

    kind: str
    root_cause_category: str
    description: str
    severity: str = "high"
    leak_rate: float = 0.0
    mem_growth_per_sec: float = 0.0
    disk_growth_per_sec: float = 0.0
    cpu_target: float = 0.0
    latency_add_ms: float = 0.0
    db_latency_add_ms: float = 0.0
    error_add: float = 0.0
    pool_size_override: int | None = None
    cache_stale: bool = False
    credentials_expired: bool = False
    deployment_related: bool = False
    symptoms: list[str] = field(default_factory=list)
    canonical_actions: list[str] = field(default_factory=list)
    """Registered actions that *can* resolve this fault. Used to validate AI choices."""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _pool(rt: ServiceRuntime) -> int:
    return rt.pool_size


def _connections_to_exhaust(rt: ServiceRuntime) -> float:
    return max(rt.pool_size - rt.base_connections, 1.0)


# ---------------------------------------------------------------------------------------
# Scenario catalogue
# ---------------------------------------------------------------------------------------
SCENARIOS: dict[str, FaultSpec] = {
    "connection_exhaustion": FaultSpec(
        kind="connection_leak",
        root_cause_category="connection_leak",
        description=(
            "A connection leak in the application opens pooled connections and never releases "
            "them, so the connection pool saturates under normal traffic."
        ),
        severity="high",
        # 0.18 connections/second exhausts a 50-connection pool in roughly four minutes: slow
        # enough to be a genuine ramp the EWMA detector can see, fast enough that the failure
        # window is meaningful. The rate is what makes a restart-only mitigation fail.
        leak_rate=0.18,
        latency_add_ms=40.0,
        symptoms=[
            "connection pool saturation climbing steadily",
            "p95 latency rising",
            "HTTP 5xx rate climbing",
            "database connection timeouts in logs",
        ],
        canonical_actions=["update_known_safe_configuration"],
    ),
    "bad_deploy_config": FaultSpec(
        kind="bad_deploy_config",
        root_cause_category="bad_deployment_config",
        description=(
            "A deployment reduced the connection pool size in configuration and removed the "
            "leak guard, so the service exhausts a much smaller pool."
        ),
        severity="high",
        leak_rate=0.45,
        pool_size_override=8,
        latency_add_ms=55.0,
        deployment_related=True,
        symptoms=[
            "deployment completed a few minutes before the incident",
            "connection pool configuration changed in the release diff",
            "connection pool saturation rising rapidly",
            "HTTP 5xx rate rising",
        ],
        canonical_actions=["rollback_deployment", "update_known_safe_configuration"],
    ),
    "memory_leak": FaultSpec(
        kind="memory_leak",
        root_cause_category="memory_leak",
        description=(
            "A slow heap leak grows resident memory until the container approaches its limit "
            "and the runtime spends more time garbage collecting than serving."
        ),
        severity="high",
        mem_growth_per_sec=0.15,
        latency_add_ms=30.0,
        symptoms=[
            "resident memory growing linearly over hours",
            "p95 latency climbing as GC pressure increases",
            "container approaching memory limit",
        ],
        canonical_actions=["update_known_safe_configuration"],
    ),
    "database_overload": FaultSpec(
        kind="database_overload",
        root_cause_category="database_overload",
        description=(
            "The database itself is saturated: query latency and connection acquisition time "
            "degrade while every application instance waits."
        ),
        severity="critical",
        db_latency_add_ms=1800.0,
        latency_add_ms=240.0,
        error_add=0.12,
        cpu_target=88.0,
        symptoms=[
            "database query latency elevated across all clients simultaneously",
            "database CPU near saturation",
            "connection acquisition timeouts",
        ],
        canonical_actions=["increase_database_capacity"],
    ),
    "cpu_saturation": FaultSpec(
        kind="cpu_saturation",
        root_cause_category="cpu_saturation",
        description=(
            "A hot loop plus increased traffic saturates CPU, degrading every request path on "
            "the instance."
        ),
        severity="high",
        cpu_target=96.0,
        latency_add_ms=180.0,
        symptoms=["sustained CPU above 95%", "request latency elevated across all endpoints", "queue depth growing"],
        canonical_actions=["scale_service"],
    ),
    "cache_misconfiguration": FaultSpec(
        kind="cache_misconfiguration",
        root_cause_category="cache_misconfiguration",
        description=(
            "The cache key namespace was changed without invalidation, so the service serves "
            "stale entries and a fraction of requests fail validation."
        ),
        severity="medium",
        cache_stale=True,
        latency_add_ms=25.0,
        symptoms=["elevated HTTP 4xx/5xx on cache-backed endpoints", "cache hit ratio elevated but incorrect"],
        canonical_actions=["clear_safe_cache"],
    ),
    "expired_credentials": FaultSpec(
        kind="expired_credentials",
        root_cause_category="expired_credentials",
        description=(
            "A rotated credential expired and the service cannot authenticate to a downstream "
            "dependency, failing a subset of requests outright."
        ),
        severity="critical",
        credentials_expired=True,
        error_add=0.30,
        symptoms=["authentication failures against a downstream dependency", "steady 5xx rate"],
        canonical_actions=["rotate_expired_token"],
    ),
    "disk_pressure": FaultSpec(
        kind="disk_pressure",
        root_cause_category="disk_pressure",
        description=(
            "Log volume filled the data volume. The only real fix is a destructive cleanup, "
            "which is deliberately absent from the remediation registry — the platform must "
            "escalate to a human instead of improvising."
        ),
        severity="critical",
        disk_growth_per_sec=0.08,
        latency_add_ms=90.0,
        symptoms=["disk utilisation climbing toward 100%", "write errors appearing in logs"],
        # canonical_actions is intentionally empty: this is the safety demonstration. Nothing in
        # the registry may perform destructive cleanup, so the safety gate correctly blocks the
        # action set and the incident escalates to a human instead.
        canonical_actions=[],
    ),
    "network_issue": FaultSpec(
        kind="network_issue",
        root_cause_category="network_issue",
        description=(
            "Network policy applied with a deployment dropped packets between two services, "
            "so calls to a dependency intermittently time out."
        ),
        severity="high",
        latency_add_ms=420.0,
        error_add=0.08,
        deployment_related=True,
        symptoms=["timeouts between two services", "intermittent 5xx on dependency calls"],
        canonical_actions=["rollback_deployment"],
    ),
}

SCENARIO_ALIASES = {
    "connection_leak": "connection_exhaustion",
    "connection_pool_exhaustion": "connection_exhaustion",
    "bad_deploy_config": "bad_deploy_config",
    "memory_leak": "memory_leak",
    "db_overload": "database_overload",
    "high_cpu": "cpu_saturation",
    "stale_cache": "cache_misconfiguration",
    "expired_token": "expired_credentials",
    "disk_full": "disk_pressure",
    "network": "network_issue",
}


def resolve_scenario(name: str) -> FaultSpec:
    key = SCENARIO_ALIASES.get(name, name)
    if key not in SCENARIOS:
        raise KeyError(f"unknown scenario '{name}'; available: {', '.join(sorted(SCENARIOS))}")
    # Return a copy: scenarios are templates and must not accumulate state.
    return FaultSpec(**{**asdict(SCENARIOS[key])})


def activate_fault(rt: ServiceRuntime, fault: FaultSpec) -> None:
    """Apply a fault's *dynamics* to a service, preserving the pre-fault working set."""
    rt.known_good_pool_size = rt.known_good_pool_size or rt.pool_size
    rt.fault_kind = fault.kind
    rt.root_cause_category = fault.root_cause_category
    rt.deployment_related = fault.deployment_related
    rt.leak_rate = fault.leak_rate
    rt.mem_growth_per_sec = fault.mem_growth_per_sec
    rt.disk_growth_per_sec = fault.disk_growth_per_sec
    rt.cpu_target = fault.cpu_target
    rt.latency_add_ms = fault.latency_add_ms
    rt.db_latency_add_ms = fault.db_latency_add_ms
    rt.error_add = fault.error_add
    rt.cache_stale = fault.cache_stale
    rt.credentials_expired = fault.credentials_expired
    if fault.pool_size_override:
        rt.pool_size = fault.pool_size_override
    rt.recompute()


def clear_fault(rt: ServiceRuntime) -> None:
    rt.fault_kind = None
    rt.root_cause_category = "unknown"
    rt.deployment_related = False
    rt.leak_rate = 0.0
    rt.mem_growth_per_sec = 0.0
    rt.disk_growth_per_sec = 0.0
    rt.cpu_target = 0.0
    rt.latency_add_ms = 0.0
    rt.db_latency_add_ms = 0.0
    rt.error_add = 0.0
    rt.cache_stale = False
    rt.credentials_expired = False
    rt.recompute()


# ---------------------------------------------------------------------------------------
# Remediation effects
# ---------------------------------------------------------------------------------------
@dataclass
class ActionResult:
    action_code: str
    resolved_cause: bool
    note: str
    changes: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _effect_restart_service(rt: ServiceRuntime, params: dict[str, Any]) -> ActionResult:
    before = rt.gauges()
    rt.active_connections = rt.base_connections
    rt.mem_pct = min(rt.base_mem_pct, rt.mem_pct)
    rt.latency_add_ms = min(rt.latency_add_ms, 10.0)
    rt.recompute()
    still_leaking = rt.leak_rate > 0
    note = (
        "process restarted: connection pool cleared and memory released"
        if not still_leaking
        else (
            "process restarted: the pool was cleared, but the underlying leak is still active "
            "so connections will climb again"
        )
    )
    return ActionResult(
        action_code="restart_service",
        resolved_cause=not still_leaking and rt.fault_kind is None,
        note=note,
        changes={"before": before, "after": rt.gauges(), "cause_removed": not still_leaking},
    )


def _effect_rollback_deployment(rt: ServiceRuntime, params: dict[str, Any]) -> ActionResult:
    before = rt.gauges()
    resolved = rt.fault_kind in {"bad_deploy_config", "network_issue"}
    if resolved:
        rt.pool_size = rt.known_good_pool_size or rt.base_pool_size
        rt.leak_rate = 0.0
        rt.latency_add_ms = 0.0
        rt.error_add = 0.0
        rt.active_connections = rt.base_connections
        rt.fault_kind = None
        rt.root_cause_category = "unknown"
        rt.deployment_related = False
    rt.recompute()
    return ActionResult(
        action_code="rollback_deployment",
        resolved_cause=resolved,
        note=(
            "rolled back to the previous known-good release; the offending change is no longer active"
            if resolved
            else "rolled back the last release, but the diagnosed root cause is not deployment-induced"
        ),
        changes={"before": before, "after": rt.gauges(), "cause_removed": resolved},
    )


def _effect_scale_service(rt: ServiceRuntime, params: dict[str, Any]) -> ActionResult:
    before = rt.gauges()
    resolved = rt.fault_kind == "cpu_saturation"
    rt.pool_size = int(rt.pool_size * 2)
    if resolved:
        rt.cpu_target = 0.0
        rt.latency_add_ms = 0.0
        rt.fault_kind = None
        rt.root_cause_category = "unknown"
    rt.recompute()
    return ActionResult(
        action_code="scale_service",
        resolved_cause=resolved,
        note=(
            "scaled out: CPU pressure relieved across more instances"
            if resolved
            else "scaled out: added pool headroom and capacity, but the underlying fault is still present"
        ),
        changes={
            "before": before,
            "after": rt.gauges(),
            "pool_size": rt.pool_size,
            "cause_removed": resolved,
        },
    )


def _effect_update_known_safe_configuration(rt: ServiceRuntime, params: dict[str, Any]) -> ActionResult:
    before = rt.gauges()
    resolved = rt.fault_kind in {
        "connection_leak",
        "bad_deploy_config",
        "memory_leak",
        "cache_misconfiguration",
    }
    if resolved:
        rt.pool_size = max(rt.known_good_pool_size, rt.base_pool_size)
        rt.leak_rate = 0.0
        rt.mem_growth_per_sec = 0.0
        rt.cache_stale = False
        rt.latency_add_ms = 0.0
        rt.active_connections = rt.base_connections
        rt.fault_kind = None
        rt.root_cause_category = "unknown"
        rt.deployment_related = False
    rt.recompute()
    return ActionResult(
        action_code="update_known_safe_configuration",
        resolved_cause=resolved,
        note=(
            "applied the reviewed known-safe configuration: pool sizing corrected and the leak "
            "guard re-enabled, which removes the cause rather than the symptom"
            if resolved
            else "applied the known-safe configuration, but it does not address this root cause"
        ),
        changes={
            "before": before,
            "after": rt.gauges(),
            "pool_size": rt.pool_size,
            "leak_rate": rt.leak_rate,
            "cause_removed": resolved,
        },
    )


def _effect_clear_safe_cache(rt: ServiceRuntime, params: dict[str, Any]) -> ActionResult:
    before = rt.gauges()
    resolved = bool(rt.cache_stale)
    if resolved:
        rt.cache_stale = False
        rt.fault_kind = None
        rt.root_cause_category = "unknown"
    rt.recompute()
    return ActionResult(
        action_code="clear_safe_cache",
        resolved_cause=resolved,
        note=(
            "cache flushed safely: stale entries removed and the namespace change is now consistent"
            if resolved
            else "cache flushed, but stale entries were not the cause of this incident"
        ),
        changes={"before": before, "after": rt.gauges(), "cause_removed": resolved},
    )


def _effect_rotate_expired_token(rt: ServiceRuntime, params: dict[str, Any]) -> ActionResult:
    before = rt.gauges()
    resolved = bool(rt.credentials_expired)
    if resolved:
        rt.credentials_expired = False
        rt.error_add = 0.0
        rt.fault_kind = None
        rt.root_cause_category = "unknown"
    rt.recompute()
    return ActionResult(
        action_code="rotate_expired_token",
        resolved_cause=resolved,
        note=(
            "credential rotated and the dependency accepted the new token"
            if resolved
            else "credential rotated, but authentication failures were not the diagnosed cause"
        ),
        changes={"before": before, "after": rt.gauges(), "cause_removed": resolved},
    )


def _effect_increase_database_capacity(rt: ServiceRuntime, params: dict[str, Any]) -> ActionResult:
    before = rt.gauges()
    resolved = rt.fault_kind == "database_overload"
    if resolved:
        rt.db_latency_add_ms = 0.0
        rt.latency_add_ms = 0.0
        rt.error_add = 0.0
        rt.cpu_target = 0.0
        rt.fault_kind = None
        rt.root_cause_category = "unknown"
    rt.recompute()
    return ActionResult(
        action_code="increase_database_capacity",
        resolved_cause=resolved,
        note=(
            "database capacity increased: query latency and acquisition time returned to normal"
            if resolved
            else "database capacity increased, but the database was not the bottleneck here"
        ),
        changes={"before": before, "after": rt.gauges(), "cause_removed": resolved},
    )


EFFECTS: dict[str, Callable[[ServiceRuntime, dict[str, Any]], ActionResult]] = {
    "restart_service": _effect_restart_service,
    "rollback_deployment": _effect_rollback_deployment,
    "scale_service": _effect_scale_service,
    "update_known_safe_configuration": _effect_update_known_safe_configuration,
    "clear_safe_cache": _effect_clear_safe_cache,
    "rotate_expired_token": _effect_rotate_expired_token,
    "increase_database_capacity": _effect_increase_database_capacity,
}


def apply_remediation(
    rt: ServiceRuntime, action_code: str, params: dict[str, Any] | None = None
) -> ActionResult:
    handler = EFFECTS.get(action_code)
    if handler is None:
        return ActionResult(
            action_code=action_code,
            resolved_cause=False,
            note=f"no simulated effect is defined for '{action_code}'",
            changes={"applied": False},
        )
    return handler(rt, params or {})


def actions_that_resolve(root_cause_category: str) -> list[str]:
    return [
        action_code
        for code, fault in ((s.kind, s) for s in SCENARIOS.values())
        if fault.root_cause_category == root_cause_category
        for action_code in fault.canonical_actions
        if code
    ]
