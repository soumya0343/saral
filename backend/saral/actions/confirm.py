"""Write confirmation + idempotency.

The idempotency key is conversation-anchored (not run-anchored) so a crash-resume re-uses the
same key and the mock backend dedups the write. The parsed value is read back to the customer
before execution; a stale pending write never auto-fires.
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Literal

from saral.config import get_settings
from saral.schemas import Intent, Language, PendingWrite

# Multilingual yes/no for confirmation.
_YES = {"yes", "y", "confirm", "ok", "okay", "haan", "haa", "ha", "हाँ", "हां", "जी", "sure"}
_NO = {"no", "n", "cancel", "stop", "nahi", "nahin", "नहीं", "ना", "mat"}


def parse_confirmation(text: str) -> Literal["yes", "no", "unclear"]:
    t = (text or "").strip().lower()
    if not t:
        return "unclear"
    tokens = set(t.replace(",", " ").replace(".", " ").split())
    if tokens & _YES:
        return "yes"
    if tokens & _NO:
        return "no"
    if t in _YES:
        return "yes"
    if t in _NO:
        return "no"
    return "unclear"


def idempotency_key(conversation_id: str, tool: str, args: dict, intent_nonce: int) -> str:
    """sha256(conversation_id + tool + canonical_args + intent_nonce) """
    canonical = json.dumps(args, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    raw = f"{conversation_id}|{tool}|{canonical}|{intent_nonce}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# Language-matched confirmation read-backs ({0}=field/value), with a yes/no suffix.
_READ_BACK = {
    "update_contact": {
        Language.EN: "Update registered {field} to {value} — confirm?",
        Language.HI: "पंजीकृत {field} को {value} में अपडेट करें — पुष्टि करें?",
        Language.HINGLISH: "Registered {field} ko {value} me update karein — confirm?",
    },
    "raise_ticket": {
        Language.EN: "Raise a support ticket: “{value}” — confirm?",
        Language.HI: "सहायता टिकट दर्ज करें: “{value}” — पुष्टि करें?",
        Language.HINGLISH: "Support ticket raise karein: “{value}” — confirm?",
    },
    "file_claim": {
        Language.EN: "File a new claim: “{value}” — confirm?",
        Language.HI: "नया क्लेम दर्ज करें: “{value}” — पुष्टि करें?",
        Language.HINGLISH: "Naya claim file karein: “{value}” — confirm?",
    },
}
_YESNO = {Language.EN: " (yes/no)", Language.HI: " (हाँ/नहीं)", Language.HINGLISH: " (haan/nahi)"}


def _read_back(tool: str, field: str | None, value: str | None, lang: Language) -> str:
    table = _READ_BACK.get(tool)
    if not table:
        return f"Execute {tool} — confirm?{_YESNO.get(lang, _YESNO[Language.EN])}"
    body = table.get(lang, table[Language.EN]).format(field=field, value=value)
    return body + _YESNO.get(lang, _YESNO[Language.EN])


def build_pending_write(
    conversation_id: str,
    intent: Intent,
    entities: dict,
    message: str,
    intent_nonce: int,
    language: Language = Language.EN,
) -> PendingWrite | None:
    """Parse a write intent into a confirmable PendingWrite, or None if args are missing."""
    tool = intent.action or ""
    field: str | None = None
    value: str | None = None
    args: dict

    if tool == "update_contact":
        if entities.get("mobile"):
            field, value = "mobile", str(entities["mobile"])
        elif entities.get("email"):
            field, value = "email", str(entities["email"])
        else:
            return None  # missing arg -> clarification, not a confirmable write
        args = {"field": field, "value": value}
    elif tool == "raise_ticket":
        value = (message[:60] + "…") if len(message) > 60 else message
        args = {"subject": value}
    elif tool == "file_claim":
        value = (message[:80] + "…") if len(message) > 80 else message
        args = {"subject": value}
    else:
        return None

    return PendingWrite(
        tool=tool,
        field=field,
        value=value,
        args=args,
        read_back=_read_back(tool, field, value, language),
        idempotency_key=idempotency_key(conversation_id, tool, args, intent_nonce),
        intent_nonce=intent_nonce,
        created_at=time.time(),
)


def is_stale(pw: PendingWrite, now: float | None = None) -> bool:
    """True if the pending write is past its TTL — execution authority is mortal (
    'Pending write'): a stale write is never auto-fired; resume re-earns step-up + confirm."""
    if pw.created_at is None:
        return False
    ttl = get_settings().pending_write_ttl_s
    return (now or time.time()) - pw.created_at > ttl
