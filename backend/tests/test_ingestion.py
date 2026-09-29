"""Connecting a real project: ingestion, validation, and the connector.

These are the tests that decide whether "point it at your project" is a real claim or a slide.
Two properties matter most:

* **Telemetry lands where the detector looks.** An ingested line has to aggregate into the same
  signatures the simulator produces, otherwise connecting a project would store data that never
  opens an incident.
* **Bad telemetry is refused.** A wrong clock or a runaway connector is the likeliest real-world
  failure, and every version of it is silent in a permissive schema: a future timestamp is simply
  never inside any window, and a NaN poisons every median it touches.
"""

from __future__ import annotations

import itertools
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.core.config import settings
from app.database.models import LogEvent, MetricSample, Service
from app.detection.logs import aggregate_signatures
from app.ingest import ensure_scope, ingest_logs, ingest_metrics, ingestion_overview, parse_timestamp
from app.ingest.service import latest_gauges, WATCHED_METRIC_NAMES
from app.sim.engine import sim_now

pytestmark = pytest.mark.usefixtures("memory_service")


# ---------------------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------------------
@pytest.fixture()
def restore_settings():
    """Snapshot and restore the settings attributes these tests mutate.

    ``settings`` is a process-wide singleton, so a test that left it changed would silently alter
    the behaviour of every test that ran after it. That is exactly the class of coupling that
    makes a suite untrustworthy.
    """
    snapshot = {
        "clock_mode": settings.clock_mode,
        "ingest_autoregister": settings.ingest_autoregister,
        "ingest_token": settings.ingest_token,
        "ingest_max_batch": settings.ingest_max_batch,
        "ingest_services_per_minute": settings.ingest_services_per_minute,
        "ingest_max_skew_seconds": settings.ingest_max_skew_seconds,
    }
    yield settings
    for key, value in snapshot.items():
        setattr(settings, key, value)


@pytest.fixture()
def svc():
    """A service name unique to each test.

    The test database persists for the whole session, so a shared service name would let an
    earlier test's telemetry become the "latest" value in a later test's assertion - and the
    failure would look like a bug in the code rather than in the test.
    """
    counter = itertools.count()
    return lambda prefix="connected": f"{prefix}-{next(counter)}"


def _scope(db, service: str, **overrides):
    payload = {"service": service, "environment": "production", "project": "connected-suite"}
    payload.update(overrides)
    return ensure_scope(db, **payload)


# ---------------------------------------------------------------------------------------
# scope resolution and auto-registration
# ---------------------------------------------------------------------------------------
def test_unknown_project_is_registered_on_first_contact(db, restore_settings, svc):
    """A connector must be able to connect a project the catalogue has never heard of."""
    scope = _scope(db, svc("unregistered-api"))
    assert scope.project.slug == "connected-suite"
    assert scope.environment.is_production is True
    assert any(item.startswith("service:") for item in scope.created)


def test_registration_is_idempotent(db, restore_settings, svc):
    """Reconnecting must reuse the rows rather than duplicating them on every restart."""
    name = svc("idempotent-api")
    first = _scope(db, name)
    second = _scope(db, name)
    assert first.service.id == second.service.id
    assert second.created == []
    count = db.scalar(select(func.count()).select_from(Service).where(Service.name == name))
    assert count == 1


def test_non_production_environment_is_not_marked_production(db, restore_settings, svc):
    scope = _scope(db, svc(), environment="staging")
    assert scope.environment.is_production is False
    assert scope.environment.kind == "staging"


def test_blank_service_name_is_refused(db, restore_settings):
    with pytest.raises(ValueError):
        ensure_scope(db, service="   ")


def test_registration_can_be_disabled(db, restore_settings):
    """With auto-registration off, an unknown service is an error rather than a silent create."""
    restore_settings.ingest_autoregister = False
    with pytest.raises(ValueError, match="INGEST_AUTOREGISTER"):
        ensure_scope(
            db, service="never-registered-service", project="never-registered-project"
        )


# ---------------------------------------------------------------------------------------
# metric ingestion
# ---------------------------------------------------------------------------------------
def test_metrics_are_stored_and_counted(db, restore_settings, svc):
    name = svc("metrics-api")
    scope = _scope(db, name)
    outcome = ingest_metrics(
        db,
        scope=scope,
        samples=[
            {"name": "error_rate", "value": 0.031},
            {"name": "latency_ms", "value": 1840.5},
            {"name": "cpu_pct", "value": 71.2},
        ],
    )
    db.commit()
    assert outcome.accepted == 3
    assert outcome.rejected == 0
    stored = db.scalar(
        select(func.count())
        .select_from(MetricSample)
        .where(MetricSample.service_id == scope.service.id)
    )
    assert stored == 3


def test_metric_outside_the_watched_set_is_flagged(db, restore_settings, svc):
    """Sending metrics detection ignores is the most confusing possible first experience."""
    scope = _scope(db, svc("unwatched-api"))
    outcome = ingest_metrics(
        db,
        scope=scope,
        samples=[{"name": "error_rate", "value": 0.02}, {"name": "disk_io_wait", "value": 12.0}],
    )
    db.commit()
    assert "disk_io_wait" in outcome.unwatched_metrics
    assert "error_rate" not in outcome.unwatched_metrics
    assert "error_rate" in WATCHED_METRIC_NAMES


def test_future_timestamp_is_rejected_not_silently_dropped(db, restore_settings, svc):
    """A wrong sender clock must be reported: a rejected sample otherwise leaves no trace."""
    scope = _scope(db, svc("skewed-api"))
    future = (sim_now() + timedelta(hours=2)).isoformat()
    outcome = ingest_metrics(
        db, scope=scope, samples=[{"name": "error_rate", "value": 0.5, "ts": future}]
    )
    db.commit()
    assert outcome.accepted == 0
    assert outcome.rejected == 1
    assert "future" in outcome.issues[0]


def test_non_finite_values_are_refused(db, restore_settings, svc):
    scope = _scope(db, svc("nan-api"))
    outcome = ingest_metrics(
        db,
        scope=scope,
        samples=[
            {"name": "error_rate", "value": float("nan")},
            {"name": "latency_ms", "value": float("inf")},
        ],
    )
    db.commit()
    assert outcome.accepted == 0
    assert outcome.rejected == 2


def test_missing_metric_name_is_refused(db, restore_settings, svc):
    scope = _scope(db, svc("nameless-api"))
    outcome = ingest_metrics(db, scope=scope, samples=[{"name": "", "value": 1.0}])
    db.commit()
    assert outcome.rejected == 1
    assert "missing metric name" in outcome.issues[0]


def test_non_numeric_value_is_refused(db, restore_settings, svc):
    scope = _scope(db, svc("stringy-api"))
    outcome = ingest_metrics(
        db, scope=scope, samples=[{"name": "error_rate", "value": "not-a-number"}]
    )
    db.commit()
    assert outcome.rejected == 1


def test_oversized_batch_is_refused(db, restore_settings, svc):
    restore_settings.ingest_max_batch = 3
    scope = _scope(db, svc("huge-api"))
    with pytest.raises(ValueError, match="INGEST_MAX_BATCH"):
        ingest_metrics(
            db, scope=scope, samples=[{"name": "error_rate", "value": 0.1} for _ in range(5)]
        )


def test_rate_ceiling_stops_a_runaway_connector(db, restore_settings, svc):
    """A loop that floods the API must be refused, not turned into a fabricated incident."""
    restore_settings.ingest_services_per_minute = 10
    scope = _scope(db, svc("flooding-api"))
    ingest_metrics(
        db, scope=scope, samples=[{"name": "error_rate", "value": 0.1} for _ in range(8)]
    )
    db.commit()
    with pytest.raises(ValueError, match="rate ceiling"):
        ingest_metrics(
            db, scope=scope, samples=[{"name": "error_rate", "value": 0.1} for _ in range(8)]
        )


def test_timestamp_parsing_accepts_iso_epoch_and_milliseconds():
    iso = parse_timestamp("2026-09-29T14:02:00Z")
    assert iso is not None and iso.year == 2026
    epoch = parse_timestamp(1_793_000_000)
    millis = parse_timestamp(1_793_000_000_000)
    assert epoch is not None and millis is not None
    # The milliseconds form is the common mistake, and it must land on the same instant.
    assert abs((epoch - millis).total_seconds()) < 1
    assert parse_timestamp("not a date") is None
    assert parse_timestamp(None) is None


def test_parsed_timestamps_are_timezone_aware():
    """Naive values here would raise only when compared against a window boundary."""
    for value in ("2026-09-29T14:02:00Z", "2026-09-29T14:02:00", 1_793_000_000):
        parsed = parse_timestamp(value)
        assert parsed is not None
        assert parsed.tzinfo is not None, f"{value} parsed to a naive datetime"


# ---------------------------------------------------------------------------------------
# log ingestion
# ---------------------------------------------------------------------------------------
def test_logs_are_stored_with_signature_and_level(db, restore_settings, svc):
    scope = _scope(db, svc("logs-api"))
    outcome = ingest_logs(
        db,
        scope=scope,
        lines=[
            {"message": "connection pool exhausted active=50 max=50", "level": "ERROR"},
            {"message": "healthcheck ok latency=12ms", "level": "INFO"},
        ],
    )
    db.commit()
    assert outcome.accepted == 2
    assert outcome.new_signatures == 2
    rows = db.scalars(select(LogEvent).where(LogEvent.service_id == scope.service.id)).all()
    assert {row.level for row in rows} == {"ERROR", "INFO"}
    assert all(row.signature_hash for row in rows)


def test_level_is_inferred_when_the_sender_omits_it(db, restore_settings, svc):
    scope = _scope(db, svc("inferred-api"))
    ingest_logs(
        db,
        scope=scope,
        lines=[
            {"message": "java.sql.SQLTransientConnectionException: pool timed out"},
            {"message": "GET /v1/orders 200 duration=42ms"},
        ],
    )
    db.commit()
    levels = {
        row.level
        for row in db.scalars(select(LogEvent).where(LogEvent.service_id == scope.service.id)).all()
    }
    assert "ERROR" in levels and "INFO" in levels


def test_sender_level_aliases_are_normalised(db, restore_settings, svc):
    """Every stack has its own vocabulary for severity; they must land in the same column."""
    scope = _scope(db, svc("aliases-api"))
    ingest_logs(
        db,
        scope=scope,
        lines=[
            {"message": "first", "level": "warning"},
            {"message": "second", "level": "critical"},
            {"message": "third", "level": "err"},
        ],
    )
    db.commit()
    levels = {
        row.level
        for row in db.scalars(select(LogEvent).where(LogEvent.service_id == scope.service.id)).all()
    }
    assert levels == {"WARN", "FATAL", "ERROR"}


def test_empty_log_message_is_refused(db, restore_settings, svc):
    scope = _scope(db, svc("empty-api"))
    outcome = ingest_logs(db, scope=scope, lines=[{"message": "   "}])
    db.commit()
    assert outcome.rejected == 1


def test_ingested_logs_aggregate_into_detection_signatures(db, restore_settings, svc):
    """The property that makes connecting a project meaningful.

    Without this, a connected project's errors would be stored but never contribute to an
    incident, and the whole pipeline would quietly do nothing - the worst possible failure mode,
    because it looks like success.

    Every line carries a different high-cardinality request id, which is what real logs look like
    and what would otherwise fork this single failure mode into twelve unrelated signatures.
    """
    scope = _scope(db, svc("signatures-api"))
    ingest_logs(
        db,
        scope=scope,
        lines=[
            {"message": f"connection pool exhausted active={40 + index} max=50 request_id={index}"}
            for index in range(12)
        ],
    )
    db.commit()
    rows = db.scalars(select(LogEvent).where(LogEvent.service_id == scope.service.id)).all()
    payload = [
        {
            "id": row.id,
            "ts": row.ts.isoformat(),
            "level": row.level,
            "message": row.message,
            "signature_hash": row.signature_hash,
            "service_name": scope.service.name,
        }
        for row in rows
    ]
    signatures = aggregate_signatures(payload, window_seconds=300, top_n=10)
    assert signatures, "ingested logs produced no signatures at all"
    # Every line must collapse into one template: the varying request_id is masked, so twelve
    # near-identical errors are one signature rather than twelve.
    assert signatures[0].count == 12


# ---------------------------------------------------------------------------------------
# status and read model
# ---------------------------------------------------------------------------------------
def test_latest_gauges_reports_only_ingested_values(db, restore_settings, svc):
    name = svc("gauges-api")
    scope = _scope(db, name)
    ingest_metrics(
        db,
        scope=scope,
        samples=[{"name": "error_rate", "value": 0.017}, {"name": "latency_ms", "value": 210.0}],
    )
    db.commit()
    gauges = latest_gauges(db, name)
    assert gauges["error_rate"] == pytest.approx(0.017)
    assert gauges["latency_ms"] == pytest.approx(210.0)


def test_latest_gauges_is_empty_for_a_service_that_never_reported(db, restore_settings, svc):
    """An empty gauge must be empty, not a plausible invention."""
    assert latest_gauges(db, svc("silent-api")) == {}


def test_overview_reports_arrivals_per_service(db, restore_settings, svc):
    name = svc("arrivals-api")
    scope = _scope(db, name)
    ingest_metrics(db, scope=scope, samples=[{"name": "error_rate", "value": 0.04}])
    ingest_logs(db, scope=scope, lines=[{"message": "boom: upstream failed", "level": "ERROR"}])
    db.commit()
    overview = ingestion_overview(db)
    entry = next(item for item in overview["services"] if item["service"] == name)
    assert entry["metrics_15m"] >= 1
    assert entry["logs_15m"] >= 1
    assert entry["errors_15m"] >= 1
    assert entry["seconds_since_last"] is not None


# ---------------------------------------------------------------------------------------
# clock mode
# ---------------------------------------------------------------------------------------
def test_real_clock_mode_reads_wall_time(restore_settings):
    """Real-clock mode is what binds detection windows to a live project's timestamps."""
    restore_settings.clock_mode = "real"
    guard = datetime.now(timezone.utc)
    now = sim_now()
    assert now.tzinfo is not None, "sim_now() must stay timezone-aware in real mode"
    assert abs((now - guard).total_seconds()) < 5


def test_clock_mode_is_reported(restore_settings):
    restore_settings.clock_mode = "real"
    assert settings.real_clock is True
    restore_settings.clock_mode = "simulated"
    assert settings.real_clock is False


# ---------------------------------------------------------------------------------------
# the connector (standard library only, so it is tested directly)
# ---------------------------------------------------------------------------------------
def test_connector_infers_level_from_text():
    from app.connectors.tail_logs import detect_level

    assert detect_level("2026-09-29 ERROR pool exhausted") == "ERROR"
    assert detect_level("Traceback (most recent call last):") == "ERROR"
    assert detect_level("FATAL: out of memory") == "FATAL"
    assert detect_level("WARNING: slow query") == "WARN"
    assert detect_level("GET /v1/orders 200 duration=42ms") == "INFO"


def test_connector_batcher_flushes_on_size():
    from app.connectors.tail_logs import Batcher

    batch = Batcher(max_lines=3, flush_seconds=999)
    for index in range(3):
        batch.add(f"line {index}")
    assert batch.due() is True
    taken = batch.take()
    assert len(taken) == 3
    assert taken[0]["message"] == "line 0"
    assert batch.due() is False


def test_connector_batcher_flushes_on_time():
    from app.connectors.tail_logs import Batcher

    batch = Batcher(max_lines=100, flush_seconds=0.0)
    batch.add("one line")
    assert batch.due() is True


def test_connector_tailer_reads_only_new_lines(tmp_path: Path):
    from app.connectors.tail_logs import Tailer

    log = tmp_path / "app.log"
    log.write_text("old line 1\nold line 2\n", encoding="utf-8")

    follower = Tailer(path=str(log), from_start=False)
    follower.prime()
    assert follower.read_new_lines() == []

    with log.open("a", encoding="utf-8") as handle:
        handle.write("new line 3\n")
    assert follower.read_new_lines() == ["new line 3"]
    # Reading twice must not re-send the same line: an at-least-once connector turns one error
    # into a spike, and a spike into a fabricated incident.
    assert follower.read_new_lines() == []


def test_connector_tailer_handles_from_start(tmp_path: Path):
    from app.connectors.tail_logs import Tailer

    log = tmp_path / "app.log"
    log.write_text("first\nsecond\n", encoding="utf-8")

    follower = Tailer(path=str(log), from_start=True)
    follower.prime()
    assert follower.read_new_lines() == ["first", "second"]
    assert follower.read_new_lines() == []


def test_connector_tailer_handles_in_place_truncation(tmp_path: Path):
    """copy-truncate rotation leaves the cursor past the end of the file.

    Written as bytes on purpose: text mode translates ``\n`` to ``\r\n`` on Windows, which
    changes the file length and would quietly turn this scenario into a different one.
    """
    from app.connectors.tail_logs import Tailer

    log = tmp_path / "app.log"
    log.write_bytes(b"first\nsecond\n")

    follower = Tailer(path=str(log), from_start=True)
    follower.prime()
    assert follower.read_new_lines() == ["first", "second"]

    log.write_bytes(b"after\n")
    assert follower.read_new_lines() == ["after"]


def test_connector_tailer_handles_rename_rotation(tmp_path: Path):
    """A rotated-in new file can be *larger* than the old cursor.

    Size alone cannot detect this, and the lines that get skipped are the ones written while the
    incident that caused the rotation was happening.
    """
    from app.connectors.tail_logs import Tailer

    log = tmp_path / "app.log"
    log.write_bytes(b"first\n")

    follower = Tailer(path=str(log), from_start=False)
    follower.prime()
    assert follower.read_new_lines() == []

    # logrotate-style: move the current file aside, then start a longer new one in its place
    log.rename(tmp_path / "app.log.1")
    log.write_bytes(b"ERROR the incident starts here\nand continues\n")

    assert follower.read_new_lines() == ["ERROR the incident starts here", "and continues"]


def test_connector_ignores_partial_trailing_writes(tmp_path: Path):
    from app.connectors.tail_logs import Tailer

    log = tmp_path / "app.log"
    log.write_text("complete line\nhalf a lin", encoding="utf-8")
    follower = Tailer(path=str(log), from_start=True)
    follower.prime()
    assert follower.read_new_lines() == ["complete line"]


def test_connector_tailer_handles_non_ascii(tmp_path: Path):
    """Logs from real services are not ASCII, and a byte cursor must not desynchronise."""
    from app.connectors.tail_logs import Tailer

    log = tmp_path / "app.log"
    log.write_text("café démarré\n", encoding="utf-8")
    follower = Tailer(path=str(log), from_start=True)
    follower.prime()
    assert follower.read_new_lines() == ["café démarré"]
    with log.open("a", encoding="utf-8") as handle:
        handle.write("érreur de connexion\n")
    assert follower.read_new_lines() == ["érreur de connexion"]


def test_connector_reads_metrics_jsonl(tmp_path: Path):
    from app.connectors.tail_logs import read_metrics_file

    feed = tmp_path / "metrics.jsonl"
    feed.write_text(
        json.dumps({"name": "error_rate", "value": 0.02})
        + "\n"
        + json.dumps({"name": "latency_ms", "value": 120})
        + "\n",
        encoding="utf-8",
    )
    cursor: dict[str, int] = {}
    samples = read_metrics_file(str(feed), cursor)
    assert [sample["name"] for sample in samples] == ["error_rate", "latency_ms"]
    # A second read returns nothing until more is appended.
    assert read_metrics_file(str(feed), cursor) == []


# ---------------------------------------------------------------------------------------
# the HTTP API
# ---------------------------------------------------------------------------------------
@pytest.fixture()
def ingest_client(user_tokens, memory_service):
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    client.headers.update({"Authorization": f"Bearer {user_tokens['sre']}"})
    return client


def test_api_registers_and_ingests(ingest_client, restore_settings, svc):
    name = svc("http-api")
    register = ingest_client.post(
        "/api/ingest/register",
        json={"service": name, "environment": "production", "tier": "critical"},
    )
    assert register.status_code == 200, register.text
    assert register.json()["registered"]["service"] == name

    metrics = ingest_client.post(
        "/api/ingest/metrics",
        json={
            "service": name,
            "environment": "production",
            "samples": [{"name": "error_rate", "value": 0.09}],
        },
    )
    assert metrics.status_code == 200, metrics.text
    assert metrics.json()["accepted"] == 1

    logs = ingest_client.post(
        "/api/ingest/logs",
        json={
            "service": name,
            "environment": "production",
            "lines": [{"message": "pool exhausted", "level": "ERROR"}],
        },
    )
    assert logs.status_code == 200, logs.text
    assert logs.json()["accepted"] == 1


def test_api_rejects_an_empty_batch(ingest_client, svc):
    response = ingest_client.post(
        "/api/ingest/logs",
        json={"service": svc("empty-batch-api"), "lines": []},
    )
    assert response.status_code == 422


def test_api_requires_the_connector_token_when_configured(ingest_client, restore_settings, svc):
    """With a token configured, an unsigned push must be refused rather than accepted."""
    restore_settings.ingest_token = "shared-secret"
    name = svc("tokened-api")

    unauthorised = ingest_client.post(
        "/api/ingest/logs",
        json={"service": name, "environment": "production", "lines": [{"message": "hello"}]},
        headers={"Authorization": "", "X-Ingest-Token": "wrong"},
    )
    assert unauthorised.status_code in {401, 403}

    authorised = ingest_client.post(
        "/api/ingest/logs",
        json={
            "service": name,
            "environment": "production",
            "lines": [{"message": "connection pool exhausted active=50 max=50", "level": "ERROR"}],
        },
        headers={"Authorization": "", "X-Ingest-Token": "shared-secret"},
    )
    assert authorised.status_code == 200, authorised.text
    assert authorised.json()["accepted"] == 1
    assert authorised.json()["actor"] == "connector-token"


def test_api_schema_documents_the_contract(ingest_client):
    response = ingest_client.get("/api/ingest/schema")
    assert response.status_code == 200
    body = response.json()
    assert "/api/ingest/logs" in body["logs"]["path"]
    assert "error_rate" in body["watched_metrics"]
    assert body["limits"]["max_batch"] > 0


def test_api_status_lists_connected_services(ingest_client, restore_settings, svc):
    name = svc("status-api")
    ingest_client.post(
        "/api/ingest/logs",
        json={
            "service": name,
            "environment": "production",
            "lines": [{"message": "hello from the connected project", "level": "INFO"}],
        },
    )
    response = ingest_client.get("/api/ingest/status")
    assert response.status_code == 200
    assert any(item["service"] == name for item in response.json()["services"])


def test_viewer_cannot_push_telemetry(user_tokens, svc):
    """Ingestion writes evidence, so a read-only role must not be able to fabricate it."""
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    client.headers.update({"Authorization": f"Bearer {user_tokens['viewer']}"})
    response = client.post(
        "/api/ingest/logs",
        json={"service": svc("forbidden-api"), "lines": [{"message": "should not be stored"}]},
    )
    assert response.status_code == 403
