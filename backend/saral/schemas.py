"""Shared domain schemas used across agents, the graph, the API, and the worker."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field


class Language(StrEnum):
    EN = "en"
    HI = "hi"
    HINGLISH = "hinglish"


class IntentType(StrEnum):
    """Coarse intent classes that drive supervisor routing (Phase 2)."""

    INFORMATION = "information"  # policy/FAQ question -> Retrieval
    ACTION = "action"  # account action -> Compliance + Action
    COMPLAINT = "complaint"  # grievance -> escalation-leaning
    SMALL_TALK = "small_talk"  # greetings / chit-chat
    UNKNOWN = "unknown"


class Intent(BaseModel):
    type: IntentType
    # Specific action name when type == ACTION (e.g. "update_contact", "get_claim_status").
    action: str | None = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class IntentResult(BaseModel):
    """Output of the Triage agent."""

    language: Language
    intents: list[Intent] = Field(default_factory=list)
    entities: dict[str, Any] = Field(default_factory=dict)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)

    @property
    def is_mixed(self) -> bool:
        kinds = {i.type for i in self.intents}
        return IntentType.INFORMATION in kinds and IntentType.ACTION in kinds

    @property
    def primary(self) -> Intent | None:
        return max(self.intents, key=lambda i: i.confidence, default=None)


# --- Retrieval ---


class Passage(BaseModel):
    """A retrieved chunk with its source citation."""

    doc_id: str  # source filename (citation)
    chunk_id: int
    text: str
    score: float = 0.0

    @property
    def citation(self) -> str:
        return f"{self.doc_id}#{self.chunk_id}"


# --- Account actions ---


class ActionRecord(BaseModel):
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = None
    ok: bool = False
    result: dict[str, Any] | None = None
    error: str | None = None
    needs_clarification: str | None = None  # prompt to re-ask the user


# --- Compliance ---

ComplianceVerdict = Literal["allow", "block", "escalate"]


class ComplianceDecision(BaseModel):
    decision: ComplianceVerdict
    reason: str
    actor: str = "compliance"  # which agent/check produced it
    action: str | None = None  # the action being gated, if any


# --- Final response (Synthesis output schema, TRD §12.6) ---

ResolutionStatus = Literal["resolved", "escalated", "blocked", "degraded"]


class ResponsePayload(BaseModel):
    resolution_status: ResolutionStatus
    message: str
    actions_taken: list[str] = Field(default_factory=list)
    citations: list[str] = Field(default_factory=list)
    escalated: bool = False


# --- Streaming trace events (worker -> SSE) ---

TraceEventType = Literal[
    "run_started",
    "agent_started",
    "agent_finished",
    "intent",
    "route",
    "retrieval",
    "compliance",
    "action",
    "synthesis",
    "final",
    "error",
    "run_finished",
]


class TraceEvent(BaseModel):
    type: TraceEventType
    run_id: str
    conversation_id: str
    agent: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)


# --- Run bus message (API -> Redis Stream -> worker) ---


class HistoryTurn(BaseModel):
    role: str
    content: str


class RunRequest(BaseModel):
    run_id: str
    conversation_id: str
    user_id: str
    message: str
    history: list[HistoryTurn] = Field(default_factory=list)  # short-term memory (last N turns)
