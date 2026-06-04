"""Typed shared state flowing through the agent graph (TRD §13.1).

Phase 1 populated language / intents / entities via triage. Phase 2 adds retrieved
passages and account-action records, plus a routing decision. Later phases add compliance
decisions and the final response payload.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from saral.schemas import ActionRecord, Intent, Language, Passage

RunStatus = Literal["in_progress", "resolved", "escalated", "degraded"]
Route = Literal["rag", "action", "end"]


class RunState(BaseModel):
    run_id: str = ""
    conversation_id: str
    user_id: str
    raw_message: str

    language: Language | None = None
    intents: list[Intent] = Field(default_factory=list)
    entities: dict[str, Any] = Field(default_factory=dict)

    route: Route | None = None
    retrieved: list[Passage] = Field(default_factory=list)
    actions: list[ActionRecord] = Field(default_factory=list)

    status: RunStatus = "in_progress"
    step_count: int = 0
