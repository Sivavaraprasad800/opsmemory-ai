"""API tests (build spec sections 46-49, testing blocks 20-30 and 80-90)."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def anonymous_client():
    from app.api.deps import reset_rate_limits
    from app.main import app

    reset_rate_limits()
    with TestClient(app) as client:
        yield client


def test_health_reports_every_subsystem(anonymous_client) -> None:
    response = anonymous_client.get("/api/health")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] in {"ok", "degraded"}
    assert payload["checks"]["database"]["ok"] is True
    assert payload["checks"]["memory"]["active_backend"] in {"hindsight", "fallback"}
    assert payload["checks"]["llm"]["mode"] in {"llm", "offline-deterministic"}


def test_root_advertises_no_secrets(anonymous_client) -> None:
    payload = anonymous_client.get("/").json()
    assert payload["secrets_exposed"] == []
    assert payload["memory_backend"] in {"hindsight", "fallback"}


def test_login_succeeds_with_seeded_credentials(anonymous_client) -> None:
    response = anonymous_client.post(
        "/api/auth/login", json={"email": "sre@acme.test", "password": "password123"}
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["role"] == "sre"
    assert payload["access_token"]
    assert "approve_remediation" in payload["permissions"]


def test_login_fails_with_a_wrong_password(anonymous_client) -> None:
    response = anonymous_client.post(
        "/api/auth/login", json={"email": "sre@acme.test", "password": "wrong-password"}
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHORIZED"


def test_login_records_a_failed_attempt(anonymous_client, db) -> None:
    from app.database.models import AuditLog
    from sqlalchemy import select

    anonymous_client.post("/api/auth/login", json={"email": "sre@acme.test", "password": "nope"})
    entry = db.scalar(select(AuditLog).where(AuditLog.action == "auth.login_failed"))
    assert entry is not None
    assert entry.result == "failure"


def test_anonymous_access_is_read_only(anonymous_client) -> None:
    assert anonymous_client.get("/api/incidents").status_code == 200
    response = anonymous_client.post("/api/incidents/detect")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "FORBIDDEN"


def test_viewer_cannot_investigate(anonymous_client, user_tokens) -> None:
    anonymous_client.headers.update({"Authorization": f"Bearer {user_tokens['viewer']}"})
    response = anonymous_client.post("/api/incidents/1/investigate", json={"execute": False})
    assert response.status_code == 403


def test_analyst_can_read_but_not_approve(anonymous_client, user_tokens) -> None:
    anonymous_client.headers.update({"Authorization": f"Bearer {user_tokens['analyst']}"})
    assert anonymous_client.get("/api/remediation/registry").status_code == 200
    assert anonymous_client.post("/api/postmortems/999/approve").status_code in {403, 404}


def test_unknown_incident_returns_typed_404(client) -> None:
    response = client.get("/api/incidents/999999")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


def test_request_validation_is_enforced(client) -> None:
    response = client.post("/api/memory/recall", json={"query": "x"})
    assert response.status_code == 422


def test_remediation_registry_exposes_the_safety_model(client) -> None:
    payload = client.get("/api/remediation/registry").json()
    assert len(payload["actions"]) >= 7
    for action in payload["actions"]:
        assert action["executes_shell"] is False
        assert action["rollback_workflow"]
        assert action["allowed_environments"]
    assert "no shell execution path" in payload["note"]


def test_memory_status_and_manual_recall(client) -> None:
    status = client.get("/api/memory/status").json()
    assert status["status"]["configured_backend"] in {"hindsight", "fallback"}
    assert "stats" in status

    recall = client.post(
        "/api/memory/recall", json={"query": "restart_service connection_leak payment-service"}
    )
    assert recall.status_code == 200
    assert recall.json()["backend"] in {"hindsight", "fallback"}


def test_simulator_endpoints_require_admin(anonymous_client, user_tokens) -> None:
    anonymous_client.headers.update({"Authorization": f"Bearer {user_tokens['analyst']}"})
    assert anonymous_client.post("/api/sim/advance", json={"seconds": 60}).status_code == 403

    anonymous_client.headers.update({"Authorization": f"Bearer {user_tokens['admin']}"})
    assert anonymous_client.post("/api/sim/advance", json={"seconds": 60}).status_code == 200


def test_scenarios_endpoint_lists_the_catalogue(client) -> None:
    payload = client.get("/api/scenarios").json()
    keys = {scenario["key"] for scenario in payload["scenarios"]}
    assert "connection_exhaustion" in keys
    assert "disk_pressure" in keys
    disk = next(s for s in payload["scenarios"] if s["key"] == "disk_pressure")
    assert disk["canonical_actions"] == [], "disk pressure deliberately has no registered fix"


def test_no_secret_value_appears_in_any_response(anonymous_client, user_tokens) -> None:
    """Defence in depth against the single most embarrassing possible bug."""
    from app.core.config import settings

    secrets = [
        value
        for value in (
            settings.openai_api_key,
            settings.hindsight_api_key,
            settings.github_token,
        )
        if value and len(value) > 8
    ]
    if not secrets:
        pytest.skip("no secrets configured in the test environment, nothing to leak")

    anonymous_client.headers.update({"Authorization": f"Bearer {user_tokens['admin']}"})
    for path in (
        "/",
        "/api/health",
        "/api/integrations",
        "/api/incidents",
        "/api/memory/status",
    ):
        body = json.dumps(anonymous_client.get(path).json())
        for secret in secrets:
            assert secret not in body, f"secret leaked through {path}"


def test_integrations_never_returns_credentials(client) -> None:
    payload = client.get("/api/integrations").json()
    assert payload["secrets_exposed_to_browser"] == []
    body = json.dumps(payload)
    assert "sk-" not in body
    assert "hsk_" not in body


def test_overview_endpoint_returns_dashboard_payload(client) -> None:
    payload = client.get("/api/system/overview").json()
    for key in (
        "sim_time",
        "status",
        "active_incidents",
        "recent_incidents",
        "investigations",
        "deployments",
        "learning_activity",
        "services",
        "top_pattern",
    ):
        assert key in payload


def test_period_analysis_and_patterns_endpoints(client) -> None:
    period = client.get("/api/analysis/period?days=30").json()
    assert period["window_days"] == 30
    assert "by_root_cause" in period

    patterns = client.get("/api/analysis/patterns?days=30").json()
    assert patterns["minimum_occurrences"] == 3
    for pattern in patterns["patterns"]:
        assert pattern["occurrences"] >= 3, "a pattern must never be claimed below the threshold"
        assert pattern["evidence_strength"] in {"weak", "moderate", "strong"}


def test_audit_log_is_readable_and_typed(client) -> None:
    payload = client.get("/api/audit").json()
    assert "entries" in payload
    for entry in payload["entries"]:
        assert entry["action"]
        assert entry["result"] in {"success", "failure", "blocked"}
