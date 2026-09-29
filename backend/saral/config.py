"""Application configuration, loaded from environment / .env.

All settings have safe local defaults so the stack runs without secrets. LLM keys are
optional: when absent, the LLM layer falls back to a deterministic stub provider.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_DEV_SESSION_SECRET = "dev-insecure-change-me-0000000000000000"


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
    # Judge: a DIFFERENT model family from synthesis (Gemini, falling back to gpt-oss) so it
    # doesn't grade its own writing. The live eval pins the first entry (no fallback).
    llm_role_judge: str = "groq:qwen/qwen3.8-27b,stub"
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
    # Optional local prompt-injection classifier layered on the deterministic rules (off when
    # empty). Free + multilingual: meta-llama/Llama-Prompt-Guard-2-86M (gated on Hugging Face:
    # accept the licence, set HF_TOKEN). Anything not labelled benign above the threshold blocks.
    injection_classifier_model: str = ""
    injection_classifier_threshold: float = 0.8

    # --- Identity & Auth ---
    # Mock IdP signing secret (HS256). Dev default is insecure on purpose; set in prod.
    session_secret: str = _DEV_SESSION_SECRET  # >=32 bytes (HS256); refused in prod
    session_ttl_min: int = 30  # short-TTL access token; the client refreshes it silently
    refresh_ttl_days: int = 14  # rotating refresh token (mock IdP); re-login after this
    # Step-up is a server-side grant bound to ONE pending write (its idempotency key), not a
    # long-lived token: it expires after this and is consumed when the write is acted on.
    step_up_grant_ttl_s: int = 300
    step_up_ttl_s: int = 300  # OTP challenge validity window
    default_tenant: str = "t_demo"  # single hardcoded tenant (multi-tenant-ready schema)
    # DEMO_MODE: there is no SMS channel, so the generated OTP is shown to the customer as a
    # simulated SMS popup (labelled "demo" in the UI). Off -> the code is never surfaced.
    demo_mode: bool = False
    otp_max_attempts: int = 5  # wrong guesses before a challenge is burned
    # Server-to-server key for the RM's system (request status updates) and ops calls such as
    # triggering an eval over HTTP. Unset -> those endpoints are disabled. No UI uses it.
    service_api_key: str | None = None

    # --- API edge ---
    # Browser origins allowed by CORS (comma-separated). Set to the deployed frontend in prod.
    frontend_origins: str = "http://localhost:3000"
    # Fixed-window rate limits (Redis). Fail-open if Redis is unreachable (logged).
    rate_limit_enabled: bool = True
    # Header carrying the real client IP behind a proxy (Fly: "Fly-Client-IP"); unset -> socket.
    client_ip_header: str | None = None

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
    eval_live_gap_s: float = 2.0  # pause between live scenarios (free-tier RPM headroom)

    # --- Agent control ---
    max_step_count: int = 25  # supervisor loop guard
    tool_timeout_s: float = 10.0
    conversation_memory_turns: int = 40  # full-chat context window (bounded for token safety)

    # --- Reliability (Phase 5) ---
    llm_retry_cap: int = 2  # retries per provider on retryable errors before fallback
    llm_backoff_base_s: float = 0.3  # jittered exponential backoff between same-provider retries
    llm_breaker_threshold: int = 3  # consecutive failures that open a provider's breaker
    llm_breaker_cooldown_s: float = 60.0
    # Whole-call budgets per role (retries + fallbacks). Past it, only the instant stub runs.
    llm_deadline_triage_s: float = 5.0
    llm_deadline_synthesis_s: float = 10.0
    llm_deadline_judge_s: float = 60.0
    checkpoint_backend: Literal["memory", "postgres"] = "memory"
    worker_metrics_port: int = 9100  # the worker's own /metrics (0 = off)
    worker_concurrency: int = 4  # runs executed at once per worker (same conversation: serial)
    claim_min_idle_ms: int = 30000  # XAUTOCLAIM: reclaim pending entries idle longer than this
    reclaim_batch: int = 10
    # Pending-write execution authority is mortal: past this TTL a suspended write
    # is abandoned and never auto-fires; the orphan reaper closes the run + escalates. The
    # dedup window is tied to the same TTL (keys are purged together).
    pending_write_ttl_s: int = 86400  # 24h
    orphan_ttl_s: int = 86400  # suspended-conversation reaper horizon

    # --- Relationship-manager (RM) requests ---
    # Every escalation becomes a request for the customer's RM; the RM performs all writes to
    # customer data outside Saral. Writes in this list are raised to the RM after OTP + "yes"
    # instead of executing (remove one to let Saral execute it against the core system).
    rm_approval_actions: str = "update_contact,raise_ticket,file_claim"
    # Fernet key (urlsafe base64, 32 bytes) encrypting the requested change at rest. Dev derives
    # one from SESSION_SECRET; prod refuses to boot without it.
    rm_request_key: str = ""

    @model_validator(mode="after")
    def _refuse_insecure_prod(self) -> Settings:
        # The mock IdP signs every session with this secret: a known default in prod would let
        # anyone forge a token for any customer. Fail fast instead of booting insecurely.
        if self.app_env == "prod" and (
            self.session_secret == _DEV_SESSION_SECRET or len(self.session_secret) < 32
        ):
            raise ValueError("SESSION_SECRET must be set to a random >=32-byte value in prod")
        if self.app_env == "prod" and not self.rm_request_key:
            raise ValueError("RM_REQUEST_KEY (Fernet key) must be set in prod")
        return self

    @property
    def rm_actions(self) -> set[str]:
        return {a.strip() for a in self.rm_approval_actions.split(",") if a.strip()}

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.frontend_origins.split(",") if o.strip()]

    @property
    def provider_chain(self) -> list[str]:
        return [p.strip() for p in self.llm_provider_order.split(",") if p.strip()]

    def role_deadline(self, role: str) -> float | None:
        return {
            "triage": self.llm_deadline_triage_s,
            "synthesis": self.llm_deadline_synthesis_s,
            "judge": self.llm_deadline_judge_s,
        }.get(role)

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
