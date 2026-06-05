"""Typed shared state flowing through the agent graph (TRD §13.1).

Phase 1 populated language / intents / entities via triage. Phase 2 adds retrieved
passages and account-action records, plus a routing decision. Later phases add compliance
decisions and the final response payload.
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

from saral.schemas import (
    ActionRecord,
    ComplianceDecision,
    HistoryTurn,
    Intent,
    Language,
    Passage,
    ResponsePayload,
)

RunStatus = Literal["in_progress", "resolved", "escalated", "degraded"]
Route = Literal["respond", "rag", "action", "mixed", "end"]


class RunState(BaseModel):
    run_id: str = ""
    conversation_id: str
    user_id: str
    raw_message: str
    history: list[HistoryTurn] = Field(default_factory=list)
    known_entities: dict[str, Any] = Field(default_factory=dict)

    language: Language | None = None
    intents: list[Intent] = Field(default_factory=list)
    entities: dict[str, Any] = Field(default_factory=dict)

    route: Route | None = None
    retrieved: list[Passage] = Field(default_factory=list)
    compliance_decisions: list[ComplianceDecision] = Field(default_factory=list)
    actions: list[ActionRecord] = Field(default_factory=list)
    final_response: ResponsePayload | None = None

    # Specialists that failed; the run continues in degraded mode (TRD §15).
    degraded_agents: Annotated[list[str], operator.add] = Field(default_factory=list)

    status: RunStatus = "in_progress"
    # Additive reducer so parallel branches (rag + action) can both increment in one step.
    step_count: Annotated[int, operator.add] = 0

    @property
    def is_blocked(self) -> bool:
        return any(d.decision in ("block", "escalate") for d in self.compliance_decisions)
