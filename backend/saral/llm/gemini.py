"""Google Gemini provider via its OpenAI-compatible endpoint.

Gemini Flash (free tier) is Hindi-strong, so it serves the synthesis/RAG-explanation role
(ADR-0003). The OpenAI-compat surface lets it reuse OpenAICompatProvider unchanged.
"""

from __future__ import annotations

from saral.config import get_settings
from saral.llm.openai_compat import OpenAICompatProvider


class GeminiProvider(OpenAICompatProvider):
    def __init__(self) -> None:
        s = get_settings()
        super().__init__(
            name="gemini",
            base_url=s.gemini_base_url,
            api_key=s.gemini_api_key,
            model=s.gemini_model,
        )
