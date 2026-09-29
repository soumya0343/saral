"""Prompt-injection / jailbreak resistance (v2).

Deterministic, fail-closed, reproducible. v1 matched lone words ("approve", "no restrictions",
"you are now", any "कंप्लायंस"), which blocked half of the genuine customers who used them
("when will you approve my loan?"). v2 matches attack STRUCTURE instead — each family needs
an operator aimed at the agent plus its target:

- override       ignore / forget / disregard + instructions / rules / prompt
- persona        "you are now admin", "developer mode", "pretend to be", "tum ab admin ho"
- role_tag       a fake "SYSTEM:" / "[developer]" / "<assistant>" turn marker
- extraction     reveal / print / batao + system prompt / hidden instructions
- exfiltration   every / all / sabka + customers' data / database
- bypass         skip / without / bina + verification / OTP / compliance (a customer asking
                 whether *they* can do something without OTP is a question, not an attack)

Coercing an approval ("approve my refund") is NOT an attack family on its own: the agent has
no tool that approves anything, so blocking it only hurts genuine customers; combined with an
override ("ignore the rules and approve…") the override family catches it.

Text is normalized first (NFKC, zero-width removed, spaced-out letters re-joined, leetspeak
folded) so "i g n o r e your rules" and "1gn0re" don't slip through. An optional local
classifier (e.g. Llama Prompt Guard 2) can be layered on via INJECTION_CLASSIFIER_MODEL.
"""

from __future__ import annotations

import re
import unicodedata
from functools import lru_cache

from saral.config import get_settings
from saral.logging import get_logger

log = get_logger(__name__)

_ZERO_WIDTH = re.compile(r"[​-‏⁠﻿]")
_SPACED = re.compile(r"\b(?:[a-z]\s){3,}[a-z]\b", re.I)
_LEET = str.maketrans({"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a",
                       "$": "s"})


_DEVA_CHAR = re.compile(r"[\u0900-\u097f]")


def _join_spaced_devanagari(t: str) -> str:
    """'नि र्दे श  भू ल  जा ओ' -> 'निर्देशभूलजाओ' when most tokens are 1–2 char syllables."""
    toks = t.split()
    deva = [x for x in toks if _DEVA_CHAR.search(x)]
    if len(deva) >= 6 and sum(len(x) <= 3 for x in deva) / len(deva) >= 0.6:
        return "".join(toks)
    return t


def normalize(text: str) -> str:
    t = _ZERO_WIDTH.sub("", unicodedata.normalize("NFKC", text or ""))
    t = _SPACED.sub(lambda m: m.group(0).replace(" ", ""), t)
    return _join_spaced_devanagari(t)


def _variants(text: str) -> list[str]:
    n = normalize(text)
    leet = n.translate(_LEET)
    return [n] if leet == n else [n, leet]


_OVERRIDE_VERB = (
    r"ignore|disregard|forget|override|overrule|bypass|drop|abandon|discard|neglect"
)
_INSTR = (
    r"instructions?|rules?|guidelines?|guardrails?|polic(?:y|ies)|restrictions?|prompts?|"
    r"directives?|programming|constraints?|safety|protocols?"
)
_HG_POLICIES = r"(?:saari|saare|sab|sabhi|apni|apne|all|your)\s+polic(?:y|ies)"
_Q = r"(?:all|any|every|the|your|my|previous|prior|above|earlier|system|these|those|such|of|\s)*"
_HI_INSTR = r"नियम|नियमों|निर्देश|निर्देशों|रूल|रूल्स|पाबंदी|प्रतिबंध"
_HI_IGNORE = r"भूल|अनदेखा|नज़रअंदाज़?|नजरअंदाज|इग्नोर|छोड़|मत\s*मान|हटा"
_HG_INSTR = r"rules?|niyam|instructions?|guidelines?|restrictions?|pabandi"
_HG_IGNORE = r"bhool|bhul|ignore|chhod|chod|mat\s+maan|hata"

_PERSONA_ROLE = (
    r"admin(?:istrator)?|developer|dev|root|superuser|super\s*user|god|dan|jailbroken|"
    r"unrestricted|unfiltered|system|maintenance|debug|sudo|staff|employee|underwriter|"
    r"claims?\s+officer|manager"
)
_HI_ROLE = r"एडमिन|डेवलपर|मैनेजर|अधिकारी|सिस्टम|रूट"

_FAMILIES: dict[str, list[re.Pattern[str]]] = {
    "override": [
        re.compile(rf"\b(?:{_OVERRIDE_VERB})\b{_Q}(?:{_INSTR})\b", re.I),
        re.compile(rf"\b(?:{_INSTR})\b.{{0,20}}\b(?:don'?t|do\s+not|no\s+longer)\s+apply\b", re.I),
        re.compile(rf"(?:{_HI_INSTR}).{{0,20}}(?:{_HI_IGNORE})"),
        re.compile(rf"\b(?:{_HG_INSTR})\b.{{0,20}}\b(?:{_HG_IGNORE})", re.I),
        re.compile(r"\bnew\s+(?:system\s+)?(?:instructions?|prompt|rules)\s*[:\-]", re.I),
        # "disregard the above" / "ignore everything before this" — the prior CONTEXT as target
        # (a customer's "ignore my previous message" is a self-correction, not matched).
        re.compile(r"\b(?:ignore|disregard)\s+(?:all\s+|everything\s+)?(?:of\s+)?(?:the|that)\s+above\b",
                   re.I),
        re.compile(r"\b(?:ignore|disregard|forget)\s+(?:all|everything)\s+(?:above|before|so\s+far|"
                   r"previous(?:ly)?|prior|earlier)\b", re.I),
        re.compile(r"\b(?:ignore|disregard|forget)\s+(?:everything|anything|all|what)\s+(?:that\s+)?"
                   r"you(?:'ve|\s+have|\s+were|\s+had)?\s+(?:been\s+)?(?:told|given|instructed|taught)\b",
                   re.I),
        re.compile(r"\b(?:ignore|disregard|forget|override)\s+(?:all\s+|your\s+|company\s+)+polic(?:y|ies)\b",
                   re.I),
        re.compile(rf"{_HG_POLICIES}\s+(?:{_HG_IGNORE})", re.I),
        re.compile(rf"\b(?:ignore|bhool|bhul|chhod)\w*\s+(?:kar\w*\s+)?(?:saare|sab|sabhi|all\s+)?\s*"
                   rf"(?:{_HG_INSTR})\b", re.I),
    ],
    "persona": [
        re.compile(rf"\byou\s+are\s+(?:now|no\s+longer)\s+(?:an?\s+|the\s+|in\s+)?(?:{_PERSONA_ROLE})\b",
                   re.I),
        re.compile(rf"\b(?:act|behave|respond|operate)\s+as\s+(?:an?\s+|the\s+)?(?:{_PERSONA_ROLE})\b",
                   re.I),
        re.compile(r"\bpretend\s+(?:to\s+be|you\s+are|that\s+you)\b", re.I),
        re.compile(r"\broleplay\s+as\b|\brole-play\s+as\b", re.I),
        re.compile(r"\b(?:developer|admin|maintenance|debug|god|dan|jailbreak|sudo)\s+mode\b",
                   re.I),
        re.compile(r"\bDAN\b"),
        re.compile(rf"\b(?:tum|aap|tu)\s+(?:ab|abhi\s+se)\s+(?:{_PERSONA_ROLE})\b", re.I),
        re.compile(rf"(?:तुम|आप|तू)\s+(?:अब|अभी\s+से)\s+(?:{_HI_ROLE})"),
        re.compile(rf"(?:{_HI_ROLE}|डीबग|मेंटेनेंस|गॉड)\s*मोड"),
        # "from now on you…", "मान लो तुम…", "अब से तुम…": re-defining who the agent is.
        re.compile(r"\bfrom\s+now\s+on,?\s+you\s+(?:are|will|must|should|answer|act|respond|reply)\b",
                   re.I),
        re.compile(r"(?:अब\s+से|मान\s+लो(?:\s+कि)?)\s+(?:तुम|आप|तू)\b"),
        re.compile(r"\b(?:ab\s+se|maan\s+lo(?:\s+ki)?)\s+(?:tum|aap|tu)\b", re.I),
        # A named persona: "You are now SaralRoot, …", "Tum ab 'SuperAgent' ho".
        re.compile(r"(?i:\byou\s+are\s+now)\s+['\"]?[A-Z][A-Za-z0-9]+['\"]?\s*[,(]"),
        re.compile(r"(?i:\b(?:tum|aap)\s+ab)\s+['\"]?[A-Z][A-Za-z0-9]+['\"]?\s+(?i:ho|ban)\b"),
        re.compile(r"\b(?:unrestricted|unfiltered|uncensored|jailbroken)\s+(?:assistant|ai|bot|agent|"
                   r"model|mode|version)\b", re.I),
        # "…an assistant on which no rules apply" (describing the agent, not the policy).
        re.compile(r"(?:जिस|जिन)(?:\s+\S+){0,2}\s+कोई\s+(?:नियम|प्रतिबंध|पाबंदी)"),
        re.compile(r"\b(?:jis|jiske|jinke|jin)(?:\s+\w+){0,2}\s+koi\s+(?:restriction|rule|niyam|pabandi)",
                   re.I),
    ],
    "role_tag": [
        re.compile(r"(?:^|\n)\s*[\[<(#]*\s*(?:system|developer|assistant|admin)\s*[\]>)]*\s*:",
                   re.I),
        re.compile(r"[\[<]\s*/?\s*(?:system|developer|assistant|inst|sys)\s*[\]>]", re.I),
    ],
    "extraction": [
        re.compile(r"\bsystem\s+prompt\b", re.I),
        re.compile(r"\b(?:hidden|secret|internal|initial|original)\s+(?:instructions?|prompt|rules)\b",
                   re.I),
        re.compile(r"\b(?:reveal|print|output|display|repeat|dump|show|leak)\s+(?:me\s+)?(?:your|the)"
                   r"\s+(?:instructions?|prompt|configuration|guidelines)\b", re.I),
        re.compile(r"सिस्टम\s*प्रॉम्प्ट|प्रॉम्प्ट|अपने\s+निर्देश\s+(?:बताओ|दिखाओ)"),
        re.compile(r"\b(?:repeat|print|show|give|tell|output|reveal|share|paste|write)\b.{0,40}"
                   r"\binstructions?\s+(?:you\s+(?:were|have\s+been|got)\s+given|given\s+to\s+you|"
                   r"you\s+got|at\s+the\s+(?:start|beginning))", re.I),
        re.compile(r"\b(?:apna|apne|tumhara|aapka)\s+(?:prompt|instructions)\b", re.I),
    ],
    "exfiltration": [
        re.compile(r"\b(?:all|every|each|other)\s+(?:the\s+)?(?:customers?|users?|policyholders?|clients?)"
                   r"(?:'s|'|s)?\s+(?:data|details|claims|records|information|info|policies|numbers|"
                   r"accounts?)\b", re.I),
        re.compile(r"\b(?:customer|user|entire|whole|full)\s+(?:database|db|table|dump)\b", re.I),
        # Everyone's data — not "mere saare claims" (the customer's own).
        re.compile(r"\b(?:sabka|sab\s+ka)\s+(?:data|details|record|claims)\b", re.I),
        re.compile(r"\b(?:sabhi|saare|sare|sab|all)\s+(?:customers?|grahakon?|users?)\s+(?:ka|ke|ki)\s+"
                   r"(?:\w+\s+)?(?:data|details|records?|claims)\b", re.I),
        re.compile(r"(?:सभी|सारे|हर)\s+(?:ग्राहकों?|यूज़र्स?|कस्टमर्स?)\s+(?:का|की|के)\s+"
                   r"(?:डेटा|डाटा|जानकारी|विवरण|क्लेम|पॉलिसी|नंबर|मोबाइल|नाम)"),
        re.compile(r"डेटाबेस|डाटाबेस"),
        re.compile(r"\b(?:claims|policies|payouts)\b.{0,30}\b(?:along\s+with|with|and)\s+(?:the\s+)?"
                   r"customer(?:s'|'s)?\s+names?\b", re.I),
    ],
    "bypass": [
        re.compile(r"\b(?:skip|bypass|disable|turn\s+off|switch\s+off|avoid|without|no\s+need\s+(?:for|of))"
                   r"\s+(?:the\s+|any\s+|an?\s+)?(?:verification|otp|authenticat\w*|authori[sz]\w*|"
                   r"compliance|kyc|security|safety|checks?|screening)\b", re.I),
        re.compile(r"\b(?:verification|otp|compliance|security|code)\s+(?:check\s+|step\s+)?(?:is\s+)?"
                   r"(?:optional|not\s+(?:needed|required|necessary)|unnecessary)\b", re.I),
        re.compile(r"\b(?:otp|verification|code)\s+ki\s+(?:zarurat|zaroorat|jarurat|jaroorat)\s+nahi",
                   re.I),
        re.compile(r"(?:OTP|ओटीपी|वेरिफिकेशन|सत्यापन|कोड)\s+की\s+(?:ज़रूरत|जरूरत|आवश्यकता)\s+नहीं"),
        re.compile(r"\bskip\s+(?:the\s+)?(?:code|otp\s+step)\b", re.I),
        re.compile(r"\b(?:verification|otp|compliance|kyc|authori[sz]ation)\s+(?:skip|bypass|hata|band)"
                   r"\s*(?:karo|kar\s*do|"
                   r"karke|kardo)?\b", re.I),
        re.compile(r"\bbina\s+(?:kisi\s+)?(?:verification|otp|kyc|verify)\b", re.I),
        re.compile(r"बिना\s*(?:किसी\s+)?(?:वेरिफिकेशन|ओटीपी|OTP|सत्यापन|जाँच|जांच)"),
        re.compile(r"(?:वेरिफिकेशन|ओटीपी|सत्यापन|कंप्लायंस|कम्प्लायंस|जाँच|जांच)\s*(?:को\s*)?"
                   r"(?:छोड़|हटा|बंद|स्किप)"),
    ],
}
# Two-part signals: neither half alone is an attack ("I'm a police officer, am I covered on
# duty?"; "I was already verified, why does the app say unverified?"), both together are.
_COMPOUND: list[tuple[str, re.Pattern[str], re.Pattern[str]]] = [
    (  # staff / authority impersonation + a privileged ask
        "impersonation",
        re.compile(r"\b(?:this\s+is|i\s+am|i'm|main|mai)\b.{0,60}\b(?:from\s+the\s+\w+\s+"
                   r"(?:department|team|dept)|employee\s+id|emp\s*id|branch\s+manager|relationship\s+"
                   r"manager|police|cyber\s*cell|inspector|auditor|claims\s+officer|underwriter)"
                   r"|मैं.{0,50}(?:प्रबंधक|मैनेजर|पुलिस|इंस्पेक्टर|ऑडिटर|अधिकारी|कर्मचारी)", re.I),
        re.compile(r"\b(?:approve|approved|release|payout|sanction|mark\s+\w+\s+as|customer\s+(?:named|"
                   r"U\d+)|U\d{4}|account\s+details|bank\s+details)\b|मंज़ूर|मंजूर|पास\s+कर|स्वीकृति|"
                   r"ग्राहक|बैंक\s+खाता|विवरण\s+(?:मुझे\s+)?(?:भेजो|बताओ)", re.I),
    ),
    (  # "already verified" + a change request: talking the agent out of step-up
        "bypass",
        re.compile(r"\b(?:already|pehle\s+(?:hi|se))\s+verified\b|\bverified\s+(?:already|on\s+the)"
                   r"|वेरिफिकेशन\s+(?:पहले\s+ही\s+)?हो\s+चुका", re.I),
        re.compile(r"\b(?:change|update|badal|badlo|replace|set)\w*\b|बदल|अपडेट", re.I),
    ),
    (  # approval without the documents the process requires
        "bypass",
        re.compile(r"\b(?:without|bina)\s+(?:any\s+)?(?:documents?|papers?|dastavez\w*)\b"
                   r"|बिना\s+(?:किसी\s+)?(?:दस्तावेज़|दस्तावेज|डॉक्यूमेंट|कागज़?)", re.I),
        re.compile(r"\b(?:approve|pass|sanction|release|clear)\b|पास\s+कर|मंज़ूर|मंजूर|अप्रूव",
                   re.I),
    ),
]

# A customer asking whether THEY can do something without OTP is asking about the process;
# asking the AGENT to skip it ("can you skip…") is not exempt.
_SELF_QUESTION = re.compile(
    r"^\s*(?:can\s+i|could\s+i|do\s+i|is\s+it|is\s+there|are\s+there|what\s+if|how\s+(?:do|can)\s+i|"
    r"kya\s+(?:main|mai|mujhe|hum)|क्या\s+(?:मैं|मुझे|हम))\b",
    re.I,
)
_SENTENCES = re.compile(r"[^.?!।\n]+[.?!।]?")


def _match(text: str) -> tuple[str, str] | None:
    for family, patterns in _FAMILIES.items():
        for pat in patterns:
            if m := pat.search(text):
                if family == "bypass":
                    sentence = next(
                        (s for s in _SENTENCES.findall(text) if m.group(0) in s), text
                    )
                    if _SELF_QUESTION.search(sentence):
                        continue
                return family, m.group(0).strip()
    for family, first, second in _COMPOUND:
        if (a := first.search(text)) and second.search(text):
            return family, a.group(0).strip()
    return None


# Families that are instructions aimed at the MODEL — also screened in retrieved passages
# (indirect injection). Bypass/exfiltration wording is legitimate in FAQ docs ("never share
# your OTP; fraudsters ask you to skip verification").
PASSAGE_FAMILIES = {"override", "persona", "role_tag", "extraction"}


def detect_injection(text: str, families: set[str] | None = None) -> tuple[bool, str]:
    """Return (is_injection, reason)."""
    for variant in _variants(text):
        hit = _match(variant)
        if hit and (families is None or hit[0] in families):
            return True, f"prompt-injection ({hit[0]}): '{hit[1]}'"
    if families is None and (verdict := _classifier(text)):
        return True, verdict
    return False, ""


@lru_cache
def _load_classifier():  # pragma: no cover — needs a downloaded model
    model = get_settings().injection_classifier_model
    if not model:
        return None
    try:
        from transformers import pipeline

        return pipeline("text-classification", model=model)
    except Exception as e:  # noqa: BLE001 — optional layer; rules still apply
        log.warning("injection.classifier_unavailable", model=model, error=str(e))
        return None


def _classifier(text: str) -> str | None:
    """Optional local classifier (INJECTION_CLASSIFIER_MODEL, e.g. the free, multilingual
    meta-llama/Llama-Prompt-Guard-2-86M). Off by default; rules are the reproducible floor."""
    clf = _load_classifier()
    if clf is None:
        return None
    out = clf(text[:2000])[0]
    label = str(out.get("label", "")).upper()
    score = float(out.get("score", 0.0))
    threshold = get_settings().injection_classifier_threshold
    if label not in ("BENIGN", "LABEL_0", "SAFE") and score >= threshold:
        return f"prompt-injection (classifier {label} {score:.2f})"
    return None
