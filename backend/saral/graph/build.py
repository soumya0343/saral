"""Supervisor-orchestrated agent graph.

The supervisor routes; specialists work. Identity runs before any data read; Compliance is a
gate, not a peer. State-changing actions pass through step-up → write-confirmation before they
execute, and no write fires without a fresh token re-validation in the same transition.

    START -> triage -> compliance -> identity -> supervisor -(route)->
        { synthesis                 (respond / injection-blocked -> escalate)
        | END                       (suspend: awaiting_input / awaiting_confirmation / abandon)
        | rag -> synthesis          (information; per-customer scoped retrieval)
        | action -> synthesis       (reads, or a CONFIRMED write executed idempotently)
        | {rag, action} -> synthesis (mixed: parallel fan-out) }
    synthesis -> END
"""

from __future__ import annotations

import re
from functools import lru_cache

from langgraph.graph import END, START, StateGraph

from saral.actions.confirm import build_pending_write, is_stale, parse_confirmation
from saral.agents.action import ActionAgent
from saral.agents.rag import RagAgent
from saral.agents.synthesis import SynthesisAgent, SynthesisContext
from saral.agents.triage import TriageAgent
from saral.auth.identity import identity_gate
from saral.auth.stepup import request_step_up
from saral.authz import allowed_domains as domains_for
from saral.authz import requires_step_up
from saral.compliance.gate import ComplianceGate
from saral.compliance.pii import redact_pii
from saral.config import get_settings
from saral.graph.state import RunState
from saral.logging import get_logger
from saral.schemas import (
    EscalationRecord,
    IntentType,
    Language,
    ResponsePayload,
)

log = get_logger(__name__)

_triage = TriageAgent()
_rag = RagAgent()
_action = ActionAgent()
_gate = ComplianceGate()
_synth = SynthesisAgent()

# Deterministic reply when an info answer is ungrounded (retrieval below floor) — we escalate
# rather than invent a clause we have no passage for.
_UNGROUNDED = {
    Language.EN: "I don't have a confident answer for that, so I'm connecting you with a human agent who can help.",  # noqa: E501
    Language.HI: "मेरे पास इसका भरोसेमंद उत्तर नहीं है, इसलिए मैं आपको एक मानव एजेंट से जोड़ रहा हूँ।",  # noqa: E501
    Language.HINGLISH: "Mere paas iska confident answer nahi hai, isliye main aapko ek human agent se connect kar raha hoon.",  # noqa: E501
}

# Language-matched identity-gate prompts (reply in the customer's language, not always English).
_CANCELLED = {
    Language.EN: "Okay, I've cancelled that change. Anything else?",
    Language.HI: "ठीक है, मैंने वह बदलाव रद्द कर दिया है। और कुछ?",
    Language.HINGLISH: "Theek hai, maine wo change cancel kar diya hai. Aur kuch?",
}
_CLARIFY_CONTACT = {
    Language.EN: "What should I update — mobile or email — and to what value?",
    Language.HI: "मैं क्या अपडेट करूँ — मोबाइल या ईमेल — और किस मान में?",
    Language.HINGLISH: "Main kya update karu — mobile ya email — aur kis value me?",
}
# Field is known but the new value is missing — ask specifically for the value.
_CLARIFY_MOBILE = {
    Language.EN: "Sure — what is the new mobile number you'd like to set?",
    Language.HI: "ज़रूर — आप कौन-सा नया मोबाइल नंबर सेट करना चाहती हैं?",
    Language.HINGLISH: "Zaroor — aap naya mobile number kya set karna chahti hain?",
}
_CLARIFY_EMAIL = {
    Language.EN: "Sure — what is the new email address you'd like to set?",
    Language.HI: "ज़रूर — आप कौन-सा नया ईमेल पता सेट करना चाहती हैं?",
    Language.HINGLISH: "Zaroor — aap naya email address kya set karna chahti hain?",
}
_EMAIL_WORDS = ("email", "e-mail", "mail", "ईमेल", "मेल")
_MOBILE_WORDS = ("mobile", "number", "phone", "नंबर", "नम्बर", "मोबाइल", "फोन")
# Value present but INVALID — corrective prompts so a bad attempt isn't shown the same clarify
# text verbatim (which reads as a stuck loop). A valid mobile is 10 digits starting 6–9.
_INVALID_MOBILE = {
    Language.EN: "That doesn't look like a valid mobile number. Please send a 10-digit number starting with 6–9.",  # noqa: E501
    Language.HI: "यह मान्य मोबाइल नंबर नहीं लगता। कृपया 6–9 से शुरू होने वाला 10 अंकों का नंबर भेजें।",  # noqa: E501
    Language.HINGLISH: "Yeh valid mobile number nahi lag raha. Kripya 6–9 se shuru hone wala 10-digit number bhejein.",  # noqa: E501
}
_INVALID_EMAIL = {
    Language.EN: "That doesn't look like a valid email address. Please send it like name@example.com.",  # noqa: E501
    Language.HI: "यह मान्य ईमेल पता नहीं लगता। कृपया इसे name@example.com जैसे भेजें।",
    Language.HINGLISH: "Yeh valid email address nahi lag raha. Kripya ise name@example.com jaise bhejein.",  # noqa: E501
}
# Repeated-failure help: spell out exactly what's expected + the escape hatch (ask something
# else). Shown on the 2nd consecutive clarify so a confused customer isn't stuck on terse text.
_CLARIFY_MOBILE_HELP = {
    Language.EN: "I still need the new mobile number to make this change. Please send just the 10 digits (starting 6–9), e.g. 9876543210 — or tell me if you'd like to do something else.",  # noqa: E501
    Language.HI: "इस बदलाव के लिए मुझे नया मोबाइल नंबर चाहिए। कृपया केवल 10 अंक (6–9 से शुरू) भेजें, जैसे 9876543210 — या बताएं कि आप कुछ और करना चाहती हैं।",  # noqa: E501
    Language.HINGLISH: "Is change ke liye mujhe naya mobile number chahiye. Kripya sirf 10 digit (6–9 se shuru) bhejein, jaise 9876543210 — ya bataiye agar aap kuch aur karna chahti hain.",  # noqa: E501
}
_CLARIFY_EMAIL_HELP = {
    Language.EN: "I still need the new email address to make this change. Please send it like name@example.com — or tell me if you'd like to do something else.",  # noqa: E501
    Language.HI: "इस बदलाव के लिए मुझे नया ईमेल पता चाहिए। कृपया इसे name@example.com जैसे भेजें — या बताएं कि आप कुछ और करना चाहती हैं।",  # noqa: E501
    Language.HINGLISH: "Is change ke liye mujhe naya email address chahiye. Kripya ise name@example.com jaise bhejein — ya bataiye agar aap kuch aur karna chahti hain.",  # noqa: E501
}
# After repeated failures, stop looping — hand to a human.
_CLARIFY_GIVEUP = {
    Language.EN: "I'm having trouble getting the new value, so I'm connecting you with a human agent who can help.",  # noqa: E501
    Language.HI: "मुझे नया मान समझने में दिक्कत हो रही है, इसलिए मैं आपको एक मानव एजेंट से जोड़ रहा हूँ।",  # noqa: E501
    Language.HINGLISH: "Mujhe naya value samajhne me dikkat ho rahi hai, isliye main aapko ek human agent se connect kar raha hoon.",  # noqa: E501
}
_NUM_RUN_RE = re.compile(r"\d{4,}")

# Every clarify/invalid/help string we may emit — used to count consecutive clarify rounds
# in the history so we can escalate the wording (and finally a human) instead of repeating.
_ALL_CLARIFY_MSGS = {
    v
    for table in (
        _CLARIFY_CONTACT, _CLARIFY_MOBILE, _CLARIFY_EMAIL,
        _INVALID_MOBILE, _INVALID_EMAIL,
        _CLARIFY_MOBILE_HELP, _CLARIFY_EMAIL_HELP,
    )
    for v in table.values()
}


def _prior_clarify_count(history) -> int:
    """How many clarify prompts we've sent in the current unbroken clarify run (most recent
    assistant turns). Resets once the assistant says anything that isn't a clarify."""
    count = 0
    for h in reversed(history):
        if h.role != "assistant":
            continue
        if h.content in _ALL_CLARIFY_MSGS:
            count += 1
        else:
            break
    return count


def _contact_field_hint(text: str) -> str | None:
    """Which contact field the customer means, even without a value yet."""
    low = text.lower()
    if any(w in low for w in _EMAIL_WORDS):
        return "email"
    if any(w in low for w in _MOBILE_WORDS):
        return "mobile"
    return None


def _clarify_table(message: str, field: str | None, detailed: bool = False) -> dict:
    """Pick the clarify/correction prompt. If a value was attempted but couldn't be parsed
    into a valid mobile/email (build returned None), correct it; otherwise ask for the value.
    `detailed` switches to the spelled-out help wording on a repeated failure."""
    if "@" in message:
        return _INVALID_EMAIL  # '@' present yet no valid email extracted -> malformed
    if _NUM_RUN_RE.search(message):
        return _INVALID_MOBILE  # digits present yet no valid mobile extracted -> invalid number
    if field == "mobile":
        return _CLARIFY_MOBILE_HELP if detailed else _CLARIFY_MOBILE
    if field == "email":
        return _CLARIFY_EMAIL_HELP if detailed else _CLARIFY_EMAIL
    return _CLARIFY_CONTACT
_STEPUP_FAILED = {
    Language.EN: "I couldn't verify the one-time code, so I've escalated this to a human agent who will follow up.",  # noqa: E501
    Language.HI: "मैं वन-टाइम कोड सत्यापित नहीं कर सका, इसलिए इसे एक मानव एजेंट को भेज दिया है जो आगे संपर्क करेगा।",  # noqa: E501
    Language.HINGLISH: "Main one-time code verify nahi kar paaya, isliye maine ise human agent ko escalate kar diya hai jo follow up karega.",  # noqa: E501
}
_STEPUP_PROMPT = {
    Language.EN: "To {action}, I need to verify it's you. I've sent a one-time code.",
    Language.HI: "{action} के लिए, मुझे सत्यापित करना होगा कि यह आप ही हैं। मैंने एक वन-टाइम कोड भेजा है।",  # noqa: E501
    Language.HINGLISH: "{action} ke liye, mujhe verify karna hoga ki yeh aap hi hain. Maine ek one-time code bheja hai.",  # noqa: E501
}
_OTP_SUFFIX = {
    Language.EN: " Please reply with the code.",
    Language.HI: " कृपया कोड के साथ उत्तर दें।",
    Language.HINGLISH: " Kripya code ke saath reply karein.",
}
# After OTP + confirmation, a critical write is NOT executed by the agent — it is raised to the
# customer's relationship manager (RM) for approval and recorded as an artifact. Actual apply is
# out of scope (a human/RM does it). These messages tell the customer it's been raised.
_RAISED_TO_RM = {
    Language.EN: "Your request to {what} has been raised to your relationship manager. It'll be updated once they approve.",  # noqa: E501
    Language.HI: "{what} का आपका अनुरोध आपके रिलेशनशिप मैनेजर को भेज दिया गया है। उनकी मंज़ूरी मिलते ही इसे अपडेट कर दिया जाएगा।",  # noqa: E501
    Language.HINGLISH: "{what} ka aapka request aapke relationship manager ko bhej diya gaya hai. Unke approve karte hi ise update kar diya jayega.",  # noqa: E501
}
# Localized "{what}" describing the requested change, slotted into _RAISED_TO_RM.
_RM_WHAT = {
    "update_contact": {
        Language.EN: "update your {field} to {value}",
        Language.HI: "आपका {field} {value} में अपडेट करने",
        Language.HINGLISH: "aapka {field} {value} me update karne",
    },
    "raise_ticket": {
        Language.EN: "raise a ticket: {value}",
        Language.HI: "टिकट दर्ज करने: {value}",
        Language.HINGLISH: "ticket raise karne: {value}",
    },
    "file_claim": {
        Language.EN: "file a claim: {value}",
        Language.HI: "क्लेम दर्ज करने: {value}",
        Language.HINGLISH: "claim file karne: {value}",
    },
}


# Confirmed writes in this set are NOT auto-executed — they're raised to the RM for approval.
# Contact changes modify the customer's identity record, so they need a human's sign-off; ticket
# / claim creation (new records, not identity edits) still execute on confirmation.
_RM_APPROVAL_ACTIONS = {"update_contact"}


def _rm_message(pending, lang: Language) -> str:
    what_tbl = _RM_WHAT.get(pending.tool, {})
    what = (_t(what_tbl, lang) or pending.tool.replace("_", " ")).format(
        field=pending.field or "", value=pending.value or ""
    )
    return _t(_RAISED_TO_RM, lang).format(what=what)


# Localized action names used inside the step-up prompt.
_ACTION_NAME = {
    "update_contact": {
        Language.EN: "update your contact details",
        Language.HI: "आपका संपर्क विवरण अपडेट करने",
        Language.HINGLISH: "aapka contact update karne",
    },
    "raise_ticket": {
        Language.EN: "raise a ticket",
        Language.HI: "टिकट दर्ज करने",
        Language.HINGLISH: "ticket raise karne",
    },
    "file_claim": {
        Language.EN: "file a claim",
        Language.HI: "क्लेम दर्ज करने",
        Language.HINGLISH: "claim file karne",
    },
}


def _t(table: dict, lang: Language | None) -> str:
    return table.get(lang or Language.EN) or table.get(Language.EN) or ""


_STATUS_FROM_RESOLUTION = {
    "resolved": "resolved",
    "escalated": "escalated",
    "blocked": "escalated",
    "degraded": "degraded",
    "awaiting": "in_progress",
}


async def triage_node(state: RunState) -> dict:
    result, degraded = await _triage.run(
        state.raw_message,
        hint=state.language_hint,
        history=state.history,
        lang_text=state.reply_text,  # detect language from the customer's actual words this turn
    )
    entities = {**state.known_entities, **result.entities}
    out: dict = {
        "language": result.language,
        "intents": result.intents,
        "entities": entities,
        "search_query": result.search_query or state.raw_message,
        "step_count": 1,
    }
    # Sarvam language-detect failed -> deterministic fallback served; flag degraded.
    if degraded:
        out["degraded_agents"] = ["triage:sarvam"]
    return out


async def compliance_node(state: RunState) -> dict:
    decisions = _gate.evaluate(state.raw_message, state.intents, state.user_id, state.entities)
    return {"compliance_decisions": decisions, "step_count": 1}


def _injection_blocked(state: RunState) -> bool:
    return any(
        d.actor == "injection_guard" and d.decision == "block"
        for d in state.compliance_decisions
)


def _suspend(status: str, message: str, **extra) -> dict:
    """Terminal suspend update: a user-facing prompt + a non-terminal lifecycle status."""
    up = {
        "route": "suspend",
        "status": status,
        "final_response": ResponsePayload(
            resolution_status="awaiting", message=message, escalated=False
),
        "step_count": 1,
    }
    up.update(extra)
    return up


async def identity_node(state: RunState) -> dict:
    """Identity gate: derive purpose-limitation domains and gate writes.

    Deterministic. Never raises auth_level itself; for a state-changing action it drives
    step-up (OTP) then write-confirmation, suspending the run between turns.
    """
    # Long-term memory is on-demand: the Interaction-history domain is authorized only when
    # the customer's question references past interactions.
    wants_history = bool(state.entities.get("wants_history"))
    up: dict = {
        "allowed_domains": domains_for(state.intents, include_history=wants_history),
        "step_count": 1,
    }

    # Injection already blocks the run — don't mint challenges for a manipulation attempt.
    if _injection_blocked(state):
        return up

    writes = [
        i
        for i in state.intents
        if i.type == IntentType.ACTION and i.action and requires_step_up(i.action)
    ]
    if not writes:
        return up  # reads / info: normal routing, no gate
    intent = writes[0]
    lang = state.language or Language.EN

    # Stale pending write: past its TTL, execution authority has expired. Discard it so the
    # write is rebuilt fresh (new nonce) and re-earns step-up + confirmation — never auto-fires
    # off an old confirmation ("authority is mortal").
    if state.pending_write is not None and is_stale(state.pending_write):
        log.info("identity.pending_write_stale", run_id=state.run_id, tool=state.pending_write.tool)
        state = state.model_copy(update={"pending_write": None, "challenge_response": None})

    # Resume "no" → abandon the pending write (execution authority is mortal).
    if state.pending_write is not None and parse_confirmation(state.resume_reply or "") == "no":
        return {
            "route": "suspend",
            "status": "resolved",
            "pending_write": None,
            "final_response": ResponsePayload(
                resolution_status="resolved",
                message=_t(_CANCELLED, lang),
                escalated=False,
            ),
            "step_count": 1,
        }

    # Build (first turn) or reuse (resume) the conversation-anchored PendingWrite.
    pending = state.pending_write
    nonce = state.intent_nonce
    if pending is None:
        nonce = state.intent_nonce + 1
        pending = build_pending_write(
            state.conversation_id, intent, state.entities, state.raw_message, nonce, lang
        )
        if pending is None:
            # No confirmable value. Distinguish "value present but INVALID" (e.g. a number that
            # isn't a valid mobile, or a malformed email) from "no value yet", and escalate the
            # wording each consecutive failure instead of repeating the same prompt forever:
            #   0 -> short ask · 1 -> spelled-out help · 2+ -> hand to a human.
            field = _contact_field_hint(state.raw_message)
            attempts = _prior_clarify_count(state.history)
            if attempts >= 2:
                return {
                    "route": "suspend",
                    "status": "escalated",
                    "pending_write": None,
                    "escalation": EscalationRecord(
                        conversation_id=state.conversation_id,
                        tenant_id=state.tenant_id,
                        detected_intent=intent.action or "update_contact",
                        attempted_actions=[intent.action or ""],
                        blocking_reason="clarification_exhausted",
                        transcript_ref=state.conversation_id,
                        sla_target="4h",
                    ),
                    "final_response": ResponsePayload(
                        resolution_status="escalated",
                        message=_t(_CLARIFY_GIVEUP, lang),
                        escalated=True,
                    ),
                    "step_count": 1,
                }
            table = _clarify_table(state.raw_message, field, detailed=attempts >= 1)
            return _suspend("awaiting_input", _t(table, lang))

    decision = identity_gate(state.auth_level, state.intents)

    # Step-up owed: auth_level below step_up for a write.
    if decision.owed:
        # A challenge was answered but auth_level is still below step_up → verification failed.
        if state.challenge_response:
            esc = EscalationRecord(
                conversation_id=state.conversation_id,
                tenant_id=state.tenant_id,
                detected_intent=intent.action or "write",
                attempted_actions=[intent.action or ""],
                blocking_reason="step_up_failed",
                transcript_ref=state.conversation_id,
                pending_action=pending.tool,
                sla_target="4h",
)
            return {
                "route": "suspend",
                "status": "escalated",
                "pending_write": pending,
                "escalation": esc,
                "final_response": ResponsePayload(
                    resolution_status="escalated",
                    message=_t(_STEPUP_FAILED, lang),
                    escalated=True,
                ),
                "step_count": 1,
            }
        chal = request_step_up(state.user_id)
        action_name = _t(_ACTION_NAME.get(pending.tool, {}), lang) or pending.tool.replace("_", " ")
        # The OTP is delivered out-of-band (simulated SMS popup), not written into the message.
        prompt = _t(_STEPUP_PROMPT, lang).format(action=action_name) + _t(_OTP_SUFFIX, lang)
        return _suspend(
            "awaiting_input",
            prompt,
            pending_write=pending,
            intent_nonce=nonce,
            step_up_owed=True,
            challenge_id=chal["challenge_id"],
            challenge_otp=chal.get("test_otp"),
        )

    # auth_level == step_up. Confirmed?
    if parse_confirmation(state.resume_reply or "") == "yes":
        # A contact CHANGE (modifying the customer's identity record) is not executed by the
        # agent — it is raised to the customer's relationship manager (RM) for approval and
        # recorded as an Escalation artifact (blocking_reason=awaiting_manager_approval); a human
        # applies it. Other writes (raise_ticket / file_claim — creating new records) execute.
        if pending.tool in _RM_APPROVAL_ACTIONS:
            return {
                "route": "suspend",
                "status": "escalated",
                "pending_write": pending,  # kept as the record of what was requested
                "intent_nonce": nonce,
                "step_up_owed": False,
                "escalation": EscalationRecord(
                    conversation_id=state.conversation_id,
                    tenant_id=state.tenant_id,
                    detected_intent=intent.action or pending.tool,
                    attempted_actions=[pending.tool],
                    blocking_reason="awaiting_manager_approval",
                    transcript_ref=state.conversation_id,
                    pending_action=pending.tool,
                    sla_target="24h",
                ),
                "final_response": ResponsePayload(
                    resolution_status="escalated",
                    message=_rm_message(pending, lang),
                    escalated=True,
                ),
                "step_count": 1,
            }
        return {
            "pending_write": pending,
            "intent_nonce": nonce,
            "step_up_owed": False,
            "step_count": 1,
        }  # route stays None → supervisor routes to action/mixed → write executes
    return _suspend(
        "awaiting_confirmation",
        pending.read_back,
        pending_write=pending,
        intent_nonce=nonce,
        step_up_owed=False,
)


def _route(state: RunState) -> str:
    if state.step_count > get_settings().max_step_count:
        return "respond"
    kinds = {i.type for i in state.intents}
    has_action = IntentType.ACTION in kinds
    has_info = IntentType.INFORMATION in kinds
    if has_action and has_info:
        return "mixed"
    if has_action:
        return "action"
    if has_info:
        return "rag"
    return "respond"


async def supervisor_node(state: RunState) -> dict:
    # Identity may have already chosen a terminal/suspend route — honour it.
    if state.route is not None:
        return {"step_count": 1}
    return {"route": _route(state), "step_count": 1}


# Meta / follow-up phrases that carry NO retrieval signal of their own — retrieve on the
# prior question instead, so "samajh nahi aaya" doesn't pull unrelated passages.
_META_FOLLOWUP = (
    "samajh", "samjh", "nahi aaya", "nhi aaya", "understand", "didn't get", "did not understand",
    "explain", "clarify", "matlab", "phir se", "dobara", "repeat", "elaborate", "samjhao",
    "what do you mean", "i don't get", "confus", "समझ", "समझा", "मतलब", "फिर से", "दोबारा",
)


def _retrieval_query(state: RunState) -> str:
    """Deterministic context query (fallback when no LLM-condensed query is available).

    A self-contained question retrieves on its own words (prepending prior turns would dilute
    a clean topic switch). A meta/short follow-up ("samajh nahi aaya", "and that?") anchors on
    the most recent SUBSTANTIVE prior question so the topic carries over.
    """
    msg = state.raw_message
    # Prefer the LLM-condensed, reference-resolved query when triage produced one.
    if state.search_query and state.search_query.strip() and state.search_query != msg:
        return state.search_query.strip()
    lower = msg.lower()
    is_meta = any(k in lower for k in _META_FOLLOWUP) or len(lower.split()) <= 3
    if is_meta:
        for h in reversed(state.history):
            if h.role == "user":
                prior = h.content
                if len(prior.split()) > 3 and not any(k in prior.lower() for k in _META_FOLLOWUP):
                    return f"{prior} {msg}"
    return msg


async def rag_node(state: RunState) -> dict:
    # Per-customer scoped retrieval: pass verified user_id + allowed domains.
    try:
        passages = await _rag.run(
            _retrieval_query(state),
            user_id=state.user_id,
            allowed_domains=state.allowed_domains,
)
        # Score-floor groundedness gate: if the best passage is below the
        # floor, the answer would be ungrounded — escalate rather than invent a clause.
        floor = get_settings().retrieval_score_floor
        top = max((p.score for p in passages), default=0.0)
        if floor > 0 and top < floor:
            log.info("rag.below_floor", run_id=state.run_id, top=round(top, 4), floor=floor)
            return {"retrieved": passages, "ungrounded": True, "step_count": 1}
        return {"retrieved": passages, "step_count": 1}
    except Exception as e:  # noqa: BLE001
        log.error("node.failed", node="rag", run_id=state.run_id, error=str(e))
        return {"degraded_agents": ["rag"], "step_count": 1}


async def action_node(state: RunState) -> dict:
    allowed = ComplianceGate.allowed_actions(state.compliance_decisions)
    permitted = [i for i in state.intents if i.action in allowed]
    try:
        records = await _action.run(
            permitted,
            state.entities,
            state.user_id,
            state.run_id,
            state.raw_message,
            pending_write=state.pending_write,
)
        return {"actions": records, "step_count": 1}
    except Exception as e:  # noqa: BLE001
        log.error("node.failed", node="action", run_id=state.run_id, error=str(e))
        return {"degraded_agents": ["action"], "step_count": 1}


async def synthesis_node(state: RunState) -> dict:
    # Ungrounded info answer (retrieval below floor): escalate instead of generating from
    # weak/empty passages. Deterministic wording, no LLM.
    if state.ungrounded:
        lang = state.language or Language.EN
        response = ResponsePayload(
            resolution_status="escalated",
            message=redact_pii(_UNGROUNDED.get(lang, _UNGROUNDED[Language.EN])),
            escalated=True,
)
        primary = next((i.action or str(i.type) for i in state.intents), "unknown")
        return {
            "final_response": response,
            "status": "escalated",
            "step_count": 1,
            "escalation": EscalationRecord(
                conversation_id=state.conversation_id,
                tenant_id=state.tenant_id,
                detected_intent=primary,
                attempted_actions=[],
                blocking_reason="retrieval_below_floor",
                transcript_ref=state.conversation_id,
                sla_target="4h",
),
        }

    ctx = SynthesisContext(
        language=state.language or Language.EN,
        message=state.raw_message,
        passages=state.retrieved,
        actions=state.actions,
        decisions=state.compliance_decisions,
        degraded=state.degraded_agents,
        history=state.history,
    )
    response = await _synth.run(ctx)
    # NOTE: the live reply to the authenticated owner shows their OWN data (e.g. the email they
    # just set) — masking it reads as a bug. PII redaction is applied at STORAGE time
    # (db.repository.persist_run) so the transcript/audit stay PII-free and erasure-compatible.
    status = _STATUS_FROM_RESOLUTION.get(response.resolution_status, "resolved")
    if state.degraded_agents and status == "resolved":
        status = "degraded"
        response.resolution_status = "degraded"

    update: dict = {
        "final_response": response,
        "status": status,
        "step_count": 1,
        "tokens_used": _synth.last_tokens,
    }
    # Escalation as a real artifact: emit a record on any escalated outcome.
    if status == "escalated" and state.escalation is None:
        primary = next((i.action or str(i.type) for i in state.intents), "unknown")
        update["escalation"] = EscalationRecord(
            conversation_id=state.conversation_id,
            tenant_id=state.tenant_id,
            detected_intent=primary,
            attempted_actions=[a.tool for a in state.actions],
            blocking_reason="compliance_block_or_low_confidence",
            transcript_ref=state.conversation_id,
            pending_action=state.pending_write.tool if state.pending_write else None,
            sla_target="4h",
)
    return update


def _branch(state: RunState):
    if _injection_blocked(state):
        return "synthesis"  # injection → escalate via synthesis
    route = state.route
    if route == "suspend":
        return "end"
    if route == "rag":
        return "rag"
    if route == "action":
        return "action"
    if route == "mixed":
        return ["rag", "action"]
    return "synthesis"  # respond / end


def _assemble() -> StateGraph:
    g = StateGraph(RunState)
    g.add_node("triage", triage_node)
    g.add_node("compliance", compliance_node)
    g.add_node("identity", identity_node)
    g.add_node("supervisor", supervisor_node)
    g.add_node("rag", rag_node)
    g.add_node("action", action_node)
    g.add_node("synthesis", synthesis_node)

    g.add_edge(START, "triage")
    g.add_edge("triage", "compliance")  # injection + authz gate on every message
    g.add_edge("compliance", "identity")  # identity before any data read; gates writes
    g.add_edge("identity", "supervisor")
    g.add_conditional_edges(
        "supervisor",
        _branch,
        {"rag": "rag", "action": "action", "synthesis": "synthesis", "end": END},
)
    g.add_edge("rag", "synthesis")
    g.add_edge("action", "synthesis")
    g.add_edge("synthesis", END)
    return g


@lru_cache
def build_graph():
    """Compiled graph without a checkpointer — used by eval and tests."""
    return _assemble().compile()


@lru_cache
def build_checkpointed_graph():
    """Compiled graph with a checkpointer so crashed runs resume (worker path)."""
    from saral.graph.checkpoint import get_checkpointer

    return _assemble().compile(checkpointer=get_checkpointer())
