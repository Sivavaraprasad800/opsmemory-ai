"""Password hashing, JWT issuing and RBAC primitives.

No third-party crypto beyond PyJWT: password hashing uses PBKDF2-HMAC-SHA256 from the
standard library, which keeps the dependency surface (and therefore the attack surface)
small for a hackathon deployment.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Any, Iterable

import jwt

from app.core.config import settings
from app.core.errors import ForbiddenError, UnauthorizedError

PBKDF2_ITERATIONS = 240_000
ALGORITHM = "HS256"


class Role(StrEnum):
    """RBAC roles, ordered by privilege (see ROLE_RANK)."""

    VIEWER = "viewer"
    ANALYST = "analyst"
    SRE = "sre"
    ADMIN = "admin"


ROLE_RANK: dict[str, int] = {
    Role.VIEWER: 10,
    Role.ANALYST: 20,
    Role.SRE: 30,
    Role.ADMIN: 40,
}

"""Who may do what. Section 46 requires action permissions to be explicit."""
PERMISSIONS: dict[str, str] = {
    "read": Role.VIEWER,
    "investigate": Role.ANALYST,
    "propose_remediation": Role.ANALYST,
    "approve_remediation": Role.SRE,
    "execute_remediation": Role.SRE,
    "edit_postmortem": Role.ANALYST,
    "finalize_postmortem": Role.SRE,
    "retain_learning": Role.SRE,
    "manage_integrations": Role.ADMIN,
    "manage_users": Role.ADMIN,
    "simulate_fault": Role.ADMIN,
    "reset_environment": Role.ADMIN,
    # Pushing telemetry from a real project. Granted to SRE and above so a signed-in engineer
    # can connect a system without the shared connector token.
    "ingest_telemetry": Role.SRE,
}


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    salt = salt or os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, iterations, salt_hex, digest_hex = encoded.split("$")
        if scheme != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iterations)
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest.hex(), digest_hex)


def create_access_token(
    *, subject: str, role: str, organization_id: int, extra: dict[str, Any] | None = None
) -> str:
    now = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "sub": subject,
        "role": role,
        "org": organization_id,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=settings.access_token_ttl_minutes)).timestamp()),
    }
    if extra:
        payload.update(extra)
    return jwt.encode(payload, settings.secret_key, algorithm=ALGORITHM)


def decode_access_token(token: str) -> dict[str, Any]:
    try:
        return jwt.decode(token, settings.secret_key, algorithms=[ALGORITHM])
    except jwt.ExpiredSignatureError as exc:
        raise UnauthorizedError("Access token has expired") from exc
    except jwt.InvalidTokenError as exc:
        raise UnauthorizedError("Invalid access token") from exc


def role_satisfies(role: str, required: str) -> bool:
    return ROLE_RANK.get(role, 0) >= ROLE_RANK.get(required, 10_000)


def has_permission(role: str, permission: str) -> bool:
    required = PERMISSIONS.get(permission)
    if required is None:
        return False
    return role_satisfies(role, required)


def require_permission(role: str, *permissions: str) -> None:
    missing = [p for p in permissions if not has_permission(role, p)]
    if missing:
        raise ForbiddenError(
            f"Role '{role}' is not permitted to perform: {', '.join(missing)}",
            detail={"missing_permissions": missing, "role": role},
        )


def permissions_for_role(role: str) -> list[str]:
    return sorted(p for p in PERMISSIONS if has_permission(role, p))


def redact(secret: str | None, *, keep: int = 4) -> str:
    """Never emit a full secret into logs, API responses or audit rows."""
    if not secret:
        return ""
    if len(secret) <= keep:
        return "*" * len(secret)
    return f"{secret[:keep]}{'*' * 8}"


def assert_no_secret_leak(payload: Iterable[str]) -> None:
    """Guard used by tests: fails if a known secret value is present in serialised output."""
    secrets = [
        settings.openai_api_key,
        settings.hindsight_api_key,
        settings.github_token,
        settings.secret_key,
    ]
    for value in payload:
        for secret in secrets:
            if secret and len(secret) > 8 and secret in value:
                raise AssertionError("Secret value leaked into a serialised payload")
