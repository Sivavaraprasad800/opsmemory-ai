"""End-to-end learning loop test (build spec sections 29, 41, 42, 52 and 56).

This is the test that matters most, because it asserts the product claim rather than a unit of
behaviour: the second incident must be resolved using knowledge that only existed because the
first incident was remembered — specifically, the knowledge that a restart already failed.

If someone later "simplifies" the memory layer, the failure ledger, or the verification engine,
this test fails.
"""

from __future__ import annotations

from app.database.models import (
    Incident,
    LearningEvent,
    MemoryReference,
    Postmortem,
    RemediationAttempt,
    VerificationRun,
)
from app.domain.incidents import detect_and_open_incidents
from app.memory.service import get_memory_service
from sqlalchemy import select

from app.api.routes.demo import LearningLoopDemo


def _phase(facts: dict, steps: list[dict], phase: str) -> dict:
    return next(step["facts"] for step in steps if step["phase"] == phase)


def test_complete_learning_loop(db, engine, memory_service) -> None:
    demo = LearningLoopDemo(
        db,
        engine,
        actor="sre@acme.test",
        actor_role="sre",
        scenario="connection_exhaustion",
        service="payment-service",
        include_failed_attempt=True,
    )
    result = demo.run()
    db.commit()

    steps = result["steps"]
    phases = [step["phase"] for step in steps]
    assert phases == [
        "incident_1_detected",
        "incident_1_investigated",
        "failed_attempt",
        "incident_1_resolved",
        "incident_2_resolved",
    ]

    # --- 1. incident 1 is detected, investigated, and has no useful precedent ------------
    first = _phase({}, steps, "incident_1_investigated")
    assert first["conclusion"]["root_cause_category"] == "connection_leak"
    assert first["memory"]["had_useful_experience"] is False
    assert "No useful prior experience" in first["memory"]["explanation"]
    assert first["tool_calls"] > 5

    # --- 2. the operator's naive mitigation is executed and honestly fails ---------------
    failed = _phase({}, steps, "failed_attempt")
    assert failed["verification"]["verdict"] == "verification_failed"
    assert failed["outcome"] in {"failed", "partial"}
    assert failed["run"]["action_code"] == "restart_service"

    # --- 3. the AI's own recommendation had already avoided the doomed action ------------
    recommended = first["recommended_action"]["action_code"]
    assert recommended != "restart_service"
    assert any("restart_service" in item for item in first["recommended_action"]["avoided_actions"])

    # --- 4. re-investigation succeeds with a different, cause-removing action ------------
    resolved = _phase({}, steps, "incident_1_resolved")
    assert resolved["remediation"]["choice"]["action_code"] == "update_known_safe_configuration"
    assert resolved["verification"]["verdict"] == "verified_success"
    assert resolved["verification"]["improvement_pct"] > 50.0
    assert resolved["postmortem_id"] is not None

    # Before/after must be real telemetry, not decorative numbers.
    before = resolved["verification"]["before"]
    after = resolved["verification"]["after"]
    assert before["connection_saturation"] > 0.8
    assert after["connection_saturation"] < 0.3
    assert before["error_rate"] > after["error_rate"]

    # --- 5. the experience was actually retained -----------------------------------------
    delta = result["learning_delta"]
    assert delta["memory_before"]["documents"] == 0
    assert delta["memory_after_incident_1"]["documents"] >= 2
    assert delta["memory_after_incident_2"]["documents"] > delta["memory_after_incident_1"]["documents"]
    assert delta["memory_after_incident_1"]["observations"] >= 1

    # --- 6. incident 2 is resolved using recalled experience -----------------------------
    second = _phase({}, steps, "incident_2_resolved")
    assert second["incident"]["status"] == "verified"
    assert second["memory"]["had_useful_experience"] is True
    assert second["memory"]["recall"]["memory_count"] > 0
    assert second["verification"]["verdict"] == "verified_success"

    choice = second["remediation"]["choice"]
    assert choice["action_code"] == "update_known_safe_configuration"
    assert delta["failed_action_avoided_in_incident_2"] is True
    assert any("restart_service" in item for item in choice["avoided_actions"])

    # --- 7. the current-vs-historical comparison is explainable ---------------------------
    comparison = second["comparison"]
    assert comparison is not None
    similarity = comparison["current_similarity"]
    assert similarity["score"] >= 0.6
    assert any("same service" in item for item in similarity["matched"])
    assert any("different environment" in item for item in similarity["differences"])
    assert similarity["contributions"], "every feature must contribute a readable line"

    # The comparison shown to a human must be like-for-like on *every* feature, including the
    # root cause — which recall cannot know when it runs, because recall deliberately happens
    # before reasoning. The orchestrator therefore recomputes it once the conclusion lands, and
    # keeps the provisional score alongside it rather than quietly replacing it.
    assert similarity["refined_after_root_cause"] is True
    assert similarity["provisional_score"] < similarity["score"]
    root_cause_feature = next(c for c in similarity["contributions"] if c["feature"] == "root_cause")
    assert root_cause_feature["raw_score"] == 1.0, "both sides now have an established root cause"

    # --- 8. both incidents, the failure ledger and the postmortem are persisted ----------
    incidents = db.scalars(select(Incident).order_by(Incident.id)).all()
    assert len(incidents) == 2
    assert {i.status for i in incidents} == {"verified"}

    attempts = db.scalars(select(RemediationAttempt).order_by(RemediationAttempt.id)).all()
    assert attempts, "the remediation ledger must record what was tried"
    assert any(a.outcome in {"failed", "partial"} for a in attempts)
    assert any(a.outcome == "succeeded" for a in attempts)

    verifications = db.scalars(select(VerificationRun).order_by(VerificationRun.id)).all()
    assert any(v.verdict == "verification_failed" for v in verifications)
    assert sum(1 for v in verifications if v.verdict == "verified_success") >= 2

    postmortems = db.scalars(select(Postmortem)).all()
    assert postmortems, "a postmortem must be drafted for a resolved incident"

    references = db.scalars(select(MemoryReference)).all()
    assert references, "Postgres must reference which memories belong to which incidents"

    events = db.scalars(select(LearningEvent)).all()
    kinds = {event.kind for event in events}
    assert {"retain", "recall"} <= kinds
    assert any(event.kind == "reflect" for event in events)


def test_learning_loop_is_repeatable_from_a_cold_start(db, engine, memory_service) -> None:
    """Build spec section 52: the loop must run twice without a dead first act."""
    for _ in range(2):
        memory_service.reset()
        from app.database.seed import clean_room, seed_catalogue

        clean_room(db, memory=memory_service)
        seed_catalogue(db)
        db.commit()
        engine.load_catalogue()
        engine.advance(900)
        db.commit()

        result = LearningLoopDemo(
            db, engine, actor="sre@acme.test", actor_role="sre"
        ).run()
        db.commit()
        assert result["learning_delta"]["memory_before"]["documents"] == 0
        assert result["learning_delta"]["failed_action_avoided_in_incident_2"] is True
        assert result["learning_delta"]["memory_after_incident_2"]["documents"] > 0


def test_a_new_incident_without_history_does_not_invent_precedent(db, engine, memory_service) -> None:
    """The anti-hallucination guarantee at the product level: no memory, no false memory."""
    memory_service.reset()
    engine.inject_fault(scenario="memory_leak", service_name="order-service")
    db.flush()
    incident = None
    for _ in range(10):
        engine.advance(90)
        db.flush()
        opened = detect_and_open_incidents(db, engine)
        if opened:
            incident = opened[0]
            break
    assert incident is not None

    from app.ai_engine.orchestrator import IncidentOrchestrator

    result = IncidentOrchestrator(
        db, engine, incident, actor="sre@acme.test", actor_role="sre"
    ).investigate(execute=False, auto_approve=False)
    db.commit()

    assert result.memory is not None
    assert result.memory["had_useful_experience"] is False
    assert result.memory["recall"]["memory_count"] == 0
    assert result.conclusion["root_cause_category"] == "memory_leak"
    # With nothing remembered, the AI must still produce evidence-backed reasoning.
    assert result.conclusion["supporting_evidence"]
    assert result.conclusion["status"] in {"supported", "weakly_supported", "needs_more_evidence"}
