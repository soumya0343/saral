"""Write confirmation + idempotency.

The idempotency key is conversation-anchored (not run-anchored) so a crash-resume re-uses the
same key and the mock backend dedups the write. The parsed value is read back to the customer
before execution; a stale pending write never auto-fires.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from typing import Literal

from saral.config import get_settings
from saral.schemas import Intent, Language, PendingWrite

# Multilingual yes/no for confirmation. A confirmation fires a state-changing request, so the
# parser is asymmetric: a negation anywhere wins over agreement, and a reply carrying BOTH
# signals ("haan nahi", "not ok", "no wait yes") is unclear -> re-ask, never yes.
_YES = {
    "yes", "y", "yeah", "yep", "yup", "confirm", "confirmed", "ok", "okay", "sure", "proceed",
    "haan", "haa", "ha", "han", "hanji", "haanji", "theek", "thik", "bilkul", "zaroor", "pakka",
    "हाँ", "हां", "हा", "ठीक", "बिलकुल", "बिल्कुल", "ज़रूर", "जरूर", "पक्का",
}
_NO = {
    "no", "n", "nope", "not", "dont", "never", "cancel", "stop", "wait", "nahi", "nahin", "nai",
    "nhi", "na", "mat", "ruko", "rehne", "नहीं", "नही", "ना", "मत", "रुको", "रहने", "रद्द",
}
# Politeness markers carry no polarity on their own: "जी नहीं" is a polite NO, "जी हाँ" a yes.
_NEUTRAL = {"जी", "ji", "please", "pls", "sir", "madam", "ma'am"}
_TOKEN_RE = re.compile(r"[^\wऀ-ॿ']+")


def _tokens(text: str) -> set[str]:
    t = (text or "").strip().lower().replace("’", "'").replace("'", "")  # don't -> dont
    return {w for w in _TOKEN_RE.split(t) if w} - _NEUTRAL


def parse_confirmation(text: str) -> Literal["yes", "no", "unclear"]:
    tokens = _tokens(text)
    yes, no = bool(tokens & _YES), bool(tokens & _NO)
    if no and not yes:
        return "no"
    if yes and not no:
        return "yes"
    return "unclear"  # empty, no signal, or mixed signals


def has_confirmation_signal(text: str) -> bool:
    """True if the reply carries any yes/no word (so an 'unclear' parse is a mixed answer to
    the read-back, not an unrelated new request)."""
    return bool(_tokens(text) & (_YES | _NO))


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


FIELD_NAME = {
    "mobile": {
        Language.EN: "mobile", Language.HI: "मोबाइल नंबर", Language.HINGLISH: "mobile number"
    },
    "email": {Language.EN: "email", Language.HI: "ईमेल", Language.HINGLISH: "email"},
}


def field_name(field: str | None, lang: Language) -> str:
    names = FIELD_NAME.get(field or "")
    return (names.get(lang) or names[Language.EN]) if names else (field or "")


def _read_back(tool: str, field: str | None, value: str | None, lang: Language) -> str:
    table = _READ_BACK.get(tool)
    if not table:
        return f"Execute {tool} — confirm?{_YESNO.get(lang, _YESNO[Language.EN])}"
    body = table.get(lang, table[Language.EN]).format(field=field_name(field, lang), value=value)
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
