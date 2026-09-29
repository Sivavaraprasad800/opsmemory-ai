"""Log pipeline: normalise -> parse -> template -> signature -> frequency -> spike.

Build spec section 9/10: deterministic parsing first, AI only when semantic interpretation is
genuinely required, and *never* send raw logs to the model. This module is the reason the
platform can summarise a 10 000-line burst in a couple of milliseconds and hand the LLM a
ranked table of at most ~20 error signatures.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Iterable, Sequence

# ---------------------------------------------------------------------------------------
# 1. Variable masking - the core of signature extraction
# ---------------------------------------------------------------------------------------
_MASK_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"), "<UUID>"),
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"), "<IP>"),
    (re.compile(r"\b[0-9a-fA-F]{32,64}\b"), "<HASH>"),
    (re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"), "<EMAIL>"),
    (re.compile(r"\b(?:[a-zA-Z0-9-]+\.)+[a-zA-Z]{2,}(?::\d+)?\b"), "<HOST>"),
    (re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?\b"), "<TIMESTAMP>"),
    (re.compile(r"\b(?:localhost|127\.0\.0\.1):\d+\b"), "<ADDR>"),
    (re.compile(r"\b\d+(?:\.\d+)?(?:ms|s|us|µs|ns)\b", re.IGNORECASE), "<DURATION>"),
    (re.compile(r"\b\d+(?:\.\d+)?(?:kb|mb|gb|b)\b", re.IGNORECASE), "<SIZE>"),
    (re.compile(r"\buser[_ =:]?\d+\b", re.IGNORECASE), "user <ID>"),
    (re.compile(r"\b(?:id|pid|port|conn|conn_id|txn|request|req|trace)[_ =:]+=?[A-Za-z]?\d+\b", re.IGNORECASE), "<ID>"),
    # Any `key=<number>` pair: keep the key, mask the value.
    #
    # This rule is load-bearing for real traffic. A service logs `active=44`, then `active=50`,
    # then `pending=7`; without this, each distinct number is a distinct template, so one failure
    # mode forks into as many signatures as it has varying values. No signature ever accumulates
    # enough count to look like a spike, and log-based detection silently stops working - while
    # every dashboard still reports "no new signatures", which reads exactly like good news.
    # The key is deliberately preserved so `active=<NUM>` and `max=<NUM>` stay distinguishable.
    (re.compile(r"\b([A-Za-z_][A-Za-z0-9_.-]*)\s*[=:]\s*-?\d+(?:\.\d+)?\b"), r"\1=<NUM>"),
    (re.compile(r"\b\d{3,}\b"), "<NUM>"),
    (re.compile(r"\b[0-9a-f]{7,40}\b"), "<SHA>"),
)

_LEVEL_RE = re.compile(r"\b(TRACE|DEBUG|INFO|NOTICE|WARN|WARNING|ERROR|ERR|CRITICAL|FATAL|PANIC)\b")
_LEVEL_ALIASES = {"ERR": "ERROR", "WARNING": "WARN", "NOTICE": "INFO", "CRITICAL": "FATAL", "PANIC": "FATAL"}

SEVERITY_BY_LEVEL = {
    "TRACE": "low",
    "DEBUG": "low",
    "INFO": "low",
    "WARN": "medium",
    "ERROR": "high",
    "FATAL": "critical",
}

ERROR_LEVELS = {"ERROR", "FATAL", "WARN"}


def normalize_message(message: str) -> str:
    """Replace variable values with typed placeholders, preserving the message shape."""
    text = message.strip()
    for pattern, replacement in _MASK_RULES:
        text = pattern.sub(replacement, text)
    # Collapse whitespace and repeated placeholders so ordering artefacts do not fork templates.
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"(?:<NUM> ?){2,}", "<NUM> ", text)
    return text.strip()


def classify_level(message: str, default: str = "INFO") -> str:
    if not message:
        return default
    match = _LEVEL_RE.search(message[:80])
    if match:
        raw = match.group(1).upper()
        return _LEVEL_ALIASES.get(raw, raw)
    lowered = message.lower()
    if any(token in lowered for token in ("exception", "traceback", "failed", "failure", "error")):
        return "ERROR"
    if any(token in lowered for token in ("timeout", "retry", "degraded", "slow")):
        return "WARN"
    return default


def signature_hash(template: str) -> str:
    return hashlib.sha1(template.encode("utf-8")).hexdigest()[:16]


def build_signature(message: str) -> tuple[str, str, str]:
    """Return ``(hash, template, level)`` for one raw log line."""
    template = normalize_message(message)
    return signature_hash(template), template, classify_level(message)


# ---------------------------------------------------------------------------------------
# 2. Aggregation
# ---------------------------------------------------------------------------------------
@dataclass
class SignatureStat:
    hash: str
    template: str
    level: str
    service_id: int | None
    service_name: str
    count: int = 0
    baseline_rate: float = 0.0
    observed_rate: float = 0.0
    spike_ratio: float = 0.0
    severity: str = "low"
    first_seen: str | None = None
    last_seen: str | None = None
    sample: str = ""
    is_new: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def aggregate_signatures(
    events: Sequence[dict[str, Any]],
    *,
    window_seconds: float | None = None,
    baseline: dict[str, float] | None = None,
    top_n: int = 25,
) -> list[SignatureStat]:
    """Group log events into ranked error signatures.

    ``events`` items: ``{"message", "level"?, "service_id"?, "service_name"?, "ts"?}``.
    ``baseline`` maps signature hash -> historical rate (per second). When a signature is
    absent from the baseline it is reported as ``is_new`` — which for our purposes is the
    single most interesting signal, because brand-new error shapes are what appear right
    after a bad deploy.
    """
    buckets: dict[tuple[str, int | None], SignatureStat] = {}
    baseline = baseline or {}
    span_start: datetime | None = None
    span_end: datetime | None = None

    for event in events:
        message = event.get("message") or ""
        if not message:
            continue
        sig, template, level = build_signature(message)
        if event.get("level"):
            level = str(event["level"]).upper()
        service_id = event.get("service_id")
        service_name = event.get("service_name") or (f"service-{service_id}" if service_id else "unknown")
        key = (sig, service_id)

        stat = buckets.get(key)
        if stat is None:
            stat = SignatureStat(
                hash=sig,
                template=template,
                level=level,
                service_id=service_id,
                service_name=service_name,
                sample=message[:400],
                first_seen=_iso(event.get("ts")),
                is_new=sig not in baseline,
            )
            buckets[key] = stat

        stat.count += 1
        stat.last_seen = _iso(event.get("ts")) or stat.last_seen
        if SEVERITY_BY_LEVEL.get(level, "low") == "critical":
            stat.level = "FATAL"

        ts = event.get("ts")
        if isinstance(ts, datetime):
            span_start = ts if span_start is None or ts < span_start else span_start
            span_end = ts if span_end is None or ts > span_end else span_end

    span = window_seconds
    if span is None and span_start and span_end:
        span = max((span_end - span_start).total_seconds(), 1.0)
    span = span or 1.0

    for stat in buckets.values():
        stat.observed_rate = round(stat.count / span, 4)
        stat.baseline_rate = round(float(baseline.get(stat.hash, 0.0)), 4)
        if stat.baseline_rate > 0:
            stat.spike_ratio = round(stat.observed_rate / stat.baseline_rate, 3)
        elif stat.count > 0:
            stat.spike_ratio = float(stat.count)  # brand new signature: no baseline to divide by
        stat.severity = _severity_for_stat(stat)

    ranked = sorted(
        buckets.values(),
        key=lambda s: (
            SEVERITY_BY_LEVEL.get(s.level, "low") == "critical",
            s.is_new,
            s.spike_ratio,
            s.count,
        ),
        reverse=True,
    )
    return ranked[:top_n]


def _severity_for_stat(stat: SignatureStat) -> str:
    base = SEVERITY_BY_LEVEL.get(stat.level, "low")
    if base == "critical":
        return "critical"
    if stat.is_new and stat.count >= 5:
        return "high" if base in {"high", "medium"} else "medium"
    if stat.spike_ratio >= 8 and stat.count >= 5:
        return "high"
    if stat.spike_ratio >= 3 and stat.count >= 3:
        return "medium"
    return base


def detect_signature_spikes(
    current: Sequence[SignatureStat], *, min_count: int = 5, min_ratio: float = 3.0
) -> list[SignatureStat]:
    """Signatures whose observed rate materially exceeds baseline (or that are brand new)."""
    spikes: list[SignatureStat] = []
    for stat in current:
        if stat.count < min_count:
            continue
        if stat.is_new or stat.spike_ratio >= min_ratio:
            spikes.append(stat)
    return spikes


def signatures_fingerprint(signatures: Iterable[SignatureStat] | Iterable[str]) -> list[str]:
    """Stable, ordered signature-hash list used by the incident fingerprint."""
    hashes: list[str] = []
    for item in signatures:
        hashes.append(item.hash if isinstance(item, SignatureStat) else str(item))
    return sorted(set(hashes))


def dominant_signature(signatures: Sequence[SignatureStat]) -> SignatureStat | None:
    for stat in signatures:
        if stat.level in {"ERROR", "FATAL"}:
            return stat
    return signatures[0] if signatures else None


@dataclass
class LogSummary:
    """Compact, LLM-safe view of a log burst."""

    total_events: int
    error_events: int
    signatures: list[SignatureStat] = field(default_factory=list)
    spikes: list[SignatureStat] = field(default_factory=list)
    top_template: str = ""

    def to_dict(self, *, max_signatures: int = 12) -> dict[str, Any]:
        return {
            "total_events": self.total_events,
            "error_events": self.error_events,
            "signature_count": len(self.signatures),
            "top_template": self.top_template,
            "spikes": [s.to_dict() for s in self.spikes[:max_signatures]],
            "signatures": [s.to_dict() for s in self.signatures[:max_signatures]],
        }

    def compact_lines(self, limit: int = 8) -> list[str]:
        lines = [
            f"[{s.severity}] x{s.count} (baseline {s.baseline_rate}/s, ratio {s.spike_ratio:g}) {s.template}"
            for s in self.signatures[:limit]
        ]
        return lines


def build_log_summary(
    events: Sequence[dict[str, Any]], *, baseline: dict[str, float] | None = None, top_n: int = 25
) -> LogSummary:
    signatures = aggregate_signatures(events, baseline=baseline, top_n=top_n)
    error_events = 0
    for event in events:
        level = str(event.get("level") or classify_level(event.get("message", "")))
        if level.upper() in ERROR_LEVELS:
            error_events += 1
    top = dominant_signature(signatures)
    return LogSummary(
        total_events=len(events),
        error_events=error_events,
        signatures=signatures,
        spikes=detect_signature_spikes(signatures),
        top_template=top.template if top else "",
    )
