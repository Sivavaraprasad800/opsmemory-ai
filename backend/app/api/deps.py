"""FastAPI dependencies.

* ``get_principal`` resolves the bearer token into a role, and falls back to an anonymous
  read-only principal outside production so the demo does not require a login ceremony.
* ``principal.require(...)`` is the single place permissions are enforced, so every mutating
  route states its permission explicitly (build spec section 46).
* ``rate_limit_write`` is a small in-process token bucket. It is deliberately simple: it exists
  to stop a runaway client from hammering the LLM and memory backends, not to be a WAF.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Header, Request
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.errors import RateLimitedError, UnauthorizedError
from app.core.security import permissions_for_role, require_permission
from app.database.models import User
from app.database.session import get_db
from app.sim.engine import SimulationEngine

# ---------------------------------------------------------------------------------------
# Principal
# ---------------------------------------------------------------------------------------
@dataclass
class Principal:
    user_id: int | None
    email: str
    role: str
    organization_id: int
    is_anonymous: bool = False

    def require(self, *permissions: str) -> None:
        require_permission(self.role, *permissions)

    @property
    def actor(self) -> str:
        return self.email or "anonymous"

    def to_dict(self) -> dict:
        return {
            "user_id": self.user_id,
            "email": self.email,
            "role": self.role,
            "organization_id": self.organization_id,
            "is_anonymous": self.is_anonymous,
            "permissions": permissions_for_role(self.role),
        }


def get_principal(
    db: Annotated[Session, Depends(get_db)],
    authorization: Annotated[str | None, Header()] = None,
) -> Principal:
    if authorization and authorization.lower().startswith("bearer "):
        from app.core.security import decode_access_token

        payload = decode_access_token(authorization.split(" ", 1)[1].strip())
        email = str(payload.get("sub") or "")
        user = db.query(User).filter(User.email == email).one_or_none() if email else None
        if user is None or not user.is_active:
            raise UnauthorizedError("The token refers to an unknown or inactive user")
        return Principal(
            user_id=user.id,
            email=user.email,
            role=user.role,
            organization_id=user.organization_id,
        )

    if settings.environment == "production":
        raise UnauthorizedError("Authentication is required in production")

    # Development / demo: anonymous read-only principal.
    return Principal(user_id=None, email="anonymous", role="viewer", organization_id=1, is_anonymous=True)


DbDep = Annotated[Session, Depends(get_db)]
PrincipalDep = Annotated[Principal, Depends(get_principal)]


# ---------------------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------------------
def get_engine(db: DbDep) -> SimulationEngine:
    engine = SimulationEngine(db)
    engine.load_catalogue()
    return engine


EngineDep = Annotated[SimulationEngine, Depends(get_engine)]


# ---------------------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------------------
_buckets: dict[str, deque[float]] = defaultdict(deque)
_bucket_lock = threading.Lock()

WRITE_LIMIT = 60
"""Requests per minute per principal for mutating routes."""

EXPENSIVE_LIMIT = 12
"""Requests per minute for routes that call the LLM or the memory backend."""


def _consume(key: str, limit: int) -> None:
    now = time.monotonic()
    with _bucket_lock:
        bucket = _buckets[key]
        while bucket and now - bucket[0] > 60.0:
            bucket.popleft()
        if len(bucket) >= limit:
            raise RateLimitedError(
                f"Rate limit exceeded for {key.split(':')[1]}: {limit} requests per minute",
                detail={"limit": limit, "window_seconds": 60},
            )
        bucket.append(now)


def rate_limit_write(principal: Principal, request: Request) -> None:
    _consume(f"write:{principal.email}", WRITE_LIMIT)


def rate_limit_expensive(principal: Principal, request: Request) -> None:
    _consume(f"expensive:{principal.email}", EXPENSIVE_LIMIT)


def reset_rate_limits() -> None:
    """Test helper."""
    with _bucket_lock:
        _buckets.clear()
