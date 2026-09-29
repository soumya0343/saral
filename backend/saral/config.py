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
    # Free-tier providers, all OpenAI-compatible (per-role free stack).
    groq_api_key: str | None = None
    groq_base_url: str = "https://api.groq.com/openai/v1"
    # Free tier (checked 2026-09-29): gpt-oss-120b/20b, qwen3.8-27b. Llama 3.x left it 2026-08-16.
    groq_model: str = "openai/gpt-oss-20b"
    cerebras_api_key: str | None = None
    cerebras_base_url: str = "https://api.cerebras.ai/v1"
    # Cerebras is a 30-day $5 trial now, not a free tier: optional bonus, not in default chains.
    cerebras_model: str = "gpt-oss-120b"
    gemini_api_key: str | None = None
    gemini_base_url: str = "https://generativelanguage.googleapis.com/v1beta/openai"
    gemini_model: str = "gemini-3.5-flash-lite"  # free; 2.x closed to new users (2026-09)
    # Default ordered fallback chain; providers without a key are skipped, then stub.
    # Sarvam is intentionally NOT a chat provider — it is used ONLY for language detection
    # (/text-lid, in triage). All generation/judging runs on Gemini/Groq/Cerebras.
    llm_provider_order: str = "gemini,groq,stub"
    # Per-role chains: models assigned per role, not one global chain. A blank role
    # falls back to llm_provider_order. Synthesis -> Hindi-strong Gemini (Groq/Cerebras
    # fallback); triage-classify -> fast Groq/Cerebras; judge is PINNED (no mid-suite swap).
    # Entries are "provider" or "provider:model"; free quotas are per model, so each role pins
    # its own model and roles don't drain one another.
    llm_role_synthesis: str = (
        "gemini:gemini-3.8-flash,gemini:gemini-3.5-flash-lite,groq:openai/gpt-oss-120b,stub"
    )
    llm_role_triage: str = "groq:openai/gpt-oss-20b,groq:qwen/qwen3.8-27b,stub"
    llm_role_judge: str = "groq:openai/gpt-oss-120b,stub"
    anthropic_model: str = "claude-sonnet-4-6"
    sarvam_model: str = "sarvam-30b"
    # Use Sarvam /text-lid for language detection. When the key is absent or
    # Sarvam is down, triage falls back to the deterministic regex detector.
    triage_llm_language: bool = True

    # --- Retrieval ---
    corpus_dir: str = "data/policy_corpus"
    customers_dir: str = "data/customers" # per-customer document-fidelity docs
    # Live path uses local multilingual-e5 (cross-lingual semantics for Hindi/Hinglish
    # per-customer retrieval — the differentiator). "hashing" is the offline/CI floor
    # (deterministic, no extra dep); conftest forces it for reproducible eval..
    embedder: Literal["hashing", "sentence-transformer"] = "sentence-transformer"
    embedder_model: str = "intfloat/multilingual-e5-small"
    retrieval_top_k: int = 4
    # Below this top-passage score an information answer is ungrounded -> escalate.
    # Calibrated for e5 cosine/RRF; hashing uses a different scale (kept 0.0 in CI).
    retrieval_score_floor: float = 0.0

    # --- Identity & Auth ---
    # Mock IdP signing secret (HS256). Dev default is insecure on purpose; set in prod.
    session_secret: str = "dev-insecure-change-me-0000000000000000"  # >=32 bytes (HS256)
    session_ttl_min: int = 30  # short-TTL session token
    step_up_ttl_s: int = 300  # OTP challenge validity window
    default_tenant: str = "t_demo"  # single hardcoded tenant (multi-tenant-ready schema)
    # Expose the generated OTP in API responses (non-prod only) since there is no SMS channel.
    expose_test_otp: bool = True

    # --- Compliance ---
    pii_backend: Literal["regex", "presidio"] = "regex"
    # Retention: redacted messages purged after N days; the hash-chained
    # audit log is held for the regulatory term (erasure-compatible — it holds no raw PII).
    message_retention_days: int = 90
    audit_retention_years: int = 7

    # --- Evaluation ---
    config_version: str = "v1"  # pin for prompt/config; bump to compare versions
    scenarios_path: str = "data/scenarios/scenarios.yaml"
    eval_reports_dir: str = "data/eval_reports"
    # A judge whose Cohen's kappa vs human labels is below this floor in a language is not
    # trusted there — that language's resolution metric is gated behind human review.
    # Languages with too few/unanimous labels are also treated as un-validated.
    judge_kappa_floor: float = 0.6

    # --- Agent control ---
    max_step_count: int = 25  # supervisor loop guard
    tool_timeout_s: float = 10.0
    conversation_memory_turns: int = 40  # full-chat context window (bounded for token safety)

    # --- Reliability (Phase 5) ---
    llm_retry_cap: int = 2  # retries per provider on structured/parse error before fallback
    checkpoint_backend: Literal["memory", "postgres"] = "memory"
    claim_min_idle_ms: int = 30000  # XAUTOCLAIM: reclaim pending entries idle longer than this
    reclaim_batch: int = 10
    # Pending-write execution authority is mortal: past this TTL a suspended write
    # is abandoned and never auto-fires; the orphan reaper closes the run + escalates. The
    # dedup window is tied to the same TTL (keys are purged together).
    pending_write_ttl_s: int = 86400  # 24h
    orphan_ttl_s: int = 86400  # suspended-conversation reaper horizon

    @property
    def provider_chain(self) -> list[str]:
        return [p.strip() for p in self.llm_provider_order.split(",") if p.strip()]

    def role_chain(self, role: str) -> list[str]:
        """Provider chain for a role. Falls back to the default chain if unset."""
        raw = {
            "synthesis": self.llm_role_synthesis,
            "triage": self.llm_role_triage,
            "judge": self.llm_role_judge,
        }.get(role, "")
        chain = [p.strip() for p in raw.split(",") if p.strip()]
        return chain or self.provider_chain


@lru_cache
def get_settings() -> Settings:
    return Settings()
