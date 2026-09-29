"""The specialized agents (build spec section 4).

Each agent owns exactly one capability and is callable independently of the others, which is
what keeps the orchestrator readable and the loop testable stage by stage:

==============================  ==========================================================
Agent                           Responsibility
==============================  ==========================================================
``EvidenceAgent``               gather evidence and persist it with provenance
``MemoryAgent``                 recall organizational experience, rank similar incidents,
                                compare current vs historical and produce the explanation
``HypothesisAgent``             raise competing hypotheses, run discriminating tests, classify
``RootCauseAgent``              select and record the supported root cause
``RemediationAgent``            choose a registered action, run the safety gate, propose
``VerificationAgent``           execute after approval, verify with multi-signal evidence,
                                close the attempt and retain the outcome as memory
``PostmortemAgent``             draft the postmortem from persisted facts (never from memory of
                                the conversation)
``LearningAgent``               retain the experience, retain pattern insights, reflect
==============================  ==========================================================

Agents never invent data: they read through the tool registry (which writes the ledger) or
directly from Postgres, and every one of them records an ``ai_decisions`` row.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Sequence

from sqlalchemy import delete, select

from app.ai_engine.context import InvestigationContext
from app.ai_engine.reasoning.base import Conclusion, HypothesisDraft, PostmortemDraft, RemediationChoice
from app.core.config import settings
from app.database.models import (
    AiDecision,
    HypothesisTest,
    Incident,
    IncidentEvidence,
    IncidentHypothesis,
    Postmortem,
    RemediationRun,
    Service,
    VerificationRun,
    utcnow,
)
from app.detection.similarity import IncidentFingerprint, rank_neighbours
from app.domain import remediation as remediation_domain
from app.domain import verification as verification_domain
from app.domain.incidents import (
    add_event,
    collect_evidence,
    compute_fingerprint,
    detect_anomalies,
    fingerprint_candidates,
)
from app.memory.documents import AttemptRecord, IncidentExperience, build_recall_query
from app.sim.engine import sim_now

logger = logging.getLogger(__name__)


def _persist_decision(ctx: InvestigationContext, entry: dict[str, Any]) -> None:
    if ctx.investigation_id is None:
        return
    ctx.db.add(
        AiDecision(
            investigation_id=ctx.investigation_id,
            stage=entry["stage"],
            decision=entry["decision"],
            rationale=entry.get("rationale", ""),
            evidence_ids=entry.get("evidence_ids") or None,
            memory_ids=entry.get("memory_ids") or None,
            status_label=entry.get("status_label", "supported"),
        )
    )


# ---------------------------------------------------------------------------------------
# Evidence Agent
# ---------------------------------------------------------------------------------------
class EvidenceAgent:
    name = "evidence_agent"

    def run(self, ctx: InvestigationContext) -> dict[str, Any]:
        verdicts = detect_anomalies(ctx.engine, service_name=ctx.service.name)
        bundle = collect_evidence(
            ctx.db,
            ctx.engine,
            incident=ctx.incident,
            service=ctx.service,
            environment=ctx.environment,
            verdicts=verdicts,
        )
        # Refresh the fingerprint now that fresh evidence exists. Root cause is still unknown,
        # so the fingerprint records its pre-conclusion state and is updated later.
        ctx.incident.fingerprint = compute_fingerprint(
            ctx.db,
            ctx.engine,
            incident=ctx.incident,
            service=ctx.service,
            environment=ctx.environment,
            verdicts=verdicts,
        ).to_dict()
        ctx.facts["evidence_bundle"] = bundle
        anomalous = [m for m, v in bundle["verdicts"].items() if v["anomalous"]]
        add_event(
            ctx.db,
            incident_id=ctx.incident.id,
            kind="evidence_collected",
            title=f"Evidence collected: {len(bundle['spikes'])} signature spike(s), "
            f"{len(anomalous)} anomalous metric(s)",
            description=(
                f"Anomalous metrics: {', '.join(anomalous) or 'none'}. "
                f"Deployments examined: {len(bundle['deployments'])}. "
                f"Configuration changes examined: {len(bundle['configuration_changes'])}."
            ),
            actor=self.name,
            source="evidence_pipeline",
            payload={
                "anomalous_metrics": anomalous,
                "signature_spikes": [s["template"][:160] for s in bundle["spikes"][:5]],
                "deployments": [d["version"] for d in bundle["deployments"]],
            },
        )
        entry = ctx.record_decision(
            stage="evidence",
            decision=f"gathered {len(bundle['verdicts'])} metric verdicts and "
            f"{len(bundle['signatures'])} error signatures",
            rationale="deterministic pipeline: anomaly committee + normalised log signatures + change correlation",
            status_label="supported",
        )
        _persist_decision(ctx, entry)
        return bundle


# ---------------------------------------------------------------------------------------
# Memory Agent
# ---------------------------------------------------------------------------------------
@dataclass
class MemoryFindings:
    recall: dict[str, Any] = field(default_factory=dict)
    neighbours: list[dict[str, Any]] = field(default_factory=list)
    comparison: dict[str, Any] | None = None
    prior_failures: list[dict[str, Any]] = field(default_factory=list)
    prior_successes: list[dict[str, Any]] = field(default_factory=list)
    explanation: str = ""
    had_useful_experience: bool = False


class MemoryAgent:
    name = "memory_agent"

    def run(self, ctx: InvestigationContext) -> MemoryFindings:
        fingerprint = IncidentFingerprint.from_dict(ctx.incident.fingerprint)
        query = build_recall_query(fingerprint=fingerprint)

        # 1. Hindsight recall (organizational experience).
        recall_result = ctx.call_tool("recall_historical_incidents", {}, decision="recall organizational experience")
        recall_payload = ctx.fact("recall_historical_incidents") or {}
        memory_hits = recall_payload.get("memories") or []

        # 2. Deterministic similarity ranking (independent of the memory backend).
        ctx.call_tool("search_similar_incidents", {"top_k": 5}, decision="rank similar historical incidents")
        similar = ctx.fact("search_similar_incidents") or {}
        neighbours = similar.get("neighbours") or []

        # 3. Compare against the closest neighbour so the UI can show the ✓/✗ breakdown.
        comparison = None
        if neighbours:
            top = neighbours[0]["incident_id"]
            result = ctx.call_tool(
                "compare_incidents", {"incident_id": top}, decision="compare with closest historical incident"
            )
            if result.ok:
                comparison = result.data

        # 4. Separate prior failures from prior successes. This is the failure-memory read that
        #    makes the second incident faster than the first.
        prior_failures: list[dict[str, Any]] = []
        prior_successes: list[dict[str, Any]] = []
        for item in memory_hits:
            text = str(item.get("text", ""))
            lowered = text.lower()
            if "fail" in lowered or "did not resolve" in lowered:
                prior_failures.append(item)
            elif "verified" in lowered or "succeeded" in lowered:
                prior_successes.append(item)

        history = ctx.fact("get_remediation_history") or {}
        for code in history.get("failed_actions") or []:
            prior_failures.append(
                {
                    "id": f"ledger:{code}",
                    "text": f"{code} previously failed verification for root cause "
                    f"'{history.get('root_cause_category')}' on this service",
                    "type": "experience",
                    "source": "postgres_remediation_attempts",
                }
            )
        for code in history.get("successful_actions") or []:
            prior_successes.append(
                {
                    "id": f"ledger:{code}",
                    "text": f"{code} previously resolved root cause "
                    f"'{history.get('root_cause_category')}' on this service",
                    "type": "experience",
                    "source": "postgres_remediation_attempts",
                }
            )

        had_useful = bool(memory_hits) or bool(history.get("attempt_count"))
        if not had_useful:
            explanation = (
                "No useful prior experience found. Organizational memory holds no recorded "
                "incident for this service and root-cause class, and the remediation ledger is "
                "empty for this root cause. This incident must be investigated from first "
                "principles."
            )
            add_event(
                ctx.db,
                incident_id=ctx.incident.id,
                kind="memory_recall",
                title="Organizational memory searched: no useful prior experience",
                description=explanation,
                actor=self.name,
                source="memory_agent",
                payload={"query": query, "backend": recall_payload.get("backend"), "hits": 0},
            )
        else:
            explanation = (
                f"Recall found {len(memory_hits)} experience item(s)"
                + (f" and {len(neighbours)} similar historical incident(s)" if neighbours else "")
                + (
                    f". Prior attempts that FAILED: {len(prior_failures)}. "
                    f"Prior attempts that succeeded: {len(prior_successes)}."
                )
            )
            add_event(
                ctx.db,
                incident_id=ctx.incident.id,
                kind="memory_recall",
                title=f"Organizational memory recalled {len(memory_hits)} item(s), "
                f"{len(neighbours)} similar incident(s)",
                description=explanation,
                actor=self.name,
                source="memory_agent",
                payload={
                    "query": query,
                    "backend": recall_payload.get("backend"),
                    "degraded": recall_payload.get("degraded"),
                    "strategy_hits": recall_payload.get("strategy_hits"),
                    "memory_ids": [m.get("id") for m in memory_hits][:10],
                    "similar": [
                        {"incident_id": n["incident_id"], "score": n["similarity"]["score"]}
                        for n in neighbours
                    ],
                    "prior_failures": [f.get("text", "")[:200] for f in prior_failures[:5]],
                    "prior_successes": [s.get("text", "")[:200] for s in prior_successes[:5]],
                },
            )

        # Persist recall hits as evidence so the reasoning layer can cite them.
        for item in memory_hits[:8]:
            ctx.db.add(
                IncidentEvidence(
                    incident_id=ctx.incident.id,
                    kind="historical_memory",
                    summary=f"[{item.get('type')}] {str(item.get('text'))[:400]}",
                    detail={
                        "memory_id": item.get("id"),
                        "type": item.get("type"),
                        "score": item.get("score"),
                        "backend": item.get("backend") or recall_payload.get("backend"),
                        "proof_count": item.get("proof_count"),
                        "source_fact_ids": item.get("source_fact_ids"),
                    },
                    source=f"hindsight:{recall_payload.get('backend')}",
                    collected_at=sim_now(),
                    strength="strong" if item.get("type") == "observation" else "moderate",
                    retrieval="memory_recall",
                )
            )
        if neighbours:
            ctx.db.add(
                IncidentEvidence(
                    incident_id=ctx.incident.id,
                    kind="similar_incident",
                    summary=(
                        f"Closest historical incident by multi-feature similarity: "
                        f"#{neighbours[0]['incident_id']} "
                        f"(score {neighbours[0]['similarity']['score']}, "
                        f"{neighbours[0]['similarity']['label']})"
                    ),
                    detail={"neighbours": neighbours, "comparison": comparison},
                    source="similarity_engine",
                    collected_at=sim_now(),
                    strength="moderate" if neighbours[0]["similarity"]["score"] >= 0.62 else "weak",
                    retrieval="deterministic",
                )
            )
        ctx.db.flush()

        entry = ctx.record_decision(
            stage="memory",
            decision=(
                f"recall returned {len(memory_hits)} memory item(s); {len(neighbours)} similar "
                f"incident(s); {len(prior_failures)} prior failure(s)"
            ),
            rationale=explanation,
            memory_ids=[m.get("id", "") for m in memory_hits][:10],
            status_label="supported" if had_useful else "needs_more_evidence",
        )
        _persist_decision(ctx, entry)

        return MemoryFindings(
            recall=recall_payload,
            neighbours=neighbours,
            comparison=comparison,
            prior_failures=prior_failures[:10],
            prior_successes=prior_successes[:10],
            explanation=explanation,
            had_useful_experience=had_useful,
        )


# ---------------------------------------------------------------------------------------
# Hypothesis Agent
# ---------------------------------------------------------------------------------------
class HypothesisAgent:
    name = "hypothesis_agent"

    def run(self, ctx: InvestigationContext, drafts: Sequence[HypothesisDraft]) -> list[IncidentHypothesis]:
        # Re-investigation replaces the hypothesis set; delete dependent tests first so the
        # foreign key stays satisfied (SQLite enforces it, and so does Postgres).
        existing = ctx.db.scalars(
            select(IncidentHypothesis).where(IncidentHypothesis.incident_id == ctx.incident.id)
        ).all()
        if existing:
            ctx.db.execute(
                delete(HypothesisTest).where(
                    HypothesisTest.hypothesis_id.in_([row.id for row in existing])
                )
            )
            for row in existing:
                ctx.db.delete(row)
        ctx.db.flush()

        rows: list[IncidentHypothesis] = []
        for index, draft in enumerate(drafts):
            row = IncidentHypothesis(
                incident_id=ctx.incident.id,
                code=f"H{index + 1}",
                statement=draft.statement,
                category=draft.category,
                status="needs_more_evidence",
                ranking=index + 1,
                supporting=draft.supporting,
                contradicting=draft.contradicting,
                missing=draft.missing,
                created_by="agent",
            )
            ctx.db.add(row)
            rows.append(row)
        ctx.db.flush()

        add_event(
            ctx.db,
            incident_id=ctx.incident.id,
            kind="hypotheses",
            title=f"{len(rows)} competing hypotheses raised",
            description=" | ".join(f"{r.code}: {r.statement[:120]}" for r in rows),
            actor=self.name,
            source="hypothesis_engine",
            payload={"hypotheses": [{"code": r.code, "category": r.category} for r in rows]},
        )
        return rows

    def record_tests(self, ctx: InvestigationContext, rows: Sequence[IncidentHypothesis]) -> None:
        """Attach the persisted test evidence to each hypothesis row."""
        tests: dict[str, list[dict[str, Any]]] = ctx.fact("hypothesis_tests") or {}
        for row in rows:
            for test in tests.get(row.category, []):
                ctx.db.add(
                    HypothesisTest(
                        hypothesis_id=row.id,
                        name=test.get("test_description") or test.get("test", ""),
                        tool="test_hypothesis",
                        arguments={"test": test.get("test")},
                        result=test.get("observation"),
                        verdict=test.get("verdict", "inconclusive"),
                        explanation=test.get("interpretation", ""),
                    )
                )
            verdicts = [t.get("verdict") for t in tests.get(row.category, [])]
            if "contradicts" in verdicts and "supports" not in verdicts:
                row.status = "ruled_out"
            elif "supports" in verdicts:
                row.status = "supported"
            elif verdicts:
                row.status = "weakly_supported"
        ctx.db.flush()


# ---------------------------------------------------------------------------------------
# Root Cause Agent
# ---------------------------------------------------------------------------------------
class RootCauseAgent:
    name = "root_cause_agent"

    def run(self, ctx: InvestigationContext, conclusion: Conclusion) -> Conclusion:
        ctx.incident.root_cause_category = conclusion.root_cause_category
        ctx.incident.root_cause_summary = conclusion.root_cause_statement
        ctx.incident.status = "investigating" if ctx.incident.status == "detected" else ctx.incident.status

        # Recompute the fingerprint now that the root cause is known — this is the single most
        # valuable feature for future similarity matching.
        ctx.incident.fingerprint = compute_fingerprint(
            ctx.db,
            ctx.engine,
            incident=ctx.incident,
            service=ctx.service,
            environment=ctx.environment,
            root_cause_category=conclusion.root_cause_category,
        ).to_dict()

        markers = [m for m in ctx.facts.get("get_metric_anomalies", {}).get("anomalies", {}).items()]
        for metric, verdict in markers:
            if verdict.get("anomalous"):
                rows = ctx.db.scalars(
                    select(IncidentEvidence).where(
                        IncidentEvidence.incident_id == ctx.incident.id,
                        IncidentEvidence.kind == "metric_anomaly",
                    )
                ).all()
                for row in rows:
                    if metric in row.summary:
                        row.supports = [conclusion.root_cause_category]
        ctx.db.flush()

        add_event(
            ctx.db,
            incident_id=ctx.incident.id,
            kind="root_cause",
            title=f"Root cause: {conclusion.root_cause_category} ({conclusion.status})",
            description=conclusion.root_cause_statement,
            actor=self.name,
            source="root_cause_engine",
            payload={
                "status": conclusion.status,
                "supporting_evidence": conclusion.supporting_evidence,
                "alternatives_ruled_out": conclusion.alternatives_ruled_out,
                "confidence_basis": conclusion.confidence_basis,
            },
        )
        entry = ctx.record_decision(
            stage="root_cause",
            decision=f"{conclusion.root_cause_category} ({conclusion.status})",
            rationale=conclusion.confidence_basis,
            status_label=conclusion.status,
        )
        _persist_decision(ctx, entry)
        return conclusion


# ---------------------------------------------------------------------------------------
# Remediation Agent
# ---------------------------------------------------------------------------------------
@dataclass
class RemediationPlan:
    choice: RemediationChoice
    candidates: list[str]
    run_id: int | None = None
    status: str = "proposed"
    safety: dict[str, Any] | None = None
    blocked_reasons: list[str] = field(default_factory=list)


class RemediationAgent:
    name = "remediation_agent"

    def run(self, ctx: InvestigationContext, choice: RemediationChoice) -> RemediationPlan:
        actions = remediation_domain.actions_for_root_cause(
            ctx.db, ctx.incident.root_cause_category or "unknown"
        )
        candidates = [a.code for a in actions]

        if not candidates:
            add_event(
                ctx.db,
                incident_id=ctx.incident.id,
                kind="remediation_blocked",
                title="No registered action resolves this root cause — escalating to a human",
                description=(
                    f"No action in the remediation registry declares applicability to root cause "
                    f"'{ctx.incident.root_cause_category}'. The platform will not improvise an "
                    f"unregistered action."
                ),
                actor=self.name,
                source="safety_gate",
            )
            ctx.incident.status = "escalated"
            return RemediationPlan(choice=choice, candidates=[], status="escalated")

        if choice.action_code not in candidates:
            choice = RemediationChoice(
                action_code=candidates[0],
                rationale=(
                    f"The recommended action '{choice.action_code}' was not in the applicable set "
                    f"for root cause '{ctx.incident.root_cause_category}'; substituting the "
                    f"highest-ranked registered candidate '{candidates[0]}'."
                ),
                status="weakly_supported",
            )

        run = remediation_domain.propose(
            ctx.db,
            incident=ctx.incident,
            service=ctx.service,
            environment=ctx.environment,
            action_code=choice.action_code,
            rationale=choice.rationale,
            parameters={"recommended_by": ctx.plan and "ai" or "ai", "memory_basis": choice.memory_basis[:5]},
            actor="ai",
            actor_role="sre",
        )
        safety = run.safety_checks or []
        blocked = bool(run.status == "blocked")
        verdict = remediation_domain.evaluate_safety_gate(
            ctx.db,
            action_code=choice.action_code,
            root_cause_category=ctx.incident.root_cause_category or "unknown",
            environment=ctx.environment,
            requested_autonomy=settings.max_autonomy_level,
            actor_role="sre",
        )

        add_event(
            ctx.db,
            incident_id=ctx.incident.id,
            kind="remediation_proposed" if not blocked else "remediation_blocked",
            title=(
                f"Remediation proposed: {choice.action_code}"
                if not blocked
                else f"Remediation blocked by the safety gate: {choice.action_code}"
            ),
            description=choice.rationale
            if not blocked
            else "; ".join(verdict.reasons),
            actor=self.name,
            source="remediation_agent",
            payload={
                "run_id": run.id,
                "action_code": choice.action_code,
                "candidates": candidates,
                "requires_approval": verdict.requires_approval,
                "memory_basis": choice.memory_basis[:5],
                "avoided_actions": choice.avoided_actions[:5],
                "safety_checks": safety,
            },
        )
        entry = ctx.record_decision(
            stage="remediation",
            decision=f"{'blocked:' if blocked else 'proposed'} {choice.action_code}",
            rationale=choice.rationale,
            memory_ids=[m.get("id", "") for m in ctx.fact("recall_historical_incidents").get("memories", [])][:6],
            status_label="contradicted" if blocked else choice.status,
        )
        _persist_decision(ctx, entry)

        ctx.incident.status = "awaiting_approval" if (not blocked and verdict.requires_approval) else "remediation_proposed"
        ctx.db.flush()
        return RemediationPlan(
            choice=choice,
            candidates=candidates,
            run_id=run.id,
            status="blocked" if blocked else ("awaiting_approval" if verdict.requires_approval else "ready"),
            safety=verdict.to_dict(),
            blocked_reasons=verdict.reasons,
        )


# ---------------------------------------------------------------------------------------
# Verification Agent
# ---------------------------------------------------------------------------------------
@dataclass
class VerificationOutcomeBundle:
    verdict: str
    outcome: str
    verification_id: int
    improvement_pct: float
    before: dict[str, float]
    after: dict[str, float]
    checks: list[dict[str, Any]] = field(default_factory=list)
    attempt: AttemptRecord | None = None


class VerificationAgent:
    name = "verification_agent"

    def execute_and_verify(
        self,
        ctx: InvestigationContext,
        *,
        run: RemediationRun,
        actor: str,
        actor_role: str,
    ) -> VerificationOutcomeBundle:
        # Execute (re-runs the safety gate internally — approval is not a permanent bypass).
        remediation_domain.execute(
            ctx.db,
            engine=ctx.engine,
            run=run,
            actor=actor,
            actor_role=actor_role,
            service=ctx.service,
            environment=ctx.environment,
            incident=ctx.incident,
        )
        ctx.incident.status = "remediating"
        add_event(
            ctx.db,
            incident_id=ctx.incident.id,
            kind="remediation_executed",
            title=f"Executed {run.action_code} (attempt {run.attempt})",
            description=(run.result or {}).get("note", ""),
            actor=actor,
            source="remediation_executor",
            payload={"run_id": run.id, "result": run.result},
        )
        return self.verify(ctx, run=run, actor=actor)

    def verify(
        self, ctx: InvestigationContext, *, run: RemediationRun, actor: str
    ) -> VerificationOutcomeBundle:
        outcome = verification_domain.verify(
            ctx.db,
            engine=ctx.engine,
            incident=ctx.incident,
            service=ctx.service,
            environment=ctx.environment,
            remediation_run=run,
            actor=actor,
        )

        attempt = AttemptRecord(
            attempt_number=run.attempt,
            action_code=run.action_code,
            action_name=(remediation_domain.get_action(ctx.db, run.action_code).name if remediation_domain.get_action(ctx.db, run.action_code) else ""),
            outcome=outcome.outcome,
            verification_results=[c.detail for c in outcome.checks if not c.passed] or [
                f"{c.metric}: {c.before} -> {c.after}" for c in outcome.checks
            ],
            reason=outcome.verdict_reason[:500],
        )

        remediation_domain.record_attempt_outcome(
            ctx.db,
            run=run,
            outcome=outcome.outcome,
            failure_reason=outcome.verdict_reason[:500] if outcome.outcome != "succeeded" else "",
            verification_id=outcome.verification_id,
        )

        # Failure memory is retained immediately, not at the end of the incident, so it is
        # available to the very next investigation even if this incident is still open.
        try:
            ctx.memory.retain_attempt_outcome(
                incident_id=ctx.incident.id,
                service=ctx.service.name,
                environment=ctx.environment.name,
                root_cause_category=ctx.incident.root_cause_category or "unknown",
                attempt=attempt,
                db=ctx.db,
            )
        except Exception as exc:  # noqa: BLE001 - memory must never break verification
            logger.warning("could not retain attempt outcome: %s", exc)
            ctx.note_degradation(f"MEMORY_DEGRADED: could not retain attempt outcome ({exc})")

        entry = ctx.record_decision(
            stage="verification",
            decision=f"{outcome.verdict} after {run.action_code}",
            rationale=outcome.verdict_reason[:800],
            status_label="supported" if outcome.verdict == "verified_success" else "contradicted",
        )
        _persist_decision(ctx, entry)

        if outcome.verdict == "verified_success":
            ctx.incident.status = "verified"
            ctx.incident.resolved_at = sim_now()
            ctx.incident.verified_at = sim_now()
            if ctx.incident.detected_at:
                ctx.incident.mttr_seconds = (
                    ctx.incident.resolved_at - ctx.incident.detected_at
                ).total_seconds()
        else:
            ctx.incident.status = "verification_failed"
        ctx.db.flush()

        return VerificationOutcomeBundle(
            verdict=outcome.verdict,
            outcome=outcome.outcome,
            verification_id=outcome.verification_id,
            improvement_pct=outcome.improvement_pct,
            before=outcome.before,
            after=outcome.after,
            checks=[c.to_dict() for c in outcome.checks],
            attempt=attempt,
        )


# ---------------------------------------------------------------------------------------
# Postmortem Agent
# ---------------------------------------------------------------------------------------
class PostmortemAgent:
    name = "postmortem_agent"

    def draft(self, ctx: InvestigationContext, draft: PostmortemDraft) -> Postmortem:
        existing = ctx.db.scalar(select(Postmortem).where(Postmortem.incident_id == ctx.incident.id))
        attempts = ctx.db.scalars(
            select(RemediationRun)
            .where(RemediationRun.incident_id == ctx.incident.id)
            .order_by(RemediationRun.id)
        ).all()
        verifications = ctx.db.scalars(
            select(VerificationRun).where(VerificationRun.incident_id == ctx.incident.id).order_by(VerificationRun.id)
        ).all()

        failed_attempts = [
            {
                "action_code": run.action_code,
                "attempt": run.attempt,
                "outcome": run.outcome,
                "note": (run.result or {}).get("note", ""),
            }
            for run in attempts
            if run.outcome in {"failed", "partial"}
        ]
        successful = next((run for run in attempts if run.outcome == "succeeded"), None)
        last_verification = verifications[-1] if verifications else None

        payload = {
            "summary": draft.summary,
            "impact": draft.impact,
            "detection": draft.detection,
            "root_cause": draft.root_cause,
            "timeline": ctx.fact("get_incident_timeline").get("timeline") or [],
            "evidence": [
                {"kind": row.kind, "summary": row.summary, "strength": row.strength}
                for row in ctx.db.scalars(
                    select(IncidentEvidence).where(IncidentEvidence.incident_id == ctx.incident.id)
                ).all()
            ][:40],
            "hypotheses": [
                {
                    "code": row.code,
                    "category": row.category,
                    "status": row.status,
                    "statement": row.statement,
                }
                for row in ctx.db.scalars(
                    select(IncidentHypothesis).where(IncidentHypothesis.incident_id == ctx.incident.id)
                ).all()
            ],
            "failed_attempts": failed_attempts,
            "successful_remediation": (
                f"{successful.action_code} (attempt {successful.attempt})"
                if successful
                else "none — no remediation has been verified for this incident"
            ),
            "verification": (
                f"{last_verification.verdict}: {last_verification.verdict_reason}"
                if last_verification
                else "no verification recorded"
            ),
            "deployment_relationship": self._deployment_relationship(ctx),
            "lessons": list(draft.lessons) or self._derive_lessons(ctx, failed_attempts, successful),
            "preventive_actions": list(draft.preventive_actions)
            or self._derive_preventive_actions(ctx, successful),
            "status": "draft",
            "authored_by": "ai",
        }

        if existing is None:
            existing = Postmortem(incident_id=ctx.incident.id, **payload)
            ctx.db.add(existing)
        else:
            for key, value in payload.items():
                setattr(existing, key, value)
            existing.updated_at = utcnow()
        ctx.db.flush()

        add_event(
            ctx.db,
            incident_id=ctx.incident.id,
            kind="postmortem_draft",
            title=f"Postmortem drafted (id {existing.id}); awaiting human approval",
            description=draft.summary[:500],
            actor=self.name,
            source="postmortem_agent",
            payload={"postmortem_id": existing.id, "failed_attempts": len(failed_attempts)},
        )
        return existing

    @staticmethod
    def _deployment_relationship(ctx: InvestigationContext) -> str:
        deployments = ctx.fact("get_recent_deployments").get("deployments") or []
        recent = [d for d in deployments if (d.get("seconds_before_detection") or 1e9) <= 3600]
        if not recent:
            return "No deployment completed within the hour before detection."
        first = recent[0]
        return (
            f"Deployment {first.get('version')} ({first.get('change_class')}) completed "
            f"{first.get('seconds_before_detection')}s before detection. This is a temporal "
            f"relationship; it is not by itself proof that the deployment caused the incident."
        )

    @staticmethod
    def _derive_lessons(
        ctx: InvestigationContext, failed: Sequence[dict[str, Any]], successful: Any
    ) -> list[str]:
        lessons: list[str] = []
        for entry in failed:
            lessons.append(
                f"{entry['action_code']} did not resolve this root cause "
                f"(attempt {entry['attempt']}); verification recorded the failure, so it is now "
                f"recorded as a known-failed approach for '{ctx.incident.root_cause_category}'."
            )
        if successful is not None:
            lessons.append(
                f"{successful.action_code} resolved the incident once the cause itself was "
                f"removed; the successful action is now the precedent for this root cause."
            )
        if not failed and not successful:
            lessons.append(
                "No remediation has been verified for this incident; the investigation outcome is "
                "recorded so the next occurrence starts from evidence rather than from zero."
            )
        return lessons

    @staticmethod
    def _derive_preventive_actions(ctx: InvestigationContext, successful: Any) -> list[str]:
        actions = [
            f"Alert on {ctx.plan.primary_metric if ctx.plan else 'the primary metric'} crossing its "
            f"rolling baseline more than 25% for two consecutive samples.",
            "Pin the reviewed configuration values in the deployment manifest so a release cannot "
            "silently change them.",
        ]
        if successful is not None:
            actions.append(
                f"Add a pre-flight check that runs '{successful.action_code}' automatically when "
                f"the detection signature for '{ctx.incident.root_cause_category}' fires again."
            )
        return actions

    def approve(self, ctx: InvestigationContext, *, postmortem: Postmortem, actor: str) -> Postmortem:
        postmortem.status = "approved"
        postmortem.approved_by = actor
        postmortem.approved_at = utcnow()
        ctx.db.flush()
        add_event(
            ctx.db,
            incident_id=ctx.incident.id,
            kind="postmortem_approved",
            title=f"Postmortem approved by {actor}",
            description="The approved learning is eligible to be retained in organizational memory.",
            actor=actor,
            source="postmortem_agent",
        )
        return postmortem


# ---------------------------------------------------------------------------------------
# Learning Agent
# ---------------------------------------------------------------------------------------
class LearningAgent:
    name = "learning_agent"

    def run(self, ctx: InvestigationContext) -> dict[str, Any]:
        attempts = ctx.db.scalars(
            select(RemediationRun)
            .where(RemediationRun.incident_id == ctx.incident.id)
            .order_by(RemediationRun.id)
        ).all()
        attempt_records = [
            AttemptRecord(
                attempt_number=run.attempt,
                action_code=run.action_code,
                action_name=(remediation_domain.get_action(ctx.db, run.action_code).name if remediation_domain.get_action(ctx.db, run.action_code) else ""),
                outcome=run.outcome or "unknown",
                reason=(run.result or {}).get("note", ""),
            )
            for run in attempts
        ]

        verifications = ctx.db.scalars(
            select(VerificationRun).where(VerificationRun.incident_id == ctx.incident.id).order_by(VerificationRun.id)
        ).all()
        last = verifications[-1] if verifications else None
        verification_text = (
            f"{last.verdict} (improvement {last.improvement_pct}%): {last.verdict_reason}"
            if last
            else "no verification was recorded"
        )

        evidence_rows = ctx.db.scalars(
            select(IncidentEvidence).where(IncidentEvidence.incident_id == ctx.incident.id)
        ).all()
        evidence_lines = [
            f"{row.kind}: {row.summary[:200]}"
            for row in evidence_rows
            if row.kind in {"metric_anomaly", "error_signature", "deployment", "configuration_change"}
        ][:10]

        timeline = ctx.fact("get_incident_timeline").get("timeline") or []
        timeline_lines = [f"{e.get('ts', '')[11:19]} {e.get('title')}" for e in timeline[:12]]

        experience = IncidentExperience(
            incident_id=ctx.incident.id,
            title=ctx.incident.title,
            service=ctx.service.name,
            environment=ctx.environment.name,
            severity=ctx.incident.severity,
            detected_at=ctx.incident.detected_at.isoformat(),
            trigger=(ctx.fact("get_metric_anomalies").get("anomalies", {}).get(
                ctx.plan.primary_metric if ctx.plan else "error_rate", {}
            ) or {}).get("reason", ""),
            symptoms=[ctx.incident.symptom],
            evidence=evidence_lines,
            timeline=timeline_lines,
            hypotheses=[
                f"{row.code} [{row.category}] {row.statement[:160]}"
                for row in ctx.db.scalars(
                    select(IncidentHypothesis).where(IncidentHypothesis.incident_id == ctx.incident.id)
                ).all()
            ],
            ruled_out=[
                f"{row.code} ({row.category}) — {row.status}"
                for row in ctx.db.scalars(
                    select(IncidentHypothesis).where(IncidentHypothesis.incident_id == ctx.incident.id)
                ).all()
                if row.status == "ruled_out"
            ],
            root_cause_category=ctx.incident.root_cause_category or "unknown",
            root_cause=ctx.incident.root_cause_summary,
            attempts=attempt_records,
            successful_remediation=(
                f"{next(r.action_code for r in attempts if r.outcome == 'succeeded')}"
                if any(r.outcome == "succeeded" for r in attempts)
                else ""
            ),
            verification=verification_text,
            business_impact=(
                f"severity {ctx.incident.severity} on a {ctx.service.tier}-tier service in "
                f"{ctx.environment.name}"
            ),
            lessons=[
                f"{r.action_code} failed to resolve '{ctx.incident.root_cause_category}' "
                f"(attempt {r.attempt}) — do not repeat it as the first choice."
                for r in attempts
                if r.outcome in {"failed", "partial"}
            ],
            preventive_actions=list(
                (ctx.db.scalar(select(Postmortem).where(Postmortem.incident_id == ctx.incident.id)) or Postmortem(preventive_actions=[])).preventive_actions
                or []
            ),
            deployment_relationship=self._deployment_relationship(ctx),
            service_dependencies=ctx.fact("get_service_dependencies").get("dependencies") or [],
            mttr_seconds=ctx.incident.mttr_seconds,
        )

        result = ctx.memory.retain_incident_experience(
            experience=experience,
            fingerprint=IncidentFingerprint.from_dict(ctx.incident.fingerprint),
            db=ctx.db,
            context=(
                f"incident #{ctx.incident.id} resolution experience "
                f"({'resolved' if ctx.incident.status == 'verified' else 'unresolved'})"
            ),
        )

        add_event(
            ctx.db,
            incident_id=ctx.incident.id,
            kind="learning_retained",
            title=f"Experience retained in organizational memory ({result.backend})",
            description=(
                f"Retained {result.facts_extracted or 'n/a'} facts covering root cause, "
                f"{len(attempt_records)} remediation attempt(s) (including failures), "
                f"verification outcome and preventive actions."
            ),
            actor=self.name,
            source="learning_agent",
            payload={
                "document_id": result.document_id,
                "memory_id": result.memory_id,
                "backend": result.backend,
                "scope": "incident_experience",
                "attempts": [
                    {"action_code": a.action_code, "outcome": a.outcome} for a in attempt_records
                ],
            },
        )

        reflection = ctx.memory.reflect(
            query=(
                f"What have we learned about resolving {ctx.incident.root_cause_category} on "
                f"{ctx.service.name}, and which approaches have failed?"
            ),
            context=f"post-resolution learning for incident #{ctx.incident.id}",
            db=ctx.db,
            incident_id=ctx.incident.id,
        )

        entry = ctx.record_decision(
            stage="learning",
            decision=f"retained incident experience via {result.backend}",
            rationale=result.detail,
            memory_ids=[result.memory_id],
            status_label="supported",
        )
        _persist_decision(ctx, entry)

        return {
            "retention": result.to_dict(),
            "reflection": reflection.to_dict(),
            "attempts": [
                {"action_code": a.action_code, "outcome": a.outcome, "attempt": a.attempt_number}
                for a in attempt_records
            ],
        }

    @staticmethod
    def _deployment_relationship(ctx: InvestigationContext) -> str:
        deployments = ctx.fact("get_recent_deployments").get("deployments") or []
        recent = [d for d in deployments if (d.get("seconds_before_detection") or 1e9) <= 3600]
        if not recent:
            return "not deployment-related"
        return (
            f"deployment {recent[0].get('version')} preceded detection by "
            f"{recent[0].get('seconds_before_detection')}s (temporal relationship only)"
        )
