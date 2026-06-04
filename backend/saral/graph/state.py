"""Typed shared state flowing through the agent graph (TRD §13.1).

Phase 1 populates language / intents / entities via triage. Later phases add retrieved
passages, action records, compliance decisions, and the final response payload.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from saral.schemas import Intent, Language

RunStatus = Literal["in_progress", "resolved", "escalated", "degraded"]


class RunState(BaseModel):
    conversation_id: str
    user_id: str
    raw_message: str

    language: Language | None = None
    intents: list[Intent] = Field(default_factory=list)
    entities: dict[str, Any] = Field(default_factory=dict)

    status: RunStatus = "in_progress"
    step_count: int = 0
