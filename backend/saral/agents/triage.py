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
from saral.llm.sarvam import SarvamProvider
from saral.llm.stub import register_structured_handler
from saral.logging import get_logger
from saral.schemas import Intent, IntentResult, IntentType, Language

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
    "number update", "nambar", "नंबर",
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
_UPDATE_RE = re.compile(
    r"\b(update|change|badl|badal|अपडेट|बदल)\b.{0,20}?\b"
    r"(mobile|number|phone|email|contact|address|नंबर|ईमेल)\b",
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
)


def _stub_handler(messages: list[Message]) -> IntentResult:
    text = next((m.content for m in reversed(messages) if m.role == "user"), "")
    return classify(text)


register_structured_handler(IntentResult, _stub_handler)


_SYSTEM_PROMPT = (
    "You are the triage agent for a multilingual insurance/lending support system. "
    "Detect the language (en, hi, or hinglish), classify the customer's intent(s), and "
    "extract entities (policy_id, claim_id, mobile, email). Intent types: information "
    "(policy/FAQ question), action (account action — set the specific action: "
    "get_claim_status, get_policy_details, update_contact, raise_ticket), complaint, "
    "small_talk, unknown. Return all that apply for mixed requests."
)


class TriageAgent:
    """Sarvam-backed language detection + deterministic intent/entity classification.

    Intent classification and entity extraction are deterministic (: correctness
    over flexibility) — they drive compliance and tool selection and are what the eval suite
    validates. Language detection routes through Sarvam's /text-lid endpoint: it
    disambiguates Hinglish (romanized Hindi) from English far better than the regex marker
    list. On Sarvam unavailability the deterministic detector takes over and the run is flagged
    degraded.
    """

    name = "triage"

    def __init__(self) -> None:
        self._sarvam = SarvamProvider()

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

    async def run(self, message: str) -> tuple[IntentResult, bool]:
        result = classify(message)  # deterministic intent + entity + baseline language
        language, degraded = await self._detect_language(message)
        result.language = language
        return result, degraded
