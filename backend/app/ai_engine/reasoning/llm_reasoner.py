"""The LLM reasoning path, wrapped in validation.

Structure, in order:

1. **Guaranteed evidence.** A deterministic evidence pass runs first, so the tool ledger always
   contains real observations even if the model's own tool-calling misbehaves.
2. **Genuine model investigation.** The model then gets the tool registry and may call any
   exposed tool to investigate further. This is the visible "the AI is actually investigating"
   moment, and each call lands in ``ai_tool_calls``.
3. **Structured decisions.** Hypotheses, the conclusion, the remediation choice, the postmortem
   and the summary are each obtained through a *forced function call* with a JSON schema, so
   the output is validated rather than parsed out of prose.
4. **Validation against reality.** Every conclusion is checked: the root-cause category must be
   in the canonical taxonomy, at least one cited observation must actually exist in the tool
   results, and a recommended action must be (a) in the registry, (b) applicable, and (c) if it
   previously failed, explicitly acknowledged as such. Any failure rejects the model's output,
   falls back to the deterministic reasoner, and records the rejection on the investigation.

Point 4 is the difference between "an LLM that sounds right" and "an agent whose claims are
checkable". It is also directly testable, which is why it lives in the codebase rather than in
a prompt instruction.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Sequence

from app.ai_engine.context import InvestigationContext
from app.ai_engine.llm import LLMClient
from app.ai_engine.prompts import (
    CONCLUSION_SCHEMA,
    CONCLUSION_SYSTEM,
    HYPOTHESES_SCHEMA,
    HYPOTHESIS_SYSTEM,
    POSTMORTEM_SCHEMA,
    POSTMORTEM_SYSTEM,
    REMEDIATION_SCHEMA,
    REMEDIATION_SYSTEM,
    SUMMARY_SCHEMA,
    SUMMARY_SYSTEM,
)
from app.ai_engine.reasoning.base import (
    Conclusion,
    HypothesisDraft,
    PostmortemDraft,
    RemediationChoice,
)
from app.ai_engine.reasoning.heuristics import CANONICAL_CATEGORIES, HeuristicReasoner
from app.ai_engine.tools import available_tool_names, openai_tool_schemas
from app.core.errors import AIMalformedResponseError, AIUnavailableError

logger = logging.getLogger(__name__)

_TOKEN_RE = re.compile(r"[a-z0-9_]{4,}")


def _tokens(text: str) -> set[str]:
    return set(_TOKEN_RE.findall((text or "").lower()))


def build_evidence_brief(ctx: InvestigationContext, *, max_chars: int = 14000) -> str:
    """Compact, structured view of everything the tools returned.

    Deliberately *not* raw logs: the log pipeline already reduced thousands of lines to ranked
    signatures, and that reduction is what goes to the model.
    """
    sections: list[str] = []
    anomalies = ctx.fact("get_metric_anomalies").get("anomalies") or {}
    if anomalies:
        lines = []
        for metric, verdict in anomalies.items():
            if not verdict.get("anomalous"):
                continue
            lines.append(
                f"  - {metric}: {verdict.get('direction')} {float(verdict.get('relative_change') or 0):+.1%} "
                f"to {verdict.get('current')} (baseline {verdict.get('baseline')}), "
                f"severity={verdict.get('severity')}, score={verdict.get('score')}"
            )
        sections.append("ANOMALIES (detector committee output)\n" + ("\n".join(lines) or "  none"))

    signatures = ctx.fact("get_error_signatures").get("signatures") or []
    if signatures:
        lines = [
            f"  - [{s.get('severity')}] x{s.get('count')} "
            f"({'NEW' if s.get('is_new') else 'ratio ' + str(s.get('spike_ratio'))}): {s.get('template')}"
            for s in signatures[:10]
        ]
        sections.append("ERROR SIGNATURES (normalised, variable values masked)\n" + "\n".join(lines))

    deployments = ctx.fact("get_recent_deployments").get("deployments") or []
    if deployments:
        lines = [
            f"  - {d.get('version')} completed "
            f"{d.get('seconds_before_detection')}s before detection, class={d.get('change_class')}: "
            f"{d.get('commit_message')}"
            for d in deployments[:5]
        ]
        sections.append("DEPLOYMENTS\n" + "\n".join(lines))

    changes = ctx.fact("get_configuration_changes").get("changes") or []
    if changes:
        lines = [
            f"  - {c.get('key')}: '{c.get('old_value')}' -> '{c.get('new_value')}' ({c.get('change_class')})"
            for c in changes[:6]
        ]
        sections.append("CONFIGURATION CHANGES\n" + "\n".join(lines))

    history = ctx.fact("get_remediation_history")
    if history:
        sections.append(
            "REMEDIATION HISTORY (Postgres ledger, for this service and root cause)\n"
            f"  failed actions: {history.get('failed_actions')}\n"
            f"  successful actions: {history.get('successful_actions')}\n"
            f"  attempts: {history.get('attempt_count')}"
        )

    memory = ctx.fact("recall_historical_incidents")
    if memory:
        lines = [
            f"  - [{m.get('type')}] {str(m.get('text'))[:300]}"
            for m in (memory.get("memories") or [])[:8]
        ]
        sections.append(
            "ORGANIZATIONAL MEMORY (Hindsight recall, backend="
            f"{memory.get('backend')}, degraded={memory.get('degraded')})\n"
            + ("\n".join(lines) or "  no prior experience found")
        )

    reflection = ctx.fact("reflect_on_historical_experience")
    if reflection and reflection.get("answer"):
        sections.append("ORGANIZATIONAL REFLECTION\n" + str(reflection["answer"])[:2500])

    similar = ctx.fact("search_similar_incidents")
    if similar:
        lines = []
        for neighbour in (similar.get("neighbours") or [])[:5]:
            sim = neighbour.get("similarity", {})
            lines.append(
                f"  - incident #{neighbour.get('incident_id')} ({neighbour.get('service')}, "
                f"root cause {neighbour.get('root_cause_category')}) score {sim.get('score')} "
                f"[{sim.get('label')}]"
            )
            for item in (sim.get("matched") or [])[:3]:
                lines.append(f"      + {item}")
            for item in (sim.get("differences") or [])[:3]:
                lines.append(f"      - {item}")
        sections.append("SIMILAR HISTORICAL INCIDENTS (explainable hybrid similarity)\n" + "\n".join(lines))

    test_rows = ctx.fact("test_hypothesis")
    if isinstance(test_rows, dict) and test_rows:
        sections.append(
            "HYPOTHESIS TEST RESULT\n" + json.dumps(test_rows.get("observation", {}), default=str)[:1200]
            + f"\n  verdict: {test_rows.get('verdict')}"
        )

    timeline = ctx.fact("get_incident_timeline").get("timeline") or []
    if timeline:
        lines = [f"  - {e.get('ts')} [{e.get('kind')}] {e.get('title')}" for e in timeline[:12]]
        sections.append("INCIDENT TIMELINE\n" + "\n".join(lines))

    actions = ctx.fact("list_registered_actions").get("actions") or []
    if actions:
        lines = [
            f"  - {a.get('code')} (risk {a.get('risk_level')}, approval={a.get('required_approval')}, "
            f"applicable={a.get('applicable_to_current_root_cause')})"
            for a in actions
        ]
        sections.append("REMEDIATION REGISTRY\n" + "\n".join(lines))

    brief = "\n\n".join(sections)
    return brief[:max_chars]


class LLMReasoner:
    """Model-driven reasoner with deterministic validation."""

    def __init__(self, *, llm: LLMClient, fallback: HeuristicReasoner | None = None) -> None:
        self.llm = llm
        self.fallback = fallback or HeuristicReasoner()
        self.degraded = not llm.available
        self.rejections: list[str] = []
        self.mode = "llm" if llm.available else "offline"

    # -- helpers -----------------------------------------------------------------------
    def _repair_llm(self, reason: str) -> None:
        self.degraded = True
        self.rejections.append(reason)
        logger.warning("LLM output rejected, falling back to deterministic reasoner: %s", reason)

    def _investigate_with_tools(self, ctx: InvestigationContext) -> None:
        """Let the model call additional tools. Failures here are never fatal."""
        try:
            result = self.llm.run_tool_loop(
                system=(
                    "You are an SRE incident investigator. Investigate the incident using the "
                    "provided tools. Call only the tools you need, then stop. Do not speculate in "
                    "prose; the tools return the facts."
                ),
                user=(
                    f"Incident: {ctx.incident.title}\n"
                    f"Service: {ctx.service.name} ({ctx.environment.name}), severity {ctx.incident.severity}\n"
                    f"Reported symptom: {ctx.incident.symptom}\n\n"
                    f"Evidence already gathered:\n{build_evidence_brief(ctx, max_chars=6000)}\n\n"
                    "Call any additional tools that would discriminate between competing causes, "
                    "then say 'investigation complete'."
                ),
                tools=openai_tool_schemas(available_tool_names()),
                executor=lambda name, args, round_index: ctx.registry.execute(
                    name,
                    args,
                    ctx.tool_ctx,
                    round_index=round_index,
                    decision="model-initiated investigation tool call",
                ).to_dict(),
                max_rounds=3,
            )
            if result.hit_round_limit:
                ctx.note_degradation("AI_TOOL_ROUND_LIMIT: model hit the tool round limit")
        except (AIUnavailableError, AIMalformedResponseError) as exc:
            ctx.note_degradation(f"AI_UNAVAILABLE: {exc}")
            self._repair_llm(str(exc))

    def _citations_are_real(self, citations: Sequence[str], ctx: InvestigationContext) -> bool:
        """Reject a conclusion whose 'evidence' does not appear in any tool result."""
        if not citations:
            return False
        corpus = json.dumps(ctx.facts, default=str).lower()
        corpus_tokens = _tokens(corpus)
        for citation in citations:
            needle = _tokens(citation)
            if not needle:
                continue
            if len(needle & corpus_tokens) / len(needle) >= 0.25:
                return True
        return False

    # -- Reasoner interface ------------------------------------------------------------
    def propose_hypotheses(self, ctx: InvestigationContext) -> list[HypothesisDraft]:
        # Deterministic evidence pass first: the ledger must never be empty.
        baseline = self.fallback.propose_hypotheses(ctx)
        if not self.llm.available or self.degraded:
            return baseline

        self._investigate_with_tools(ctx)
        try:
            payload = self.llm.structured_decision(
                system=HYPOTHESIS_SYSTEM,
                user=(
                    f"Incident: {ctx.incident.title}\n"
                    f"Service: {ctx.service.name} in {ctx.environment.name}, severity {ctx.incident.severity}\n"
                    f"Symptom: {ctx.incident.symptom}\n\n"
                    f"Evidence gathered from tools:\n{build_evidence_brief(ctx)}\n\n"
                    "Propose the competing hypotheses."
                ),
                function_name="submit_hypotheses",
                schema=HYPOTHESES_SCHEMA,
                description="Return the competing incident hypotheses.",
            )
        except (AIUnavailableError, AIMalformedResponseError) as exc:
            ctx.note_degradation(f"AI_UNAVAILABLE: {exc}")
            self._repair_llm(str(exc))
            return baseline

        drafts: list[HypothesisDraft] = []
        for raw in payload.get("hypotheses") or []:
            if not isinstance(raw, dict) or not raw.get("statement"):
                continue
            drafts.append(
                HypothesisDraft(
                    statement=str(raw.get("statement"))[:600],
                    category=str(raw.get("category") or "unknown")[:80],
                    supporting=[str(x)[:400] for x in (raw.get("supporting") or [])][:6],
                    contradicting=[str(x)[:400] for x in (raw.get("contradicting") or [])][:4],
                    missing=[str(x)[:300] for x in (raw.get("missing") or [])][:4],
                    test_plan=[str(x) for x in (raw.get("test_plan") or [])][:4],
                )
            )

        if len(drafts) < 2:
            self._repair_llm(f"model returned {len(drafts)} usable hypotheses (minimum 2)")
            return baseline

        # Validation: at least one hypothesis must cite evidence that exists.
        if not any(self._citations_are_real(d.supporting, ctx) for d in drafts):
            self._repair_llm("no hypothesis cited an observation present in the tool results")
            return baseline

        ctx.record_decision(
            stage="hypotheses",
            decision=f"model raised {len(drafts)} hypotheses",
            rationale="; ".join(f"{d.category}" for d in drafts)[:500],
            status_label="supported",
        )
        return drafts

    def conclude(
        self, ctx: InvestigationContext, hypotheses: Sequence[HypothesisDraft]
    ) -> Conclusion:
        baseline = self.fallback.conclude(ctx, hypotheses)
        if not self.llm.available or self.degraded:
            return baseline

        try:
            payload = self.llm.structured_decision(
                system=CONCLUSION_SYSTEM,
                user=(
                    f"Incident: {ctx.incident.title}\n"
                    f"Service: {ctx.service.name} in {ctx.environment.name}\n"
                    f"Hypotheses under consideration:\n"
                    + "\n".join(
                        f"  {i + 1}. [{h.category}] {h.statement} "
                        f"(support: {len(h.supporting)}, contradict: {len(h.contradicting)})"
                        for i, h in enumerate(hypotheses)
                    )
                    + f"\n\nEvidence gathered from tools:\n{build_evidence_brief(ctx)}\n\n"
                    "State the root cause."
                ),
                function_name="submit_conclusion",
                schema=CONCLUSION_SCHEMA,
                description="Return the root-cause conclusion.",
            )
        except (AIUnavailableError, AIMalformedResponseError) as exc:
            ctx.note_degradation(f"AI_UNAVAILABLE: {exc}")
            self._repair_llm(str(exc))
            return baseline

        category = str(payload.get("root_cause_category") or "").strip().lower()
        supporting = [str(x)[:400] for x in (payload.get("supporting_evidence") or [])][:8]
        status = str(payload.get("status") or "needs_more_evidence")

        if category not in CANONICAL_CATEGORIES and category != "unknown":
            self._repair_llm(f"model returned non-canonical root cause category '{category}'")
            return baseline
        if not self._citations_are_real(supporting, ctx):
            self._repair_llm("model conclusion cited evidence not present in any tool result")
            return baseline
        if status not in {"supported", "weakly_supported", "needs_more_evidence", "contradicted"}:
            status = "weakly_supported"

        conclusion = Conclusion(
            root_cause_category=category,
            root_cause_statement=str(payload.get("root_cause_statement") or "")[:1200],
            supporting_evidence=supporting,
            alternatives_ruled_out=[str(x)[:300] for x in (payload.get("alternatives_ruled_out") or [])][:6],
            status=status,
            confidence_basis=str(payload.get("confidence_basis") or "")[:600],
        )
        ctx.record_decision(
            stage="root_cause",
            decision=f"{conclusion.root_cause_category} ({conclusion.status})",
            rationale=conclusion.confidence_basis,
            status_label=conclusion.status,
        )
        return conclusion

    def recommend_remediation(
        self,
        ctx: InvestigationContext,
        conclusion: Conclusion,
        candidates: Sequence[str],
        history: dict[str, Any],
    ) -> RemediationChoice:
        baseline = self.fallback.recommend_remediation(ctx, conclusion, candidates, history)
        if not self.llm.available or self.degraded:
            return baseline

        failed_before = set(history.get("failed_actions") or [])
        try:
            payload = self.llm.structured_decision(
                system=REMEDIATION_SYSTEM,
                user=(
                    f"Concluded root cause: {conclusion.root_cause_category} "
                    f"({conclusion.status})\n{conclusion.root_cause_statement}\n\n"
                    f"Candidate actions you may choose from: {list(candidates)}\n\n"
                    f"REMEDIATION HISTORY for this root cause: failed={sorted(failed_before)}, "
                    f"successful={history.get('successful_actions')}\n\n"
                    f"REGISTRY:\n{build_evidence_brief(ctx, max_chars=8000)}\n\n"
                    "Choose exactly one action."
                ),
                function_name="submit_remediation_choice",
                schema=REMEDIATION_SCHEMA,
                description="Return the chosen remediation action.",
            )
        except (AIUnavailableError, AIMalformedResponseError) as exc:
            ctx.note_degradation(f"AI_UNAVAILABLE: {exc}")
            self._repair_llm(str(exc))
            return baseline

        action_code = str(payload.get("action_code") or "").strip()
        rationale = str(payload.get("rationale") or "")[:1500]

        if action_code not in candidates:
            self._repair_llm(
                f"model chose '{action_code}' which is not a candidate for this root cause"
            )
            return baseline
        if action_code in failed_before:
            # Allowed only if the model explicitly acknowledges the previous failure.
            acknowledgement = any(
                token in rationale.lower() for token in ("failed", "did not resolve", "previous", "previously")
            )
            if not acknowledgement:
                self._repair_llm(
                    f"model chose '{action_code}' which previously failed, without acknowledging it"
                )
                return baseline

        choice = RemediationChoice(
            action_code=action_code,
            rationale=rationale,
            memory_basis=[str(x)[:300] for x in (payload.get("memory_basis") or [])][:6],
            avoided_actions=[str(x)[:200] for x in (payload.get("avoided_actions") or [])][:6],
            evidence=[str(x)[:300] for x in (payload.get("evidence") or [])][:6],
            status="supported",
        )
        ctx.record_decision(
            stage="remediation",
            decision=f"model recommends {action_code}",
            rationale=rationale,
            status_label="supported",
        )
        return choice

    def write_postmortem(self, ctx: InvestigationContext) -> PostmortemDraft:
        baseline = self.fallback.write_postmortem(ctx)
        if not self.llm.available or self.degraded:
            return baseline

        from app.database.models import VerificationRun
        from sqlalchemy import select

        verifications = ctx.db.scalars(
            select(VerificationRun).where(VerificationRun.incident_id == ctx.incident.id)
        ).all()
        verification_text = "\n".join(
            f"  verdict={v.verdict} improvement={v.improvement_pct}% reason={v.verdict_reason[:400]}"
            for v in verifications
        ) or "  no verification has run for this incident"

        try:
            payload = self.llm.structured_decision(
                system=POSTMORTEM_SYSTEM,
                user=(
                    f"Incident #{ctx.incident.id}: {ctx.incident.title}\n"
                    f"Service {ctx.service.name} ({ctx.environment.name}), severity {ctx.incident.severity}\n"
                    f"Symptom: {ctx.incident.symptom}\n"
                    f"Root cause: {ctx.incident.root_cause_summary}\n"
                    f"Timeline: {json.dumps(ctx.fact('get_incident_timeline').get('timeline', [])[:12], default=str)}\n"
                    f"VERIFICATION RESULTS (report these exactly):\n{verification_text}\n"
                    f"REMEDIATION HISTORY:\n{json.dumps(ctx.fact('get_remediation_history'), default=str)[:1500]}\n\n"
                    "Write the postmortem."
                ),
                function_name="submit_postmortem",
                schema=POSTMORTEM_SCHEMA,
                description="Return the postmortem narrative.",
            )
        except (AIUnavailableError, AIMalformedResponseError) as exc:
            ctx.note_degradation(f"AI_UNAVAILABLE: {exc}")
            self._repair_llm(str(exc))
            return baseline

        # Guard the most dangerous failure mode: claiming a fix that verification did not confirm.
        claims_success = any(
            token in str(payload.get("root_cause", "")).lower()
            for token in ("resolved", "fixed", "recovered")
        )
        verified = any(v.verdict == "verified_success" for v in verifications)
        if claims_success and not verified:
            self._repair_llm("postmortem claimed resolution that verification did not confirm")
            return baseline

        return PostmortemDraft(
            summary=str(payload.get("summary") or baseline.summary)[:2000],
            impact=str(payload.get("impact") or baseline.impact)[:1000],
            detection=str(payload.get("detection") or baseline.detection)[:1000],
            root_cause=str(payload.get("root_cause") or baseline.root_cause)[:1500],
            lessons=[str(x)[:300] for x in (payload.get("lessons") or [])][:8],
            preventive_actions=[str(x)[:300] for x in (payload.get("preventive_actions") or [])][:8],
        )

    def summarize_investigation(
        self, ctx: InvestigationContext, conclusion: Conclusion, choice: RemediationChoice
    ) -> str:
        baseline = self.fallback.summarize_investigation(ctx, conclusion, choice)
        if not self.llm.available or self.degraded:
            return baseline
        try:
            payload = self.llm.structured_decision(
                system=SUMMARY_SYSTEM,
                user=(
                    f"Incident: {ctx.incident.title}\n"
                    f"Service: {ctx.service.name}, severity {ctx.incident.severity}\n"
                    f"Root cause: {conclusion.root_cause_category} ({conclusion.status})\n"
                    f"{conclusion.root_cause_statement}\n"
                    f"Recommended action: {choice.action_code}\n{choice.rationale}\n"
                    f"Tool calls made: {ctx.registry.call_count}"
                ),
                function_name="submit_summary",
                schema=SUMMARY_SCHEMA,
                description="Return the on-call summary.",
            )
        except (AIUnavailableError, AIMalformedResponseError):
            return baseline
        return str(payload.get("summary") or baseline)[:1200]
