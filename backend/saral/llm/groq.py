"""Groq provider (OpenAI-compatible). Fastest free Llama tier — the latency-critical triage
intent-classify role (ADR-0003)."""

from __future__ import annotations

from saral.config import get_settings
from saral.llm.openai_compat import OpenAICompatProvider


class GroqProvider(OpenAICompatProvider):
    def __init__(self) -> None:
        s = get_settings()
        super().__init__(
            name="groq",
            base_url=s.groq_base_url,
            api_key=s.groq_api_key,
            model=s.groq_model,
        )
