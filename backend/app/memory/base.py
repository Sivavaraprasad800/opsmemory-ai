"""Memory value objects and the narrow ``MemoryStore`` interface.

Hindsight is the organization's long-term experience layer, so the rest of the platform is
written against this interface rather than against the Hindsight SDK directly. Two
implementations exist:

* ``HindsightStore``   — the real service (`retain` / `recall` / `reflect`).
* ``FallbackStore``    — a faithful in-process implementation of the same semantics, used
  when no Hindsight server is configured or reachable.

The interface is intentionally *small*. Anything the platform needs beyond
retain/recall/reflect is a platform concern and belongs in PostgreSQL, not in the memory
layer.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any, Protocol, Sequence, runtime_checkable

# Hindsight's memory taxonomy. Kept as literals so a request can pass them straight through.
WORLD = "world"
EXPERIENCE = "experience"
OBSERVATION = "observation"
MEMORY_TYPES = (WORLD, EXPERIENCE, OBSERVATION)


class MemoryScope(StrEnum):
    """What kind of organizational experience a memory holds."""

    INCIDENT_EXPERIENCE = "incident_experience"
    REMEDIATION_OUTCOME = "remediation_outcome"
    DEPLOYMENT_EXPERIENCE = "deployment_experience"
    RUNBOOK_EXPERIENCE = "runbook_experience"
    VERIFICATION_EXPERIENCE = "verification_experience"
    POSTMORTEM_LEARNING = "postmortem_learning"
    SERVICE_BEHAVIOUR = "service_behaviour"
    PATTERN_INSIGHT = "pattern_insight"


@dataclass
class RecallHit:
    id: str
    text: str
    type: str = WORLD
    score: float = 0.0
    context: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    entities: list[str] = field(default_factory=list)
    occurred_start: str | None = None
    occurred_end: str | None = None
    mentioned_at: str | None = None
    document_id: str | None = None
    proof_count: int | None = None
    source_fact_ids: list[str] = field(default_factory=list)
    retrieval: list[str] = field(default_factory=list)
    """Which of the four retrieval arms surfaced this hit — the explainability hook."""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RecallResult:
    query: str
    hits: list[RecallHit] = field(default_factory=list)
    backend: str = "fallback"
    degraded: bool = False
    note: str = ""
    strategy_hits: dict[str, int] = field(default_factory=dict)

    @property
    def empty(self) -> bool:
        return not self.hits

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "backend": self.backend,
            "degraded": self.degraded,
            "note": self.note,
            "strategy_hits": self.strategy_hits,
            "hits": [h.to_dict() for h in self.hits],
        }


@dataclass
class RetentionResult:
    memory_id: str
    document_id: str
    backend: str
    accepted: bool = True
    facts_extracted: int = 0
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ReflectionResult:
    text: str
    backend: str = "fallback"
    degraded: bool = False
    citations: list[str] = field(default_factory=list)
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@runtime_checkable
class MemoryStore(Protocol):
    """The three verbs plus health and bank setup. Nothing else is part of memory."""

    name: str
    bank_id: str

    def ensure_bank(self, *, mission: str, directives: Sequence[str] = ()) -> None: ...

    def retain(
        self,
        *,
        content: str,
        context: str = "",
        document_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        tags: Sequence[str] = (),
        occurred_at: str | None = None,
    ) -> RetentionResult: ...

    def recall(
        self,
        *,
        query: str,
        types: Sequence[str] = (WORLD, EXPERIENCE, OBSERVATION),
        budget: str = "mid",
        max_tokens: int = 2048,
        tags: Sequence[str] = (),
        top_k: int = 8,
    ) -> RecallResult: ...

    def reflect(
        self,
        *,
        query: str,
        context: str = "",
        budget: str = "mid",
    ) -> ReflectionResult: ...

    def list_memories(
        self, *, memory_type: str | None = None, search: str | None = None, limit: int = 100
    ) -> list[RecallHit]: ...

    def health(self) -> dict[str, Any]: ...


class MemoryUnavailable(RuntimeError):
    """Raised by a store when the backing service cannot be reached."""
