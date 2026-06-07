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
    # Mock core-system store: "postgres" (durable, live) or "memory" (in-proc SQLite, tests).
    store_backend: Literal["memory", "postgres"] = "memory"

    # --- Redis Streams run bus ---
    run_stream: str = "agent-runs"
    run_consumer_group: str = "workers"
    # Local dev convenience: run the agent worker as a task inside the API process so the
    # in-memory mock backend is shared. In production API and worker are separate pods.
    run_worker_inproc: bool = False

    # --- LLM providers (all optional in dev) ---
    sarvam_api_key: str | None = None
    sarvam_base_url: str = "https://api.sarvam.ai"
    anthropic_api_key: str | None = None
    # Ordered fallback chain; providers without a key are skipped, then stub.
    llm_provider_order: str = "sarvam,anthropic,stub"
    anthropic_model: str = "claude-sonnet-4-6"
    sarvam_model: str = "sarvam-30b"
    # Use Sarvam /text-lid for language detection (TRD §12.2). When the key is absent or
    # Sarvam is down, triage falls back to the deterministic regex detector (TRD §15).
    triage_llm_language: bool = True

    # --- Retrieval ---
    corpus_dir: str = "data/policy_corpus"
    customers_dir: str = "data/customers"  # per-customer document-fidelity docs (FR-16)
    embedder: Literal["hashing", "sentence-transformer"] = "hashing"
    retrieval_top_k: int = 4
    retrieval_score_floor: float = 0.0  # below this, treat as ungrounded -> escalate (FR-18)

    # --- Identity & Auth (TRD §11.5) ---
    # Mock IdP signing secret (HS256). Dev default is insecure on purpose; set in prod.
    session_secret: str = "dev-insecure-change-me-0000000000000000"  # >=32 bytes (HS256)
    session_ttl_min: int = 30  # short-TTL session token
    step_up_ttl_s: int = 300  # OTP challenge validity window
    default_tenant: str = "t_demo"  # single hardcoded tenant (multi-tenant-ready schema)
    # Expose the generated OTP in API responses (non-prod only) since there is no SMS channel.
    expose_test_otp: bool = True

    # --- Compliance ---
    pii_backend: Literal["regex", "presidio"] = "regex"

    # --- Evaluation ---
    config_version: str = "v1"  # pin for prompt/config; bump to compare versions
    scenarios_path: str = "data/scenarios/scenarios.yaml"
    eval_reports_dir: str = "data/eval_reports"

    # --- Agent control ---
    max_step_count: int = 25  # supervisor loop guard
    tool_timeout_s: float = 10.0
    conversation_memory_turns: int = 8

    # --- Reliability (Phase 5) ---
    llm_retry_cap: int = 2  # retries per provider on structured/parse error before fallback
    checkpoint_backend: Literal["memory", "postgres"] = "memory"
    claim_min_idle_ms: int = 30000  # XAUTOCLAIM: reclaim pending entries idle longer than this
    reclaim_batch: int = 10

    @property
    def provider_chain(self) -> list[str]:
        return [p.strip() for p in self.llm_provider_order.split(",") if p.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
