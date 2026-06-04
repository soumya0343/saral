"""Triage / Intent agent.

Detects language (En/Hi/Hinglish), classifies intent(s), and extracts entities into an
`IntentResult`. Uses the LLM layer's structured output. The deterministic classifier below
doubles as the StubProvider handler, so triage works with no API key and stays reproducible
for eval.
"""

from __future__ import annotations

import re

from saral.llm.base import Message
from saral.llm.factory import get_llm
from saral.llm.stub import register_structured_handler
from saral.schemas import Intent, IntentResult, IntentType, Language

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
_INFORMATION = {
    "cover", "coverage", "covered", "policy detail", "policy details",
    "what does", "premium", "exclusion", "claim process", "how do i",
    "how to", "eligible", "kya cover", "policy", "faq", "deductible",
    "waiting period", "कवर", "पॉलिसी", "प्रीमियम",
    "loan", "emi", "foreclosure", "tenure", "interest rate", "grace period",
    "reinstate", "nominee", "statement", "miss",
}
_COMPLAINT = {"not working", "worst", "angry", "horrible", "complaint", "शिकायत", "bekar"}
_GREETING = {"hi", "hello", "hey", "namaste", "नमस्ते", "good morning", "good evening"}

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
    return entities


def _hits(text: str, lexicon: set[str]) -> bool:
    return any(kw in text for kw in lexicon)


def classify_intents(text: str) -> list[Intent]:
    lower = text.lower()
    intents: list[Intent] = []

    if _UPDATE_RE.search(text) or _hits(lower, {k.lower() for k in _UPDATE_CONTACT}):
        intents.append(Intent(type=IntentType.ACTION, action="update_contact", confidence=0.9))
    if _hits(lower, {k.lower() for k in _RAISE_TICKET}):
        intents.append(Intent(type=IntentType.ACTION, action="raise_ticket", confidence=0.85))
    has_claim_id = bool(_CLAIM_RE.search(text))
    claim_phrase = _hits(lower, _CLAIM_STATUS_PHRASES) or (
        "क्लेम" in text and "स्टेटस" in text
    )
    if has_claim_id or claim_phrase:
        intents.append(Intent(type=IntentType.ACTION, action="get_claim_status", confidence=0.8))
    if _hits(lower, {k.lower() for k in _INFORMATION}):
        intents.append(Intent(type=IntentType.INFORMATION, confidence=0.8))
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
    name = "triage"

    def __init__(self) -> None:
        self._llm = get_llm()

    async def run(self, message: str) -> IntentResult:
        messages = [
            Message(role="system", content=_SYSTEM_PROMPT),
            Message(role="user", content=message),
        ]
        return await self._llm.structured(messages, IntentResult)
