"""Request models. Every user-supplied value is validated here before it reaches a service."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


class LoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=200)
    password: str = Field(min_length=1, max_length=200)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    role: str
    user: dict[str, Any]
    permissions: list[str]


class InvestigateRequest(BaseModel):
    execute: bool = True
    auto_approve: bool = False
    autonomy_level: int | None = Field(default=None, ge=0, le=5)


class RemediationRequest(BaseModel):
    action_code: str = Field(min_length=2, max_length=80)
    rationale: str = Field(default="", max_length=2000)
    autonomy_level: int | None = Field(default=None, ge=0, le=5)
    parameters: dict[str, Any] = Field(default_factory=dict)

    @field_validator("action_code")
    @classmethod
    def _no_shell_characters(cls, value: str) -> str:
        """Defence in depth: the registry is the real gate, but reject obvious command syntax."""
        forbidden = set(";|&$`><\n\r\\")
        if forbidden & set(value):
            raise ValueError("action_code must not contain shell metacharacters")
        return value


class ApproveRequest(BaseModel):
    reason: str = Field(default="", max_length=1000)


class VerifyRequest(BaseModel):
    settle_seconds: int | None = Field(default=None, ge=0, le=3600)


class FaultRequest(BaseModel):
    scenario: str = Field(min_length=2, max_length=60)
    service: str = Field(min_length=2, max_length=120)
    environment: str = Field(default="production", max_length=60)
    severity: Literal["low", "medium", "high", "critical"] | None = None


class DeployRequest(BaseModel):
    service: str = Field(min_length=2, max_length=120)
    environment: str = Field(default="production", max_length=60)
    version: str = Field(min_length=1, max_length=60)
    commit_message: str = Field(default="", max_length=1000)
    author: str = Field(default="engineer@example.com", max_length=160)
    change_class: str = Field(default="application_config", max_length=60)
    file_path: str = Field(default="deploy/service.yaml", max_length=300)
    with_scenario: str | None = Field(default=None, max_length=60)


class AdvanceRequest(BaseModel):
    seconds: float = Field(gt=0, le=86400)


class RecallRequest(BaseModel):
    query: str = Field(min_length=2, max_length=1500)
    types: list[Literal["world", "experience", "observation"]] = Field(
        default_factory=lambda: ["world", "experience", "observation"]
    )
    budget: Literal["low", "mid", "high"] = "mid"
    max_tokens: int = Field(default=2048, ge=64, le=16000)


class ReflectRequest(BaseModel):
    query: str = Field(min_length=2, max_length=1500)
    context: str = Field(default="", max_length=1000)


class RetainRequest(BaseModel):
    content: str = Field(min_length=5, max_length=20000)
    context: str = Field(default="manual retention", max_length=400)
    scope: str = Field(default="incident_experience", max_length=60)


class PostmortemUpdateRequest(BaseModel):
    summary: str | None = None
    impact: str | None = None
    root_cause: str | None = None
    lessons: list[str] | None = None
    preventive_actions: list[str] | None = None


class HistorySeedRequest(BaseModel):
    count: int = Field(default=60, ge=10, le=200)
    retain_to_memory: bool = False
    days: int = Field(default=45, ge=1, le=365)


class DigestReport(BaseModel):
    checked: dict[str, Any] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)
