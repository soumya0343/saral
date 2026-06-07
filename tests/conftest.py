"""Test isolation: force deterministic, offline settings before any Settings load.

Without this, pydantic-settings reads the developer's `.env` (real Sarvam key, Postgres
store) into the test run — making tests slow and non-reproducible. Setting these in the
process environment takes precedence over `.env`, so tests always use the stub LLM and the
in-memory SQLite store.
"""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ["LLM_PROVIDER_ORDER"] = "stub"
# Force every per-role chain to the stub too — triage now classifies via the LLM layer, so
# without this a real key in .env would make tests hit the network / be non-deterministic.
os.environ["LLM_ROLE_SYNTHESIS"] = "stub"
os.environ["LLM_ROLE_TRIAGE"] = "stub"
os.environ["LLM_ROLE_JUDGE"] = "stub"
os.environ["SARVAM_API_KEY"] = ""
os.environ["ANTHROPIC_API_KEY"] = ""
os.environ["GROQ_API_KEY"] = ""
os.environ["CEREBRAS_API_KEY"] = ""
os.environ["GEMINI_API_KEY"] = ""
os.environ["STORE_BACKEND"] = "memory"
# Hashing floor: deterministic + dependency-free, so eval/tests are reproducible and don't
# pull the multilingual-e5 weights (ADR-0003: hashing is the offline/CI floor).
os.environ["EMBEDDER"] = "hashing"
