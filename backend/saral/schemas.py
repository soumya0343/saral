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


# --- Streaming trace events (worker -> SSE) ---

TraceEventType = Literal[
    "run_started",
    "agent_started",
    "agent_finished",
    "intent",
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


class RunRequest(BaseModel):
    run_id: str
    conversation_id: str
    user_id: str
    message: str
