"""LLM-interpreted resume routing (Option B).

When a suspended run is resumed, the customer's reply may be a confirmation (yes), a rejection
(no/cancel), the missing value, a field correction ("nahi, mobile"), or an entirely different
request. An LLM classifies it from the suspend context + recent history; a deterministic
fallback (parse_confirmation + value-shape) covers offline/eval and any LLM failure.

The actual write trigger stays deterministic downstream: the route normalizes a "confirm" to the
canonical "yes" and identity_node still re-parses yes/no before acting — so the LLM understands
phrasing but never fires a write by itself.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel

from saral.actions.confirm import parse_confirmation
from saral.llm.base import LLMError, Message
from saral.llm.factory import get_llm
from saral.logging import get_logger
from saral.schemas import HistoryTurn, PendingWrite

log = get_logger(__name__)

ResumeKind = Literal["confirm", "reject", "value", "correction", "other"]


class ResumeVerdict(BaseModel):
    kind: ResumeKind = "other"


_VALUE_SHAPE = re.compile(r"\d{4,}|@")


def _deterministic(suspend_status: str, reply: str) -> ResumeVerdict:
    """Fallback: yes/no via the confirmation lexicon, value via shape (digits / '@')."""
    c = parse_confirmation(reply)
    if suspend_status == "awaiting_confirmation":
        if c == "yes":
            return ResumeVerdict(kind="confirm")
        if c == "no":
            return ResumeVerdict(kind="reject")
        return ResumeVerdict(kind="other")
    # clarification (awaiting_input, no OTP challenge)
    if c == "no":
        return ResumeVerdict(kind="reject")
    if _VALUE_SHAPE.search(reply or ""):
        return ResumeVerdict(kind="value")
    return ResumeVerdict(kind="other")


_SYSTEM = (
    "You route a customer's reply inside a suspended support flow. The assistant previously asked "
    "the customer to either CONFIRM a pending change (yes/no) or PROVIDE a missing value (a new "
    "mobile number or email). Classify the reply into exactly one kind:\n"
    "- confirm: agrees to proceed (yes, haan, ok, go ahead, pakka, kar do, theek hai)\n"
    "- reject: declines or cancels (no, nahi, cancel, rehne do, mat karo)\n"
    "- value: supplies the requested value (a phone number or an email address)\n"
    "- correction: changes WHICH field to update (e.g. 'nahi, mobile' when you asked about email)\n"
    "- other: an unrelated new request or question\n"
    "Use the conversation context to decide. Return only the kind."
)


async def interpret_resume(
    suspend_status: str,
    pending: PendingWrite | None,
    original: str,
    reply: str,
    history: list[HistoryTurn],
) -> ResumeVerdict:
    llm = get_llm("triage")
    if not llm.has_real_provider:
        return _deterministic(suspend_status, reply)
    ctx = (
        f"Suspend state: {suspend_status}. "
        f"Pending change: {pending.read_back if pending else 'asking for a contact value'}. "
        f"Original request: {original!r}."
    )
    msgs = [Message(role="system", content=_SYSTEM + "\n" + ctx)]
    for h in history[-8:]:
        role = "assistant" if h.role == "assistant" else "user"
        msgs.append(Message(role=role, content=h.content))
    msgs.append(Message(role="user", content=reply))
    try:
        return await llm.structured(msgs, ResumeVerdict, max_tokens=10)
    except LLMError as e:
        log.warning("resume.llm_failed", error=str(e))
        return _deterministic(suspend_status, reply)
