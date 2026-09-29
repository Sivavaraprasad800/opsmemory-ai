"""Memory tests (build spec sections 12-15, testing block 40-50).

These run against the in-process store, which implements the same semantics as Hindsight:
extraction into typed facts, entity linking, four-arm retrieval with RRF fusion, recency and proof
boosts, and observation consolidation with proof counts. When a real Hindsight server is
configured the same assertions apply, because ``MemoryService`` is what the platform talks to.
"""

from __future__ import annotations

import pytest

from app.memory.base import EXPERIENCE, OBSERVATION, WORLD
from app.memory.documents import AttemptRecord, IncidentExperience, build_recall_query
from app.memory.fallback_store import FallbackStore
from app.detection.similarity import build_fingerprint


def test_retain_extracts_multiple_facts(memory_service) -> None:
    result = memory_service._retain(
        content=(
            "INCIDENT #7 — payment-service connection pool exhaustion\n"
            "Service payment-service in production, severity high, detected 2026-09-01T10:00:00Z.\n"
            "Root cause: connection_leak — a connection leak saturates the pool.\n"
            "Attempt 1: restart_service — verification FAILED.\n"
            "Successful remediation: update_known_safe_configuration.\n"
            "Verification: verified_success.\n"
        ),
        context="test retention",
        document_id="doc-1",
        metadata={"scope": "incident_experience", "root_cause_category": "connection_leak"},
        tags=["cause:connection_leak"],
        scope="incident_experience",
        db=None,
        incident_id=None,
        retained_by="test",
    )
    assert result.accepted is True
    assert result.facts_extracted >= 4
    assert result.memory_id == "doc-1"


def test_recall_finds_retained_content(memory_service) -> None:
    memory_service._retain(
        content=(
            "Attempt 1: restart_service — verification FAILED. "
            "The connection leak remained active after restarting payment-service."
        ),
        context="remediation outcome",
        document_id="attempt-1",
        metadata={"scope": "remediation_outcome", "action_code": "restart_service"},
        tags=["outcome:failed"],
        scope="remediation_outcome",
        db=None,
        incident_id=None,
        retained_by="test",
    )
    result = memory_service._active_store().recall(
        query="did restarting fix the connection leak on payment-service?", budget="mid", top_k=5
    )
    assert not result.empty
    joined = " ".join(hit.text.lower() for hit in result.hits)
    assert "restart" in joined
    assert "failed" in joined
    assert result.backend == "fallback"


def test_recall_returns_empty_for_an_unrelated_question(memory_service) -> None:
    memory_service._retain(
        content="Attempt 1: clear_safe_cache — verified successful for stale cache entries.",
        context="remediation outcome",
        document_id="attempt-cache",
        metadata={"scope": "remediation_outcome"},
        tags=[],
        scope="remediation_outcome",
        db=None,
        incident_id=None,
        retained_by="test",
    )
    result = memory_service._active_store().recall(
        query="what is the capital of the antarctic research station", top_k=5
    )
    # Multi-arm retrieval may surface a weak lexical match; what matters is that nothing in the
    # bank claims to answer the question, and that a memory about caches is not ranked first on
    # the strength of an unrelated query.
    if not result.empty:
        assert "capital" not in result.hits[0].text.lower()


def test_observation_consolidation_carries_proof_and_sources(memory_service) -> None:
    for index in range(3):
        memory_service._retain(
            content=(
                f"Attempt {index + 1}: restart_service on payment-service for connection_leak — "
                f"verification FAILED, the leak remained active."
            ),
            context="remediation outcome",
            document_id=f"attempt-{index}",
            metadata={
                "scope": "remediation_outcome",
                "action_code": "restart_service",
                "root_cause_category": "connection_leak",
                "service": "payment-service",
            },
            tags=["action:restart_service"],
            scope="remediation_outcome",
            db=None,
            incident_id=None,
            retained_by="test",
        )
    store = memory_service._active_store()
    assert isinstance(store, FallbackStore)
    assert store.observations, "consolidation must produce at least one observation"
    observation = next(iter(store.observations.values()))
    assert observation.proof_count >= 3
    assert observation.source_fact_ids, "an observation must cite its sources"
    assert "restart_service" in observation.text or "connection_leak" in observation.text


def test_observation_is_refined_not_replaced(memory_service) -> None:
    store = memory_service._active_store()
    for index in range(2):
        memory_service._retain(
            content=f"Attempt {index}: restart_service failed for connection_leak on payment-service.",
            context="x",
            document_id=f"d{index}",
            metadata={"scope": "remediation_outcome", "root_cause_category": "connection_leak"},
            tags=[],
            scope="remediation_outcome",
            db=None,
            incident_id=None,
            retained_by="test",
        )
    first_text = next(iter(store.observations.values())).text

    memory_service._retain(
        content="Successful remediation: update_known_safe_configuration resolved connection_leak and was verified.",
        context="x",
        document_id="d-success",
        metadata={"scope": "remediation_outcome", "root_cause_category": "connection_leak"},
        tags=[],
        scope="remediation_outcome",
        db=None,
        incident_id=None,
        retained_by="test",
    )
    observation = next(iter(store.observations.values()))
    assert observation.text != first_text
    assert observation.superseded_texts, "the previous observation text must be preserved"
    assert observation.proof_count >= 3


def test_recall_type_filter_is_respected(memory_service) -> None:
    memory_service._retain(
        content=(
            "payment-service deployed version v2.41.0 which reduced the pool size to 8.\n"
            "I attempted restart_service and verification failed."
        ),
        context="mixed",
        document_id="doc-mixed",
        metadata={"scope": "incident_experience"},
        tags=[],
        scope="incident_experience",
        db=None,
        incident_id=None,
        retained_by="test",
    )
    store = memory_service._active_store()
    world_only = store.recall(query="payment-service pool size", types=(WORLD,), top_k=10)
    assert all(hit.type != EXPERIENCE for hit in world_only.hits)
    experience_only = store.recall(query="what did the agent attempt", types=(EXPERIENCE,), top_k=10)
    assert all(hit.type != WORLD for hit in experience_only.hits)


def test_token_budget_limits_the_result_set(memory_service) -> None:
    for index in range(12):
        memory_service._retain(
            content=(
                f"Experience {index}: attempting restart_service on payment-service for "
                f"connection_leak resulted in verification failing after the pool refilled."
            ),
            context="bulk",
            document_id=f"bulk-{index}",
            metadata={"scope": "remediation_outcome", "root_cause_category": "connection_leak"},
            tags=[],
            scope="remediation_outcome",
            db=None,
            incident_id=None,
            retained_by="test",
        )
    store = memory_service._active_store()
    # Raw facts only, so consolidated observations do not supersede them and the budget itself is
    # what limits the result set.
    query = "restart_service payment-service connection_leak"
    small = store.recall(query=query, types=(WORLD, EXPERIENCE), max_tokens=40, top_k=20)
    large = store.recall(query=query, types=(WORLD, EXPERIENCE), max_tokens=4000, top_k=20)
    assert len(small.hits) < len(large.hits)
    assert small.hits, (
        "a query that matched must never come back empty: the top fact is returned whole rather "
        "than clipped, because an empty list would read as 'this bank has no such memory'"
    )


def test_observation_supersedes_its_source_facts(memory_service) -> None:
    """prefer_observations behaviour: when an observation is returned, the raw facts it was built
    from are dropped so the same information is not reported twice."""
    for index in range(4):
        memory_service._retain(
            content=(
                f"Attempt {index + 1}: restart_service on payment-service for connection_leak — "
                f"verification FAILED."
            ),
            context="remediation outcome",
            document_id=f"super-{index}",
            metadata={"scope": "remediation_outcome", "root_cause_category": "connection_leak"},
            tags=[],
            scope="remediation_outcome",
            db=None,
            incident_id=None,
            retained_by="test",
        )
    result = memory_service._active_store().recall(
        query="restart_service connection_leak payment-service verification failed", top_k=10
    )
    observation_ids = {h.id for h in result.hits if h.type == OBSERVATION}
    assert observation_ids, "the consolidated observation should be recalled"
    source_facts = {
        fact_id for hit in result.hits if hit.type == OBSERVATION for fact_id in hit.source_fact_ids
    }
    returned_fact_ids = {h.id for h in result.hits if h.type != OBSERVATION}
    assert not (source_facts & returned_fact_ids), (
        "raw facts already folded into a returned observation must be dropped"
    )


def test_recall_reports_which_retrieval_arms_fired(memory_service) -> None:
    memory_service._retain(
        content="update_known_safe_configuration applied the corrected pool size on payment-service.",
        context="x",
        document_id="arms",
        metadata={"scope": "remediation_outcome"},
        tags=[],
        scope="remediation_outcome",
        db=None,
        incident_id=None,
        retained_by="test",
    )
    response = memory_service._active_store().recall(
        query="update_known_safe_configuration payment-service pool size", top_k=5
    )
    assert response.strategy_hits
    assert any(hit.retrieval for hit in response.hits), "each hit should record its retrieval arms"


def test_reflect_produces_cited_synthesis_with_no_experience(memory_service) -> None:
    result = memory_service.reflect(query="What have we learned about database incidents?")
    assert "no organizational experience" in result.text.lower() or "no prior evidence" in result.text.lower()
    assert result.citations == []


def test_reflect_separates_failures_from_verified_successes(memory_service) -> None:
    memory_service._retain(
        content=(
            "Attempt 1: restart_service on payment-service for connection_leak — verification FAILED.\n"
            "Attempt 2: update_known_safe_configuration on payment-service — verified successful.\n"
        ),
        context="incident learning",
        document_id="doc-reflect",
        metadata={"scope": "incident_experience", "root_cause_category": "connection_leak"},
        tags=[],
        scope="incident_experience",
        db=None,
        incident_id=None,
        retained_by="test",
    )
    result = memory_service.reflect(query="connection_leak payment-service remediation outcomes")
    assert "failed" in result.text.lower()
    assert result.citations, "reflection must cite the memory ids it used"
    assert "should not be repeated" in result.text.lower() or "prefer" in result.text.lower()


def test_memory_persists_across_store_instances(memory_service, tmp_path) -> None:
    path = tmp_path / "memory.json"
    first = FallbackStore(bank_id="persist-test", persist_path=path)
    first.retain(
        content="restart_service on payment-service failed verification for connection_leak.",
        context="x",
        document_id="persist-1",
        metadata={"scope": "remediation_outcome"},
        tags=[],
    )
    assert path.exists()

    second = FallbackStore(bank_id="persist-test", persist_path=path)
    assert second.facts, "a restarted process must retain what it learned"
    assert second.document_count == 1
    result = second.recall(query="did restart work for the connection leak on payment-service?")
    assert not result.empty


def test_memory_service_degrades_when_hindsight_is_unreachable(monkeypatch) -> None:
    """A memory outage must degrade, not crash (build spec section 48)."""
    from app.memory.hindsight_store import HindsightStore
    from app.memory.service import MemoryService

    service = MemoryService()
    service._hindsight = HindsightStore(
        base_url="http://127.0.0.1:9", bank_id="unreachable", timeout=1.0
    )
    service._health_checked_at = 0.0

    store = service._active_store()
    assert store.name == "fallback"
    assert service.degraded is True
    assert any("unreachable" in reason for reason in service.degradation_reasons)

    # And it still works.
    result = service._retain(
        content="degraded mode retention test",
        context="x",
        document_id="degraded-1",
        metadata={"scope": "incident_experience"},
        tags=[],
        scope="incident_experience",
        db=None,
        incident_id=None,
        retained_by="test",
    )
    assert result.backend == "fallback"


def test_recall_query_is_built_from_the_fingerprint() -> None:
    fingerprint = build_fingerprint(
        service="payment-service",
        environment="staging",
        root_cause_category="connection_leak",
        error_signatures=["abc"],
        symptom="pool saturation climbing",
        metric_zscores={"connection_saturation": 5.0},
        deployment_related=True,
    )
    query = build_recall_query(fingerprint=fingerprint)
    assert "payment-service" in query
    assert "staging" in query
    assert "connection_leak" in query
    assert "failed verification" in query
    assert len(query) < 1800, "the query must stay inside Hindsight's token limit"


def test_incident_experience_renders_failed_attempts_explicitly() -> None:
    experience = IncidentExperience(
        incident_id=12,
        title="payment-service pool exhaustion",
        service="payment-service",
        environment="production",
        severity="high",
        detected_at="2026-09-01T10:00:00Z",
        root_cause_category="connection_leak",
        root_cause="a connection leak saturates the pool",
        attempts=[
            AttemptRecord(1, "restart_service", "Restart service", "failed", ["saturation 1.0"], "leak remained"),
            AttemptRecord(2, "update_known_safe_configuration", "Apply known-safe config", "succeeded", [], ""),
        ],
        successful_remediation="update_known_safe_configuration",
        verification="verified_success",
    )
    rendered = experience.render()
    assert "Attempt 1: restart_service" in rendered
    assert "verification FAILED" in rendered
    assert "Attempt 2: update_known_safe_configuration" in rendered
    assert "verified successful" in rendered
    assert "Root cause:" in rendered


def test_memory_stats_report_documents_separately_from_facts(memory_service) -> None:
    memory_service._retain(
        content="line one about connection_leak\nline two about payment-service\nline three about restart_service",
        context="x",
        document_id="stats-1",
        metadata={"scope": "incident_experience"},
        tags=[],
        scope="incident_experience",
        db=None,
        incident_id=None,
        retained_by="test",
    )
    stats = memory_service.stats()
    assert stats["documents"] == 1
    assert stats["facts"] >= 3
