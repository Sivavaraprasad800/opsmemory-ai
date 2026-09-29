"""Connecting a real project to the platform.

Everything the platform knows about a system arrives through one narrow seam: the
``metric_samples`` and ``log_events`` tables. Detection windows, baselines, incident
fingerprints, verification comparisons and the AI's evidence tools all query those two tables,
and none of them can tell whether a row came from the simulator or from a live service.

That is the whole reason connecting a real project does not need a second detector. It needs a
writer, with enough validation that a misconfigured connector fails loudly instead of quietly
filling the database with garbage that looks like an production incident.
"""

from app.ingest.service import (
    IngestOutcome,
    Scope,
    ensure_scope,
    ingest_logs,
    ingest_metrics,
    ingestion_overview,
    latest_gauges,
    parse_timestamp,
)

__all__ = [
    "IngestOutcome",
    "Scope",
    "ensure_scope",
    "ingest_logs",
    "ingest_metrics",
    "ingestion_overview",
    "latest_gauges",
    "parse_timestamp",
]
