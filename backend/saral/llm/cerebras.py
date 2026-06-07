"""Cerebras provider (OpenAI-compatible). Free fast Llama tier — triage-classify fallback
behind Groq."""

from __future__ import annotations

from saral.config import get_settings
from saral.llm.openai_compat import OpenAICompatProvider


class CerebrasProvider(OpenAICompatProvider):
    def __init__(self) -> None:
        s = get_settings()
        super().__init__(
            name="cerebras",
            base_url=s.cerebras_base_url,
            api_key=s.cerebras_api_key,
            model=s.cerebras_model,
        )
