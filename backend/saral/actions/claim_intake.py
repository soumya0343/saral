"""Claim intake v1: collect the facts a claim needs before it goes to the RM.

A filed claim used to be the raw chat message. Now the agent fills slots — policy (asked only
when the customer holds several), incident date, amount, hospital / garage, what happened —
over as many turns as needed, in the customer's language, then reads them all back before OTP +
confirmation. Extraction is deterministic (dates, ₹ amounts, "<name> hospital", policy ids); an
LLM may fill what the rules missed, but its values are re-validated (a real date not in the
future, a positive amount) — it can never invent a policy the customer doesn't hold.
"""

from __future__ import annotations

import re
import time
from datetime import date, timedelta

from pydantic import BaseModel

from saral.actions.confirm import idempotency_key
from saral.logging import get_logger
from saral.schemas import Language, PendingWrite

log = get_logger(__name__)

REQUIRED = ("policy_id", "incident_date", "amount_inr", "provider", "description")
MAX_ROUNDS = 3

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8,
    "sep": 9, "oct": 10, "nov": 11, "dec": 12,
    "जनवरी": 1, "फरवरी": 2, "फ़रवरी": 2, "मार्च": 3, "अप्रैल": 4, "मई": 5, "जून": 6,
    "जुलाई": 7, "अगस्त": 8, "सितंबर": 9, "सितम्बर": 9, "अक्टूबर": 10, "नवंबर": 11,
    "नवम्बर": 11, "दिसंबर": 12, "दिसम्बर": 12,
}
_MONTH_ALT = "|".join(sorted((re.escape(m) for m in _MONTHS), key=len, reverse=True))
_DMY = re.compile(r"\b(\d{1,2})[/.-](\d{1,2})[/.-](\d{2,4})\b")
_ISO = re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b")
_D_MON = re.compile(
    rf"(?<!\d)(\d{{1,2}})\s*(?:st|nd|rd|th)?\s*({_MONTH_ALT})[a-z]*\.?,?\s*(\d{{4}})?",
    re.IGNORECASE,
)
_RELATIVE = {
    "today": 0, "aaj": 0, "आज": 0,
    "yesterday": 1, "kal": 1, "कल": 1,  # "kal" for a past incident means yesterday
    "day before yesterday": 2, "parso": 2, "परसों": 2,
}
_REL_RE = re.compile(
    r"(?<![\wऀ-ॿ])(day before yesterday|yesterday|today|aaj|kal|parso|आज|कल|परसों)"
    r"(?![\wऀ-ॿ])",
    re.IGNORECASE,
)
_MULT = {"lakh": 100_000, "lac": 100_000, "लाख": 100_000, "k": 1_000, "thousand": 1_000,
         "hazar": 1_000, "hazaar": 1_000, "हज़ार": 1_000, "हजार": 1_000}
_AMOUNT_UNIT = r"lakh|lac|लाख|k\b|thousand|hazaa?r|हज़ार|हजार"
_AMOUNT = re.compile(
    rf"(?:₹|rs\.?|inr|rupees?|रुपये|रुपए|रु\.?)\s*(\d[\d,]*(?:\.\d+)?)\s*({_AMOUNT_UNIT})?"
    rf"|(\d[\d,]*(?:\.\d+)?)\s*({_AMOUNT_UNIT}|rupees?|rs\b|रुपये|रुपए)",
    re.IGNORECASE,
)
_BARE_NUMBER = re.compile(r"(?<![\d/.-])(\d{3,7}|\d{1,3}(?:,\d{2,3})+)(?![\d/.-])")
_PROVIDER_KIND = (
    r"(?i:hospitals?|clinic|nursing\s+home|medical\s+cent(?:er|re)|garage|motors|"
    r"service\s+cent(?:er|re)|workshop)"
)
# "Apollo Hospital", "Sharma Motors", "city hospital"; Hindi "सिटी अस्पताल".
_PROVIDER = re.compile(
    rf"\b([A-Z][\w&.'-]*(?:\s+[A-Z][\w&.'-]*){{0,2}})\s+({_PROVIDER_KIND})\b"
    rf"|\b([a-z][\w&.'-]*)\s+({_PROVIDER_KIND})\b"
    r"|((?:[\u0900-\u097f]+\s+){1,2})(अस्पताल|हॉस्पिटल|क्लिनिक|गैराज)"
)
_PROVIDER_STOP = {
    "at", "in", "the", "from", "my", "a", "an", "to", "and", "was", "admitted", "repaired",
    "kal", "aaj", "mein", "me", "se", "ka", "ki", "ke", "aur", "local", "nearby", "some",
    "mujhe", "mera", "meri", "mere", "i", "we", "our", "your", "his", "her", "for", "of",
    "को", "में", "से", "का", "की", "के", "और", "कल", "आज", "एक",
}
_POLICY = re.compile(r"\bPOL\d+\b", re.IGNORECASE)
# Words that carry no description of the incident (intent phrasing, fillers, slot words).
_FILLER = re.compile(
    r"\b(?:i|want|wanna|would|like|to|file|lodge|raise|register|make|a|an|the|new|claim|claims|"
    r"please|pls|my|for|on|of|it|is|was|and|at|in|with|policy|amount|date|rs|inr|rupees|hospital|"
    r"karna|karni|hai|mujhe|mera|meri|ek|naya|claim|file|karo|kar|do|dena|chahta|chahti|chahiye|"
    r"ka|ki|ke|se|mein|me|tha|thi|hua|hui|aur|lakh|hazar|yesterday|today|kal|aaj)\b"
    r"|नया|क्लेम|दर्ज|करना|करनी|है|मुझे|मेरा|मेरी|एक|फाइल|का|की|के|से|में|को|था|थी|हुआ|और|लाख|हज़ार|"
    r"कल|आज|रुपये|रुपए|अस्पताल",
    re.IGNORECASE,
)


def _valid_date(d: date, today: date) -> bool:
    return today - timedelta(days=3 * 365) <= d <= today


def parse_date(text: str, today: date | None = None) -> str | None:
    today = today or date.today()
    if m := _ISO.search(text):
        y, mo, d = (int(x) for x in m.groups())
    elif m := _DMY.search(text):
        d, mo, y = (int(x) for x in m.groups())
        y = y + 2000 if y < 100 else y
    elif m := _D_MON.search(text):
        d = int(m.group(1))
        mo = _MONTHS[next(k for k in _MONTHS if m.group(2).lower().startswith(k))]
        y = int(m.group(3)) if m.group(3) else today.year
        try:
            if not m.group(3) and date(y, mo, d) > today:
                y -= 1  # "12 Dec" said in January means last December
        except ValueError:
            return None
    elif m := _REL_RE.search(text):
        return (today - timedelta(days=_RELATIVE[m.group(1).lower()])).isoformat()
    else:
        return None
    try:
        got = date(y, mo, d)
    except ValueError:
        return None
    return got.isoformat() if _valid_date(got, today) else None


def parse_amount(text: str, allow_bare: bool = False) -> float | None:
    for m in _AMOUNT.finditer(text):
        num, mult = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
        try:
            value = float(num.replace(",", ""))
        except ValueError:
            continue
        value *= _MULT.get((mult or "").lower(), 1)
        if 0 < value <= 100_000_000:
            return value
    if allow_bare:
        stripped = _DMY.sub(" ", _ISO.sub(" ", text))
        for m in _BARE_NUMBER.finditer(stripped):
            value = float(m.group(1).replace(",", ""))
            if 100 <= value <= 100_000_000 and not re.fullmatch(r"(19|20)\d\d", m.group(1)):
                return value
    return None


def parse_provider(text: str) -> str | None:
    for m in _PROVIDER.finditer(text):
        if m.group(1):
            words = [w for w in m.group(1).split() if w.lower() not in _PROVIDER_STOP]
            kind = m.group(2)
        elif m.group(3):
            words = [] if m.group(3).lower() in _PROVIDER_STOP else [m.group(3)]
            kind = m.group(4)
        else:
            words = [w for w in m.group(5).split() if w not in _PROVIDER_STOP]
            kind = m.group(6)
        if words:
            return f"{' '.join(words)} {kind}"
    return None


def parse_description(text: str) -> str | None:
    """What happened, from what's left once intent words, amounts, dates and ids are removed."""
    rest = _AMOUNT.sub(" ", _DMY.sub(" ", _ISO.sub(" ", _D_MON.sub(" ", text))))
    rest = _POLICY.sub(" ", _REL_RE.sub(" ", rest))
    if (p := parse_provider(rest)) is not None:
        rest = rest.replace(p, " ")
    words = [w for w in re.split(r"[^\wऀ-ॿ]+", _FILLER.sub(" ", rest)) if len(w) > 1]
    words = [w for w in words if not w.isdigit()]
    if len(words) < 2:
        return None
    return " ".join(words)[:120]


class ClaimSlots(BaseModel):
    """What an LLM may fill in when the rules find nothing (re-validated before use)."""

    incident_date: str | None = None  # ISO yyyy-mm-dd
    amount_inr: float | None = None
    provider: str | None = None
    description: str | None = None


def extract(
    text: str, *, missing: set[str] | None = None, policies: list[str] | None = None
) -> dict:
    """Deterministic slot extraction. With `missing`, a bare number fills the amount only when
    the amount is what we asked for, and free text fills the description only then."""
    missing = missing if missing is not None else set(REQUIRED)
    out: dict = {}
    if (d := parse_date(text)) is not None:
        out["incident_date"] = d
    if (a := parse_amount(text, allow_bare="amount_inr" in missing)) is not None:
        out["amount_inr"] = a
    if (p := parse_provider(text)) is not None:
        out["provider"] = p
    if (desc := parse_description(text)) is not None and "description" in missing:
        out["description"] = desc
    owned = {x.upper() for x in (policies or [])}
    for m in _POLICY.findall(text):
        if m.upper() in owned:
            out["policy_id"] = m.upper()
            break
    return out


async def llm_extract(text: str, missing: set[str]) -> dict:
    """Optional LLM pass for what the rules missed; values re-validated, never trusted."""
    from saral.llm.base import LLMError, Message
    from saral.llm.factory import get_llm

    llm = get_llm("triage")
    if not llm.has_real_provider or not (missing - {"policy_id"}):
        return {}
    prompt = (
        "Extract insurance-claim facts from the customer's message (English, Hindi or Hinglish). "
        f"Today is {date.today().isoformat()}. Return incident_date as yyyy-mm-dd (resolve "
        "'yesterday'/'kal'), amount_inr as a number of rupees (1 lakh = 100000), provider as the "
        "hospital or garage name, description as a short English phrase of what happened. Use "
        "null for anything not stated. Never guess."
    )
    try:
        got = await llm.structured(
            [Message(role="system", content=prompt), Message(role="user", content=text)],
            ClaimSlots,
            max_tokens=200,
        )
    except LLMError as e:
        log.info("claim_intake.llm_failed", error=str(e))
        return {}
    out: dict = {}
    if "incident_date" in missing and got.incident_date:
        try:
            d = date.fromisoformat(got.incident_date)
            if _valid_date(d, date.today()):
                out["incident_date"] = d.isoformat()
        except ValueError:
            pass
    if "amount_inr" in missing and got.amount_inr and 0 < got.amount_inr <= 100_000_000:
        out["amount_inr"] = float(got.amount_inr)
    if "provider" in missing and got.provider and 3 < len(got.provider) <= 80:
        out["provider"] = got.provider.strip()
    if "description" in missing and got.description and len(got.description) >= 4:
        out["description"] = got.description.strip()[:120]
    return out


# --- prompts --------------------------------------------------------------------------------

_SLOT_NAME = {
    "policy_id": ("which policy ({policies})", "कौन-सी पॉलिसी ({policies})",
                  "kaunsi policy ({policies})"),
    "incident_date": ("the date it happened", "घटना की तारीख़", "ghatna ki date"),
    "amount_inr": ("the claim amount (₹)", "क्लेम की राशि (₹)", "claim amount (₹)"),
    "provider": ("the hospital or garage name", "अस्पताल या गैराज का नाम",
                 "hospital ya garage ka naam"),
    "description": ("what happened, in a line", "एक पंक्ति में क्या हुआ", "ek line mein kya hua"),
}
_ASK = {
    Language.EN: "To file your claim I need {slots}. You can send them together.",
    Language.HI: "आपका क्लेम दर्ज करने के लिए मुझे {slots} चाहिए। आप ये सब एक साथ भेज सकते हैं।",
    Language.HINGLISH: "Aapka claim file karne ke liye mujhe {slots} chahiye. Aap sab ek saath bhej sakte hain.",  # noqa: E501
}
_AND = {Language.EN: " and ", Language.HI: " और ", Language.HINGLISH: " aur "}
_READ_BACK = {
    Language.EN: "File a claim on {policy_id}: {description}, at {provider} on {incident_date}, for {amount} — confirm? (yes/no)",  # noqa: E501
    Language.HI: "{policy_id} पर क्लेम दर्ज करें: {description}, {provider} में, {incident_date} को, राशि {amount} — पुष्टि करें? (हाँ/नहीं)",  # noqa: E501
    Language.HINGLISH: "{policy_id} par claim file karein: {description}, {provider} mein, {incident_date} ko, amount {amount} — confirm? (haan/nahi)",  # noqa: E501
}
_IDX = {Language.EN: 0, Language.HI: 1, Language.HINGLISH: 2}


def missing_slots(args: dict) -> list[str]:
    return [k for k in REQUIRED if not args.get(k)]


def ask_prompt(missing: list[str], lang: Language, policies: list[str]) -> str:
    i = _IDX.get(lang, 0)
    names = [_SLOT_NAME[k][i].format(policies=", ".join(policies)) for k in missing]
    sep = _AND.get(lang, " and ")
    joined = names[0] if len(names) == 1 else ", ".join(names[:-1]) + sep + names[-1]
    return (_ASK.get(lang) or _ASK[Language.EN]).format(slots=joined)


def summary_line(args: dict) -> str:
    from saral.rag.customer_docs import inr

    return (
        f"{args.get('policy_id')} · {args.get('incident_date')} · {inr(args.get('amount_inr'))} · "
        f"{args.get('provider')} · {args.get('description')}"
    )


def pending_claim(
    conversation_id: str, args: dict, nonce: int, lang: Language
) -> PendingWrite:
    """A PendingWrite for the claim: still collecting (args['missing']) or complete (read-back
    lists every slot, idempotency key over the final facts)."""
    from saral.rag.customer_docs import inr

    facts = {k: args.get(k) for k in REQUIRED}
    missing = missing_slots(facts)
    full = {**facts, "missing": missing, "rounds": int(args.get("rounds", 0))}
    read_back = ""
    if not missing:
        read_back = (_READ_BACK.get(lang) or _READ_BACK[Language.EN]).format(
            **{**facts, "amount": inr(facts["amount_inr"])}
        )
    return PendingWrite(
        tool="file_claim",
        value=summary_line(facts) if not missing else None,
        args=full,
        read_back=read_back,
        idempotency_key=idempotency_key(conversation_id, "file_claim", facts, nonce),
        intent_nonce=nonce,
        created_at=time.time(),
    )


async def fill(text: str, args: dict, policies: list[str]) -> dict:
    """Merge the slots found in `text` into `args` (existing values are kept)."""
    facts = {k: args.get(k) for k in REQUIRED}
    if not facts.get("policy_id") and len(policies) == 1:
        facts["policy_id"] = policies[0]
    missing = set(missing_slots(facts))
    found = extract(text, missing=missing, policies=policies)
    still = missing - set(found)
    if still:
        found = {**await llm_extract(text, still), **found}
    for k, v in found.items():
        if not facts.get(k):
            facts[k] = v
    return facts


def is_slot_reply(text: str, pending: PendingWrite, policies: list[str]) -> bool:
    """During intake, does this reply answer what we asked (vs. a new request)?"""
    missing = set(pending.args.get("missing") or REQUIRED)
    return bool(extract(text, missing=missing, policies=policies))
