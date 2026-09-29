"""Deterministic, evidence-driven reasoner.

This is the offline brain: it runs when no ``OPENAI_API_KEY`` is configured, and it is also the
**validator** for the LLM path (``llm_reasoner.py``). It is deliberately not a stub — it
implements the hybrid root-cause method the build spec asks for in section 19:

    rules + evidence correlation + hypothesis testing + historical experience

It calls exactly the same tools as the LLM path, so the tool ledger and the evidence trail are
identical in both modes; only the *reasoning* differs (rules instead of generation).

Why ship a rules engine at all rather than requiring an API key? Because a hackathon demo must
not depend on venue wifi, because the test suite must be able to assert on reasoning outcomes,
and because section 19 explicitly says *do not rely solely on the LLM*.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Sequence

from app.ai_engine.context import InvestigationContext
from app.ai_engine.reasoning.base import (
    Conclusion,
    HypothesisDraft,
    PostmortemDraft,
    RemediationChoice,
)
from app.ai_engine.tools import TEST_KINDS

logger = logging.getLogger(__name__)

CANONICAL_CATEGORIES = (
    "connection_leak",
    "bad_deployment_config",
    "memory_leak",
    "database_overload",
    "cpu_saturation",
    "disk_pressure",
    "cache_misconfiguration",
    "expired_credentials",
    "network_issue",
)

CATEGORY_STATEMENTS = {
    "connection_leak": (
        "The service is leaking pooled database connections: connections are opened and never "
        "released, so the pool saturates under otherwise normal traffic."
    ),
    "bad_deployment_config": (
        "A recent deployment introduced a configuration or code change that reduced the "
        "available connection capacity and removed the safeguard that previously contained it."
    ),
    "memory_leak": (
        "The service has a slow memory leak: resident memory grows steadily until the runtime "
        "spends a growing share of its time reclaiming memory instead of serving requests."
    ),
    "database_overload": (
        "The database itself is the saturation point: query latency is elevated across all "
        "clients at once, which points below the application rather than at it."
    ),
    "cpu_saturation": (
        "The instance is CPU-bound: sustained CPU saturation is inflating latency across every "
        "request path served by this instance rather than one code path in particular."
    ),
    "disk_pressure": (
        "The data volume is filling up and write operations are beginning to fail."
    ),
    "cache_misconfiguration": (
        "The cache is serving entries under an inconsistent namespace, so a fraction of requests "
        "receive stale data and fail validation."
    ),
    "expired_credentials": (
        "A service credential has expired, so calls to a downstream dependency fail to "
        "authenticate."
    ),
    "network_issue": (
        "Network connectivity to a dependency is degraded, most likely because of a change "
        "shipped alongside a deployment."
    ),
}


@dataclass
class CauseCandidate:
    category: str
    weight: float
    evidence: list[str]
    contradicting: list[str]
    missing: list[str]
    tests: list[str]


class HeuristicReasoner:
    """Rules over real telemetry. Deterministic and therefore unit-testable."""

    mode = "offline"

    def __init__(self) -> None:
        self.degraded = False
        self._candidates: list[CauseCandidate] | None = None

    # -- evidence gathering ------------------------------------------------------------
    def _gather(self, ctx: InvestigationContext, *, include_memory: bool = True) -> None:
        """Fetch the evidence set. Cached in ``ctx.facts`` so re-entry is cheap."""
        plan: list[tuple[str, dict[str, Any], str]] = [
            ("get_metric_anomalies", {}, "read per-metric anomaly verdicts"),
            ("get_error_signatures", {"window_seconds": 1800}, "identify normalised error signatures"),
            ("get_recent_deployments", {"hours": 4}, "check for a change shortly before detection"),
            ("get_configuration_changes", {"hours": 4}, "check for configuration changes"),
            ("get_service_health", {}, "capture current health and fault state"),
            ("get_service_dependencies", {}, "establish topology"),
            ("get_remediation_history", {}, "read what has already been tried for this root cause"),
            ("search_similar_incidents", {"top_k": 5}, "rank historical incidents by similarity"),
            ("list_registered_actions", {}, "read the permitted remediation registry"),
            ("get_incident_timeline", {}, "read the persisted timeline"),
        ]
        if include_memory:
            plan.append(
                (
                    "recall_historical_incidents",
                    {"focus": "which remediation attempts failed verification"},
                    "recall organizational experience from memory",
                )
            )
            plan.append(
                (
                    "reflect_on_historical_experience",
                    {
                        "question": (
                            "What remediation approaches have repeatedly failed for this class of "
                            "incident, and which have been verified to work?"
                        )
                    },
                    "reflect over consolidated experience",
                )
            )
        for name, args, decision in plan:
            if ctx.has(name):
                continue
            result = ctx.call_tool(name, args, decision=decision)
            if not result.ok:
                ctx.note_degradation(f"tool {name} failed: {result.error_message}")

    # -- cause inference ---------------------------------------------------------------
    def _infer_candidates(self, ctx: InvestigationContext) -> list[CauseCandidate]:
        anomalies: dict[str, Any] = (ctx.fact("get_metric_anomalies").get("anomalies") or {})
        signatures: list[dict[str, Any]] = ctx.fact("get_error_signatures").get("signatures") or []
        deployments: list[dict[str, Any]] = ctx.fact("get_recent_deployments").get("deployments") or []
        changes: list[dict[str, Any]] = ctx.fact("get_configuration_changes").get("changes") or []

        templates = " ".join(str(s.get("template", "")).lower() for s in signatures if s.get("count"))
        new_templates = " ".join(
            str(s.get("template", "")).lower() for s in signatures if s.get("is_new")
        )
        recent_deployments = [
            d
            for d in deployments
            if d.get("seconds_before_detection") is not None
            and 0 <= d["seconds_before_detection"] <= 3600
        ]
        config_touching_changes = [
            c
            for c in changes
            if c.get("change_class") in {"application_config", "infrastructure", "simulated_fault"}
        ]

        def anomalous(metric: str) -> tuple[bool, float, dict[str, Any]]:
            verdict = anomalies.get(metric) or {}
            return bool(verdict.get("anomalous")), float(verdict.get("relative_change") or 0.0), verdict

        candidates: list[CauseCandidate] = []

        sat_anom, sat_rel, sat_verdict = anomalous("connection_saturation")
        if sat_anom:
            evidence = [
                f"connection pool saturation {sat_verdict.get('direction')} {sat_rel:+.0%} to "
                f"{sat_verdict.get('current')} against a baseline of {sat_verdict.get('baseline')}"
            ]
            if any(token in templates for token in ("connection", "pool", "acquisition")):
                evidence.append("log signatures contain connection pool acquisition failures")
            if "not released" in templates or "leak" in templates:
                evidence.append("log signatures show connections not being released after requests complete")
            candidates.append(
                CauseCandidate(
                    category="connection_leak",
                    weight=3.0 + (1.0 if len(evidence) > 1 else 0.0),
                    evidence=evidence,
                    contradicting=[],
                    missing=["connection pool configuration diff for the current release"],
                    tests=["check_active_connections", "check_pool_configuration"],
                )
            )
            deploy_evidence = [
                f"deployment {d.get('version')} completed {d.get('seconds_before_detection')}s before "
                f"detection with commit '{d.get('commit_message')}'"
                for d in recent_deployments
            ]
            config_evidence = [
                f"{c.get('key')} changed from '{c.get('old_value')}' to '{c.get('new_value')}' "
                f"({c.get('change_class')})"
                for c in config_touching_changes[:3]
            ]
            if deploy_evidence or config_evidence:
                candidates.append(
                    CauseCandidate(
                        category="bad_deployment_config",
                        weight=2.8 + (0.7 if config_evidence else 0.0),
                        evidence=[*sat_verdict_evidence(sat_verdict), *deploy_evidence, *config_evidence],
                        contradicting=[
                            "no deployment completed within the correlation window"
                        ]
                        if not recent_deployments
                        else [],
                        missing=["the exact configuration diff that changed pool capacity"],
                        tests=["check_deployment_impact", "check_pool_configuration"],
                    )
                )

        mem_anom, mem_rel, mem_verdict = anomalous("mem_pct")
        if mem_anom:
            evidence = [
                f"memory utilisation {mem_verdict.get('direction')} {mem_rel:+.0%} to "
                f"{mem_verdict.get('current')}% against a baseline of {mem_verdict.get('baseline')}%"
            ]
            if any(token in templates for token in ("heap", "gc", "memory", "oom")):
                evidence.append("log signatures show heap growth and garbage-collection pressure")
            candidates.append(
                CauseCandidate(
                    category="memory_leak",
                    weight=2.9 + (1.0 if len(evidence) > 1 else 0.0),
                    evidence=evidence,
                    contradicting=[],
                    missing=["heap profile attributing the growth to a specific allocation site"],
                    tests=["check_resource_pressure", "check_active_connections"],
                )
            )

        lat_anom, lat_rel, lat_verdict = anomalous("latency_ms")
        if lat_anom:
            if any(token in templates for token in ("database", "query", "acquisition")):
                candidates.append(
                    CauseCandidate(
                        category="database_overload",
                        weight=3.2,
                        evidence=[
                            f"latency {lat_verdict.get('direction')} {lat_rel:+.0%} to "
                            f"{lat_verdict.get('current')}ms (baseline {lat_verdict.get('baseline')}ms)",
                            "database query signatures are degraded, which affects all clients",
                        ],
                        contradicting=[
                            "only this service is affected, which would argue against a shared database"
                        ],
                        missing=["database server resource telemetry"],
                        tests=["check_database_health", "check_dependency_health"],
                    )
                )
            if recent_deployments and any(token in templates for token in ("timed out", "timeout")):
                candidates.append(
                    CauseCandidate(
                        category="network_issue",
                        weight=2.4,
                        evidence=[
                            "upstream call timeouts appear in log signatures",
                            *[
                                f"deployment {d.get('version')} completed "
                                f"{d.get('seconds_before_detection')}s before detection"
                                for d in recent_deployments[:2]
                            ],
                        ],
                        contradicting=[],
                        missing=["packet-loss or retry telemetry between the two services"],
                        tests=["check_dependency_health", "check_deployment_impact"],
                    )
                )

        cpu_anom, cpu_rel, cpu_verdict = anomalous("cpu_pct")
        if cpu_anom and cpu_rel > 0.15:
            candidates.append(
                CauseCandidate(
                    category="cpu_saturation",
                    weight=2.5 if not mem_anom else 1.6,
                    evidence=[
                        f"CPU utilisation {cpu_verdict.get('direction')} {cpu_rel:+.0%} to "
                        f"{cpu_verdict.get('current')}% (baseline {cpu_verdict.get('baseline')}%)"
                    ],
                    contradicting=[],
                    missing=["per-endpoint CPU attribution"],
                    tests=["check_resource_pressure"],
                )
            )

        disk_anom, disk_rel, disk_verdict = anomalous("disk_pct")
        if disk_anom and disk_rel > 0.05:
            evidence = [
                f"disk utilisation {disk_verdict.get('direction')} {disk_rel:+.0%} to "
                f"{disk_verdict.get('current')}% (baseline {disk_verdict.get('baseline')}%)"
            ]
            if "no space" in templates or "disk" in templates:
                evidence.append("log signatures show write failures from disk exhaustion")
            candidates.append(
                CauseCandidate(
                    category="disk_pressure",
                    weight=3.1,
                    evidence=evidence,
                    contradicting=[],
                    missing=["which path or log source is consuming the volume"],
                    tests=["check_resource_pressure"],
                )
            )

        if any(token in templates for token in ("cache", "stale", "namespace")):
            candidates.append(
                CauseCandidate(
                    category="cache_misconfiguration",
                    weight=3.0,
                    evidence=["log signatures report stale cache entries being served"],
                    contradicting=[],
                    missing=["cache namespace configuration diff"],
                    tests=["check_error_signatures"],
                )
            )

        if any(token in templates for token in ("authentication", "token expired", "unauthorized")):
            candidates.append(
                CauseCandidate(
                    category="expired_credentials",
                    weight=3.4,
                    evidence=["log signatures report authentication failures against a dependency"],
                    contradicting=[],
                    missing=["credential expiry timestamps"],
                    tests=["check_dependency_health", "check_error_signatures"],
                )
            )

        # Historical experience nudges the ranking but never invents a candidate.
        memory = ctx.fact("recall_historical_incidents").get("memories") or []
        for candidate in candidates:
            hits = [
                m
                for m in memory
                if candidate.category.replace("_", " ") in str(m.get("text", "")).lower()
                or candidate.category in str(m.get("text", "")).lower()
            ]
            if hits:
                candidate.weight += min(0.6, 0.2 * len(hits))
                candidate.evidence.append(
                    f"organizational memory holds {len(hits)} related experience item(s) for "
                    f"'{candidate.category}'"
                )

        # Never return nothing: fall back to the primary metric's most plausible family.
        if not candidates:
            primary = anomalies.get(ctx.plan.primary_metric) if ctx.plan else None
            candidates.append(
                CauseCandidate(
                    category="configuration",
                    weight=1.0,
                    evidence=[
                        f"no specific cause was discriminated; the primary anomalous metric is "
                        f"{ctx.plan.primary_metric if ctx.plan else 'unknown'}"
                    ],
                    contradicting=[],
                    missing=["additional telemetry to discriminate between candidate causes"],
                    tests=list(TEST_KINDS)[:3],
                )
            )

        candidates.sort(key=lambda c: c.weight, reverse=True)
        return candidates

    # -- tests -------------------------------------------------------------------------
    def reset_cache(self) -> None:
        """Drop inferred candidates so a re-investigation re-derives them from fresh evidence."""
        self._candidates = None

    def _run_tests(self, ctx: InvestigationContext, candidates: Sequence[CauseCandidate]) -> dict[str, Any]:
        """Run each candidate's discriminating tests, then re-rank on the verdicts."""
        results: dict[str, list[dict[str, Any]]] = {}
        for candidate in candidates:
            candidate_results: list[dict[str, Any]] = []
            for test in candidate.tests[:2]:
                result = ctx.call_tool(
                    "test_hypothesis",
                    {"test": test},
                    decision=f"test hypothesis '{candidate.category}' with {test}",
                )
                if not result.ok:
                    continue
                candidate_results.append({"test": test, **result.data})
                verdict = result.data.get("verdict")
                if verdict == "supports":
                    candidate.weight += 0.8
                elif verdict == "contradicts":
                    candidate.weight -= 1.4
            results[candidate.category] = candidate_results
        candidates = sorted(candidates, key=lambda c: c.weight, reverse=True)
        ctx.facts["hypothesis_tests"] = results
        return results

    # -- Reasoner interface ------------------------------------------------------------
    def propose_hypotheses(self, ctx: InvestigationContext) -> list[HypothesisDraft]:
        self._gather(ctx)
        candidates = self._infer_candidates(ctx)
        self._run_tests(ctx, candidates)
        self._candidates = candidates

        drafts: list[HypothesisDraft] = []
        for candidate in candidates[:5]:
            drafts.append(
                HypothesisDraft(
                    statement=CATEGORY_STATEMENTS.get(
                        candidate.category, f"A {candidate.category} fault is affecting the service."
                    ),
                    category=candidate.category,
                    supporting=candidate.evidence[:6],
                    contradicting=candidate.contradicting[:4],
                    missing=candidate.missing[:4],
                    test_plan=candidate.tests[:3],
                )
            )
        ctx.record_decision(
            stage="hypotheses",
            decision=f"raised {len(drafts)} hypotheses: {', '.join(d.category for d in drafts)}",
            rationale="rule-based inference over anomaly verdicts, error signatures, changes and history",
            status_label="supported" if drafts else "needs_more_evidence",
        )
        return drafts

    def conclude(
        self, ctx: InvestigationContext, hypotheses: Sequence[HypothesisDraft]
    ) -> Conclusion:
        candidates = self._candidates or self._infer_candidates(ctx)
        if not candidates:
            return Conclusion(
                root_cause_category="unknown",
                root_cause_statement="The evidence does not yet discriminate between causes.",
                status="needs_more_evidence",
                confidence_basis="no candidate cause accumulated sufficient evidence weight",
            )

        top = candidates[0]
        runner_up = candidates[1].weight if len(candidates) > 1 else 0.0
        separation = top.weight - runner_up

        if top.weight >= 3.5 and separation >= 1.0:
            status = "supported"
        elif top.weight >= 2.5:
            status = "weakly_supported"
        else:
            status = "needs_more_evidence"

        tests = ctx.fact("test_hypothesis")
        ruled_out: list[str] = []
        for candidate in candidates[1:]:
            if candidate.weight <= 1.2 or candidate.contradicting:
                reason = (
                    candidate.contradicting[0]
                    if candidate.contradicting
                    else "accumulated evidence weight stayed at or below the noise floor"
                )
                ruled_out.append(f"{candidate.category}: {reason}")

        basis = (
            f"{len(top.evidence)} supporting observation(s) with weighted score {top.weight:.1f} "
            f"versus {runner_up:.1f} for the next candidate (separation {separation:.1f}); "
            f"{len(top.tests)} discriminating test(s) executed"
        )

        conclusion = Conclusion(
            root_cause_category=top.category,
            root_cause_statement=CATEGORY_STATEMENTS.get(top.category, "Root cause identified."),
            supporting_evidence=top.evidence[:8],
            alternatives_ruled_out=ruled_out[:5],
            status=status,
            confidence_basis=basis,
        )
        ctx.record_decision(
            stage="root_cause",
            decision=f"{conclusion.root_cause_category} ({status})",
            rationale=basis,
            status_label=status,
        )
        return conclusion

    def recommend_remediation(
        self,
        ctx: InvestigationContext,
        conclusion: Conclusion,
        candidates: Sequence[str],
        history: dict[str, Any],
    ) -> RemediationChoice:
        memory = ctx.fact("recall_historical_incidents").get("memories") or []
        failed_actions = set(history.get("failed_actions") or [])
        successful_actions = set(history.get("successful_actions") or [])

        memory_failed: set[str] = set()
        memory_succeeded: set[str] = set()
        memory_basis: list[str] = []
        for item in memory:
            text = str(item.get("text", ""))
            lowered = text.lower()
            for action in ctx.fact("list_registered_actions").get("actions", []):
                code = str(action.get("code"))
                if code in lowered:
                    if "failed" in lowered or "did not resolve" in lowered:
                        memory_failed.add(code)
                        memory_basis.append(f"{code}: {text[:240]}")
                    elif "verified" in lowered or "successful" in lowered:
                        memory_succeeded.add(code)
                        memory_basis.append(f"{code}: {text[:240]}")

        scored: list[tuple[float, str, list[str]]] = []
        for code in candidates:
            score = 0.0
            reasons: list[str] = []
            action_meta = next(
                (
                    a
                    for a in ctx.fact("list_registered_actions").get("actions", [])
                    if a.get("code") == code
                ),
                {},
            )
            if action_meta.get("applicable_to_current_root_cause"):
                score += 2.0
                reasons.append("registered as applicable to the concluded root cause")
            if code in successful_actions:
                score += 2.5
                reasons.append("previously verified to resolve this root cause in this service")
            if code in memory_succeeded:
                score += 1.5
                reasons.append("organizational memory records a verified success")
            if action_meta.get("risk_level") == "low":
                score += 0.4
                reasons.append("low risk")
            if code in failed_actions:
                score -= 5.0
                reasons.append("previously FAILED verification for this root cause")
            if code in memory_failed:
                score -= 3.0
                reasons.append("organizational memory records a failed attempt")
            if code == "restart_service" and conclusion.root_cause_category in {
                "connection_leak",
                "bad_deployment_config",
                "memory_leak",
            }:
                score -= 2.0
                reasons.append(
                    "restarting clears the counter but does not remove a leak, so it would "
                    "likely fail verification again"
                )
            scored.append((score, code, reasons))

        if not scored:
            return RemediationChoice(
                action_code="",
                rationale=(
                    "No registered remediation action applies to the concluded root cause. "
                    "Escalating to a human is the correct outcome; the platform must not improvise "
                    "an unregistered action."
                ),
                status="needs_more_evidence",
                memory_basis=memory_basis[:5],
            )

        scored.sort(key=lambda item: item[0], reverse=True)
        best_score, best_code, best_reasons = scored[0]
        avoided = [
            f"{code} (score {score:.1f}): {', '.join(reasons)}"
            for score, code, reasons in scored[1:]
            if score < best_score
        ]
        for code in sorted(failed_actions | memory_failed):
            if code != best_code:
                avoided.append(f"{code}: previously failed verification for this root cause")

        choice = RemediationChoice(
            action_code=best_code,
            rationale=(
                f"Chose '{best_code}' because " + "; ".join(best_reasons) + "."
            ),
            memory_basis=memory_basis[:6],
            avoided_actions=avoided[:6],
            evidence=conclusion.supporting_evidence[:5],
            status="supported" if best_score >= 2.0 else "weakly_supported",
        )
        ctx.record_decision(
            stage="remediation",
            decision=f"recommend {best_code}",
            rationale=choice.rationale,
            memory_ids=[m.get("id", "") for m in memory[:10]],
            status_label=choice.status,
        )
        return choice

    def write_postmortem(self, ctx: InvestigationContext) -> PostmortemDraft:
        timeline = ctx.fact("get_incident_timeline").get("timeline") or []
        root_cause = ctx.incident.root_cause_summary or "Root cause under investigation."
        detection = ctx.incident.detection_meta or {}
        primary = detection.get("primary_metric", "the primary metric")

        return PostmortemDraft(
            summary=(
                f"{ctx.incident.title}. {ctx.incident.symptom[:300]}"
            ),
            impact=(
                f"Severity {ctx.incident.severity} on {ctx.service.name} in "
                f"{ctx.environment.name}. The pre-incident error-rate baseline was "
                f"{(ctx.incident.baseline or {}).get('error_rate', 0):.4f}; "
                f"degradation was measured against that baseline by the verification engine."
            ),
            detection=(
                f"Detected by the anomaly committee (robust z/MAD, rolling median, EWMA, CUSUM) "
                f"with a two-pass debounce; primary signal was {primary}."
            ),
            root_cause=root_cause,
            lessons=[],
            preventive_actions=[],
        )

    def summarize_investigation(
        self, ctx: InvestigationContext, conclusion: Conclusion, choice: RemediationChoice
    ) -> str:
        return (
            f"Incident #{ctx.incident.id} on {ctx.service.name} ({ctx.environment.name}, "
            f"severity {ctx.incident.severity}). Analysis of {ctx.registry.call_count} tool "
            f"call(s) points to {conclusion.root_cause_category} ({conclusion.status}). "
            f"Recommended action: {choice.action_code or 'escalate to a human'}."
        )


def sat_verdict_evidence(verdict: dict[str, Any]) -> list[str]:
    if not verdict:
        return []
    return [
        f"pool saturation reached {verdict.get('current')} against a pre-incident baseline of "
        f"{verdict.get('baseline')}"
    ]
