"""Application configuration, loaded from environment / .env.

All settings have safe local defaults so the stack runs without secrets. LLM keys are
optional: when absent, the LLM layer falls back to a deterministic stub provider.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- App ---
    app_env: Literal["dev", "test", "prod"] = "dev"
    log_level: str = "INFO"
    log_json: bool = True

    # --- Datastores ---
    database_url: str = Field(
        default="postgresql+asyncpg://saral:saral@localhost:5432/saral",
        description="Async SQLAlchemy (asyncpg) DSN.",
    )
    redis_url: str = "redis://localhost:6379/0"

    # --- Redis Streams run bus ---
    run_stream: str = "agent-runs"
    run_consumer_group: str = "workers"

    # --- LLM providers (all optional in dev) ---
    sarvam_api_key: str | None = None
    sarvam_base_url: str = "https://api.sarvam.ai"
    anthropic_api_key: str | None = None
    # Ordered fallback chain; providers without a key are skipped, then stub.
    llm_provider_order: str = "sarvam,anthropic,stub"
    anthropic_model: str = "claude-sonnet-4-6"
    sarvam_model: str = "sarvam-m"

    # --- Agent control ---
    max_step_count: int = 25  # supervisor loop guard
    tool_timeout_s: float = 10.0
    conversation_memory_turns: int = 8

    @property
    def provider_chain(self) -> list[str]:
        return [p.strip() for p in self.llm_provider_order.split(",") if p.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
