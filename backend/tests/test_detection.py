"""Detection tests (build spec sections 8 and 10, testing block 50-60).

The most important test in this file is ``test_mad_resists_self_masking``: it asserts the exact
property that justifies choosing MAD over a mean/std z-score, and it fails on an implementation
that uses mean/std. That is the difference between a comment claiming a design decision and a test
enforcing it.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from app.detection.logs import (
    aggregate_signatures,
    build_signature,
    classify_level,
    detect_signature_spikes,
    normalize_message,
    signature_hash,
)
from app.detection.metrics import (
    DetectorConfig,
    cusum,
    detect_anomaly,
    detect_anomaly_vs_baseline,
    ewma,
    ewma_residual_z,
    median_abs_deviation,
    robust_z,
    rolling_baseline,
    severity_for,
)

rng = np.random.default_rng(1234)


# ---------------------------------------------------------------------------------------
# Robust statistics
# ---------------------------------------------------------------------------------------
def test_robust_z_is_zero_for_a_flat_signal() -> None:
    assert robust_z([5.0] * 20) == pytest.approx(0.0, abs=1e-6)


def test_mad_resists_self_masking() -> None:
    """A sustained shift must not be able to hide itself.

    This is the real masking scenario, and it is the one that matters: an incident is a
    *sustained* level shift, not a single sample. When a meaningful fraction of the window is
    elevated, a mean/std z-score pulls its own mean up and inflates its own sigma until the very
    deviation it should detect drops below threshold (here ~1.7 sigma, i.e. invisible).

    MAD has a 50% breakdown point, so the elevated samples cannot influence the scale estimate
    and the same shift scores far above threshold.
    """
    local = np.random.default_rng(7)
    baseline = list(local.normal(100.0, 2.0, 60))
    shifted = baseline + list(local.normal(130.0, 2.0, 20))

    mean_based = (shifted[-1] - float(np.mean(shifted))) / float(np.std(shifted))
    robust = robust_z(shifted)

    assert mean_based < 3.5, f"the mean/std z-score is self-masked here (it scored {mean_based:.2f})"
    assert robust > 3.5, "the MAD-based z-score must still flag the sustained shift"


def test_single_sample_spike_alone_does_not_open_an_incident() -> None:
    """MAD is sensitive, so the committee plus debounce is what stops one noisy sample opening
    an incident. Here only the outlier is elevated. The robust z does fire, but the operational
    floor rejects the relative change on a flat signal."""
    local = np.random.default_rng(11)
    values = list(local.normal(50.0, 0.5, 79)) + [52.5]
    verdict = detect_anomaly("cpu_pct", values, anomaly_expected_direction="up")
    assert verdict.anomalous is False


def test_median_abs_deviation_of_constant_signal() -> None:
    assert median_abs_deviation(np.array([7.0, 7.0, 7.0])) == 0.0


def test_rolling_baseline_band_excludes_the_last_sample() -> None:
    values = [10.0] * 30 + [90.0]
    median, lower, upper, _mad = rolling_baseline(values, window=30)
    assert median == pytest.approx(10.0, abs=1.0)
    assert 90.0 > upper, "the current value should sit above the trailing band"


def test_ewma_detects_a_gradual_ramp_that_a_static_threshold_misses() -> None:
    """The memory-leak signature: every individual step is small, the total is not."""
    ramp = [50.0 + i * 0.35 for i in range(80)]
    assert max(ramp) - min(ramp) > 20.0
    residual = ewma_residual_z(ramp, alpha=0.3)
    assert abs(residual) > 3.0, "the EWMA residual must register a sustained ramp"

    flat = [50.0] * 80
    assert abs(ewma_residual_z(flat, alpha=0.3)) < 1e-6


def test_ewma_series_converges_toward_the_signal() -> None:
    smoothed = ewma([0.0, 10.0, 10.0, 10.0], alpha=0.5)
    assert smoothed[-1] > smoothed[0]
    assert smoothed[-1] == pytest.approx(10.0 - 10.0 * 0.5**3, rel=1e-6)


def test_cusum_locates_the_change_point() -> None:
    """CUSUM locates *where* behaviour changed, with an expected detection delay.

    The delay is inherent to CUSUM (it must accumulate evidence before crossing the decision
    threshold) so the assertion is that the change point lands after the true change and within a
    bounded delay, not exactly on the true change.
    """
    values = [20.0] * 40 + [20.0 + (i * 1.5) for i in range(30)]
    c_plus, c_minus, up_idx, down_idx = cusum(values, slack=0.5, threshold=5.0, baseline=20.0)
    assert down_idx is None
    assert up_idx is not None
    assert 40 <= up_idx <= 62, f"change point located at {up_idx}, expected after the true change at 40"
    assert up_idx - 40 <= 25, "the detection delay must stay bounded"
    assert c_plus > c_minus


# ---------------------------------------------------------------------------------------
# Committee behaviour
# ---------------------------------------------------------------------------------------
def test_committee_does_not_flag_a_stable_signal() -> None:
    config = DetectorConfig(quorum=2)
    flat = list(np.random.default_rng(3).normal(50.0, 0.5, 60))
    verdict = detect_anomaly("cpu_pct", flat, config=config)
    assert verdict.anomalous is False
    assert verdict.score == 0.0
    assert verdict.reason, "a non-detection must still explain itself"


def test_committee_flags_a_recent_level_shift() -> None:
    """A shift confined to the tail of the window is detected by every arm."""
    local = np.random.default_rng(5)
    values = list(local.normal(0.05, 0.004, 68)) + list(local.normal(0.42, 0.02, 12))
    verdict = detect_anomaly("error_rate", values, anomaly_expected_direction="up")
    assert verdict.anomalous is True
    assert verdict.direction == "up"
    assert verdict.severity in {"medium", "high", "critical"}
    assert verdict.relative_change > 1.0
    breached = [d for d in verdict.detectors if d.breached]
    assert len(breached) >= 2


def test_committee_adapts_to_a_shift_longer_than_its_baseline_window() -> None:
    """Honest limitation, made explicit by a test.

    The single-window detector uses a trailing baseline, so once a shift persists longer than that
    baseline window the detector treats the new level as normal. This is *why* the platform uses
    ``detect_anomaly_vs_baseline`` instead, comparing a recent window against a separate older
    reference: without that, a long-running incident would become the new normal.
    """
    local = np.random.default_rng(9)
    values = list(local.normal(0.05, 0.004, 60)) + list(local.normal(0.42, 0.02, 40))
    single_window = detect_anomaly("error_rate", values, config=DetectorConfig(), anomaly_expected_direction="up")
    assert single_window.anomalous is False, "the trailing baseline has adapted to the new level"

    two_window = detect_anomaly_vs_baseline(
        "error_rate", values[-40:], values[:60], anomaly_expected_direction="up"
    )
    assert two_window.anomalous is True, "the two-window detector still sees the shift"


def test_wrong_direction_is_not_reported_as_an_incident() -> None:
    values = list(rng.normal(0.5, 0.02, 60)) + list(rng.normal(0.02, 0.003, 40))
    verdict = detect_anomaly("error_rate", values, anomaly_expected_direction="up")
    assert verdict.anomalous is False
    assert "direction" in verdict.reason


def test_operationally_irrelevant_change_is_rejected() -> None:
    values = list(rng.normal(1000.0, 0.5, 60)) + list(rng.normal(1001.0, 0.5, 40))
    verdict = detect_anomaly("latency_ms", values, anomaly_expected_direction="up")
    assert verdict.anomalous is False, "a 0.1% change is statistically real but not an incident"


def test_insufficient_samples_reports_rather_than_guesses() -> None:
    verdict = detect_anomaly("cpu_pct", [1.0, 2.0, 3.0])
    assert verdict.anomalous is False
    assert "insufficient" in verdict.reason


# ---------------------------------------------------------------------------------------
# Two-window detector (the one the platform actually uses)
# ---------------------------------------------------------------------------------------
def test_two_window_detector_survives_a_contaminated_reference_only_when_it_should() -> None:
    """A ramp is detected against an older healthy window even when the recent median is high."""
    baseline = list(rng.normal(0.12, 0.01, 400))
    ramp = list(np.linspace(0.12, 1.0, 150))
    verdict = detect_anomaly_vs_baseline("connection_saturation", ramp, baseline)
    assert verdict.anomalous is True
    assert verdict.relative_change > 2.0
    assert verdict.baseline == pytest.approx(0.12, abs=0.02)


def test_single_window_detector_would_miss_the_same_ramp() -> None:
    """Documents *why* the two-window detector exists, by showing the failure it removes."""
    ramp = list(np.linspace(0.12, 1.0, 150))
    single = detect_anomaly("connection_saturation", ramp)
    assert single.anomalous is False
    assert abs(single.relative_change) < 1.0, "the ramp is averaged into its own baseline"


def test_two_window_detector_stays_quiet_when_nothing_changed() -> None:
    baseline = list(rng.normal(80.0, 3.0, 300))
    recent = list(rng.normal(80.0, 3.0, 150))
    verdict = detect_anomaly_vs_baseline("cpu_pct", recent, baseline)
    assert verdict.anomalous is False


def test_severity_bands_are_ordered() -> None:
    assert severity_for(0.95) == "critical"
    assert severity_for(0.80) == "high"
    assert severity_for(0.60) == "medium"
    assert severity_for(0.40) == "low"
    assert severity_for(0.05) == "none"


# ---------------------------------------------------------------------------------------
# Log pipeline
# ---------------------------------------------------------------------------------------
def test_variable_values_collapse_into_one_signature() -> None:
    """The core promise of section 10: three different user ids are one template."""
    messages = [
        "payment-service DB connection failed for user 1001 conn_id=8812",
        "payment-service DB connection failed for user 1002 conn_id=8899",
        "payment-service DB connection failed for user 1003 conn_id=8901",
    ]
    templates = {normalize_message(message) for message in messages}
    assert len(templates) == 1, templates
    assert "<ID>" in templates.pop()
    hashes = {signature_hash(normalize_message(message)) for message in messages}
    assert len(hashes) == 1


def test_normalisation_masks_typed_values() -> None:
    template = normalize_message(
        "timeout calling 10.0.3.14:5432 hostpayments.internal duration=2.41s "
        "trace 4f2a1c9e8b7d6a5f4e3d2c1b0a9f8e7d request req=99812"
    )
    assert "<IP>" in template or "<ADDR>" in template or "<HOST>" in template
    assert "<DURATION>" in template
    assert "<HASH>" in template or "<SHA>" in template


def test_level_classification() -> None:
    assert classify_level("ERROR connection reset by peer") == "ERROR"
    assert classify_level("WARNING pool utilisation high") == "WARN"
    assert classify_level("INFO healthcheck ok") == "INFO"
    assert classify_level("Traceback (most recent call last): ValueError") == "ERROR"
    assert classify_level("nothing remarkable here") == "INFO"


def test_aggregation_ranks_new_signatures_above_repeats() -> None:
    events = [
        {"message": "payment-service GET /v1/payments 200 duration=0.12s", "level": "INFO", "service_id": 1, "service_name": "payment-service"},
        {"message": "payment-service connection acquisition timeout: pool exhausted active=50 max=50 conn_id=1", "level": "ERROR", "service_id": 1, "service_name": "payment-service"},
        {"message": "payment-service connection acquisition timeout: pool exhausted active=50 max=50 conn_id=2", "level": "ERROR", "service_id": 1, "service_name": "payment-service"},
    ]
    stats = aggregate_signatures(events)
    assert len(stats) == 2
    assert stats[0].level == "ERROR"
    assert stats[0].count == 2
    assert stats[0].is_new is True, "an unseen signature must be flagged as new"


def test_new_signature_with_a_baseline_is_not_flagged_as_new() -> None:
    message = "payment-service database query slow duration=1.20s"
    template = normalize_message(message)
    baseline = {signature_hash(template): 0.001}
    stats = aggregate_signatures(
        [{"message": message, "level": "WARN", "service_id": 1, "service_name": "svc"}] * 4,
        baseline=baseline,
        window_seconds=100.0,
    )
    assert stats[0].is_new is False
    assert stats[0].spike_ratio > 1.0


def test_spike_detection_requires_count_and_ratio() -> None:
    events = [
        {"message": "payment-service database query timeout after 2.00s", "level": "ERROR", "service_id": 1, "service_name": "svc"}
    ] * 3
    stats = aggregate_signatures(events)
    assert detect_signature_spikes(stats, min_count=5) == [], "below the count floor"

    many = [
        {"message": "payment-service database query timeout after 2.00s", "level": "ERROR", "service_id": 1, "service_name": "svc"}
    ] * 9
    assert len(detect_signature_spikes(aggregate_signatures(many), min_count=5)) == 1


def test_build_signature_is_deterministic() -> None:
    first = build_signature("payment-service ERROR failed id=42")
    second = build_signature("payment-service ERROR failed id=97")
    assert first[0] == second[0]
    assert first[1] == second[1]


def test_mad_is_not_fooled_by_a_single_extreme_sample() -> None:
    values = np.array([10.0] * 20 + [10_000.0])
    assert median_abs_deviation(values) == 0.0
    assert math.isfinite(robust_z(list(values)))
