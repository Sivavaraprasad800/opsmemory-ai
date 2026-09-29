"""Similarity tests (build spec section 11).

The point of these tests is that the *explanation* is checkable, not just the score. A single
opaque float would be impossible to test meaningfully; per-feature contributions are assertions
about why two incidents were considered related.
"""

from __future__ import annotations

import pytest

from app.detection.similarity import (
    FEATURE_WEIGHTS,
    TfidfVectorizer,
    IncidentFingerprint,
    build_fingerprint,
    compare_fingerprints,
    cosine,
    metric_pattern_correlation,
    rank_neighbours,
    service_family,
)


def _fingerprint(**overrides) -> IncidentFingerprint:
    base = dict(
        service="payment-service",
        environment="production",
        environment_kind="production",
        severity="high",
        root_cause_category="connection_leak",
        error_signatures=["sig-a", "sig-b"],
        symptom="connection pool saturation climbing steadily",
        symptoms=["connection acquisition timeout", "pool exhausted"],
        metric_zscores={"connection_saturation": 5.4, "latency_ms": 2.1, "error_rate": 1.8},
        deployment_related=True,
        deployment_change_classes=["application_config"],
    )
    base.update(overrides)
    return build_fingerprint(**base)


def _contribution(result, feature: str):
    return next(c for c in result.contributions if c.feature == feature)


def test_weights_are_normalised_and_documented() -> None:
    assert sum(FEATURE_WEIGHTS.values()) == pytest.approx(1.0, abs=1e-9)
    assert set(FEATURE_WEIGHTS) == {
        "service",
        "environment",
        "error_signatures",
        "metric_pattern",
        "root_cause",
        "deployment",
        "symptom_text",
    }


def test_identical_fingerprints_score_one() -> None:
    fingerprint = _fingerprint()
    result = compare_fingerprints(fingerprint, fingerprint)
    assert result.score == pytest.approx(1.0, abs=1e-6)
    assert result.label == "very_similar"
    assert len(result.matched) == len(FEATURE_WEIGHTS)


def test_environment_mismatch_appears_in_differences() -> None:
    """The build spec's own example: same service, different environment."""
    current = _fingerprint()
    historical = _fingerprint(environment="staging", environment_kind="staging")
    result = compare_fingerprints(current, historical)

    environment = _contribution(result, "environment")
    assert environment.raw_score == 0.0
    assert "different environment" in environment.detail
    assert any("staging" in item and "production" in item for item in result.differences)
    assert 0.5 < result.score < 1.0
    assert result.label in {"similar", "very_similar"}


def test_service_family_partially_matches_different_services() -> None:
    current = _fingerprint(service="payment-service")
    historical = _fingerprint(service="payment-gateway")
    assert service_family("payment-service") == "payment"
    result = compare_fingerprints(current, historical)
    assert _contribution(result, "service").raw_score == pytest.approx(0.6)


def test_signature_blend_does_not_punish_the_longer_incident() -> None:
    """Plain Jaccard reports 0.50 here because the longer incident has a tail; the blend reports
    0.75 because the core signature set is fully contained."""
    current = _fingerprint(error_signatures=["sig-a", "sig-b", "sig-c", "sig-d"])
    historical = _fingerprint(error_signatures=["sig-a", "sig-b"])
    result = compare_fingerprints(current, historical)
    signature = _contribution(result, "error_signatures")
    assert signature.raw_score == pytest.approx(0.75, abs=0.01)
    assert "Jaccard 0.50" in signature.detail
    assert "core-overlap 1.00" in signature.detail


def test_no_shared_signatures_is_reported_as_a_mismatch() -> None:
    result = compare_fingerprints(
        _fingerprint(error_signatures=["x"]), _fingerprint(error_signatures=["y"])
    )
    assert _contribution(result, "error_signatures").raw_score == 0.0
    assert "no shared error signatures" in _contribution(result, "error_signatures").detail


def test_metric_pattern_correlation_detects_shared_shape() -> None:
    a = {"connection_saturation": 5.0, "latency_ms": 2.0, "error_rate": 1.5, "cpu_pct": 0.0}
    b = {"connection_saturation": 4.6, "latency_ms": 1.8, "error_rate": 1.2, "cpu_pct": 0.0}
    rho, detail, count = metric_pattern_correlation(a, b)
    assert rho > 0.95
    assert count == 4
    assert "rho" in detail


def test_metric_pattern_correlation_flags_a_different_shape() -> None:
    pool_only = {"connection_saturation": 6.0, "latency_ms": 0.1, "cpu_pct": 0.0, "mem_pct": 0.0}
    cpu_only = {"connection_saturation": 0.0, "latency_ms": 0.1, "cpu_pct": 6.0, "mem_pct": 0.0}
    rho, _detail, _count = metric_pattern_correlation(pool_only, cpu_only)
    assert rho < 0.6


def test_metric_pattern_correlation_handles_flat_profiles() -> None:
    flat = {"cpu_pct": 0.0, "mem_pct": 0.0}
    rho, detail, _count = metric_pattern_correlation(flat, flat)
    assert rho == 0.0
    assert "flat" in detail


def test_root_cause_families_are_related_but_not_identical() -> None:
    same = compare_fingerprints(_fingerprint(), _fingerprint())
    assert _contribution(same, "root_cause").raw_score == 1.0

    related = compare_fingerprints(
        _fingerprint(root_cause_category="connection_leak"),
        _fingerprint(root_cause_category="connection_pool_exhaustion"),
    )
    assert _contribution(related, "root_cause").raw_score == 0.5

    unrelated = compare_fingerprints(
        _fingerprint(root_cause_category="connection_leak"),
        _fingerprint(root_cause_category="network_issue"),
    )
    assert _contribution(unrelated, "root_cause").raw_score == 0.0


def test_unknown_root_cause_does_not_award_similarity() -> None:
    result = compare_fingerprints(_fingerprint(), _fingerprint(root_cause_category="unknown"))
    assert _contribution(result, "root_cause").raw_score == 0.0
    assert "not yet established" in _contribution(result, "root_cause").detail


def test_deployment_relationship_scoring() -> None:
    both = compare_fingerprints(_fingerprint(), _fingerprint())
    assert _contribution(both, "deployment").raw_score == 1.0

    neither = compare_fingerprints(
        _fingerprint(deployment_related=False, deployment_change_classes=[]),
        _fingerprint(deployment_related=False, deployment_change_classes=[]),
    )
    assert _contribution(neither, "deployment").raw_score == pytest.approx(0.6)

    mixed = compare_fingerprints(_fingerprint(), _fingerprint(deployment_related=False, deployment_change_classes=[]))
    assert _contribution(mixed, "deployment").raw_score == 0.0


def test_symptom_text_similarity_works_without_a_fitted_corpus() -> None:
    """compare_fingerprints must not silently zero this feature when called pairwise."""
    similar = compare_fingerprints(
        _fingerprint(symptom="connection pool saturation climbing steadily"),
        _fingerprint(symptom="connection pool saturation rose steadily"),
    )
    assert _contribution(similar, "symptom_text").raw_score > 0.3

    dissimilar = compare_fingerprints(
        _fingerprint(symptom="connection pool saturation climbing steadily"),
        _fingerprint(symptom="disk volume filled and writes began failing"),
    )
    assert (
        _contribution(dissimilar, "symptom_text").raw_score
        < _contribution(similar, "symptom_text").raw_score
    )


def test_tfidf_vectorizer_is_case_insensitive_and_stopword_aware() -> None:
    vectorizer = TfidfVectorizer().fit(["connection pool saturation", "disk volume full"])
    assert cosine(
        vectorizer.transform("CONNECTION pool saturation"),
        vectorizer.transform("connection pool saturation"),
    ) == pytest.approx(1.0, abs=1e-9)


def test_build_metric_pattern_clips_and_truncates() -> None:
    pattern = build_fingerprint(
        service="s",
        environment="production",
        metric_zscores={f"m{i}": float(i) for i in range(20)} | {"huge": 500.0},
    ).metric_pattern
    assert len(pattern) <= 12
    assert max(abs(v) for v in pattern.values()) <= 8.0


def test_fingerprint_round_trips_through_json_like_dicts() -> None:
    fingerprint = _fingerprint()
    restored = IncidentFingerprint.from_dict(fingerprint.to_dict())
    assert restored.to_dict() == fingerprint.to_dict()
    assert IncidentFingerprint.from_dict(None).service == ""


def test_rank_neighbours_filters_and_orders() -> None:
    current = _fingerprint()
    candidates = [
        (1, "identical", "payment-service", "high", "connection_leak", "2026-01-01T00:00:00Z", _fingerprint()),
        (
            2,
            "different env",
            "payment-service",
            "high",
            "connection_leak",
            "2026-01-02T00:00:00Z",
            _fingerprint(environment="staging", environment_kind="staging"),
        ),
        (
            3,
            "unrelated",
            "notification-service",
            "low",
            "disk_pressure",
            "2026-01-03T00:00:00Z",
            _fingerprint(
                service="notification-service",
                root_cause_category="disk_pressure",
                error_signatures=["other"],
                metric_zscores={"disk_pct": 6.0},
                deployment_related=False,
                deployment_change_classes=[],
                symptom="disk filled up",
                symptoms=["write failed"],
            ),
        ),
    ]
    neighbours = rank_neighbours(current, candidates, top_k=2, min_score=0.3)
    assert len(neighbours) == 2
    assert neighbours[0].incident_id == 1
    assert neighbours[1].incident_id == 2
    assert all(n.similarity.score >= 0.3 for n in neighbours)
    assert all(isinstance(n.similarity.matched, list) for n in neighbours)


def test_rank_neighbours_handles_an_empty_corpus() -> None:
    assert rank_neighbours(_fingerprint(), []) == []
