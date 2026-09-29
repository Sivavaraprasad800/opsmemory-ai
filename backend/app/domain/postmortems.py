"""Postmortem approval and editing (build spec section 32).

The AI drafts; a human approves. Approval is what makes the learning eligible for retention as
a *postmortem learning* memory — mirroring the spec's rule that the approved learning is what
gets remembered, not the raw draft.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from app.core.security import require_permission
from app.database.models import AuditLog, Incident, Postmortem, utcnow
from app.memory.service import get_memory_service

logger = logging.getLogger(__name__)


def update_postmortem(db: Session, *, postmortem: Postmortem, actor: str, actor_role: str, payload: dict[str, Any]) -> Postmortem:
    require_permission(actor_role, "edit_postmortem")
    changes: dict[str, Any] = {}
    for field_name in ("summary", "impact", "root_cause", "lessons", "preventive_actions"):
        value = payload.get(field_name)
        if value is None:
            continue
        setattr(postmortem, field_name, value)
        changes[field_name] = value
    postmortem.updated_at = utcnow()
    postmortem.authored_by = f"ai+{actor}"
    db.add(
        AuditLog(
            action="postmortem.edited",
            actor=actor,
            actor_role=actor_role,
            target_type="postmortem",
            target_id=str(postmortem.id),
            result="success",
            reason="human edit of AI-drafted postmortem",
            detail={"fields_changed": list(changes)},
        )
    )
    db.flush()
    return postmortem


def approve_postmortem(db: Session, *, postmortem: Postmortem, actor: str, actor_role: str) -> dict[str, Any]:
    require_permission(actor_role, "finalize_postmortem")
    postmortem.status = "approved"
    postmortem.approved_by = actor
    postmortem.approved_at = utcnow()
    db.flush()

    incident = db.get(Incident, postmortem.incident_id)
    retention: dict[str, Any] | None = None
    if incident is not None:
        content = (
            f"APPROVED POSTMORTEM LEARNING for incident #{incident.id} "
            f"({incident.title}).\n"
            f"Root cause: {postmortem.root_cause}\n"
            f"Verified outcome: {postmortem.verification}\n"
            f"Failed attempts: "
            + (
                "; ".join(
                    f"{a.get('action_code')} (attempt {a.get('attempt')}) — {a.get('outcome')}"
                    for a in (postmortem.failed_attempts or [])
                )
                or "none"
            )
            + "\nSuccessful remediation: "
            + (postmortem.successful_remediation or "none recorded")
            + "\nLessons: "
            + "; ".join(postmortem.lessons or [])
            + "\nPreventive actions: "
            + "; ".join(postmortem.preventive_actions or [])
        )
        try:
            result = get_memory_service()._retain(
                content=content,
                context="human-approved postmortem learning",
                document_id=f"incident-{incident.id}-postmortem-learning",
                metadata={
                    "scope": "postmortem_learning",
                    "incident_id": incident.id,
                    "root_cause_category": incident.root_cause_category or "unknown",
                    "approved_by": actor,
                },
                tags=[
                    "kind:postmortem",
                    f"cause:{incident.root_cause_category or 'unknown'}",
                ],
                scope="postmortem_learning",
                db=db,
                incident_id=incident.id,
                retained_by="postmortem_agent",
            )
            retention = result.to_dict()
        except Exception as exc:  # noqa: BLE001 - memory failure must not block approval
            logger.warning("could not retain approved postmortem learning: %s", exc)
            retention = {"error": str(exc)}

    db.add(
        AuditLog(
            action="postmortem.approved",
            actor=actor,
            actor_role=actor_role,
            target_type="postmortem",
            target_id=str(postmortem.id),
            result="success",
            reason="human approval of the postmortem and its retained learning",
            detail={"retention": retention},
        )
    )
    db.flush()
    return {"postmortem_id": postmortem.id, "status": postmortem.status, "retention": retention}
