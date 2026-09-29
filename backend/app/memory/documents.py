"""Rendering organizational experience into stuff worth remembering.

Build spec section 12 is explicit: *do not blindly copy every PostgreSQL record into
memory*. Only information with future reasoning value is retained. This module is the filter
— it decides what an experience document contains and, just as importantly, what it omits
(raw metric dumps, log lines, ids nobody will ever query by).

The rendered text is deliberately written as natural-language statements, because Hindsight
runs fact extraction over it. Prose with explicit outcomes extracts far better than JSON.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Sequence

from app.detection.similarity import IncidentFingerprint


@dataclass
class AttemptRecord:
    attempt_number: int
    action_code: str
    action_name: str = ""
    outcome: str = "unknown"
    verification_results: list[str] = field(default_factory=list)
    reason: str = ""

    def render(self) -> str:
        results = "; ".join(self.verification_results[:4])
        verdict = {
            "failed": "verification FAILED",
            "succeeded": "verified successful",
            "partial": "partially effective, did not resolve the incident",
            "blocked": "blocked by the safety gate and never executed",
        }.get(self.outcome, self.outcome)
        line = f"Attempt {self.attempt_number}: {self.action_code}"
        if self.action_name:
            line += f" ({self.action_name})"
        line += f" — {verdict}."
        if self.reason:
            line += f" Reason: {self.reason}"
        if results:
            line += f" Observed: {results}"
        return line


@dataclass
class IncidentExperience:
    incident_id: int
    title: str
    service: str
    environment: str
    severity: str
    detected_at: str
    trigger: str = ""
    symptoms: Sequence[str] = ()
    evidence: Sequence[str] = ()
    timeline: Sequence[str] = ()
    hypotheses: Sequence[str] = ()
    ruled_out: Sequence[str] = ()
    root_cause_category: str = "unknown"
    root_cause: str = ""
    attempts: Sequence[AttemptRecord] = ()
    successful_remediation: str = ""
    verification: str = ""
    business_impact: str = ""
    lessons: Sequence[str] = ()
    preventive_actions: Sequence[str] = ()
    deployment_relationship: str = ""
    service_dependencies: Sequence[str] = ()
    mttr_seconds: float | None = None

    def render(self) -> str:
        """Render the experience block that is handed to Hindsight's retain()."""
        lines: list[str] = []
        lines.append(
            f"INCIDENT #{self.incident_id} — {self.title}\n"
            f"Service {self.service} in {self.environment} environment, severity {self.severity}, "
            f"detected {self.detected_at}."
        )
        if self.mttr_seconds is not None:
            lines.append(f"Time to resolution: {self.mttr_seconds / 60:.1f} minutes.")

        if self.trigger:
            lines.append(f"Trigger: {self.trigger}")
        if self.deployment_relationship:
            lines.append(f"Deployment relationship: {self.deployment_relationship}")
        if self.service_dependencies:
            lines.append(f"Affected dependencies: {', '.join(self.service_dependencies)}")

        if self.symptoms:
            lines.append("Symptoms: " + "; ".join(self.symptoms))
        if self.evidence:
            lines.append("Evidence:")
            lines.extend(f"  - {item}" for item in self.evidence[:10])
        if self.timeline:
            lines.append("Timeline:")
            lines.extend(f"  - {item}" for item in self.timeline[:10])
        if self.hypotheses:
            lines.append("Hypotheses considered:")
            lines.extend(f"  - {item}" for item in self.hypotheses[:8])
        if self.ruled_out:
            lines.append("Alternative explanations ruled out: " + "; ".join(self.ruled_out[:6]))

        lines.append(f"Root cause: {self.root_cause} (category: {self.root_cause_category})")

        if self.attempts:
            lines.append("Remediation history:")
            lines.extend(f"  - {attempt.render()}" for attempt in self.attempts)

        if self.successful_remediation:
            lines.append(f"Successful remediation: {self.successful_remediation}")
        else:
            lines.append("Successful remediation: none recorded — the incident is not yet resolved.")

        if self.verification:
            lines.append(f"Verification: {self.verification}")
        if self.business_impact:
            lines.append(f"Business impact: {self.business_impact}")
        if self.lessons:
            lines.append("Lessons: " + "; ".join(self.lessons))
        if self.preventive_actions:
            lines.append("Preventive actions: " + "; ".join(self.preventive_actions))

        return "\n".join(lines)

    def metadata(self, *, scope: str, fingerprint: IncidentFingerprint | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "scope": scope,
            "incident_id": self.incident_id,
            "service": self.service,
            "environment": self.environment,
            "severity": self.severity,
            "root_cause_category": self.root_cause_category,
            "outcome": self.successful_remediation and "resolved" or "unresolved",
        }
        if fingerprint:
            payload["service_family"] = fingerprint.service_family
            payload["environment_kind"] = fingerprint.environment_kind
            payload["deployment_related"] = fingerprint.deployment_related
        return payload


def render_attempt_memory(
    *,
    incident_id: int,
    service: str,
    environment: str,
    root_cause_category: str,
    attempt: AttemptRecord,
) -> str:
    """A focused, immediately-retained memory for a single remediation attempt.

    Retained the moment the verdict lands rather than at the end of the incident, so failure
    memory exists even if the incident is still open — which is exactly the case the demo
    needs to be able to show.
    """
    verdict = {
        "failed": "Verification failed, so the incident was not resolved.",
        "succeeded": "Verification succeeded and the incident was resolved.",
        "partial": "It only partially helped and did not resolve the incident.",
        "blocked": "It was blocked by the safety gate and never ran.",
    }.get(attempt.outcome, f"Outcome: {attempt.outcome}.")

    lines = [
        f"REMEDIATION OUTCOME for incident #{incident_id} on {service} in {environment}.",
        f"Root cause category was {root_cause_category}.",
        f"I attempted {attempt.action_code}"
        + (f" ({attempt.action_name})" if attempt.action_name else "")
        + f" as attempt number {attempt.attempt_number}.",
        verdict,
    ]
    if attempt.reason:
        lines.append(f"Reason: {attempt.reason}")
    if attempt.verification_results:
        lines.append("Verification observed: " + "; ".join(attempt.verification_results[:5]))
    return "\n".join(lines)


def render_pattern_insight(
    *,
    root_cause_category: str,
    occurrence_count: int,
    window_days: int,
    services: Sequence[str],
    failed_actions: Sequence[str],
    successful_actions: Sequence[str],
    statement: str,
) -> str:
    """Knowledge-level memory for a recurring pattern (build spec sections 30 and 33)."""
    lines = [
        f"ORGANIZATIONAL PATTERN OBSERVED: {statement}",
        f"The pattern '{root_cause_category}' occurred {occurrence_count} time(s) in {window_days} days.",
    ]
    if services:
        lines.append("Services affected by this pattern: " + ", ".join(services[:8]))
    if failed_actions:
        lines.append(
            "Remediation actions that have repeatedly failed for this pattern: "
            + ", ".join(failed_actions)
        )
    if successful_actions:
        lines.append(
            "Remediation actions that have worked for this pattern: " + ", ".join(successful_actions)
        )
    return "\n".join(lines)


def build_recall_query(*, fingerprint: IncidentFingerprint, extra: Sequence[str] = ()) -> str:
    """Turn a fingerprint into the natural-language query used for recall.

    Hindsight's query drives all four retrieval arms at once (embedding, BM25 tokenisation,
    graph seeding, temporal parsing), so the query deliberately mixes three registers:
    the operational concept, the exact technical vocabulary, and the root-cause taxonomy.
    """
    parts: list[str] = []
    parts.append(
        f"How have we previously resolved incidents on {fingerprint.service} in the "
        f"{fingerprint.environment} environment?"
    )
    if fingerprint.symptom:
        parts.append(f"Symptoms were: {fingerprint.symptom}")
    if fingerprint.symptoms:
        parts.append("Also observed: " + "; ".join(fingerprint.symptoms[:4]))
    if fingerprint.root_cause_category and fingerprint.root_cause_category != "unknown":
        parts.append(f"Root cause category: {fingerprint.root_cause_category}")
    if fingerprint.metric_pattern:
        metrics = ", ".join(list(fingerprint.metric_pattern)[:6])
        parts.append(f"Anomalous metrics: {metrics}")
    if fingerprint.deployment_related:
        parts.append("A recent deployment is suspected to be related.")
    else:
        parts.append("No recent deployment was identified.")
    parts.append(
        "Which remediation attempts failed verification, and which remediation was verified "
        "to resolve it?"
    )
    if extra:
        parts.extend(extra)
    return " ".join(parts)


def digest(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def experience_to_dict(experience: IncidentExperience) -> dict[str, Any]:
    payload = asdict(experience)
    payload["attempts"] = [asdict(a) for a in experience.attempts]
    return payload
