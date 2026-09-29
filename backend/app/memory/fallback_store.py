"""A faithful in-process implementation of Hindsight's memory semantics.

This is **not** a stub that returns canned strings. It implements the same model the real
service documents, because the platform's behaviour must be identical whichever backend is
wired in:

===========================  ==================================================================
Hindsight behaviour          Implementation here
===========================  ==================================================================
Retain → LLM extraction      Line/sentence decomposition into atomic facts, classified into
                             ``world`` (objective facts received) vs ``experience`` (the
                             agent's own actions and their outcomes).
Entity linking               Entity vocabulary built from capitalised tokens, service names,
                             action codes and taxonomy terms; each fact is linked to entities.
TEMPR 4-arm retrieval        semantic (TF-IDF cosine) + keyword (BM25-lite) + graph (entity
                             co-occurrence expansion) + temporal (recency window), run in
                             parallel and fused with RRF ``Σ 1/(60+rank)``.
Recency / proof boosts       Multiplicative boosts, mirroring the documented ranking stages.
Observation consolidation    Related facts are consolidated into deduplicated, evidence-grounded
                             observations carrying a proof count and source fact ids, and are
                             *refined rather than overwritten* when new evidence arrives.
Reflect                      A deterministic retrieval-first synthesis with citations, instead
                             of an LLM loop.
===========================  ==================================================================

Why this exists (docs/RESEARCH.md section 1): a hackathon demo must not be able to die
because the venue network blocked a memory server, and the retain/recall/reflect test block
of the build spec must actually be executable in CI with no keys. When a real Hindsight
server is configured and healthy, this implementation is bypassed entirely.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import threading
import uuid
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from app.memory.base import (
    EXPERIENCE,
    OBSERVATION,
    WORLD,
    MemoryScope,
    RecallHit,
    RecallResult,
    ReflectionResult,
    RetentionResult,
)

def _mentions_failure(text: str) -> bool:
    lowered = (text or "").lower()
    return "failed" in lowered or "did not resolve" in lowered or "verification failed" in lowered


def _mentions_success(text: str) -> bool:
    lowered = (text or "").lower()
    return (
        "verified successful" in lowered
        or "verified success" in lowered
        or "succeeded" in lowered
        or "successful remediation" in lowered
    )


RRF_K = 60
"""Reciprocal Rank Fusion constant, as documented by Hindsight: Σ 1/(60 + rank)."""

_STOPWORDS = {
    "the", "and", "for", "with", "was", "were", "has", "have", "had", "that", "this", "from",
    "into", "after", "before", "then", "than", "but", "not", "did", "does", "are", "its",
    "our", "their", "his", "her", "them", "they", "you", "your", "which", "when", "where",
    "while", "been", "being", "will", "would", "could", "should", "may", "might", "must",
}

_ACTION_MARKERS = (
    "i ", "we ", "recommended", "applied", "executed", "restarted", "rolled back", "rolled",
    "attempt", "attempted", "tried", "verified", "retained", "proposed", "approved",
)
_OUTCOME_MARKERS = ("failed", "succeeded", "verified", "verification failed", "passed", "resolved")
_WORLD_MARKERS = (
    "deployed", "deployment", "spiked", "increased", "decreased", "occurred", "detected",
    "metric", "error rate", "latency", "connections", "pool", "cpu", "memory", "disk",
    "commit", "config", "configuration", "root cause", "symptom", "service",
)

_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9_.:/-]{1,}")
_ENTITY_RE = re.compile(r"\b[A-Z][A-Za-z0-9_]*(?:[-_][A-Za-z0-9]+)*\b")
_CAMEL_OR_CODE_RE = re.compile(r"\b[a-z]+_[a-z_]{2,}\b")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def tokenize(text: str) -> list[str]:
    return [
        t for t in _TOKEN_RE.findall((text or "").lower()) if len(t) > 2 and t not in _STOPWORDS
    ]


# ---------------------------------------------------------------------------------------
# Storage records
# ---------------------------------------------------------------------------------------
@dataclass
class FactRecord:
    id: str
    text: str
    type: str
    scope: str
    entities: list[str]
    tokens: list[str]
    document_id: str
    context: str
    tags: list[str]
    metadata: dict[str, Any]
    mentioned_at: str
    occurred_start: str | None = None
    occurred_end: str | None = None
    proof_count: int = 1
    source_fact_ids: list[str] = field(default_factory=list)
    corrects: str | None = None
    """Set when a newer fact supersedes an older one — history is preserved, not deleted."""

    def to_hit(self, *, score: float = 0.0, retrieval: list[str] | None = None) -> RecallHit:
        return RecallHit(
            id=self.id,
            text=self.text,
            type=self.type,
            score=round(score, 4),
            context=self.context,
            metadata=self.metadata,
            tags=self.tags,
            entities=self.entities,
            occurred_start=self.occurred_start,
            occurred_end=self.occurred_end,
            mentioned_at=self.mentioned_at,
            document_id=self.document_id,
            proof_count=self.proof_count,
            source_fact_ids=self.source_fact_ids,
            retrieval=retrieval or [],
        )


@dataclass
class ObservationRecord:
    id: str
    text: str
    scope: str
    entities: list[str]
    tokens: list[str]
    proof_count: int
    source_fact_ids: list[str]
    created_at: str
    updated_at: str
    superseded_texts: list[str] = field(default_factory=list)

    def to_hit(self, *, score: float = 0.0, retrieval: list[str] | None = None) -> RecallHit:
        return RecallHit(
            id=self.id,
            text=self.text,
            type=OBSERVATION,
            score=round(score, 4),
            context=f"consolidated observation · scope={self.scope}",
            metadata={"scope": self.scope, "superseded": self.superseded_texts},
            entities=self.entities,
            mentioned_at=self.updated_at,
            proof_count=self.proof_count,
            source_fact_ids=self.source_fact_ids,
            retrieval=retrieval or [],
        )


# ---------------------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------------------
class FallbackStore:
    """Local memory store with Hindsight-compatible semantics.

    State lives in memory and is mirrored to a JSON file so learning genuinely *persists*
    across process restarts — a demo where memory evaporates on restart would not prove
    anything.
    """

    name = "fallback"

    def __init__(self, *, bank_id: str, persist_path: Path | None = None, mission: str = "") -> None:
        self.bank_id = bank_id
        self.mission = mission
        self.directives: list[str] = []
        self._lock = threading.RLock()
        self.facts: dict[str, FactRecord] = {}
        self.observations: dict[str, ObservationRecord] = {}
        self.document_entities: dict[str, set[str]] = {}
        self.entity_index: dict[str, set[str]] = {}
        self.document_count = 0
        self._persist_path = persist_path
        self._idf: dict[str, float] | None = None
        self._avg_len = 1.0
        if persist_path and persist_path.exists():
            self._load()

    # -- identity ----------------------------------------------------------------------
    @property
    def persist_path(self) -> Path | None:
        return self._persist_path

    def ensure_bank(self, *, mission: str, directives: Sequence[str] = ()) -> None:
        self.mission = mission or self.mission
        if directives:
            self.directives = list(directives)

    # -- retain ------------------------------------------------------------------------
    def retain(
        self,
        *,
        content: str,
        context: str = "",
        document_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        tags: Sequence[str] = (),
        occurred_at: str | None = None,
    ) -> RetentionResult:
        with self._lock:
            document_id = document_id or f"doc-{uuid.uuid4().hex[:12]}"
            metadata = dict(metadata or {})
            statements = self._extract_facts(content)
            extracted: list[FactRecord] = []
            doc_entities: set[str] = set()

            for statement in statements:
                fact_type = self._classify(statement)
                entities = self._entities(statement)
                doc_entities.update(entities)
                record = FactRecord(
                    id=f"fact-{uuid.uuid4().hex[:16]}",
                    text=statement,
                    type=fact_type,
                    scope=str(metadata.get("scope", MemoryScope.INCIDENT_EXPERIENCE)),
                    entities=sorted(entities),
                    tokens=tokenize(statement),
                    document_id=document_id,
                    context=context,
                    tags=list(tags),
                    metadata=metadata,
                    mentioned_at=_now_iso(),
                    occurred_start=occurred_at,
                    occurred_end=occurred_at,
                    proof_count=1,
                )
                # Supersede an earlier, near-identical fact of the same type in the same scope.
                previous = self._find_superseded(record)
                if previous is not None:
                    record.proof_count = previous.proof_count + 1
                    record.corrects = previous.id
                    previous.superseded_by = record.id  # type: ignore[attr-defined]
                self.facts[record.id] = record
                extracted.append(record)
                for entity in record.entities:
                    self.entity_index.setdefault(entity.lower(), set()).add(record.id)

            self.document_entities[document_id] = doc_entities
            self.document_count += 1
            self._idf = None
            self._consolidate(metadata, doc_entities)
            self._save()

            return RetentionResult(
                memory_id=document_id,
                document_id=document_id,
                backend=self.name,
                accepted=True,
                facts_extracted=len(extracted),
                detail=(
                    f"extracted {len(extracted)} facts, linked {len(doc_entities)} entities, "
                    f"{len(self.observations)} observations consolidated"
                ),
            )

    def _extract_facts(self, content: str) -> list[str]:
        """Hindsight runs an LLM extraction; we do a deterministic decomposition that keeps
        the same property that matters downstream: atomic, self-contained statements."""
        statements: list[str] = []
        for raw_line in (content or "").splitlines():
            line = raw_line.strip().lstrip("-•*").strip()
            if not line:
                continue
            # Split long lines into clause-level statements so each fact is atomic.
            parts = re.split(r"(?<=[.;])\s+(?=[A-Z(])", line)
            for part in parts:
                part = part.strip()
                if len(part) < 12:
                    continue
                statements.append(part)
        return statements[:120]

    def _classify(self, statement: str) -> str:
        lowered = statement.lower()
        if any(marker in lowered for marker in _ACTION_MARKERS):
            return EXPERIENCE
        if any(marker in lowered for marker in _OUTCOME_MARKERS) and " i " in f" {lowered} ":
            return EXPERIENCE
        if any(marker in lowered for marker in _WORLD_MARKERS):
            return WORLD
        return WORLD if re.search(r"\b(is|are|has|have|was|were)\b", lowered) else EXPERIENCE

    def _entities(self, statement: str) -> set[str]:
        entities: set[str] = set()
        for match in _ENTITY_RE.findall(statement):
            if len(match) > 2:
                entities.add(match)
        for match in _CAMEL_OR_CODE_RE.findall(statement.lower()):
            entities.add(match)
        for token in tokenize(statement):
            if token in {"connection_leak", "restart_service", "rollback_deployment"}:
                entities.add(token)
        return entities

    def _find_superseded(self, record: FactRecord) -> FactRecord | None:
        record_tokens = set(record.tokens)
        if not record_tokens:
            return None
        for existing in self.facts.values():
            if existing.type != record.type or existing.scope != record.scope:
                continue
            if getattr(existing, "superseded_by", None):
                continue
            existing_tokens = set(existing.tokens)
            if not existing_tokens:
                continue
            overlap = len(record_tokens & existing_tokens) / len(record_tokens | existing_tokens)
            if overlap >= 0.85:
                return existing
        return None

    # -- observation consolidation -----------------------------------------------------
    def _consolidate(self, metadata: dict[str, Any], doc_entities: set[str]) -> None:
        """Consolidate facts into deduplicated, evidence-grounded observations.

        Grouping key is ``(scope, root-cause category or service)`` — the dimension along
        which organizational experience actually generalises. An observation is *refined*
        rather than replaced: the previous statement is retained in ``superseded_texts``.
        """
        scope = str(metadata.get("scope", MemoryScope.INCIDENT_EXPERIENCE))
        focus = (
            metadata.get("root_cause_category")
            or metadata.get("service")
            or metadata.get("action_code")
            or "_global"
        )
        key = f"{scope}::{focus}"
        relevant = [
            fact
            for fact in self.facts.values()
            if fact.scope == scope
            and focus in {f.lower() for f in fact.entities}
            | {str(fact.metadata.get("root_cause_category", "")).lower()}
            | {str(fact.metadata.get("service", "")).lower()}
            | {str(fact.metadata.get("action_code", "")).lower()}
        ]
        if not relevant:
            return

        outcomes = Counter()
        for fact in relevant:
            lowered = fact.text.lower()
            if "verification failed" in lowered or "failed" in lowered:
                outcomes["failed"] += 1
            if "verified" in lowered and "failed" not in lowered:
                outcomes["succeeded"] += 1

        observation_id = f"obs-{hashlib.sha1(key.encode()).hexdigest()[:16]}"
        existing = self.observations.get(observation_id)

        notable = self._pick_notable(relevant)
        text_parts = [
            f"Organization experience for {focus} ({scope}): "
            f"{len(notable)} fact(s) retained from {self.document_count} document(s)"
        ]
        if outcomes:
            text_parts.append(
                "outcomes — " + ", ".join(f"{k}: {v}" for k, v in sorted(outcomes.items()))
            )
        text_parts.extend(notable)
        new_text = " · ".join(text_parts)

        if existing is None:
            self.observations[observation_id] = ObservationRecord(
                id=observation_id,
                text=new_text,
                scope=scope,
                entities=sorted({f for fact in relevant for f in fact.entities}),
                tokens=tokenize(new_text),
                proof_count=len(relevant),
                source_fact_ids=[f.id for f in relevant][:40],
                created_at=_now_iso(),
                updated_at=_now_iso(),
            )
        else:
            superseded = list(existing.superseded_texts)
            if existing.text != new_text:
                superseded.append(existing.text)
            existing.text = new_text
            existing.tokens = tokenize(new_text)
            existing.proof_count = max(existing.proof_count, len(relevant))
            existing.source_fact_ids = [f.id for f in relevant][:40]
            existing.entities = sorted({f for fact in relevant for f in fact.entities})
            existing.superseded_texts = superseded[-5:]
            existing.updated_at = _now_iso()

    @staticmethod
    def _pick_notable(facts: Sequence[FactRecord], limit: int = 4) -> list[str]:
        """Rank retained facts by informational value for the observation text."""
        def score(fact: FactRecord) -> tuple[int, int]:
            lowered = fact.text.lower()
            priority = 0
            if "verification failed" in lowered:
                priority = 4
            elif "successful remediation" in lowered or "verified" in lowered:
                priority = 3
            elif "root cause" in lowered:
                priority = 2
            elif "lesson" in lowered or "preventive" in lowered:
                priority = 1
            return priority, len(fact.text)

        return [f.text for f in sorted(facts, key=score, reverse=True)[:limit]]

    # -- retrieval ---------------------------------------------------------------------
    def _build_idf(self) -> None:
        docs = list(self.facts.values())
        n = max(len(docs), 1)
        df: Counter[str] = Counter()
        for fact in docs:
            for token in set(fact.tokens):
                df[token] += 1
        self._idf = {token: math.log((n - count + 0.5) / (count + 0.5) + 1.0) for token, count in df.items()}
        total = sum(len(f.tokens) for f in docs) or 1
        self._avg_len = total / n

    def _bm25(self, query_tokens: Sequence[str], doc_tokens: Sequence[str], *, k1: float = 1.5, b: float = 0.75) -> float:
        if self._idf is None:
            self._build_idf()
        assert self._idf is not None
        if not doc_tokens or not query_tokens:
            return 0.0
        counts = Counter(doc_tokens)
        length = len(doc_tokens)
        score = 0.0
        for token in set(query_tokens):
            if token not in counts:
                continue
            idf = self._idf.get(token, math.log(1.0 + 1.0) )
            tf = counts[token]
            denom = tf + k1 * (1 - b + b * length / max(self._avg_len, 1e-9))
            score += idf * (tf * (k1 + 1)) / max(denom, 1e-9)
        return score

    @staticmethod
    def _cosine(a: Counter[str] | dict[str, float], b: Counter[str] | dict[str, float]) -> float:
        if not a or not b:
            return 0.0
        shared = set(a) & set(b)
        if not shared:
            return 0.0
        num = sum(a[k] * b[k] for k in shared)
        den = math.sqrt(sum(v * v for v in a.values())) * math.sqrt(sum(v * v for v in b.values()))
        return float(num / den) if den else 0.0

    def _semantic_scores(self, query_tokens: Sequence[str]) -> dict[str, float]:
        query_vec = Counter(query_tokens)
        return {f.id: self._cosine(query_vec, Counter(f.tokens)) for f in self.facts.values()}

    def _keyword_scores(self, query_tokens: Sequence[str]) -> dict[str, float]:
        return {f.id: self._bm25(query_tokens, f.tokens) for f in self.facts.values()}

    def _graph_scores(self, query_tokens: Sequence[str]) -> dict[str, float]:
        """Entity-graph arm: facts connected to the query through shared entities.

        This is what lets recall answer "what failed for payment-service?" even when the query
        shares no vocabulary with the fact text, because ``payment-service`` links them.
        """
        seed_entities: set[str] = set()
        for token in query_tokens:
            if token in self.entity_index:
                seed_entities.add(token)
        for entity, fact_ids in self.entity_index.items():
            if any(entity in token or token in entity for token in query_tokens):
                seed_entities.add(entity)
        if not seed_entities:
            return {}

        scores: dict[str, float] = {}
        for entity in seed_entities:
            for fact_id in self.entity_index.get(entity, set()):
                fact = self.facts.get(fact_id)
                if not fact:
                    continue
                # Weight by entity overlap strength.
                overlap = len(set(fact.entities) & seed_entities) / max(len(set(fact.entities)), 1)
                scores[fact_id] = max(scores.get(fact_id, 0.0), overlap)
        return scores

    @staticmethod
    def _temporal_scores(facts: Iterable[FactRecord], query_tokens: Sequence[str]) -> dict[str, float]:
        """Temporal arm: recency weighting, amplified when the query mentions time words."""
        now = datetime.now(timezone.utc)
        time_bias = 1.0
        if any(w in query_tokens for w in ("recent", "recently", "latest", "last", "today", "yesterday", "week")):
            time_bias = 2.0
        scores: dict[str, float] = {}
        for fact in facts:
            try:
                mentioned = datetime.fromisoformat(fact.mentioned_at)
            except (ValueError, TypeError):
                mentioned = now
            age_hours = max((now - mentioned).total_seconds() / 3600.0, 0.0)
            scores[fact.id] = time_bias / (1.0 + age_hours / 24.0)
        return scores

    @staticmethod
    def _rrf(rankings: dict[str, list[str]], weights: dict[str, float] | None = None) -> dict[str, float]:
        weights = weights or {}
        fused: dict[str, float] = {}
        for arm, ranked_ids in rankings.items():
            weight = weights.get(arm, 1.0)
            for rank, doc_id in enumerate(ranked_ids, start=1):
                fused[doc_id] = fused.get(doc_id, 0.0) + weight / (RRF_K + rank)
        return fused

    def recall(
        self,
        *,
        query: str,
        types: Sequence[str] = (WORLD, EXPERIENCE, OBSERVATION),
        budget: str = "mid",
        max_tokens: int = 2048,
        tags: Sequence[str] = (),
        top_k: int = 8,
    ) -> RecallResult:
        with self._lock:
            query_tokens = tokenize(query)
            arm_weights = {"semantic": 1.0, "keyword": 1.1, "graph": 1.2, "temporal": 0.6}
            if budget == "low":
                arm_weights = {"keyword": 1.2, "graph": 1.0, "semantic": 0.8, "temporal": 0.4}
            elif budget == "high":
                arm_weights = {"semantic": 1.2, "keyword": 1.1, "graph": 1.4, "temporal": 1.0}

            allowed = set(types) if types else {WORLD, EXPERIENCE, OBSERVATION}

            semantic = self._semantic_scores(query_tokens)
            keyword = self._keyword_scores(query_tokens)
            graph = self._graph_scores(query_tokens)
            temporal = self._temporal_scores(list(self.facts.values()), query_tokens)

            def top_ids(scores: dict[str, float], keep: int) -> list[str]:
                ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
                return [doc_id for doc_id, value in ranked[:keep] if value > 0]

            depth = {"low": 40, "mid": 80, "high": 200}.get(budget, 80)
            rankings = {
                "semantic": top_ids(semantic, depth),
                "keyword": top_ids(keyword, depth),
                "graph": top_ids(graph, depth),
                "temporal": top_ids(temporal, depth),
            }
            fused = self._rrf(rankings, arm_weights)

            # Observations are ranked in the same fused space.
            obs_scores: dict[str, float] = {}
            for obs in self.observations.values():
                text_score = self._cosine(Counter(query_tokens), Counter(obs.tokens))
                kw = self._bm25(query_tokens, obs.tokens)
                entity_overlap = len({e.lower() for e in obs.entities} & set(query_tokens))
                combined = text_score + 0.35 * kw + 0.25 * entity_overlap
                if combined > 0:
                    obs_scores[obs.id] = combined * (1.0 + min(obs.proof_count, 10) / 20.0)
            if obs_scores:
                fused.update(self._rrf({"observation": top_ids(obs_scores, depth)}, arm_weights))

            hits: list[RecallHit] = []
            for doc_id, base in sorted(fused.items(), key=lambda kv: kv[1], reverse=True):
                if doc_id in self.observations:
                    observation = self.observations[doc_id]
                    if OBSERVATION not in allowed:
                        continue
                    hit = observation.to_hit(score=base * 100.0, retrieval=["observation"])
                    hits.append(hit)
                    continue

                fact = self.facts.get(doc_id)
                if not fact or fact.type not in allowed:
                    continue
                if tags and not set(tags) & set(fact.tags):
                    continue
                arms = [arm for arm, ids in rankings.items() if doc_id in ids]
                boost = self._boost(fact, arms)
                hit = fact.to_hit(score=base * 100.0 * boost, retrieval=arms)
                if fact.metadata:
                    hit.metadata = {**fact.metadata, **hit.metadata}
                hits.append(hit)

            # Proof-count boost already applied to observations; cap the result set.
            hits.sort(key=lambda h: h.score, reverse=True)
            selected = self._apply_token_budget(hits, max_tokens, top_k)

            # prefer_observations behaviour: drop the raw facts an observation was built from.
            obs_sources = {
                fid
                for hit in selected
                if hit.type == OBSERVATION
                for fid in hit.source_fact_ids
            }
            if any(h.type == OBSERVATION for h in selected):
                selected = [h for h in selected if h.type == OBSERVATION or h.id not in obs_sources]

            strategy_hits = {arm: len(ids) for arm, ids in rankings.items()}
            return RecallResult(
                query=query,
                hits=selected,
                backend=self.name,
                note=(
                    "in-process fallback memory: TEMPR-equivalent multi-arm retrieval with RRF "
                    "fusion. Set HINDSIGHT_BASE_URL to use the real service."
                ),
                strategy_hits=strategy_hits,
            )

    @staticmethod
    def _boost(fact: FactRecord, arms: Sequence[str]) -> float:
        """Recency + multi-arm agreement + proof boosts, mirroring the documented ranking."""
        boost = 1.0
        try:
            mentioned = datetime.fromisoformat(fact.mentioned_at)
            age_days = max((datetime.now(timezone.utc) - mentioned).total_seconds() / 86400.0, 0.0)
            boost *= 1.0 + 0.10 / (1.0 + age_days / 30.0)
        except (ValueError, TypeError):
            pass
        boost *= 1.0 + 0.05 * max(len(arms) - 1, 0)
        boost *= 1.0 + min(fact.proof_count - 1, 5) * 0.03
        return boost

    @staticmethod
    def _apply_token_budget(hits: Sequence[RecallHit], max_tokens: int, top_k: int) -> list[RecallHit]:
        """Facts are included in relevance order until the token budget is spent."""
        if max_tokens <= 0:
            return []
        out: list[RecallHit] = []
        used = 0
        for hit in hits:
            cost = max(len(hit.text) // 4, 1)
            if used + cost > max_tokens and out:
                continue
            out.append(hit)
            used += cost
            if len(out) >= top_k:
                break
        return out

    # -- reflect -----------------------------------------------------------------------
    def reflect(self, *, query: str, context: str = "", budget: str = "mid") -> ReflectionResult:
        """Retrieval-first synthesis with citations.

        The real Hindsight service runs an agent loop here. Without an LLM we still produce
        something genuinely useful: the ranked evidence, the observed outcome counts, and an
        explicit statement of the bank's directives, with every claim carrying a memory id.
        """
        recall = self.recall(query=query, budget=budget, max_tokens=3072, top_k=12)
        if recall.empty:
            return ReflectionResult(
                text=(
                    "No organizational experience matches this question. "
                    "This bank has no prior evidence to reason from."
                ),
                backend=self.name,
                citations=[],
                note="empty recall",
            )

        lines: list[str] = []
        if context:
            lines.append(f"Context: {context}")
        lines.append(f"Question: {query}")
        lines.append("")

        observations = [h for h in recall.hits if h.type == OBSERVATION]
        # Classification is text-based across *every* hit type, not just raw experience facts.
        # Consolidation means the most useful signal often arrives folded into an observation, and
        # ignoring those would make reflection blind to exactly the knowledge it exists to surface.
        failures = [h for h in recall.hits if _mentions_failure(h.text) and h.type != OBSERVATION]
        successes = [h for h in recall.hits if _mentions_success(h.text) and h.type != OBSERVATION]
        observation_failures = [h for h in observations if _mentions_failure(h.text)]
        observation_successes = [h for h in observations if _mentions_success(h.text)]

        if observations:
            lines.append("Consolidated observations:")
            for obs in observations[:3]:
                lines.append(f"  • {obs.text} [proof={obs.proof_count}, {obs.id}]")
            lines.append("")
        if failures:
            lines.append("Approaches that have failed before:")
            for hit in failures[:4]:
                lines.append(f"  • {hit.text} [{hit.id}]")
            lines.append("")
        if successes:
            lines.append("Approaches that have worked before:")
            for hit in successes[:4]:
                lines.append(f"  • {hit.text} [{hit.id}]")
            lines.append("")

        trend = self._trend_statement(recall.hits)
        if trend:
            lines.append(trend)
        if observation_failures and observation_successes:
            lines.append(
                "Consolidated knowledge records both failed and verified approaches in this "
                "scope: prefer the verified one."
            )

        if self.directives:
            lines.append("Bank directives applied: " + "; ".join(self.directives))

        return ReflectionResult(
            text="\n".join(lines).strip(),
            backend=self.name,
            citations=[h.id for h in recall.hits],
            note="deterministic synthesis over TEMPR recall (no LLM configured)",
        )

    @staticmethod
    def _trend_statement(hits: Sequence[RecallHit]) -> str:
        failed = sum(1 for h in hits if _mentions_failure(h.text) and h.type != OBSERVATION)
        verified = sum(1 for h in hits if _mentions_success(h.text) and h.type != OBSERVATION)
        if failed and not verified:
            return (
                f"Pattern: {failed} recorded failure(s) and no verified success in this scope — "
                f"the previously attempted approach should not be repeated as-is."
            )
        if failed and verified:
            return (
                f"Pattern: {failed} recorded failure(s) alongside {verified} verified success(es) — "
                f"prefer the approach that has been verified."
            )
        if verified:
            return f"Pattern: {verified} verified success(es) recorded in this scope."
        return ""

    # -- introspection -----------------------------------------------------------------
    def list_memories(
        self, *, memory_type: str | None = None, search: str | None = None, limit: int = 100
    ) -> list[RecallHit]:
        with self._lock:
            hits: list[RecallHit] = []
            for fact in self.facts.values():
                if memory_type and fact.type != memory_type:
                    continue
                if search and search.lower() not in fact.text.lower():
                    continue
                hits.append(fact.to_hit())
            for obs in self.observations.values():
                if memory_type and memory_type != OBSERVATION:
                    continue
                if search and search.lower() not in obs.text.lower():
                    continue
                hits.append(obs.to_hit())
            hits.sort(key=lambda h: h.mentioned_at or "", reverse=True)
            return hits[:limit]

    def stats(self) -> dict[str, Any]:
        with self._lock:
            by_type: Counter[str] = Counter(f.type for f in self.facts.values())
            by_scope: Counter[str] = Counter(f.scope for f in self.facts.values())
            return {
                "backend": self.name,
                "bank_id": self.bank_id,
                "facts": len(self.facts),
                "observations": len(self.observations),
                "documents": self.document_count,
                "entities": len(self.entity_index),
                "facts_by_type": dict(by_type),
                "facts_by_scope": dict(by_scope),
                "persisted_to": str(self._persist_path) if self._persist_path else None,
            }

    def health(self) -> dict[str, Any]:
        return {
            "backend": self.name,
            "healthy": True,
            "detail": "in-process memory store (Hindsight semantics)",
            **{k: v for k, v in self.stats().items() if k != "backend"},
        }

    def reset(self) -> None:
        with self._lock:
            self.facts.clear()
            self.observations.clear()
            self.document_entities.clear()
            self.entity_index.clear()
            self.document_count = 0
            self._idf = None
            self._save()

    # -- persistence -------------------------------------------------------------------
    def _save(self) -> None:
        if not self._persist_path:
            return
        self._persist_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "bank_id": self.bank_id,
            "mission": self.mission,
            "directives": self.directives,
            "document_count": self.document_count,
            "facts": {
                fid: {**asdict(fact), "superseded_by": getattr(fact, "superseded_by", None)}
                for fid, fact in self.facts.items()
            },
            "observations": {oid: asdict(obs) for oid, obs in self.observations.items()},
            "document_entities": {k: sorted(v) for k, v in self.document_entities.items()},
            "entity_index": {k: sorted(v) for k, v in self.entity_index.items()},
        }
        tmp = self._persist_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=1), encoding="utf-8")
        tmp.replace(self._persist_path)

    def _load(self) -> None:
        try:
            payload = json.loads(self._persist_path.read_text(encoding="utf-8"))  # type: ignore[union-attr]
        except (OSError, json.JSONDecodeError):
            return
        self.mission = payload.get("mission", self.mission)
        self.directives = payload.get("directives", [])
        self.document_count = payload.get("document_count", 0)
        for fid, raw in payload.get("facts", {}).items():
            raw.pop("superseded_by", None)
            record = FactRecord(**raw)
            self.facts[fid] = record
        for oid, raw in payload.get("observations", {}).items():
            self.observations[oid] = ObservationRecord(**raw)
        for doc_id, entities in payload.get("document_entities", {}).items():
            self.document_entities[doc_id] = set(entities)
        for entity, ids in payload.get("entity_index", {}).items():
            self.entity_index[entity] = set(ids)
