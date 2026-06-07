"""Test isolation: force deterministic, offline settings before any Settings load.

Without this, pydantic-settings reads the developer's `.env` (real Sarvam key, Postgres
store) into the test run — making tests slow and non-reproducible. Setting these in the
process environment takes precedence over `.env`, so tests always use the stub LLM and the
in-memory SQLite store.
"""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ["LLM_PROVIDER_ORDER"] = "stub"
os.environ["SARVAM_API_KEY"] = ""
os.environ["ANTHROPIC_API_KEY"] = ""
os.environ["STORE_BACKEND"] = "memory"
# Hashing floor: deterministic + dependency-free, so eval/tests are reproducible and don't
# pull the multilingual-e5 weights (ADR-0003: hashing is the offline/CI floor).
os.environ["EMBEDDER"] = "hashing"
