"""Authentication and identity."""

from __future__ import annotations

from fastapi import APIRouter, Request
from sqlalchemy import select

from app.api.deps import DbDep, PrincipalDep, rate_limit_write
from app.core.errors import UnauthorizedError
from app.core.security import create_access_token, permissions_for_role, verify_password
from app.database.models import AuditLog, User
from app.schemas.api import LoginRequest, TokenResponse

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/login", response_model=TokenResponse)
def login(payload: LoginRequest, request: Request, db: DbDep) -> TokenResponse:
    user = db.scalar(select(User).where(User.email == payload.email.lower().strip()))
    if user is None or not user.is_active or not verify_password(payload.password, user.password_hash):
        db.add(
            AuditLog(
                action="auth.login_failed",
                actor=payload.email[:160],
                actor_role="unknown",
                target_type="user",
                target_id=payload.email[:60],
                result="failure",
                reason="invalid credentials",
                ip_address=request.client.host if request.client else "",
            )
        )
        db.commit()
        raise UnauthorizedError("Invalid email or password")

    token = create_access_token(
        subject=user.email, role=user.role, organization_id=user.organization_id
    )
    db.add(
        AuditLog(
            action="auth.login",
            actor=user.email,
            actor_role=user.role,
            target_type="user",
            target_id=str(user.id),
            result="success",
            reason="password authentication",
            ip_address=request.client.host if request.client else "",
        )
    )
    db.commit()
    return TokenResponse(
        access_token=token,
        role=user.role,
        user={"id": user.id, "email": user.email, "full_name": user.full_name, "role": user.role},
        permissions=permissions_for_role(user.role),
    )


@router.get("/me")
def me(principal: PrincipalDep) -> dict:
    return principal.to_dict()


@router.get("/users")
def list_users(principal: PrincipalDep, db: DbDep) -> dict:
    principal.require("read")
    users = db.scalars(select(User).order_by(User.id)).all()
    return {
        "users": [
            {
                "id": user.id,
                "email": user.email,
                "full_name": user.full_name,
                "role": user.role,
                "is_active": user.is_active,
            }
            for user in users
        ]
    }
