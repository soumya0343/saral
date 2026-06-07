"""Typed shared state flowing through the agent graph.

Two orthogonal axes: `auth_level` (token-derived) × `status` (lifecycle, with
suspend states `awaiting_input` / `awaiting_confirmation`). No state-changing tool fires
without a fresh token re-validation in the same transition — suspension never carries
execution authority forward.
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

from saral.schemas import (
    ActionRecord,
    AuthLevel,
    ComplianceDecision,
    EscalationRecord,
    HistoryTurn,
    Intent,
    Language,
    Passage,
    PendingWrite,
    ResponsePayload,
)

RunStatus = Literal[
    "in_progress",
    "awaiting_input", # clarification — soft signal, expects a customer reply
    "awaiting_confirmation",  # write read-back — expects yes/no
    "resolved",
    "escalated",
    "degraded",
    # Abnormal terminal closes: set by the worker / orphan reaper, never the graph.
    "crashed",
    "timed_out",
    "orphaned",
    "abandoned",
]
Route = Literal["respond", "rag", "action", "mixed", "suspend", "end"]


class RunState(BaseModel):
    run_id: str = ""
    conversation_id: str
    tenant_id: str = "t_demo"
    user_id: str # token-derived only
    auth_level: AuthLevel = AuthLevel.SESSION
    raw_message: str
    history: list[HistoryTurn] = Field(default_factory=list)
    known_entities: dict[str, Any] = Field(default_factory=dict)

    language: Language | None = None
    language_hint: Language | None = None  # conversation's established language (sticky detection)
    intents: list[Intent] = Field(default_factory=list)
    entities: dict[str, Any] = Field(default_factory=dict)
    allowed_domains: list[str] = Field(default_factory=list)  # intent→domain map output

    route: Route | None = None
    retrieved: list[Passage] = Field(default_factory=list)
    # Top retrieval score fell below the floor -> ungrounded info answer; escalate not invent
    #. Set by rag_node, consumed by synthesis_node.
    ungrounded: bool = False
    compliance_decisions: list[ComplianceDecision] = Field(default_factory=list)
    actions: list[ActionRecord] = Field(default_factory=list)
    final_response: ResponsePayload | None = None
    escalation: EscalationRecord | None = None

    # --- write-confirmation + step-up ---
    pending_write: PendingWrite | None = None
    intent_nonce: int = 0  # server-incremented; never from the message body
    step_up_owed: bool = False
    challenge_id: str | None = None  # surfaced to the customer when step-up is requested
    # Simulated OTP delivery (no SMS): surfaced as a popup event, never written to a message.
    challenge_otp: str | None = None
    # Resume inputs (set on a confirmation/clarification reply; see /conversations/{id}/reply):
    resume_reply: str | None = None
    challenge_response: str | None = None

    # Specialists that failed; the run continues in degraded mode.
    degraded_agents: Annotated[list[str], operator.add] = Field(default_factory=list)

    status: RunStatus = "in_progress"
    # Additive reducer so parallel branches (rag + action) can both increment in one step.
    step_count: Annotated[int, operator.add] = 0
    # LLM tokens consumed this run (synthesis phrasing); additive across nodes.
    tokens_used: Annotated[int, operator.add] = 0

    @property
    def is_blocked(self) -> bool:
        return any(d.decision in ("block", "escalate") for d in self.compliance_decisions)
