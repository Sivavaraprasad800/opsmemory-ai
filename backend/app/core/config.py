"""Typed application configuration.

Every integration is optional at import time: the platform is designed to boot, run and be
demonstrated with zero secrets configured (see docs/RESEARCH.md section 1).
"""

from __future__ import annotations

import functools
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", "../.env", "../../.env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- application ------------------------------------------------------------------
    environment: str = "development"
    app_name: str = "OpsMemory AI"
    log_level: str = "INFO"
    secret_key: str = "dev-only-insecure-secret-key-change-me"
    access_token_ttl_minutes: int = 720
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    # --- database ---------------------------------------------------------------------
    database_url: str = "sqlite:///./opsmemory.db"

    # --- openai -----------------------------------------------------------------------
    openai_api_key: str = ""
    openai_base_url: str = ""
    """Optional. Point the OpenAI SDK at any OpenAI-compatible endpoint (e.g. Groq)."""
    openai_model: str = "gpt-4o-mini"
    openai_timeout_seconds: float = 45.0
    openai_max_retries: int = 2
    openai_max_tool_rounds: int = 6
    openai_temperature: float = 0.0

    # --- hindsight --------------------------------------------------------------------
    hindsight_base_url: str = ""
    hindsight_api_key: str = ""
    hindsight_bank_id: str = "opsmemory-org"
    hindsight_recall_budget: Literal["low", "mid", "high"] = "mid"
    hindsight_recall_max_tokens: int = 2048
    hindsight_timeout_seconds: float = 30.0
    hindsight_required: bool = False

    # --- simulated environment --------------------------------------------------------
    sim_tick_ms: int = 250
    sim_seconds_per_tick: int = 2
    sim_retention_minutes: int = 180
    sim_warmup_seconds: float = 900.0
    """Simulated seconds replayed at startup when the database holds no telemetry yet."""
    sim_autostart: bool = True
    """Background clock. Off in tests so every scenario is deterministic."""
    baseline_window_seconds: int = 1800
    """Trailing window used to establish the pre-incident baseline."""

    # --- clock ------------------------------------------------------------------------
    clock_mode: Literal["simulated", "real"] = "simulated"
    """Which clock the whole platform reads.

    ``simulated`` (default) runs the demo world: the ticker advances a fast-forward clock and
    generates telemetry. ``real`` binds the platform to wall-clock UTC and expects telemetry to
    arrive through the ingestion API instead — this is the mode you run against a live project.
    """

    # --- ingestion (connecting a real project) -----------------------------------------
    ingest_token: str = ""
    """Shared secret for the connector. Empty means ingestion requires a signed-in user."""
    ingest_services_per_minute: int = 600
    """Per-service ceiling on ingested telemetry rows per minute, to absorb a runaway agent."""
    ingest_max_batch: int = 5000
    """Reject a single request larger than this instead of stalling the database."""
    ingest_max_skew_seconds: int = 300
    """Reject samples stamped further than this into the future (a wrong-clock guard)."""
    ingest_autoregister: bool = True
    """Create unknown projects, environments and services on first sample, so any project can
    connect without a manual catalogue step. Set false to require explicit registration."""

    # --- remediation safety -----------------------------------------------------------
    max_autonomy_level: int = Field(default=4, ge=0, le=5)
    approval_required_above_risk: Literal["low", "medium", "high", "critical"] = "low"
    remediation_max_retries: int = 2
    verification_settle_seconds: int = 45
    verification_min_improvement_pct: float = 25.0

    # --- github / ci ------------------------------------------------------------------
    github_token: str = ""
    github_webhook_secret: str = ""
    github_repo: str = ""

    @field_validator("database_url")
    @classmethod
    def _normalise_sqlite(cls, value: str) -> str:
        # Allow bare paths in DATABASE_URL for convenience during local development.
        if value and "://" not in value:
            return f"sqlite:///{value}"
        return value

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def real_clock(self) -> bool:
        """True when the platform is bound to wall-clock time and fed by ingestion."""
        return self.clock_mode == "real"

    @property
    def llm_enabled(self) -> bool:
        """True when a real LLM reasoning layer is configured."""
        return bool(self.openai_api_key.strip())

    @property
    def llm_provider(self) -> str:
        """Human readable name of the configured reasoning provider."""
        host = self.openai_base_url.lower()
        if "groq" in host:
            return "groq"
        if "openai" in host or not host:
            return "openai"
        return "openai-compatible"

    @property
    def hindsight_enabled(self) -> bool:
        """True when a real Hindsight server is configured (reachability checked at runtime)."""
        return bool(self.hindsight_base_url.strip())


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
