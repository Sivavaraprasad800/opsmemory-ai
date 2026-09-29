"""Anomaly detection: a committee of four detectors, not a single threshold.

Why a committee (see docs/RESEARCH.md section 3.1):

* **Robust z-score (median/MAD)** — the classic mean/std z-score is *self-masking*: the very
  spike we want to detect inflates sigma, which shrinks the z-score. MAD has a 50 % breakdown
  point, so a spike cannot hide itself.
* **Rolling median baseline** — catches slow drift against a trailing window.
* **EWMA control limits** — catches *gradual* degradation (connection leaks, memory growth)
  that never crosses a static threshold.
* **CUSUM** — locates the *moment* behaviour changed, which is what feeds the "what changed?"
  correlation against deployments.

Statements emitted by this module are deliberately non-causal. A detector may say a metric is
"consistent with" or "preceded by" a change; it never says "caused by" (build spec section 8).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Sequence

import numpy as np

MAD_SCALE = 0.6745
"""1/0.6745 = 1.4826. Converts MAD to a standard-deviation-equivalent for normal data."""

SEVERITY_BANDS: tuple[tuple[float, str], ...] = (
    (0.90, "critical"),
    (0.75, "high"),
    (0.55, "medium"),
    (0.35, "low"),
)


@dataclass(frozen=True)
class DetectorConfig:
    """Tunable thresholds. Kept in one place so they are documented, not scattered."""

    mad_threshold: float = 3.5
    rolling_window: int = 30
    rolling_threshold: float = 3.5
    ewma_alpha: float = 0.30
    ewma_limit: float = 3.0
    cusum_slack: float = 0.5
    cusum_threshold: float = 5.0
    min_samples: int = 12
    min_relative_change: float = 0.20
    """Ignore anomalies that are statistically real but operationally irrelevant."""

    weights: dict[str, float] = field(
        default_factory=lambda: {
            "robust_z": 0.35,
            "rolling_baseline": 0.25,
            "ewma": 0.20,
            "cusum": 0.20,
        }
    )
    quorum: int = 2
    anomalous_score: float = 0.50


@dataclass
class DetectorVerdict:
    detector: str
    breached: bool
    score: float
    statistic: float
    threshold: float
    message: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AnomalyVerdict:
    metric: str
    anomalous: bool
    severity: str
    direction: str
    score: float
    current: float
    baseline: float
    absolute_change: float
    relative_change: float
    change_point_index: int | None
    detectors: list[DetectorVerdict]
    reason: str

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["detectors"] = [d.to_dict() if hasattr(d, "to_dict") else d for d in self.detectors]
        return payload


def _as_array(values: Sequence[float] | Iterable[float]) -> np.ndarray:
    arr = np.asarray(list(values), dtype=float)
    return arr[np.isfinite(arr)]


def median_abs_deviation(arr: np.ndarray) -> float:
    if arr.size == 0:
        return 0.0
    med = float(np.median(arr))
    return float(np.median(np.abs(arr - med)))


def robust_z(values: Sequence[float], *, target: float | None = None) -> float:
    """Robust z-score of ``target`` (default: last sample) against the window.

    ``z = 0.6745 * (x - median) / MAD``

    Falls back to a mean/std score when MAD degenerates to ~0 (a perfectly flat signal),
    where any deviation is meaningful.
    """
    arr = _as_array(values)
    if arr.size < 3:
        return 0.0
    x = float(arr[-1] if target is None else target)
    med = float(np.median(arr))
    mad = median_abs_deviation(arr)
    if mad < 1e-9:
        std = float(np.std(arr))
        if std < 1e-9:
            scale = max(abs(med) * 0.01, 1e-6)
            return (x - med) / scale
        return (x - med) / std
    return MAD_SCALE * (x - med) / mad


def _saturating(statistic: float, threshold: float) -> float:
    """Map |statistic|/threshold onto a 0..1 score with a soft knee at the threshold."""
    if threshold <= 0:
        return 0.0
    ratio = abs(statistic) / threshold
    if ratio <= 1.0:
        return 0.5 * ratio
    return min(1.0, 0.5 + 0.5 * (1.0 - np.exp(-(ratio - 1.0))))


def rolling_baseline(
    values: Sequence[float], *, window: int = 30
) -> tuple[float, float, float, float]:
    """Return ``(median, lower, upper, mad)`` of the trailing window *excluding* the last point."""
    arr = _as_array(values)
    if arr.size < 2:
        v = float(arr[-1]) if arr.size else 0.0
        return v, v, v, 0.0
    hist = arr[:-1][-window:]
    med = float(np.median(hist))
    mad = median_abs_deviation(hist)
    if mad < 1e-9:
        std = float(np.std(hist))
        spread = std if std > 1e-9 else max(abs(med) * 0.02, 1e-6)
    else:
        spread = mad / MAD_SCALE
    return med, med - 3.0 * spread, med + 3.0 * spread, mad


def ewma(values: Sequence[float], *, alpha: float = 0.3) -> np.ndarray:
    arr = _as_array(values)
    if arr.size == 0:
        return arr
    out = np.empty_like(arr)
    out[0] = arr[0]
    for i in range(1, arr.size):
        out[i] = alpha * arr[i] + (1.0 - alpha) * out[i - 1]
    return out


def ewma_residual_z(values: Sequence[float], *, alpha: float = 0.3) -> float:
    """Standardised one-step-ahead residual of the EWMA filter.

    A gradual ramp (the signature of a leak) produces small per-sample steps but a growing,
    sustained residual — which is exactly what a static threshold misses.
    """
    arr = _as_array(values)
    if arr.size < 4:
        return 0.0
    smoothed = ewma(arr, alpha=alpha)
    residuals = arr[1:] - smoothed[:-1]
    sigma = float(np.std(residuals))
    if sigma < 1e-9:
        sigma = max(abs(float(np.mean(arr))) * 0.01, 1e-6)
    # Compare the mean residual of the recent tail against noise.
    tail = residuals[-max(3, arr.size // 4) :]
    return float(np.mean(tail) / sigma)


def cusum(
    values: Sequence[float],
    *,
    slack: float = 0.5,
    threshold: float = 5.0,
    baseline: float | None = None,
) -> tuple[float, float, int | None, int | None]:
    """Two-sided CUSUM.

    Returns ``(c_plus, c_minus, up_change_index, down_change_index)`` where the indices are
    the first sample at which the cumulative sum crossed the decision threshold.
    """
    arr = _as_array(values)
    if arr.size < 4:
        return 0.0, 0.0, None, None
    mu = float(baseline if baseline is not None else np.median(arr[: max(4, arr.size // 3)]))
    mad = median_abs_deviation(arr)
    sigma = (mad / MAD_SCALE) if mad > 1e-9 else float(np.std(arr))
    if sigma < 1e-9:
        sigma = max(abs(mu) * 0.01, 1e-6)
    k = slack * sigma
    h = threshold * sigma

    c_plus = c_minus = 0.0
    up_idx: int | None = None
    down_idx: int | None = None
    for i, x in enumerate(arr):
        c_plus = max(0.0, c_plus + (x - mu) - k)
        c_minus = max(0.0, c_minus + (mu - x) - k)
        if up_idx is None and c_plus > h:
            up_idx = i
        if down_idx is None and c_minus > h:
            down_idx = i
    return float(c_plus), float(c_minus), up_idx, down_idx


def _direction_and_change(
    arr: np.ndarray, baseline: float, config: DetectorConfig
) -> tuple[str, float, float]:
    current = float(arr[-1]) if arr.size else 0.0
    absolute = current - baseline
    denom = abs(baseline) if abs(baseline) > 1e-9 else 1.0
    relative = absolute / denom
    direction = "up" if absolute > 0 else ("down" if absolute < 0 else "flat")
    return direction, absolute, relative


def severity_for(score: float, *, critical_override: bool = False) -> str:
    if critical_override:
        return "critical"
    for threshold, label in SEVERITY_BANDS:
        if score >= threshold:
            return label
    return "none"


def detect_anomaly(
    metric: str,
    values: Sequence[float],
    *,
    config: DetectorConfig | None = None,
    anomaly_expected_direction: str = "up",
    critical_metric: bool = False,
) -> AnomalyVerdict:
    """Run the full committee over a metric window and return an explicable verdict."""
    config = config or DetectorConfig()
    arr = _as_array(values)

    if arr.size < config.min_samples:
        return AnomalyVerdict(
            metric=metric,
            anomalous=False,
            severity="none",
            direction="flat",
            score=0.0,
            current=float(arr[-1]) if arr.size else 0.0,
            baseline=float(np.median(arr)) if arr.size else 0.0,
            absolute_change=0.0,
            relative_change=0.0,
            change_point_index=None,
            detectors=[],
            reason=f"insufficient samples ({arr.size} < {config.min_samples})",
        )

    med, lower, upper, _mad = rolling_baseline(arr, window=config.rolling_window)
    direction, absolute_change, relative_change = _direction_and_change(arr, med, config)

    z = robust_z(arr)
    z_score = _saturating(z, config.mad_threshold)
    z_breached = abs(z) >= config.mad_threshold

    current = float(arr[-1])
    if current > upper:
        rolling_stat = (current - upper) / max(abs(upper - med), 1e-9)
    elif current < lower:
        rolling_stat = -(lower - current) / max(abs(med - lower), 1e-9)
    else:
        rolling_stat = 0.0
    rolling_breached = current > upper or current < lower
    rolling_score = _saturating(rolling_stat, 1.0) if rolling_breached else 0.0

    e_z = ewma_residual_z(arr, alpha=config.ewma_alpha)
    ewma_breached = abs(e_z) >= config.ewma_limit
    ewma_score = _saturating(e_z, config.ewma_limit)

    c_plus, c_minus, up_idx, down_idx = cusum(
        arr, slack=config.cusum_slack, threshold=config.cusum_threshold, baseline=med
    )
    dominant_cusum = max(c_plus, c_minus)
    cusum_breached = dominant_cusum > config.cusum_threshold * 0.0 or (
        up_idx is not None or down_idx is not None
    )
    cusum_score = _saturating(dominant_cusum, config.cusum_threshold)

    detectors = [
        DetectorVerdict(
            detector="robust_z",
            breached=z_breached,
            score=round(z_score, 4),
            statistic=round(z, 3),
            threshold=config.mad_threshold,
            message=(
                f"robust z={z:.2f} (median {med:.3g}, MAD-based, threshold {config.mad_threshold})"
            ),
        ),
        DetectorVerdict(
            detector="rolling_baseline",
            breached=rolling_breached,
            score=round(rolling_score, 4),
            statistic=round(current, 4),
            threshold=round(upper, 4),
            message=f"current {current:.4g} vs rolling band [{lower:.4g}, {upper:.4g}]",
        ),
        DetectorVerdict(
            detector="ewma",
            breached=ewma_breached,
            score=round(ewma_score, 4),
            statistic=round(e_z, 3),
            threshold=config.ewma_limit,
            message=f"EWMA residual z={e_z:.2f} (alpha={config.ewma_alpha}) — gradual-change detector",
        ),
        DetectorVerdict(
            detector="cusum",
            breached=cusum_breached,
            score=round(cusum_score, 4),
            statistic=round(dominant_cusum, 3),
            threshold=config.cusum_threshold,
            message=f"CUSUM c+={c_plus:.2f} c-={c_minus:.2f} — change point at index "
            f"{up_idx if up_idx is not None else down_idx}",
        ),
    ]

    breached_count = sum(1 for d in detectors if d.breached)
    weighted = sum(
        config.weights.get(d.detector, 0.0) * (1.0 if d.breached else 0.0) for d in detectors
    )
    weighted /= max(sum(config.weights.values()), 1e-9)

    operationally_relevant = abs(relative_change) >= config.min_relative_change
    wrong_direction = (
        anomaly_expected_direction != "both"
        and direction != anomaly_expected_direction
        and direction != "flat"
    )

    anomalous = (
        breached_count >= config.quorum
        and weighted >= config.anomalous_score
        and operationally_relevant
        and not wrong_direction
    )

    score = 0.0
    if anomalous:
        magnitude = max(z_score, rolling_score, ewma_score, cusum_score)
        score = round(min(1.0, 0.6 * weighted + 0.4 * magnitude), 4)

    change_point_index = up_idx if direction == "up" else down_idx
    if change_point_index is None:
        change_point_index = up_idx if up_idx is not None else down_idx

    reasons: list[str] = []
    if not operationally_relevant:
        reasons.append(f"relative change {relative_change:+.1%} below operational floor")
    if wrong_direction:
        reasons.append(f"direction '{direction}' does not match expected '{anomaly_expected_direction}'")
    if breached_count < config.quorum:
        reasons.append(
            f"only {breached_count}/{len(detectors)} detectors breached (quorum {config.quorum})"
        )
    reason = (
        "; ".join(reasons)
        if reasons
        else f"{breached_count}/{len(detectors)} detectors breached, weighted score {weighted:.2f}"
    )

    return AnomalyVerdict(
        metric=metric,
        anomalous=anomalous,
        severity=severity_for(score, critical_override=critical_metric and anomalous and score > 0.75),
        direction=direction,
        score=score,
        current=round(current, 6),
        baseline=round(med, 6),
        absolute_change=round(absolute_change, 6),
        relative_change=round(relative_change, 6),
        change_point_index=change_point_index,
        detectors=detectors,
        reason=reason,
    )


@dataclass
class DetectionState:
    """Consecutive-breach debounce state (N of M) so one noisy sample cannot open an incident."""

    consecutive: int = 0
    required: int = 2
    severity: str = "none"

    def observe(self, verdict: AnomalyVerdict) -> bool:
        if verdict.anomalous:
            self.consecutive += 1
            self.severity = verdict.severity
        else:
            self.consecutive = 0
            self.severity = "none"
        return self.consecutive >= self.required

    @property
    def primed(self) -> bool:
        return self.consecutive >= self.required


def detect_anomaly_vs_baseline(
    metric: str,
    recent: Sequence[float],
    baseline: Sequence[float],
    *,
    config: DetectorConfig | None = None,
    anomaly_expected_direction: str = "up",
    critical_metric: bool = False,
) -> AnomalyVerdict:
    """Compare a recent window against a *separate, older* baseline window.

    This is the detector the platform actually uses, and the reason is worth stating plainly: a
    single-window detector averages the incident into its own baseline. During a slow ramp — the
    signature of a connection leak or a memory leak — the median of a 15-minute window drifts up
    with the incident, so ``relative_change`` collapses toward zero exactly when the incident is
    at its worst. Comparing a short recent window against an explicitly older reference window
    removes that failure mode, and it is what a human on-call does when they ask "is this worse
    than it was an hour ago?" rather than "is this worse than it is now?".

    If the metrics show no separation, this is equivalent to a MAD z-score against the baseline.
    """
    config = config or DetectorConfig()
    recent_arr = _as_array(recent)
    base_arr = _as_array(baseline)

    if recent_arr.size < config.min_samples or base_arr.size < 8:
        return AnomalyVerdict(
            metric=metric,
            anomalous=False,
            severity="none",
            direction="flat",
            score=0.0,
            current=float(recent_arr[-1]) if recent_arr.size else 0.0,
            baseline=float(np.median(base_arr)) if base_arr.size else 0.0,
            absolute_change=0.0,
            relative_change=0.0,
            change_point_index=None,
            detectors=[],
            reason=(
                f"insufficient data (recent {recent_arr.size} < {config.min_samples} or "
                f"baseline {base_arr.size} < 8)"
            ),
        )

    base_med = float(np.median(base_arr))
    base_mad = median_abs_deviation(base_arr)
    if base_mad > 1e-9:
        spread = base_mad / MAD_SCALE
    else:
        spread = float(np.std(base_arr)) or max(abs(base_med) * 0.02, 1e-6)

    # Reference the recent window by its own median, not its last sample: one noisy final
    # sample should not decide whether an incident opens.
    tail = recent_arr[-max(5, recent_arr.size // 2) :]
    recent_ref = float(np.median(tail))
    current = float(recent_arr[-1])

    z = (recent_ref - base_med) / max(spread, 1e-9)
    z_score = _saturating(z, config.mad_threshold)
    z_breached = abs(z) >= config.mad_threshold

    upper = base_med + config.rolling_threshold * spread
    lower = base_med - config.rolling_threshold * spread
    excess = max(0.0, current - upper) if current > upper else max(0.0, lower - current)
    rolling_breached = current > upper or current < lower
    rolling_score = _saturating(excess / max(spread, 1e-9), 1.0) if rolling_breached else 0.0

    e_z = ewma_residual_z(recent_arr, alpha=config.ewma_alpha)
    ewma_breached = abs(e_z) >= config.ewma_limit
    ewma_score = _saturating(e_z, config.ewma_limit)

    c_plus, c_minus, up_idx, down_idx = cusum(
        recent_arr, slack=config.cusum_slack, threshold=config.cusum_threshold, baseline=base_med
    )
    dominant = max(c_plus, c_minus)
    cusum_breached = up_idx is not None or down_idx is not None
    cusum_score = _saturating(dominant, config.cusum_threshold)

    absolute_change = current - base_med
    relative_change = absolute_change / (abs(base_med) if abs(base_med) > 1e-9 else 1.0)
    direction = "up" if absolute_change > 0 else ("down" if absolute_change < 0 else "flat")

    detectors = [
        DetectorVerdict(
            detector="robust_z",
            breached=z_breached,
            score=round(z_score, 4),
            statistic=round(z, 3),
            threshold=config.mad_threshold,
            message=(
                f"robust z={z:.2f} of the recent median {recent_ref:.4g} against the reference "
                f"baseline {base_med:.4g} (MAD-derived spread {spread:.4g})"
            ),
        ),
        DetectorVerdict(
            detector="rolling_baseline",
            breached=rolling_breached,
            score=round(rolling_score, 4),
            statistic=round(current, 4),
            threshold=round(upper, 4),
            message=f"current {current:.4g} vs reference band [{lower:.4g}, {upper:.4g}]",
        ),
        DetectorVerdict(
            detector="ewma",
            breached=ewma_breached,
            score=round(ewma_score, 4),
            statistic=round(e_z, 3),
            threshold=config.ewma_limit,
            message=f"EWMA residual z={e_z:.2f} — detects gradual ramps a static threshold misses",
        ),
        DetectorVerdict(
            detector="cusum",
            breached=cusum_breached,
            score=round(cusum_score, 4),
            statistic=round(dominant, 3),
            threshold=config.cusum_threshold,
            message=(
                f"CUSUM c+={c_plus:.2f} c-={c_minus:.2f} against the reference baseline — "
                f"change point at recent index {up_idx if up_idx is not None else down_idx}"
            ),
        ),
    ]

    breached_count = sum(1 for d in detectors if d.breached)
    weighted = sum(
        config.weights.get(d.detector, 0.0) * (1.0 if d.breached else 0.0) for d in detectors
    ) / max(sum(config.weights.values()), 1e-9)

    operationally_relevant = abs(relative_change) >= config.min_relative_change
    wrong_direction = (
        anomaly_expected_direction != "both"
        and direction != anomaly_expected_direction
        and direction != "flat"
    )

    anomalous = (
        breached_count >= config.quorum
        and weighted >= config.anomalous_score
        and operationally_relevant
        and not wrong_direction
    )

    score = 0.0
    if anomalous:
        magnitude = max(z_score, rolling_score, ewma_score, cusum_score)
        score = round(min(1.0, 0.6 * weighted + 0.4 * magnitude), 4)

    reasons: list[str] = []
    if not operationally_relevant:
        reasons.append(f"relative change {relative_change:+.1%} below the operational floor")
    if wrong_direction:
        reasons.append(f"direction '{direction}' does not match expected '{anomaly_expected_direction}'")
    if breached_count < config.quorum:
        reasons.append(
            f"only {breached_count}/{len(detectors)} detectors breached the quorum of {config.quorum}"
        )
    reason = (
        "; ".join(reasons)
        if reasons
        else (
            f"{breached_count}/{len(detectors)} detectors breached: recent median {recent_ref:.4g} vs "
            f"reference baseline {base_med:.4g} ({relative_change:+.0%})"
        )
    )

    return AnomalyVerdict(
        metric=metric,
        anomalous=anomalous,
        severity=severity_for(score, critical_override=critical_metric and anomalous and score > 0.78),
        direction=direction,
        score=score,
        current=round(current, 6),
        baseline=round(base_med, 6),
        absolute_change=round(absolute_change, 6),
        relative_change=round(relative_change, 6),
        change_point_index=up_idx if direction == "up" else (down_idx if down_idx is not None else up_idx),
        detectors=detectors,
        reason=reason,
    )


def correlate_with_change(
    *,
    metric_change_ts: str,
    change_ts: str,
    metric_name: str,
    change_label: str,
    max_lag_seconds: int = 900,
) -> dict[str, Any]:
    """Emit an explicitly non-causal relationship between a metric shift and a change.

    Returns one of ``preceded_by`` / ``correlated_with`` / ``unrelated`` with the observed lag.
    Build spec section 8: "Do not automatically claim causality."
    """
    from datetime import datetime

    def _parse(value: str) -> datetime | None:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None

    a, b = _parse(metric_change_ts), _parse(change_ts)
    if a is None or b is None:
        return {"relationship": "unknown", "lag_seconds": None, "statement": "unparseable timestamps"}

    lag = (a - b).total_seconds()
    if 0 <= lag <= max_lag_seconds:
        rel = "preceded_by"
        statement = (
            f"{change_label} preceded the {metric_name} change by {lag:.0f}s; "
            f"temporal proximity is consistent with the change contributing, "
            f"but this is not proof of causation."
        )
    elif abs(lag) <= max_lag_seconds:
        rel = "correlated_with"
        statement = (
            f"{change_label} occurred within {abs(lag):.0f}s of the {metric_name} change "
            f"(correlated_with, causality not established)."
        )
    else:
        rel = "unrelated"
        statement = (
            f"{change_label} is {abs(lag):.0f}s away from the {metric_name} change — "
            f"outside the correlation window."
        )
    return {
        "relationship": rel,
        "lag_seconds": round(lag, 1),
        "statement": statement,
        "metric": metric_name,
        "change": change_label,
        "max_lag_seconds": max_lag_seconds,
        "causal_claim": False,
    }
