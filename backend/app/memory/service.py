"""The platform's single entry point to organizational memory.

Responsibilities:

1. **Backend selection.** Use the real Hindsight service when it is configured *and*
   reachable; otherwise use the in-process store with identical semantics. Reachability is
   cached briefly so an unreachable host does not add a timeout to every recall.
2. **Degradation.** A memory outage must not take the platform down (spec section 48). Every
   memory call is wrapped; on failure the platform continues, reports ``MEMORY_DEGRADED``, and
   the orchestrator records that fact on the investigation.
3. **Provenance.** Postgres stores *which* memory belongs to *which* incident
   (spec section 36) via ``MemoryReference``, plus a ``LearningEvent`` trail. Hindsight holds
   the experience; Postgres holds the relationship.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.database.models import LearningEvent, MemoryReference, utcnow
from app.detection.similarity import IncidentFingerprint
from app.memory.base import (
    EXPERIENCE,
    OBSERVATION,
    WORLD,
    MemoryScope,
    MemoryUnavailable,
    RecallResult,
    ReflectionResult,
    RetentionResult,
)
from app.memory.documents import (
    AttemptRecord,
    IncidentExperience,
    build_recall_query,
    digest,
    render_attempt_memory,
    render_pattern_insight,
)
from app.memory.fallback_store import FallbackStore

logger = logging.getLogger(__name__)

BANK_MISSION = (
    "You are the long-term organizational memory of an SRE team. You hold incident "
    "experience, remediation outcomes, deployment lessons and postmortem learning. "
    "Prefer evidence that has been verified end-to-end over evidence that has not. "
    "Treat a remediation as effective only when verification confirmed recovery."
)

BANK_DIRECTIVES = (
    "Always distinguish failed remediation attempts from verified successful ones.",
    "Never assert that a change caused an incident purely from temporal proximity.",
    "Cite the incident identifier for every claim about past experience.",
)

HEALTH_TTL_SECONDS = 60.0


@dataclass
class MemoryStatus:
    active_backend: str
    configured_backend: str
    degraded: bool
    reasons: list[str] = field(default_factory=list)
    last_health: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "active_backend": self.active_backend,
            "configured_backend": self.configured_backend,
            "degraded": self.degraded,
            "reasons": self.reasons,
            "health": self.last_health,
        }


class MemoryService:
    """Facade over a Hindsight-compatible memory store."""

    def __init__(self) -> None:
        self.bank_id = settings.hindsight_bank_id
        self._lock = threading.RLock()
        self._degraded = False
        self._degradation_reasons: list[str] = []
        self._health_checked_at = 0.0
        self._last_health: dict[str, Any] = {}

        persist_path = Path(__file__).resolve().parents[2] / ".opsmemory" / f"{self.bank_id}.json"
        self._fallback = FallbackStore(bank_id=self.bank_id, persist_path=persist_path)
        self._fallback.ensure_bank(mission=BANK_MISSION, directives=BANK_DIRECTIVES)

        self._hindsight = None
        if settings.hindsight_enabled:
            try:
                from app.memory.hindsight_store import HindsightStore

                self._hindsight = HindsightStore(
                    base_url=settings.hindsight_base_url,
                    bank_id=self.bank_id,
                    api_key=settings.hindsight_api_key,
                    timeout=settings.hindsight_timeout_seconds,
                )
            except MemoryUnavailable as exc:
                self._record_degradation(f"could not initialise Hindsight client: {exc}")

    # -- backend selection -------------------------------------------------------------
    @property
    def configured_backend(self) -> str:
        return "hindsight" if self._hindsight is not None else "fallback"

    @property
    def degraded(self) -> bool:
        return self._degraded

    @property
    def degradation_reasons(self) -> list[str]:
        return list(self._degradation_reasons)

    def _record_degradation(self, reason: str) -> None:
        with self._lock:
            self._degraded = True
            if reason not in self._degradation_reasons:
                self._degradation_reasons.append(reason)
                logger.warning("memory degraded: %s", reason)

    def clear_degradation(self) -> None:
        with self._lock:
            self._degraded = False
            self._degradation_reasons.clear()

    def _active_store(self):
        """Return the store to use right now, verifying Hindsight health at most once a minute."""
        if self._hindsight is None:
            return self._fallback
        now = time.monotonic()
        if now - self._health_checked_at > HEALTH_TTL_SECONDS:
            self._health_checked_at = now
            health = self._hindsight.health()
            self._last_health = health
            if health.get("healthy"):
                # Recovery matters as much as detection. A transient blip (a cold start whose
                # startup hook raced the event loop, a network hiccup, a restarting server)
                # must not permanently pin the platform to the in-process store - Hindsight is
                # the mandated organizational memory, so the next successful probe restores it.
                if self._degraded:
                    logger.info("Hindsight is healthy again; restoring the real memory backend")
                self.clear_degradation()
                self._hindsight.ensure_bank(mission=BANK_MISSION, directives=BANK_DIRECTIVES)
            else:
                self._record_degradation(
                    f"Hindsight at {settings.hindsight_base_url} is unreachable: {health.get('detail')}"
                )
                if settings.hindsight_required:
                    raise MemoryUnavailable(
                        "HINDSIGHT_REQUIRED=true but the Hindsight server is unreachable"
                    )
                return self._fallback
        return self._fallback if self._degraded else self._hindsight

    def status(self) -> MemoryStatus:
        health = self._last_health or {"backend": self.configured_backend, "healthy": not self._degraded}
        return MemoryStatus(
            active_backend=self._active_store().name,
            configured_backend=self.configured_backend,
            degraded=self._degraded,
            reasons=self.degradation_reasons,
            last_health=health,
        )

    def health(self, *, force: bool = False) -> dict[str, Any]:
        if force:
            self._health_checked_at = 0.0
        store = self._active_store()
        try:
            health = store.health()
        except MemoryUnavailable as exc:
            self._record_degradation(str(exc))
            health = {"backend": "fallback", "healthy": True, "detail": f"degraded: {exc}"}
        self._last_health = health
        return health

    def stats(self) -> dict[str, Any]:
        """Counts for the dashboard, read from whichever backend is actually serving memory.

        A store that cannot answer reports ``counts_available: False`` and omits the counts,
        so callers can render "unknown" instead of a confident, wrong zero.
        """
        store = self._active_store()
        if isinstance(store, FallbackStore):
            return store.stats()
        try:
            return store.stats()
        except (MemoryUnavailable, AttributeError) as exc:
            logger.info("store %s cannot report stats: %s", store.name, exc)
            return {"backend": store.name, "bank_id": self.bank_id, "counts_available": False}

    # -- retain ------------------------------------------------------------------------
    def _retain(
        self,
        *,
        content: str,
        context: str,
        document_id: str,
        metadata: dict[str, Any],
        tags: Sequence[str],
        scope: str,
        db: Session | None,
        incident_id: int | None,
        retained_by: str,
        occurred_at: str | None = None,
    ) -> RetentionResult:
        store = self._active_store()
        try:
            result = store.retain(
                content=content,
                context=context,
                document_id=document_id,
                metadata=metadata,
                tags=tags,
                occurred_at=occurred_at,
            )
        except MemoryUnavailable as exc:
            self._record_degradation(str(exc))
            result = self._fallback.retain(
                content=content,
                context=context,
                document_id=document_id,
                metadata=metadata,
                tags=tags,
                occurred_at=occurred_at,
            )

        if db is not None:
            reference = MemoryReference(
                incident_id=incident_id,
                bank_id=self.bank_id,
                memory_type=EXPERIENCE,
                hindsight_memory_id=result.memory_id or document_id,
                document_id=document_id,
                scope=scope,
                content_digest=digest(content),
                content_preview=content[:600],
                backend=result.backend,
                retained_by=retained_by,
            )
            db.add(reference)
            db.add(
                LearningEvent(
                    incident_id=incident_id,
                    kind="retain",
                    summary=(
                        f"Retained {scope} experience in {result.backend} "
                        f"({result.facts_extracted or 'n/a'} facts)"
                    ),
                    payload={
                        "scope": scope,
                        "document_id": document_id,
                        "backend": result.backend,
                        "facts_extracted": result.facts_extracted,
                        "metadata": metadata,
                    },
                    memory_ids=[result.memory_id or document_id],
                )
            )
            db.flush()
        return result

    def retain_incident_experience(
        self,
        *,
        experience: IncidentExperience,
        fingerprint: IncidentFingerprint | None = None,
        db: Session | None = None,
        context: str = "completed incident learning",
    ) -> RetentionResult:
        document_id = f"incident-{experience.incident_id}-experience"
        content = experience.render()
        metadata = experience.metadata(scope=MemoryScope.INCIDENT_EXPERIENCE, fingerprint=fingerprint)
        metadata["title"] = experience.title
        tags = [
            f"service:{experience.service}",
            f"env:{experience.environment}",
            f"cause:{experience.root_cause_category}",
            f"severity:{experience.severity}",
        ]
        return self._retain(
            content=content,
            context=context,
            document_id=document_id,
            metadata=metadata,
            tags=tags,
            scope=MemoryScope.INCIDENT_EXPERIENCE,
            db=db,
            incident_id=experience.incident_id,
            retained_by="learning_agent",
            occurred_at=experience.detected_at,
        )

    def retain_attempt_outcome(
        self,
        *,
        incident_id: int,
        service: str,
        environment: str,
        root_cause_category: str,
        attempt: AttemptRecord,
        db: Session | None = None,
    ) -> RetentionResult:
        document_id = f"incident-{incident_id}-attempt-{attempt.attempt_number}"
        content = render_attempt_memory(
            incident_id=incident_id,
            service=service,
            environment=environment,
            root_cause_category=root_cause_category,
            attempt=attempt,
        )
        scope = MemoryScope.REMEDIATION_OUTCOME
        return self._retain(
            content=content,
            context=f"remediation attempt outcome ({attempt.outcome})",
            document_id=document_id,
            metadata={
                "scope": scope,
                "incident_id": incident_id,
                "service": service,
                "environment": environment,
                "root_cause_category": root_cause_category,
                "action_code": attempt.action_code,
                "outcome": attempt.outcome,
                "attempt_number": attempt.attempt_number,
            },
            tags=[
                f"service:{service}",
                f"cause:{root_cause_category}",
                f"action:{attempt.action_code}",
                f"outcome:{attempt.outcome}",
            ],
            scope=scope,
            db=db,
            incident_id=incident_id,
            retained_by="learning_agent",
        )

    def retain_pattern_insight(
        self,
        *,
        root_cause_category: str,
        occurrence_count: int,
        window_days: int,
        services: Sequence[str],
        failed_actions: Sequence[str],
        successful_actions: Sequence[str],
        statement: str,
        db: Session | None = None,
        incident_id: int | None = None,
    ) -> RetentionResult:
        content = render_pattern_insight(
            root_cause_category=root_cause_category,
            occurrence_count=occurrence_count,
            window_days=window_days,
            services=services,
            failed_actions=failed_actions,
            successful_actions=successful_actions,
            statement=statement,
        )
        scope = MemoryScope.PATTERN_INSIGHT
        return self._retain(
            content=content,
            context="organizational pattern analysis",
            document_id=f"pattern-{root_cause_category}-{duration_tag(window_days)}",
            metadata={
                "scope": scope,
                "root_cause_category": root_cause_category,
                "occurrence_count": occurrence_count,
                "window_days": window_days,
            },
            tags=[f"cause:{root_cause_category}", "kind:pattern"],
            scope=scope,
            db=db,
            incident_id=incident_id,
            retained_by="pattern_detector",
        )

    # -- recall ------------------------------------------------------------------------
    def recall_for_incident(
        self,
        *,
        fingerprint: IncidentFingerprint,
        db: Session | None = None,
        incident_id: int | None = None,
        extra_query: Sequence[str] = (),
        types: Sequence[str] = (WORLD, EXPERIENCE, OBSERVATION),
        log_event: bool = True,
    ) -> RecallResult:
        query = build_recall_query(fingerprint=fingerprint, extra=extra_query)
        store = self._active_store()
        try:
            result = store.recall(
                query=query,
                types=types,
                budget=settings.hindsight_recall_budget,
                max_tokens=settings.hindsight_recall_max_tokens,
                top_k=8,
            )
        except MemoryUnavailable as exc:
            self._record_degradation(str(exc))
            result = self._fallback.recall(
                query=query,
                types=types,
                budget=settings.hindsight_recall_budget,
                max_tokens=settings.hindsight_recall_max_tokens,
                top_k=8,
            )
            result.degraded = True
            result.note = f"degraded to in-process memory: {exc}"

        if db is not None:
            # Provenance: mark which referenced memories were actually recalled.
            memory_ids = [hit.id for hit in result.hits]
            if memory_ids:
                refs = db.scalars(
                    select(MemoryReference).where(
                        MemoryReference.hindsight_memory_id.in_(memory_ids)
                    )
                ).all()
                for ref in refs:
                    ref.recall_count += 1
            if log_event:
                db.add(
                    LearningEvent(
                        incident_id=incident_id,
                        kind="recall",
                        summary=(
                            f"Recalled {len(result.hits)} organisational memory item(s) "
                            f"from {result.backend}"
                        ),
                        payload={
                            "query": query,
                            "backend": result.backend,
                            "degraded": result.degraded,
                            "strategy_hits": result.strategy_hits,
                            "hit_ids": memory_ids[:20],
                        },
                        memory_ids=memory_ids[:20],
                    )
                )
                db.flush()
        return result

    def recall_query_for_display(self, fingerprint: IncidentFingerprint) -> str:
        return build_recall_query(fingerprint=fingerprint)

    # -- reflect -----------------------------------------------------------------------
    def reflect(
        self,
        *,
        query: str,
        context: str = "",
        db: Session | None = None,
        incident_id: int | None = None,
    ) -> ReflectionResult:
        store = self._active_store()
        try:
            result = store.reflect(query=query, context=context, budget="mid")
        except MemoryUnavailable as exc:
            self._record_degradation(str(exc))
            result = self._fallback.reflect(query=query, context=context, budget="mid")
            result.degraded = True
            result.note = f"degraded to in-process memory: {exc}"

        if db is not None:
            db.add(
                LearningEvent(
                    incident_id=incident_id,
                    kind="reflect",
                    summary=f"Reflected over organizational memory ({result.backend})",
                    payload={
                        "query": query,
                        "context": context,
                        "backend": result.backend,
                        "degraded": result.degraded,
                        "citation_count": len(result.citations),
                    },
                    memory_ids=result.citations[:20],
                )
            )
            db.flush()
        return result

    # -- introspection / demo control --------------------------------------------------
    def list_memories(
        self, *, memory_type: str | None = None, search: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        store = self._active_store()
        try:
            hits = store.list_memories(memory_type=memory_type, search=search, limit=limit)
        except MemoryUnavailable as exc:
            self._record_degradation(str(exc))
            hits = self._fallback.list_memories(memory_type=memory_type, search=search, limit=limit)
        return [hit.to_dict() for hit in hits]

    def reset(self) -> None:
        """Wipe organizational memory. Used by the clean-room demo reset (spec section 52)."""
        with self._lock:
            self._fallback.reset()
            self._hindsight = None
            if settings.hindsight_enabled:
                from app.memory.hindsight_store import HindsightStore

                try:
                    self._hindsight = HindsightStore(
                        base_url=settings.hindsight_base_url,
                        bank_id=self.bank_id,
                        api_key=settings.hindsight_api_key,
                        timeout=settings.hindsight_timeout_seconds,
                    )
                except MemoryUnavailable as exc:
                    self._record_degradation(str(exc))
            self.clear_degradation()

    def started_at_iso(self) -> str:
        return utcnow().isoformat()


def duration_tag(window_days: int) -> str:
    return f"{window_days}d"


_service: MemoryService | None = None
_service_lock = threading.Lock()


def get_memory_service() -> MemoryService:
    global _service
    if _service is None:
        with _service_lock:
            if _service is None:
                _service = MemoryService()
    return _service


def reset_memory_service() -> None:
    """Test helper: rebuild the singleton so settings changes take effect."""
    global _service
    with _service_lock:
        _service = None
