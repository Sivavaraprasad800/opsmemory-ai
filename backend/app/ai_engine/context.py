"""Shared state for one investigation.

Both reasoners receive this object. It owns the tool registry and accumulates every tool
result into ``facts`` under the tool name, so:

* an agent can read a result another agent already fetched instead of paying for it twice;
* the LLM path and the deterministic path see exactly the same evidence;
* every read of the evidence set is reproducible after the fact from ``ai_tool_calls``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from app.ai_engine.reasoning.base import InvestigationPlan
from app.ai_engine.tools import ToolContext, ToolRegistry, ToolResult
from app.database.models import Environment, Incident, Service
from app.memory.base import RecallResult
from app.memory.service import MemoryService
from app.sim.engine import SimulationEngine


@dataclass
class InvestigationContext:
    db: Session
    engine: SimulationEngine
    incident: Incident
    service: Service
    environment: Environment
    memory: MemoryService
    registry: ToolRegistry
    investigation_id: int | None = None
    plan: InvestigationPlan | None = None
    facts: dict[str, Any] = field(default_factory=dict)
    decisions: list[dict[str, Any]] = field(default_factory=list)
    degraded: list[str] = field(default_factory=list)

    # -- tool access -------------------------------------------------------------------
    @property
    def tool_ctx(self) -> ToolContext:
        return ToolContext(
            db=self.db,
            engine=self.engine,
            incident=self.incident,
            service=self.service,
            environment=self.environment,
            memory=self.memory,
            investigation_id=self.investigation_id,
        )

    def call_tool(
        self, name: str, arguments: dict[str, Any] | None = None, *, decision: str = "", round_index: int = 0
    ) -> ToolResult:
        result = self.registry.execute(
            name, arguments or {}, self.tool_ctx, round_index=round_index, decision=decision
        )
        if result.ok:
            self.facts[name] = result.data
            # Keep a latest-arguments map so a caller can tell whether the cached read is stale.
            self.facts[f"{name}::args"] = arguments or {}
        return result

    def fact(self, name: str, default: Any = None) -> Any:
        return self.facts.get(name, default if default is not None else {})

    def has(self, name: str) -> bool:
        return name in self.facts

    # -- bookkeeping -------------------------------------------------------------------
    def note_degradation(self, reason: str) -> None:
        if reason not in self.degraded:
            self.degraded.append(reason)

    def record_decision(
        self,
        *,
        stage: str,
        decision: str,
        rationale: str = "",
        evidence_ids: list[int] | None = None,
        memory_ids: list[str] | None = None,
        status_label: str = "supported",
    ) -> dict[str, Any]:
        entry = {
            "stage": stage,
            "decision": decision,
            "rationale": rationale,
            "evidence_ids": evidence_ids or [],
            "memory_ids": memory_ids or [],
            "status_label": status_label,
        }
        self.decisions.append(entry)
        return entry


def store_recall_result(ctx: InvestigationContext, recall: RecallResult) -> None:
    ctx.facts["recall_historical_incidents"] = {
        "backend": recall.backend,
        "degraded": recall.degraded,
        "note": recall.note,
        "query": recall.query,
        "strategy_hits": recall.strategy_hits,
        "memory_count": len(recall.hits),
        "memories": [hit.to_dict() for hit in recall.hits],
    }
