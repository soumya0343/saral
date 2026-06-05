"""Synthesis / Response agent.

Composes the final answer in the user's language as a JSON-schema-constrained
ResponsePayload (TRD §12.6). Every factual claim is bound to a citation surfaced by the
RAG agent — the agent never asserts what retrieval did not return.

The deterministic stub composer doubles as the no-key path and keeps eval reproducible.
"""

from __future__ import annotations

import contextlib

from pydantic import BaseModel, Field

from saral.llm.base import LLMError, Message
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
    degraded: list[str] = Field(default_factory=list)


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
        if rec.tool == "get_claim_status":
            return "You don't have any claims on file yet. Would you like to file one?"
        return f"Could not complete {rec.tool}: {rec.error}"
    if rec.tool == "get_claim_status":
        r = rec.result or {}
        return f"Claim {r.get('claim_id')} status: {r.get('status')} ({r.get('note', '')})".strip()
    if rec.tool == "file_claim":
        r = rec.result or {}
        return (
            f"Filed your claim {r.get('claim_id')} — status {r.get('status')}. "
            f"{r.get('note', '')}"
        ).strip()
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

    if ctx.degraded:
        parts.append(
            "(Some information is temporarily unavailable; a human agent will follow up.)"
        )

    message = " ".join(p for p in parts if p) or "How can I help with your policy or account?"
    status = "resolved"
    if escalated:
        status = "escalated"
    elif ctx.degraded:
        status = "degraded"
    return ResponsePayload(
        resolution_status=status,
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


_LANG_NAME = {Language.EN: "English", Language.HI: "Hindi", Language.HINGLISH: "Hinglish"}

_PHRASING_PROMPT = (
    "You are a customer-support agent for an insurer. Write a SHORT, warm reply (1-3 "
    "sentences) in {lang}. Use ONLY the facts below — do not invent policy details, numbers, "
    "or outcomes. If a source is given, you may reference it. Answer directly; do not show "
    "your reasoning.\n\nFacts:\n{facts}"
)


class SynthesisAgent:
    name = "synthesis"

    def __init__(self) -> None:
        self._llm = get_llm()

    async def run(self, ctx: SynthesisContext) -> ResponsePayload:
        # Deterministic structure (status / citations / actions) — never delegated.
        payload = compose(ctx)
        # Real-LLM phrasing of the customer-facing message when a model is available, and
        # only for normal replies (safety/escalation wording stays deterministic).
        if (
            self._llm.has_real_provider
            and payload.resolution_status in ("resolved", "degraded")
            and not ctx.degraded
        ):
            with contextlib.suppress(LLMError):
                payload.message = await self._phrase(ctx, payload)  # else keep deterministic draft
        return payload

    async def _phrase(self, ctx: SynthesisContext, payload: ResponsePayload) -> str:
        facts: list[str] = []
        for rec in ctx.actions:
            facts.append(f"- action: {_action_summary(rec, ctx.language)}")
        for p in ctx.passages[:3]:
            facts.append(f"- source {p.citation}: {p.text}")
        if not facts:
            facts.append(f"- draft: {payload.message}")
        prompt = _PHRASING_PROMPT.format(
            lang=_LANG_NAME.get(ctx.language, "English"), facts="\n".join(facts)
        )
        messages = [
            Message(role="system", content=prompt),
            Message(role="user", content=ctx.message),
        ]
        text = await self._llm.complete(messages, max_tokens=1500)
        return text.strip() or payload.message
