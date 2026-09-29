"""Supervisor-orchestrated agent graph.

The supervisor routes; specialists work. Identity runs before any data read; Compliance is a
gate, not a peer. State-changing actions pass through step-up → write-confirmation before they
execute, and no write fires without a fresh token re-validation in the same transition.

    START -> triage -> compliance -> identity -> supervisor -(route)->
        { synthesis                 (respond / injection-blocked -> escalate)
        | handoff                   (suspend: awaiting_input / awaiting_confirmation / abandon)
        | rag -> synthesis          (information; per-customer scoped retrieval)
        | action -> synthesis       (reads, or a CONFIRMED write executed idempotently)
        | {rag, action} -> synthesis (mixed: parallel fan-out) }
    synthesis -> handoff -> END   (any escalation -> a deduplicated request for the RM)
"""

from __future__ import annotations

import asyncio
import re
from functools import lru_cache

from langgraph.graph import END, START, StateGraph

from saral import metrics
from saral.actions import claim_intake
from saral.actions.confirm import (
    build_pending_write,
    field_name,
    is_stale,
    parse_confirmation,
)
from saral.agents.action import ActionAgent
from saral.agents.rag import RagAgent
from saral.agents.synthesis import COMPLAINT_REPLIES, SynthesisAgent, SynthesisContext
from saral.agents.triage import TriageAgent
from saral.auth.identity import identity_gate
from saral.auth.stepup import request_step_up
from saral.authz import allowed_domains as domains_for
from saral.authz import requires_step_up
from saral.compliance.gate import ComplianceGate
from saral.compliance.injection import PASSAGE_FAMILIES, detect_injection
from saral.compliance.pii import redact_pii
from saral.config import get_settings
from saral.graph.state import RunState
from saral.llm import usage as llm_usage
from saral.logging import get_logger
from saral.rag.customer_index import mentioned_entity_ids
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
    Language.HI: "ज़रूर — कृपया नया मोबाइल नंबर बताएं।",
    Language.HINGLISH: "Zaroor — kripya naya mobile number bataiye.",
}
_CLARIFY_EMAIL = {
    Language.EN: "Sure — what is the new email address you'd like to set?",
    Language.HI: "ज़रूर — कृपया नया ईमेल पता बताएं।",
    Language.HINGLISH: "Zaroor — kripya naya email address bataiye.",
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
    Language.HI: "इस बदलाव के लिए मुझे नया मोबाइल नंबर चाहिए। कृपया केवल 10 अंक (6–9 से शुरू) भेजें, जैसे 9876543210 — या कुछ और करना हो तो बताएं।",  # noqa: E501
    Language.HINGLISH: "Is change ke liye mujhe naya mobile number chahiye. Kripya sirf 10 digit (6–9 se shuru) bhejein, jaise 9876543210 — ya kuch aur karna ho to bataiye.",  # noqa: E501
}
_CLARIFY_EMAIL_HELP = {
    Language.EN: "I still need the new email address to make this change. Please send it like name@example.com — or tell me if you'd like to do something else.",  # noqa: E501
    Language.HI: "इस बदलाव के लिए मुझे नया ईमेल पता चाहिए। कृपया इसे name@example.com जैसे भेजें — या कुछ और करना हो तो बताएं।",  # noqa: E501
    Language.HINGLISH: "Is change ke liye mujhe naya email address chahiye. Kripya ise name@example.com jaise bhejein — ya kuch aur karna ho to bataiye.",  # noqa: E501
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
_OTP_RETRY = {
    Language.EN: "That code didn't match. Please try again — {n} attempt(s) left.",
    Language.HI: "यह कोड मेल नहीं खाया। कृपया फिर से कोशिश करें — {n} प्रयास बाकी हैं।",
    Language.HINGLISH: "Yeh code match nahi hua. Kripya dobara try karein — {n} attempt baaki hain.",  # noqa: E501
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
    Language.EN: "Your request to {what} has been raised to your relationship manager, who will act on it within {sla}.",  # noqa: E501
    Language.HI: "{what} का आपका अनुरोध आपके रिलेशनशिप मैनेजर को भेज दिया गया है, जो {sla} के भीतर इस पर कार्रवाई करेंगे।",  # noqa: E501
    Language.HINGLISH: "{what} ka aapka request aapke relationship manager ko bhej diya gaya hai, jo {sla} ke andar ispar action lenge.",  # noqa: E501
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


# Confirmed writes in `settings.rm_actions` (default: every write) are NOT executed — they're
# raised to the customer's relationship manager, the human who owns all writes to customer data.


_RM_SLA = "24h"


def _rm_message(pending, lang: Language) -> str:
    what_tbl = _RM_WHAT.get(pending.tool, {})
    what = (_t(what_tbl, lang) or pending.tool.replace("_", " ")).format(
        field=field_name(pending.field, lang), value=pending.value or ""
    )
    return _t(_RAISED_TO_RM, lang).format(what=what, sla=_RM_SLA)


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
    # The gate reads the (sync) core-system store for ownership: keep it off the event loop so
    # concurrent runs in the worker don't stall on each other's DB round-trips.
    decisions = await asyncio.to_thread(
        _gate.evaluate, state.raw_message, state.intents, state.user_id, state.entities
    )
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


def _has_reads(state: RunState) -> bool:
    """Intents that can be answered right now without step-up (info / read actions)."""
    return any(
        i.type == IntentType.INFORMATION
        or (i.type == IntentType.ACTION and i.action and not requires_step_up(i.action))
        for i in state.intents
    )


def _is_fresh_turn(state: RunState) -> bool:
    return (
        state.pending_write is None
        and state.resume_reply is None
        and state.challenge_response is None
    )


def _gate_write(state: RunState, status: str, message: str, **extra) -> dict:
    """Suspend for a write — but on a fresh mixed turn ("claim status + update my number"),
    answer the reads first: route normally and let synthesis append this prompt and suspend."""
    if _is_fresh_turn(state) and _has_reads(state):
        up = {"suspend_prompt": message, "status": status, "step_count": 1}
        up.update(extra)
        return up
    return _suspend(status, message, **extra)


def _clarify_exhausted(state: RunState, action: str, lang: Language) -> dict:
    """Repeated failed clarifications: stop looping and hand over to a human."""
    return {
        "route": "suspend",
        "status": "escalated",
        "pending_write": None,
        "escalation": EscalationRecord(
            conversation_id=state.conversation_id,
            tenant_id=state.tenant_id,
            detected_intent=action,
            attempted_actions=[action],
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
    # A language we don't serve: say so (bilingual), read nothing, start no write.
    if state.language == Language.UNSUPPORTED:
        return {**up, "allowed_domains": [], "route": "respond"}

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
    just_completed = False  # claim facts completed THIS turn: read back, never auto-confirm

    # Claim intake: collect the claim's facts (over as many turns as needed) before step-up.
    if intent.action == "file_claim":
        policies = [str(p) for p in state.entities.get("policy_ids") or []]
        base: dict | None = None
        if pending is None or pending.tool != "file_claim":
            nonce = state.intent_nonce + 1
            base, text, rounds = {}, state.raw_message, 0
        elif pending.args.get("missing"):
            base, text = pending.args, state.resume_reply or ""
            rounds = int(pending.args.get("rounds", 0)) + (1 if state.resume_reply else 0)
        if base is not None:
            slots = await claim_intake.fill(text, base, policies)
            pending = claim_intake.pending_claim(
                state.conversation_id, {**slots, "rounds": rounds}, nonce, lang
            )
            missing = pending.args["missing"]
            if missing and rounds >= claim_intake.MAX_ROUNDS:
                return _clarify_exhausted(state, intent.action, lang)
            if missing:
                return {
                    **_gate_write(
                        state,
                        "awaiting_input",
                        claim_intake.ask_prompt(missing, lang, policies),
                        pending_write=pending,
                        intent_nonce=nonce,
                    ),
                    "allowed_domains": up["allowed_domains"],
                }
            just_completed = True

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
                return _clarify_exhausted(state, intent.action or "update_contact", lang)
            table = _clarify_table(state.raw_message, field, detailed=attempts >= 1)
            return {
                **_gate_write(state, "awaiting_input", _t(table, lang)),
                "allowed_domains": up["allowed_domains"],
            }

    # The same change is already with the RM (open): say so now — no second OTP, no duplicate.
    fresh = state.pending_write is None or just_completed
    if pending.tool in get_settings().rm_actions and fresh:
        dup = await _open_rm_request(state.user_id, pending)
        if dup is not None:
            return {
                "route": "suspend",
                "status": "resolved",
                "pending_write": None,
                "rm_request_id": dup.id,
                "rm_request_kind": pending.tool,
                "rm_request_created": False,
                "final_response": ResponsePayload(
                    resolution_status="resolved",
                    message=_t(_REQ_DUPLICATE, lang).format(id=dup.id, sla=dup.sla_target),
                ),
                "allowed_domains": up["allowed_domains"],
                "step_count": 1,
            }

    decision = identity_gate(state.auth_level, state.intents)

    # Step-up owed: auth_level below step_up for a write.
    if decision.owed:
        # A challenge was answered but auth_level is still below step_up → wrong code. Re-prompt
        # on the same challenge while tries remain; escalate once it is burned.
        if state.challenge_response and state.otp_attempts_left > 0 and state.challenge_id:
            return _suspend(
                "awaiting_input",
                _t(_OTP_RETRY, lang).format(n=state.otp_attempts_left),
                pending_write=pending,
                intent_nonce=nonce,
                step_up_owed=True,
                challenge_id=state.challenge_id,
            )
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
        chal = await asyncio.to_thread(request_step_up, state.user_id)
        action_name = _t(_ACTION_NAME.get(pending.tool, {}), lang) or pending.tool.replace("_", " ")
        # The OTP is delivered out-of-band (simulated SMS popup), not written into the message.
        prompt = _t(_STEPUP_PROMPT, lang).format(action=action_name) + _t(_OTP_SUFFIX, lang)
        return _gate_write(
            state,
            "awaiting_input",
            prompt,
            allowed_domains=up["allowed_domains"],
            pending_write=pending,
            intent_nonce=nonce,
            step_up_owed=True,
            challenge_id=chal["challenge_id"],
            challenge_otp=chal.get("test_otp"),
        )

    # auth_level == step_up. Confirmed?
    if not just_completed and parse_confirmation(state.resume_reply or "") == "yes":
        # A contact CHANGE (modifying the customer's identity record) is not executed by the
        # agent — it is raised to the customer's relationship manager (RM) for approval and
        # recorded as an Escalation artifact (blocking_reason=awaiting_manager_approval); a human
        # applies it. Other writes (raise_ticket / file_claim — creating new records) execute.
        if pending.tool in get_settings().rm_actions:
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
                    sla_target=_RM_SLA,
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
            "write_confirmed": True,  # the ONLY path that lets the action node execute it
            "allowed_domains": up["allowed_domains"],
            "step_count": 1,
        }  # route stays None → supervisor routes to action/mixed → write executes
    return _gate_write(
        state,
        "awaiting_confirmation",
        pending.read_back,
        pending_write=pending,
        intent_nonce=nonce,
        step_up_owed=False,
        allowed_domains=up["allowed_domains"],
    )


def _route(state: RunState) -> str:
    if state.step_count > get_settings().max_step_count:
        return "respond"
    kinds = {i.type for i in state.intents}
    # A write only routes to the action node once confirmed; unconfirmed it is suspended.
    has_action = any(
        i.type == IntentType.ACTION
        and i.action
        and (not requires_step_up(i.action) or state.write_confirmed)
        for i in state.intents
    )
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
        query = _retrieval_query(state)
        passages = await _rag.run(
            query,
            user_id=state.user_id,
            allowed_domains=state.allowed_domains,
            # Only ids the customer actually named (this message or the resolved query) —
            # never the account's default ids — narrow personal docs to that claim/policy.
            mentioned_ids=mentioned_entity_ids(state.raw_message, query),
            language=str(state.language) if state.language else None,
        )
        # Indirect injection: a retrieved passage carrying instructions aimed at the model
        # (poisoned doc / correspondence) is dropped before synthesis ever sees it.
        clean = [p for p in passages if not detect_injection(p.text, PASSAGE_FAMILIES)[0]]
        if len(clean) < len(passages):
            log.warning(
                "rag.passage_injection_dropped",
                run_id=state.run_id,
                dropped=[p.citation for p in passages if p not in clean],
            )
        passages = clean
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
            # Defence in depth: the write executes only after an explicit "yes".
            pending_write=state.pending_write if state.write_confirmed else None,
)
        return {"actions": records, "step_count": 1}
    except Exception as e:  # noqa: BLE001
        log.error("node.failed", node="action", run_id=state.run_id, error=str(e))
        return {"degraded_agents": ["action"], "step_count": 1}


async def synthesis_node(state: RunState) -> dict:
    # Ungrounded info answer (retrieval below floor): escalate instead of generating from
    # weak/empty passages. Deterministic wording, no LLM.
    # A read in a mixed turn escalated: don't also hold a write (or its OTP) open.
    drop_write = (
        {"pending_write": None, "challenge_id": None, "challenge_otp": None, "suspend_prompt": None}
        if state.suspend_prompt
        else {}
    )
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
            **drop_write,
        }

    kinds = {i.type for i in state.intents}
    complaint = IntentType.COMPLAINT in kinds and not (
        kinds & {IntentType.ACTION, IntentType.INFORMATION}
    )
    ctx = SynthesisContext(
        language=state.language or Language.EN,
        message=state.raw_message,
        passages=state.retrieved,
        actions=state.actions,
        decisions=state.compliance_decisions,
        degraded=state.degraded_agents,
        history=state.history,
        write_pending=bool(state.suspend_prompt),
        complaint=complaint,
        # We already offered a ticket in this conversation and they're still unhappy.
        repeat_complaint=complaint
        and any(h.role == "assistant" and h.content in COMPLAINT_REPLIES for h in state.history),
    )
    tokens_before = llm_usage.current()
    response = await _synth.run(ctx)
    tokens = llm_usage.current() - tokens_before
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
        "tokens_used": tokens,
    }
    # Mixed read + write (fresh turn): reads answered above; now ask for the OTP / confirmation
    # and suspend with the status identity chose.
    if state.suspend_prompt:
        if status == "escalated":
            update.update(drop_write)
        else:
            response.message = f"{response.message}\n\n{state.suspend_prompt}".strip()
            response.resolution_status = "awaiting"
            response.escalated = False
            update["status"] = state.status
            update["route"] = "suspend"
            return update
    # Escalation as a real artifact: emit a record on any escalated outcome.
    if status == "escalated" and state.escalation is None:
        primary = next((i.action or str(i.type) for i in state.intents), "unknown")
        update["escalation"] = EscalationRecord(
            conversation_id=state.conversation_id,
            tenant_id=state.tenant_id,
            detected_intent=primary,
            attempted_actions=[a.tool for a in state.actions],
            blocking_reason=(
                "repeated_complaint" if ctx.repeat_complaint
                else "compliance_block_or_low_confidence"
            ),
            transcript_ref=state.conversation_id,
            pending_action=state.pending_write.tool if state.pending_write else None,
            sla_target="4h",
)
    return update


# --- Handoff: every escalation becomes a request for the customer's relationship manager ---

_REQ_REF = {
    Language.EN: " Your request reference is {id}.",
    Language.HI: " आपका अनुरोध संदर्भ {id} है।",
    Language.HINGLISH: " Aapka request reference {id} hai.",
}
_REQ_DUPLICATE = {
    Language.EN: "You already have an open request ({id}) for this with your relationship manager, so I haven't raised it again. They'll act on it within {sla}.",  # noqa: E501
    Language.HI: "इसके लिए आपका अनुरोध ({id}) पहले से आपके रिलेशनशिप मैनेजर के पास खुला है, इसलिए मैंने इसे दोबारा नहीं भेजा। वे {sla} के भीतर इस पर कार्रवाई करेंगे।",  # noqa: E501
    Language.HINGLISH: "Iske liye aapka request ({id}) pehle se aapke relationship manager ke paas open hai, isliye maine ise dobara nahi bheja. Woh {sla} ke andar ispar action lenge.",  # noqa: E501
}


def _change_identity(pw) -> dict:
    """What makes two requested changes 'the same' (dedup identity)."""
    return {k: v for k, v in pw.args.items() if k not in ("missing", "rounds")}


async def _open_rm_request(user_id: str, pending):
    from saral.rm.requests import dedup_key, get_requests

    key = dedup_key(user_id, pending.tool, _change_identity(pending))
    try:
        return await asyncio.to_thread(get_requests().find_open, key)
    except Exception as e:  # noqa: BLE001 — no store: proceed; persistence still dedups
        log.warning("rm.lookup_failed", error=str(e))
        return None


def _rm_request_parts(state: RunState) -> tuple[str, dict | None, dict]:
    """(kind, encrypted-change payload, dedup identity) for this run's escalation."""
    esc, pw = state.escalation, state.pending_write
    assert esc is not None
    if esc.blocking_reason == "awaiting_manager_approval" and pw is not None:
        args = _change_identity(pw)
        return pw.tool, {"tool": pw.tool, "field": pw.field, "value": pw.value, "args": args}, args
    # Anything else is a request for a human to look at this conversation.
    identity = {"conversation_id": state.conversation_id, "reason": esc.blocking_reason}
    return "handoff", None, identity


async def handoff_node(state: RunState) -> dict:
    """Raise (or find) the RM request for this run's escalation and give the customer its
    reference. Deduplicated: the same change asked twice while open returns the open request.
    A request-store failure never loses the reply — the escalation row still persists."""
    esc = state.escalation
    if esc is None or state.rm_request_id:
        return {}
    from saral.rm.requests import dedup_key, get_requests
    from saral.rm.summary import case_summary

    kind, change, identity = _rm_request_parts(state)
    lang = state.language or Language.EN
    try:
        req, created = await asyncio.to_thread(
            get_requests().raise_request,
            tenant_id=state.tenant_id,
            user_id=state.user_id,
            conversation_id=state.conversation_id,
            kind=kind,
            reason=esc.blocking_reason,
            key=dedup_key(state.user_id, kind, identity),
            change=change,
            summary=None,
            language=str(lang),
            sla_target=esc.sla_target,
        )
        if created:
            summary = await case_summary(state, kind, esc.blocking_reason)
            await asyncio.to_thread(get_requests().set_summary, req.id, summary)
    except Exception as e:  # noqa: BLE001
        log.error("rm.request_failed", run_id=state.run_id, error=str(e))
        return {}

    metrics.ESCALATIONS.labels(esc.blocking_reason).inc()
    metrics.RM_REQUESTS.labels(kind, str(created).lower()).inc()
    update: dict = {
        "rm_request_id": req.id,
        "rm_request_kind": kind,
        "rm_request_created": created,
        "escalation": esc.model_copy(update={"request_id": req.id, "user_id": state.user_id}),
    }
    if state.final_response is not None:
        resp = state.final_response.model_copy()
        if not created and kind != "handoff":
            resp.message = _t(_REQ_DUPLICATE, lang).format(id=req.id, sla=req.sla_target)
        elif req.id not in resp.message:
            resp.message = resp.message.rstrip() + _t(_REQ_REF, lang).format(id=req.id)
        update["final_response"] = resp
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
    g.add_node("handoff", handoff_node)

    g.add_edge(START, "triage")
    g.add_edge("triage", "compliance")  # injection + authz gate on every message
    g.add_edge("compliance", "identity")  # identity before any data read; gates writes
    g.add_edge("identity", "supervisor")
    g.add_conditional_edges(
        "supervisor",
        _branch,
        {"rag": "rag", "action": "action", "synthesis": "synthesis", "end": "handoff"},
)
    g.add_edge("rag", "synthesis")
    g.add_edge("action", "synthesis")
    g.add_edge("synthesis", "handoff")  # every escalation becomes an RM request
    g.add_edge("handoff", END)
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
