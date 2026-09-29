"""Case summary for the RM: what was asked, what was tried, why it escalated, relevant clauses.

Always in English (the RM's working language) even when the chat was in Hindi/Hinglish. The
deterministic summary is built from the run's own state, so every request has one; when a real
model is available it rewrites that into readable prose, and the rewrite is kept only if every
number / id in it traces to the facts. Neither version carries the requested value (new mobile,
claim amount) — that lives only in the encrypted `requested_change`.
"""

from __future__ import annotations

import contextlib

from saral.compliance.pii import redact_pii
from saral.graph.state import RunState
from saral.llm.base import LLMError, Message
from saral.llm.factory import get_llm
from saral.logging import get_logger

log = get_logger(__name__)

_LANG = {"en": "English", "hi": "Hindi", "hinglish": "Hinglish (Hindi in Roman script)"}
_REASON = {
    "awaiting_manager_approval": "the customer verified with a one-time code and confirmed a "
    "change that only the RM may apply",
    "step_up_failed": "the customer could not verify the one-time code",
    "clarification_exhausted": "the agent could not get a valid value after repeated attempts",
    "retrieval_below_floor": "the agent found no confident, grounded answer",
    "repeated_complaint": "the customer is still dissatisfied after being offered a ticket",
    "compliance_block_or_low_confidence": "a compliance check blocked the request or the agent "
    "was not confident",
}
_WHAT = {
    "update_contact": "change the registered {field} (new value is in the encrypted request)",
    "file_claim": "file a new claim (incident date, amount, hospital/garage and description are "
    "in the encrypted request)",
    "raise_ticket": "raise a support ticket (subject is in the encrypted request)",
}


def facts(state: RunState, kind: str, reason: str) -> list[str]:
    lang = str(state.language or "en")
    out = [
        f"Customer: {state.user_id} (chat language: {_LANG.get(lang, lang)}).",
        f"Customer's words (PII redacted): \"{redact_pii(state.raw_message)[:300]}\".",
    ]
    pw = state.pending_write
    if pw is not None and kind == pw.tool:
        out.append("Requested: " + _WHAT.get(pw.tool, pw.tool).format(field=pw.field or "") + ".")
    intents = sorted({i.action or str(i.type) for i in state.intents})
    if intents:
        out.append("Detected intent: " + ", ".join(intents) + ".")
    tried = [f"{a.tool} ({'ok' if a.ok else 'failed'})" for a in state.actions]
    if tried:
        out.append("Tools tried: " + ", ".join(tried) + ".")
    blocks = [d.reason for d in state.compliance_decisions if d.decision == "block"]
    if blocks:
        out.append("Compliance: " + "; ".join(blocks)[:200] + ".")
    if state.retrieved:
        out.append(
            "Relevant sources: " + ", ".join(p.citation for p in state.retrieved[:3]) + "."
        )
    out.append(f"Escalated because {_REASON.get(reason, reason.replace('_', ' '))}.")
    return out


def deterministic_summary(state: RunState, kind: str, reason: str) -> str:
    return " ".join(facts(state, kind, reason))


_PROMPT = (
    "Write a case summary for a bank/insurer relationship manager, in English, 3-5 plain "
    "sentences: what the customer asked (translate if needed), what the assistant did, why it "
    "was handed over, and which policy clauses/sources matter. Use ONLY these facts; do not add "
    "phone numbers, e-mails, amounts or values that are not in them.\n\nFacts:\n{facts}"
)


async def case_summary(state: RunState, kind: str, reason: str) -> str:
    base = deterministic_summary(state, kind, reason)
    llm = get_llm("synthesis")
    if not llm.has_real_provider:
        return base
    with contextlib.suppress(LLMError):
        from saral.agents.synthesis import _grounded

        text = await llm.complete(
            [Message(role="user", content=_PROMPT.format(facts=base))], max_tokens=400
        )
        text = redact_pii(text.strip())
        if text and not text.startswith("[stub]") and _grounded(text, base):
            return text
        log.info("rm.summary_rejected")
    return base
