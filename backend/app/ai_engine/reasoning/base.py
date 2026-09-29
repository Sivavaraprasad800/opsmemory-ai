"""Reasoner interface shared by the LLM path and the deterministic offline path.

The orchestrator depends on this interface, never on the OpenAI SDK. That is what makes the
platform demonstrable with zero API keys while keeping the *tool ledger* — the artefact the
UI actually shows — equally real in both modes: both reasoners call the same tool registry and
both decisions are persisted to ``ai_decisions``.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Protocol, Sequence


@dataclass
class HypothesisDraft:
    statement: str
    category: str
    supporting: list[str] = field(default_factory=list)
    contradicting: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    test_plan: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Conclusion:
    root_cause_category: str
    root_cause_statement: str
    supporting_evidence: list[str] = field(default_factory=list)
    alternatives_ruled_out: list[str] = field(default_factory=list)
    status: str = "supported"
    confidence_basis: str = ""
    """Why we believe this. Never a bare percentage: build spec section 16 forbids arbitrary
    confidence numbers, so we record the *evidence basis* instead."""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RemediationChoice:
    action_code: str
    rationale: str
    memory_basis: list[str] = field(default_factory=list)
    avoided_actions: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    status: str = "supported"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PostmortemDraft:
    summary: str
    impact: str
    detection: str
    root_cause: str
    lessons: list[str] = field(default_factory=list)
    preventive_actions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class InvestigationPlan:
    """What the reasoner should look at for this incident."""

    incident_id: int
    service: str
    environment: str
    severity: str
    symptom: str
    primary_metric: str
    baseline: dict[str, Any] = field(default_factory=dict)
    detection: dict[str, Any] = field(default_factory=dict)
    dependency_names: Sequence[str] = ()


class Reasoner(Protocol):
    """Decisions the model is allowed to make, and nothing else."""

    mode: str
    degraded: bool

    def propose_hypotheses(self, ctx) -> list[HypothesisDraft]: ...

    def conclude(self, ctx, hypotheses: Sequence[HypothesisDraft]) -> Conclusion: ...

    def recommend_remediation(
        self, ctx, conclusion: Conclusion, candidates: Sequence[str], history: dict[str, Any]
    ) -> RemediationChoice: ...

    def write_postmortem(self, ctx) -> PostmortemDraft: ...

    def summarize_investigation(self, ctx, conclusion: Conclusion, choice: RemediationChoice) -> str: ...
