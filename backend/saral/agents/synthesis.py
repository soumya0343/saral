"""Synthesis / Response agent.

Composes the final answer in the user's language as a JSON-schema-constrained
ResponsePayload (TRD §12.6). Every factual claim is bound to a citation surfaced by the
RAG agent — the agent never asserts what retrieval did not return.

The deterministic stub composer doubles as the no-key path and keeps eval reproducible.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from saral.llm.base import Message
from saral.llm.factory import get_llm
from saral.llm.stub import register_structured_handler
from saral.schemas import (
    ActionRecord,
    ComplianceDecision,
    Language,
    Passage,
    ResponsePayload,
)


class SynthesisContext(BaseModel):
    """Everything synthesis is allowed to use — passed to the LLM and the stub composer."""

    language: Language = Language.EN
    message: str = ""
    passages: list[Passage] = Field(default_factory=list)
    actions: list[ActionRecord] = Field(default_factory=list)
    decisions: list[ComplianceDecision] = Field(default_factory=list)


# --- Language-specific templates (stub composer) ---

_BLOCKED = {
    Language.EN: "I can't process this request for security reasons. I'm escalating it to a human agent.",  # noqa: E501
    Language.HI: "सुरक्षा कारणों से मैं यह अनुरोध संसाधित नहीं कर सकता। इसे मानव एजेंट को भेजा जा रहा है।",  # noqa: E501
    Language.HINGLISH: "Security reasons se main yeh request process nahi kar sakta. Ise human agent ko bhej raha hoon.",  # noqa: E501
}
_UNAUTHORIZED = {
    Language.EN: "I'm not able to authorize this action on your account. Escalating to a human agent.",  # noqa: E501
    Language.HI: "मैं आपके खाते पर यह कार्रवाई अधिकृत नहीं कर सकता। मानव एजेंट को भेजा जा रहा है।",  # noqa: E501
    Language.HINGLISH: "Main yeh action authorize nahi kar sakta. Human agent ko escalate kar raha hoon.",  # noqa: E501
}
_SOURCED = {
    Language.EN: "Based on our policy ({cite}): {answer}",
    Language.HI: "हमारी पॉलिसी के अनुसार ({cite}): {answer}",
    Language.HINGLISH: "Hamari policy ke according ({cite}): {answer}",
}


def _action_summary(rec: ActionRecord, lang: Language) -> str:
    if rec.needs_clarification:
        return rec.needs_clarification
    if not rec.ok:
        return f"Could not complete {rec.tool}: {rec.error}"
    if rec.tool == "get_claim_status":
        r = rec.result or {}
        return f"Claim {r.get('claim_id')} status: {r.get('status')} ({r.get('note', '')})".strip()
    if rec.tool == "get_policy_details":
        r = rec.result or {}
        return f"Policy {r.get('policy_id')}: {r.get('product')}, status {r.get('status')}."
    if rec.tool == "update_contact":
        ch = (rec.result or {}).get("changed", {})
        return f"Updated your {ch.get('field')} to {ch.get('new')}."
    if rec.tool == "raise_ticket":
        return f"Raised ticket {(rec.result or {}).get('ticket_id')}."
    return f"{rec.tool} done."


def compose(ctx: SynthesisContext) -> ResponsePayload:
    lang = ctx.language
    injection_block = any(
        d.actor == "injection_guard" and d.decision == "block" for d in ctx.decisions
    )
    authz_block = any(d.actor == "authz" and d.decision == "block" for d in ctx.decisions)

    if injection_block:
        return ResponsePayload(
            resolution_status="blocked",
            message=_BLOCKED.get(lang, _BLOCKED[Language.EN]),
            escalated=True,
        )
    if authz_block:
        return ResponsePayload(
            resolution_status="escalated",
            message=_UNAUTHORIZED.get(lang, _UNAUTHORIZED[Language.EN]),
            escalated=True,
        )

    parts: list[str] = []
    actions_taken: list[str] = []
    escalated = False
    for rec in ctx.actions:
        parts.append(_action_summary(rec, lang))
        if rec.ok:
            actions_taken.append(rec.tool)
        if rec.needs_clarification:
            escalated = True

    citations: list[str] = []
    if ctx.passages:
        top = ctx.passages[0]
        citations = [p.citation for p in ctx.passages]
        answer = top.text if len(top.text) <= 280 else top.text[:277] + "…"
        template = _SOURCED.get(lang, _SOURCED[Language.EN])
        parts.append(template.format(cite=top.citation, answer=answer))

    message = " ".join(p for p in parts if p) or "How can I help with your policy or account?"
    return ResponsePayload(
        resolution_status="escalated" if escalated else "resolved",
        message=message,
        actions_taken=actions_taken,
        citations=citations,
        escalated=escalated,
    )


def _stub_handler(messages: list[Message]) -> ResponsePayload:
    raw = next((m.content for m in reversed(messages) if m.role == "user"), "{}")
    ctx = SynthesisContext.model_validate_json(raw)
    return compose(ctx)


register_structured_handler(ResponsePayload, _stub_handler)


_SYSTEM_PROMPT = (
    "You are the synthesis agent. Compose the final customer reply in the SAME language as "
    "the customer (en/hi/hinglish). You may ONLY state facts present in the provided passages, "
    "and you MUST cite them. Summarize completed actions. If compliance blocked the request, "
    "explain briefly and set escalated=true. Return a ResponsePayload."
)


class SynthesisAgent:
    name = "synthesis"

    def __init__(self) -> None:
        self._llm = get_llm()

    async def run(self, ctx: SynthesisContext) -> ResponsePayload:
        messages = [
            Message(role="system", content=_SYSTEM_PROMPT),
            Message(role="user", content=ctx.model_dump_json()),
        ]
        return await self._llm.structured(messages, ResponsePayload)
