"""Triage / Intent agent.

Detects language (En/Hi/Hinglish), classifies intent(s), and extracts entities into an
`IntentResult`. Language detection uses Sarvam's /text-lid endpoint (the Indian-language
differentiator); intent classification and entity extraction stay deterministic
(they drive compliance + tool selection, so correctness over flexibility —). The
deterministic classifier below doubles as the StubProvider handler, so triage works with no
API key and stays reproducible for eval.
"""

from __future__ import annotations

import re

from saral.config import get_settings
from saral.llm.base import LLMError, Message
from saral.llm.factory import get_llm
from saral.llm.sarvam import SarvamProvider
from saral.llm.stub import register_structured_handler
from saral.logging import get_logger
from saral.schemas import HistoryTurn, Intent, IntentResult, IntentType, Language

# The closed set of actions the LLM may map an intent to — anything else is normalized away,
# so the LLM can UNDERSTAND free phrasing but can never invent a tool or widen scope.
_ALLOWED_ACTIONS = {
    "get_claim_status",
    "get_policy_details",
    "update_contact",
    "raise_ticket",
    "file_claim",
}

log = get_logger(__name__)

# --- Keyword lexicons (English + romanized Hindi + Devanagari) ---

_DEVANAGARI = re.compile(r"[ऀ-ॿ]")

# Distinctive romanized-Hindi word markers that signal Hinglish when the script is Latin.
# Matched on word boundaries only (avoids e.g. "do" inside "does").
_HINGLISH_MARKERS = {
    "kya", "mera", "meri", "mere", "karo", "kardo", "krdo", "hai", "kaise",
    "nahi", "nahin", "batao", "chahiye", "kab", "kitna", "kitni", "nambar",
    "mujhe", "karna", "karni", "kaisa", "kaisi",
}

# Claim-status is an action only when a claim ID is present or the phrasing is explicit —
# a bare "claim" (e.g. "how do I file a claim?") is an information question, not an action.
_CLAIM_STATUS_PHRASES = {
    "claim status", "claim ka status", "claim status kya", "status of my claim",
    "claim ki status",
}
_UPDATE_CONTACT = {
    "update number", "change number", "update mobile", "change mobile",
    "update contact", "update my number", "mobile number update", "नंबर अपडेट",
    "नंबर बदल", "update email", "change email", "update address",
    "number update", "nambar",
    # word-order + Hinglish variants ("phone number change karna hai", "number badalna")
    "number change", "mobile change", "phone change", "phone number change",
    "number badal", "number badalna", "mobile badal", "mobile badalna",
    "naya number", "new number", "change my number", "change my mobile",
    "phone number update", "contact update", "email change", "email update",
    "नंबर बदलना", "नंबर चेंज", "मोबाइल बदल", "मोबाइल अपडेट", "ईमेल बदल",
}
_RAISE_TICKET = {
    "raise ticket", "raise a ticket", "file complaint", "register complaint",
    "ticket banao", "शिकायत", "complaint darj", "open ticket",
}
_FILE_CLAIM = {
    "file a claim", "file claim", "lodge a claim", "lodge claim", "register a claim",
    "raise a claim", "new claim", "claim file", "claim lodge", "claim dakhil",
    "claim karna", "नया क्लेम", "क्लेम दर्ज", "क्लेम फाइल",
}
_INFORMATION = {
    "cover", "coverage", "covered", "policy detail", "policy details",
    "what does", "premium", "exclusion", "claim process", "how do i",
    "how to", "eligible", "kya cover", "policy", "faq", "deductible",
    "waiting period", "कवर", "पॉलिसी", "प्रीमियम", "प्रतीक्षा", "अवधि", "अपवर्जन",
    "loan", "emi", "foreclosure", "tenure", "interest rate", "grace period",
    "reinstate", "nominee", "statement", "miss",
    # Explanation / adjudication questions about the customer's own claim — the differentiator.
    "why was", "why is", "why did", "rejected", "reject", "reduced", "partial",
    "partially", "deduction", "co-pay", "copay", "clause", "rider", "lapse", "lapsed",
    "क्यों", "अस्वीकृत", "खंड", "लैप्स", "kyun", "kyu", "reject kyun",
}
_COMPLAINT = {"not working", "worst", "angry", "horrible", "complaint", "शिकायत", "bekar"}
_GREETING = {"hi", "hello", "hey", "namaste", "नमस्ते", "good morning", "good evening"}
# Capability / permission questions ("can I…?", "kya main … sakti/sakta hu", "how do I…").
# These ASK ABOUT an action, they don't request it — route to INFORMATION, never a write.
_CAPABILITY = {
    "can i", "could i", "do i", "am i able", "is it possible", "how do i", "how can i",
    "how to", "what is the process", "allowed to", "eligible to",
    "kya main", "kya mai", "kya hum", "kar sakta", "kar sakti", "kar sakte",
    "kar sakta hu", "kar sakti hu", "kar sakte hain", "kaise kar", "kaise karu",
    "kaise karun", "kaise file", "possible hai", "kar paunga", "kar paungi",
    "क्या मैं", "कैसे कर", "सकता हूँ", "सकती हूँ", "कैसे करूँ", "कर सकते",
}
# History-seeking phrasing: authorizes the Interaction-history domain on demand (long-term
# memory). Never eager — only when the customer references their past interactions.
_HISTORY = {
    "last time", "previously", "previous", "earlier", "before", "my history",
    "past complaint", "past ticket", "last call", "spoke earlier", "told you",
    "pichli baar", "pehle", "pichhle", "last conversation", "इतिहास", "पिछली बार", "पहले",
}

# --- Entity extractors ---

# Match the full ID token (prefix + digits) so "CLM2001" is captured whole, and plain
# words like "cover" are never mistaken for an id.
_POLICY_RE = re.compile(r"\bPOL\d+\b", re.IGNORECASE)
_CLAIM_RE = re.compile(r"\bCLM\d+\b", re.IGNORECASE)
_MOBILE_RE = re.compile(r"\b(?:\+?91[-\s]?)?([6-9]\d{9})\b")
_EMAIL_RE = re.compile(r"\b([\w.+-]+@[\w-]+\.[\w.-]+)\b")

# Action: "update/change ... (mobile|number|email|contact|address)".
# Match an update verb and a contact field in EITHER order ("change my number" /
# "phone number change karna hai"). Verbs include Hinglish badal/badalna and Devanagari.
_UPD_VERB = r"update|change|badal|badalna|badl|naya|new|अपडेट|बदल|बदलना|चेंज"
_UPD_FIELD = r"mobile|number|phone|email|contact|address|नंबर|मोबाइल|ईमेल|फोन"
_UPDATE_RE = re.compile(
    rf"\b(?:{_UPD_VERB})\b.{{0,20}}?\b(?:{_UPD_FIELD})\b"
    rf"|\b(?:{_UPD_FIELD})\b.{{0,20}}?\b(?:{_UPD_VERB})\b",
    re.IGNORECASE | re.DOTALL,
)
_WORD_RE = re.compile(r"[a-z]+")


def detect_language(text: str) -> Language:
    if _DEVANAGARI.search(text):
        # Mixed Devanagari + Latin words -> still treat as Hindi-script (hi).
        return Language.HI
    words = set(_WORD_RE.findall(text.lower()))
    if words & _HINGLISH_MARKERS:
        return Language.HINGLISH
    return Language.EN


def extract_entities(text: str) -> dict:
    entities: dict = {}
    if m := _POLICY_RE.search(text):
        entities["policy_id"] = m.group(0).upper()
    if m := _CLAIM_RE.search(text):
        entities["claim_id"] = m.group(0).upper()
    if m := _MOBILE_RE.search(text):
        entities["mobile"] = m.group(1)
    if m := _EMAIL_RE.search(text):
        entities["email"] = m.group(1)
    # On-demand long-term memory signal: the customer is referencing past interactions.
    if _hits(text.lower(), _HISTORY):
        entities["wants_history"] = True
    return entities


def _hits(text: str, lexicon: set[str]) -> bool:
    return any(kw in text for kw in lexicon)


def classify_intents(text: str) -> list[Intent]:
    lower = text.lower()
    intents: list[Intent] = []

    # A capability/permission question ("can I file a claim?", "kya main claim file kar sakti
    # hu?") ASKS ABOUT a write — it is information, not a request to perform it. Suppress the
    # write action so it never triggers step-up; answer the "how/whether" from retrieval.
    capability = _hits(lower, {k.lower() for k in _CAPABILITY})

    if not capability and (
        _UPDATE_RE.search(text) or _hits(lower, {k.lower() for k in _UPDATE_CONTACT})
    ):
        intents.append(Intent(type=IntentType.ACTION, action="update_contact", confidence=0.9))
    if not capability and _hits(lower, {k.lower() for k in _RAISE_TICKET}):
        intents.append(Intent(type=IntentType.ACTION, action="raise_ticket", confidence=0.85))
    if not capability and _hits(lower, {k.lower() for k in _FILE_CLAIM}):
        intents.append(Intent(type=IntentType.ACTION, action="file_claim", confidence=0.9))
    has_claim_id = bool(_CLAIM_RE.search(text))
    claim_phrase = _hits(lower, _CLAIM_STATUS_PHRASES) or (
        "क्लेम" in text and "स्टेटस" in text
)
    # "Why was my claim CLM2010 rejected/reduced" is an EXPLANATION (grounded retrieval), not a
    # status lookup — the claim id is retrieval context. Suppress get_claim_status so it routes
    # to pure RAG, unless the customer explicitly asked for "status".
    _EXPLAIN = ("why", "reason", "rejected", "reduced", "kyun", "kyu", "क्यों", "अस्वीकृत", "घटा")
    is_explanation = any(w in lower or w in text for w in _EXPLAIN)
    if (has_claim_id or claim_phrase) and not (is_explanation and not claim_phrase):
        intents.append(Intent(type=IntentType.ACTION, action="get_claim_status", confidence=0.8))

    # get_policy_details: a POL id plus a "look it up" phrasing is a read action (and the
    # ownership check then blocks a cross-customer policy id). A pure policy lookup suppresses
    # the broad INFORMATION match so it routes cleanly to the action path, not mixed.
    has_policy_id = bool(_POLICY_RE.search(text))
    _LOOKUP = ("detail", "विवरण", "vivaran", "dikhao", "show", "batao", "दिखाओ", "chahiye")
    policy_lookup = has_policy_id and any(w in lower or w in text for w in _LOOKUP)
    if policy_lookup:
        intents.append(
            Intent(type=IntentType.ACTION, action="get_policy_details", confidence=0.85)
)
    has_information = _hits(lower, {k.lower() for k in _INFORMATION})
    # A capability question is informational even if its topic word isn't in the FAQ lexicon
    # (e.g. "claim" alone) — so route the suppressed write to INFORMATION.
    has_info_intent = any(i.type == IntentType.INFORMATION for i in intents)
    if not policy_lookup and (has_information or capability) and not has_info_intent:
        intents.append(Intent(type=IntentType.INFORMATION, confidence=0.7))
    if not intents and _hits(lower, {k.lower() for k in _GREETING}):
        intents.append(Intent(type=IntentType.SMALL_TALK, confidence=0.7))
    if _hits(lower, {k.lower() for k in _COMPLAINT}):
        intents.append(Intent(type=IntentType.COMPLAINT, confidence=0.6))

    if not intents:
        intents.append(Intent(type=IntentType.UNKNOWN, confidence=0.3))
    return intents


def classify(text: str) -> IntentResult:
    """Deterministic triage used directly and as the stub handler."""
    intents = classify_intents(text)
    return IntentResult(
        language=detect_language(text),
        intents=intents,
        entities=extract_entities(text),
        confidence=max((i.confidence for i in intents), default=0.0),
        search_query=text,  # deterministic: query == message (rag_node anchors meta follow-ups)
    )


def _stub_handler(messages: list[Message]) -> IntentResult:
    text = next((m.content for m in reversed(messages) if m.role == "user"), "")
    return classify(text)


register_structured_handler(IntentResult, _stub_handler)


_SYSTEM_PROMPT = (
    "You are the triage agent for a multilingual insurance/lending support system. "
    "Classify the customer's intent(s) into this FIXED set and extract entities "
    "(policy_id, claim_id, mobile, email).\n"
    "Intent types: information, action, complaint, small_talk, unknown.\n"
    "Action values (set `action` only for type=action): get_claim_status (check an existing "
    "claim), get_policy_details (look up a policy), update_contact (change mobile/email), "
    "raise_ticket (open a support ticket/complaint), file_claim (lodge a NEW insurance claim).\n"
    "CRITICAL: A question about WHETHER or HOW to do something — 'can I…', 'how do I…', "
    "'kya main … kar sakta/sakti hu', 'is it possible' — is type=information, NEVER an action. "
    "Use an action ONLY when the customer directly asks you to perform it now "
    "(e.g. 'update my number to 98…', 'file a claim for my hospital bill', 'raise a complaint'). "
    "Use the conversation history to resolve short follow-ups like 'haan kar do' / 'ok do it'. "
    "Also set `search_query`: a STANDALONE retrieval query for the user's request — resolve "
    "references and follow-ups using the history (e.g. 'samajh nahi aaya' after a claim question "
    "-> 'why was my claim rejected'); if the message is already self-contained, copy it as-is. "
    "Return all intents that apply for mixed requests."
)


def _all_unknown(intents: list[Intent]) -> bool:
    return all(i.type == IntentType.UNKNOWN for i in intents) if intents else True


def _no_writes(intents: list[Intent]) -> list[Intent]:
    """Downgrade state-changing actions to information (capability-question safety guard)."""
    out: list[Intent] = []
    for i in intents:
        if i.type == IntentType.ACTION and i.action in _STEP_UP_ACTIONS_RT:
            out.append(Intent(type=IntentType.INFORMATION, confidence=i.confidence))
        else:
            out.append(i)
    return out or [Intent(type=IntentType.INFORMATION, confidence=0.7)]


# Write actions (mirror authz step-up tier) — used by the capability-question guard.
_STEP_UP_ACTIONS_RT = {"update_contact", "raise_ticket", "file_claim"}


def _normalize(res: IntentResult) -> IntentResult:
    """Fit the LLM's understanding into the closed intent/action set the gates expect.

    The LLM may phrase loosely or invent an action; we clamp every intent to the enum and the
    allowed-action set. An action with no valid tool becomes an information intent (answerable),
    never an unauthorized write. This is what keeps the deterministic security gates safe even
    though an LLM produced the label.
    """
    clean: list[Intent] = []
    for i in res.intents:
        action = i.action if i.action in _ALLOWED_ACTIONS else None
        typ = i.type
        if action:
            typ = IntentType.ACTION
        elif typ == IntentType.ACTION:
            typ = IntentType.INFORMATION  # "action" with no valid tool -> answer, don't act
        clean.append(Intent(type=typ, action=action, confidence=i.confidence or 0.7))
    res.intents = clean or [Intent(type=IntentType.UNKNOWN, confidence=0.3)]
    return res


class TriageAgent:
    """LLM-understood, deterministically-normalized triage.

    The LLM (fast Groq/Cerebras) *understands* free phrasing and maps it into the FIXED intent
    + action set; `_normalize` clamps the result so the downstream gates (compliance, step-up,
    intent->domain map) stay deterministic and injection-safe regardless of what the LLM said.
    Offline / in tests there is no key, so `structured()` falls to the stub — which returns the
    deterministic regex classifier — keeping eval fully reproducible. If the LLM can't classify,
    it is re-prompted once, then the deterministic classifier is the final fallback.

    Language detection routes through Sarvam's /text-lid (best Hinglish discrimination), with a
    deterministic fallback (degraded) and conversation-sticky carry-over for weak signals.
    """

    name = "triage"

    def __init__(self) -> None:
        self._sarvam = SarvamProvider()
        self._llm = get_llm("triage")

    async def _detect_language(self, message: str) -> tuple[Language, bool]:
        """Returns (language, degraded). degraded=True only when Sarvam was expected but failed."""
        settings = get_settings()
        if not (settings.triage_llm_language and self._sarvam.available):
            # No Sarvam configured: deterministic detection is the expected path, not degraded.
            return detect_language(message), False
        try:
            return await self._sarvam.detect_language(message), False
        except LLMError as e:
            log.warning("triage.sarvam_lid_failed", error=str(e))
            return detect_language(message), True

    async def _classify(
        self, message: str, history: list[HistoryTurn], retry: bool = False
    ) -> IntentResult:
        """Understand the message via the LLM (stub -> deterministic) into the normalized set."""
        system = _SYSTEM_PROMPT
        if retry:
            system += (
                " The previous attempt was unclear. Pick the single most likely intent; only "
                "use 'unknown' if truly nothing applies."
            )
        msgs = [Message(role="system", content=system)]
        for h in history[-12:]:  # ample prior turns so follow-ups ("ok do it") resolve in context
            role = "assistant" if h.role == "assistant" else "user"
            msgs.append(Message(role=role, content=h.content))
        msgs.append(Message(role="user", content=message))
        try:
            res = await self._llm.structured(msgs, IntentResult, max_tokens=300)
        except LLMError as e:
            log.warning("triage.intent_llm_failed", error=str(e))
            return classify(message)  # deterministic fallback
        return _normalize(res)

    async def run(
        self,
        message: str,
        hint: Language | None = None,
        history: list[HistoryTurn] | None = None,
    ) -> tuple[IntentResult, bool]:
        history = history or []
        # Deterministic FIRST (fast, free, reproducible). Only when it can't classify do we
        # spend an LLM call to UNDERSTAND the phrasing — this keeps scarce free-tier LLM budget
        # for the grounded answer (synthesis), and re-prompts the LLM once if still undefined.
        result = classify(message)
        if _all_unknown(result.intents) and self._llm.has_real_provider:
            llm_res = await self._classify(message, history)
            if _all_unknown(llm_res.intents):
                llm_res = await self._classify(message, history, retry=True)
            if not _all_unknown(llm_res.intents):
                result = llm_res
        # Deterministic safety guard: a capability/permission QUESTION must never become a write,
        # whatever the LLM said. Writes fire only on a direct request — keeps step-up honest.
        if _hits(message.lower(), {k.lower() for k in _CAPABILITY}):
            result.intents = _no_writes(result.intents)
        # Entities come from precise regex (IDs/mobile/email), authoritative over any LLM guess.
        result.entities = {**result.entities, **extract_entities(message)}

        language, degraded = await self._detect_language(message)
        # Sticky language: a short / mostly-numeric reply ("3456", "ok", "yes") carries no
        # reliable signal — keep the conversation's established language. Devanagari always wins.
        if hint is not None and _is_weak_language_signal(message):
            language = hint
        result.language = language
        return result, degraded


def _is_weak_language_signal(text: str) -> bool:
    """True when the message is too short/numeric to reliably detect a language from."""
    if _DEVANAGARI.search(text):
        return False  # Devanagari is an unambiguous Hindi signal
    words = _WORD_RE.findall(text.lower())
    # Few alphabetic words, or dominated by digits/IDs -> not enough signal.
    return len(words) <= 3
