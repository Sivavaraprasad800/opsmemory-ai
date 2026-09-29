"""Explicit, typed failure states.

Section 48 of the build spec requires that no single integration failure can crash the
platform. Every integration boundary therefore raises one of these, and the orchestrator
degrades gracefully while recording the degraded state on the investigation record.
"""

from __future__ import annotations

from enum import StrEnum


class ErrorCode(StrEnum):
    AI_UNAVAILABLE = "AI_UNAVAILABLE"
    AI_MALFORMED_RESPONSE = "AI_MALFORMED_RESPONSE"
    AI_TIMEOUT = "AI_TIMEOUT"
    AI_TOOL_ROUND_LIMIT = "AI_TOOL_ROUND_LIMIT"

    MEMORY_UNAVAILABLE = "MEMORY_UNAVAILABLE"
    MEMORY_DEGRADED = "MEMORY_DEGRADED"

    ACTION_BLOCKED = "ACTION_BLOCKED"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    ACTION_NOT_REGISTERED = "ACTION_NOT_REGISTERED"

    VERIFICATION_FAILED = "VERIFICATION_FAILED"
    DEPLOYMENT_FAILED = "DEPLOYMENT_FAILED"

    TOOL_FAILED = "TOOL_FAILED"
    NOT_FOUND = "NOT_FOUND"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    UNAUTHORIZED = "UNAUTHORIZED"
    FORBIDDEN = "FORBIDDEN"
    RATE_LIMITED = "RATE_LIMITED"
    INTERNAL = "INTERNAL"


class OpsMemoryError(Exception):
    """Base class for every typed platform failure."""

    code: ErrorCode = ErrorCode.INTERNAL
    http_status: int = 500

    def __init__(self, message: str, *, detail: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail or {}

    def to_payload(self) -> dict:
        return {"error": {"code": self.code.value, "message": self.message, "detail": self.detail}}


class NotFoundError(OpsMemoryError):
    code = ErrorCode.NOT_FOUND
    http_status = 404


class ValidationError(OpsMemoryError):
    code = ErrorCode.VALIDATION_FAILED
    http_status = 422


class UnauthorizedError(OpsMemoryError):
    code = ErrorCode.UNAUTHORIZED
    http_status = 401


class ForbiddenError(OpsMemoryError):
    code = ErrorCode.FORBIDDEN
    http_status = 403


class RateLimitedError(OpsMemoryError):
    code = ErrorCode.RATE_LIMITED
    http_status = 429


class AIUnavailableError(OpsMemoryError):
    code = ErrorCode.AI_UNAVAILABLE
    http_status = 503


class AIMalformedResponseError(OpsMemoryError):
    code = ErrorCode.AI_MALFORMED_RESPONSE
    http_status = 502


class MemoryUnavailableError(OpsMemoryError):
    code = ErrorCode.MEMORY_UNAVAILABLE
    http_status = 503


class ActionBlockedError(OpsMemoryError):
    code = ErrorCode.ACTION_BLOCKED
    http_status = 409

    def __init__(self, message: str, *, reasons: list[str] | None = None) -> None:
        super().__init__(message, detail={"reasons": reasons or []})
        self.reasons = reasons or []


class VerificationFailedError(OpsMemoryError):
    code = ErrorCode.VERIFICATION_FAILED
    http_status = 409
