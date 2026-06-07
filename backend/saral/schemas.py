"""Shared domain schemas used across agents, the graph, the API, and the worker."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field


class Language(StrEnum):
    EN = "en"
    HI = "hi"
    HINGLISH = "hinglish"


class AuthLevel(StrEnum):
    """Verification tier derived from the customer's token.

    Ordered: a read action needs >= SESSION; a state-changing action needs STEP_UP.
    """

    UNVERIFIED = "unverified"  # no valid token
    SESSION = "session"  # valid token — read actions ok
    STEP_UP = "step_up"  # OTP/known-datum verified — writes ok

    @property
    def rank(self) -> int:
        return {"unverified": 0, "session": 1, "step_up": 2}[self.value]


class CloseReason(StrEnum):
    """Terminal reason a run closed. Abnormal closes are never context-loss:
    the transcript + close_reason + closed_at are retained even when a run ends badly."""

    RESOLVED = "resolved"
    ESCALATED = "escalated"
    DEGRADED = "degraded"
    CRASHED = "crashed"
    TIMED_OUT = "timed_out"
    ORPHANED = "orphaned"
    ABANDONED = "abandoned"


def close_reason_for(status: str) -> CloseReason | None:
    """The CloseReason for a terminal run status; None for non-terminal (awaiting_*, in_progress).

    Terminal statuses share their names with CloseReason values (resolved/escalated/degraded,
    plus the abnormal crashed/timed_out/orphaned/abandoned set the worker sets directly).
    """
    try:
        return CloseReason(status)
    except ValueError:
        return None


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
    scope: Literal["generic", "customer"] = "generic"  # generic corpus vs per-customer doc
    domain: str | None = None  # data domain (per-customer only)

    @property
    def citation(self) -> str:
        return f"{self.doc_id}#{self.chunk_id}"

    @property
    def is_personal(self) -> bool:
        return self.scope == "customer"


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


class ReasonCode(StrEnum):
    """Enum reason a compliance/identity decision was made.

    Recorded in the audit log instead of prose-with-identifiers, so the log leaks no PII; the
    human-readable story is reconstructed from the code via REASON_CODE_DISPLAY.
    """

    NO_GATED_ACTIONS = "no_gated_actions"
    AUTHORIZED = "authorized"
    INJECTION_DETECTED = "injection_detected"
    OWNERSHIP_DENIED = "ownership_denied"  # referenced another customer's id
    AUTHZ_DENIED = "authz_denied"  # not permitted for this action
    STEP_UP_OWED = "step_up_owed"
    STEP_UP_FAILED = "step_up_failed"
    RETRIEVAL_BELOW_FLOOR = "retrieval_below_floor"
    ORPHAN_TTL = "orphan_ttl"


REASON_CODE_DISPLAY: dict[str, str] = {
    ReasonCode.NO_GATED_ACTIONS: "No gated actions in this request.",
    ReasonCode.AUTHORIZED: "Action authorized for this customer.",
    ReasonCode.INJECTION_DETECTED: "Prompt-injection / manipulation detected.",
    ReasonCode.OWNERSHIP_DENIED: "Request referenced another customer's record.",
    ReasonCode.AUTHZ_DENIED: "Customer not authorized for this action.",
    ReasonCode.STEP_UP_OWED: "Step-up verification required before this write.",
    ReasonCode.STEP_UP_FAILED: "Step-up verification failed.",
    ReasonCode.RETRIEVAL_BELOW_FLOOR: "No grounded answer above the relevance floor.",
    ReasonCode.ORPHAN_TTL: "Suspended request abandoned past its TTL.",
}


class ComplianceDecision(BaseModel):
    decision: ComplianceVerdict
    reason: str  # human-readable detail (live logs only; never the audit's PII-free record)
    actor: str = "compliance"  # which agent/check produced it
    action: str | None = None  # the action being gated, if any
    reason_code: ReasonCode | None = None  # enum recorded in the audit log (PII-free)


# --- Write confirmation + identifiers ---


class PendingWrite(BaseModel):
    """A state-changing action read back, awaiting the customer's confirmation.

    Its execution authority is mortal: a stale pending write never
    auto-fires — resume re-earns step-up + confirm. The idempotency key is conversation-anchored
    (not run-anchored) so a crash-resume re-uses the same key.
    """

    tool: str
    field: str | None = None
    value: str | None = None
    args: dict[str, Any] = Field(default_factory=dict)
    read_back: str  # human-facing "Update X to Y — confirm? (yes/no)"
    idempotency_key: str
    intent_nonce: int
    created_at: float | None = None  # epoch seconds; past TTL the write is abandoned, never fires


# --- Escalation / human handoff ---


class EscalationRecord(BaseModel):
    conversation_id: str
    tenant_id: str
    detected_intent: str
    attempted_actions: list[str] = Field(default_factory=list)
    blocking_reason: str
    transcript_ref: str
    pending_action: str | None = None
    sla_target: str  # e.g. "4h"


# --- Final response (Synthesis output schema) ---

ResolutionStatus = Literal["resolved", "escalated", "blocked", "degraded", "awaiting"]


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
    user_id: str # token-derived only; never read from the message body
    tenant_id: str = "t_demo"
    auth_level: AuthLevel = AuthLevel.SESSION  # derived from the validated session token
    message: str
    history: list[HistoryTurn] = Field(default_factory=list)  # short-term memory (last N turns)
    # Customer's own ids (policy_id/claim_id) so "my claim status" resolves without an ID.
    known_entities: dict[str, Any] = Field(default_factory=dict)
    # Resume of a suspended run: the customer's reply to a confirmation / clarification.
    resume_reply: str | None = None
    pending_write: PendingWrite | None = None
    intent_nonce: int = 0
    # Step-up challenge echoed back by the customer (the OTP), validated on resume.
    challenge_id: str | None = None
    challenge_response: str | None = None
