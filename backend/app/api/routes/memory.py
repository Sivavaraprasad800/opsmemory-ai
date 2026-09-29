"""Organizational memory routes — the Memory page answers "what has the organization learned?"."""

from __future__ import annotations

from fastapi import APIRouter, Query, Request
from sqlalchemy import select

from app.api.deps import DbDep, PrincipalDep, rate_limit_expensive, rate_limit_write
from app.api.serializers import learning_event_payload
from app.core.config import settings
from app.database.models import Incident, LearningEvent, MemoryReference, Service
from app.memory.base import MemoryScope
from app.memory.documents import digest
from app.memory.service import get_memory_service
from app.schemas.api import RecallRequest, ReflectRequest, RetainRequest

router = APIRouter(prefix="/api/memory", tags=["memory"])


@router.get("/status")
def memory_status(principal: PrincipalDep) -> dict:
    principal.require("read")
    service = get_memory_service()
    return {
        "status": service.status().to_dict(),
        "health": service.health(),
        "stats": service.stats(),
        "configured": {
            "hindsight_base_url": bool(settings.hindsight_base_url),
            "bank_id": settings.hindsight_bank_id,
            "recall_budget": settings.hindsight_recall_budget,
            "required": settings.hindsight_required,
        },
    }


@router.get("/items")
def list_memories(
    principal: PrincipalDep,
    memory_type: str | None = Query(default=None, description="world | experience | observation"),
    search: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict:
    principal.require("read")
    service = get_memory_service()
    items = service.list_memories(memory_type=memory_type, search=search, limit=limit)
    return {
        "count": len(items),
        "backend": service.status().active_backend,
        "items": items,
    }


@router.post("/recall")
def recall(principal: PrincipalDep, payload: RecallRequest, request: Request) -> dict:
    principal.require("investigate")
    rate_limit_expensive(principal, request)
    service = get_memory_service()
    store = service._active_store()
    result = store.recall(
        query=payload.query,
        types=tuple(payload.types),
        budget=payload.budget,
        max_tokens=payload.max_tokens,
        top_k=12,
    )
    return result.to_dict()


@router.post("/reflect")
def reflect(principal: PrincipalDep, db: DbDep, payload: ReflectRequest, request: Request) -> dict:
    principal.require("investigate")
    rate_limit_expensive(principal, request)
    service = get_memory_service()
    result = service.reflect(query=payload.query, context=payload.context, db=db)
    db.commit()
    return result.to_dict()


@router.post("/retain")
def retain(principal: PrincipalDep, db: DbDep, payload: RetainRequest, request: Request) -> dict:
    principal.require("retain_learning")
    rate_limit_write(principal, request)
    service = get_memory_service()
    result = service._retain(
        content=payload.content,
        context=payload.context,
        document_id=f"manual-{digest(payload.content)[:16]}",
        metadata={"scope": payload.scope, "source": "manual_retention", "actor": principal.actor},
        tags=[f"scope:{payload.scope}", "source:manual"],
        scope=payload.scope,
        db=db,
        incident_id=None,
        retained_by=principal.actor,
    )
    db.commit()
    return {"retention": result.to_dict()}


@router.get("/scopes")
def list_scopes(principal: PrincipalDep) -> dict:
    principal.require("read")
    return {"scopes": [scope.value for scope in MemoryScope]}


@router.get("/learning-events")
def learning_events(
    principal: PrincipalDep,
    db: DbDep,
    kind: str | None = Query(default=None, description="retain | recall | reflect"),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict:
    principal.require("read")
    stmt = select(LearningEvent)
    if kind:
        stmt = stmt.where(LearningEvent.kind == kind)
    rows = db.scalars(stmt.order_by(LearningEvent.created_at.desc()).limit(limit)).all()
    return {"count": len(rows), "events": [learning_event_payload(row) for row in rows]}


@router.get("/references")
def memory_references(
    principal: PrincipalDep,
    db: DbDep,
    incident_id: int | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=1000),
) -> dict:
    """Postgres side of the memory relationship: which memory belongs to which incident."""
    principal.require("read")
    stmt = select(MemoryReference)
    if incident_id:
        stmt = stmt.where(MemoryReference.incident_id == incident_id)
    rows = db.scalars(stmt.order_by(MemoryReference.retained_at.desc()).limit(limit)).all()

    def service_name(incident_id: int | None) -> str:
        if not incident_id:
            return ""
        incident = db.get(Incident, incident_id)
        if incident is None:
            return ""
        service = db.get(Service, incident.service_id)
        return service.name if service else ""

    return {
        "count": len(rows),
        "references": [
            {
                "id": row.id,
                "incident_id": row.incident_id,
                "service": service_name(row.incident_id),
                "scope": row.scope,
                "bank_id": row.bank_id,
                "memory_id": row.hindsight_memory_id,
                "document_id": row.document_id,
                "backend": row.backend,
                "retained_by": row.retained_by,
                "retained_at": row.retained_at.isoformat(),
                "recall_count": row.recall_count,
                "preview": row.content_preview[:600],
            }
            for row in rows
        ],
    }
