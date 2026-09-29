"""Which language is a reply written in? (deterministic, for the language_match metric)

FR-1 says reply in the customer's language: Hindi → Devanagari, Hinglish → Hindi in Latin
script, English → English. Script is measured on letters only (digits, ₹, ids like CLM2030 and
clause numbers are neutral). Hinglish vs English is decided by the share of distinctive
romanized-Hindi function words; English loanwords common in Hinglish ("policy", "claim") are
neutral, so they don't count either way.
"""

from __future__ import annotations

import re

_DEVA = re.compile(r"[ऀ-ॿ]")
_LATIN_WORD = re.compile(r"[A-Za-z]+")
_ID = re.compile(r"\b[A-Z]{2,}[-/]?\d+\b")  # CLM2030, POL1003, TKT5001

# Distinctive romanized-Hindi words. Words that are also common English words ("to", "the",
# "me") are excluded — they'd make English replies look Hinglish.
_HINGLISH = {
    "hai", "hain", "ho", "hoga", "hogi", "honge", "tha", "thi", "gaya", "gayi", "gaye",
    "raha", "rahi", "rahe", "ka", "ki", "ke", "ko", "se", "mein", "aur",
    "kya", "kyun", "kyon", "kaise", "kab", "kitna", "kitni", "nahi", "nahin", "haan", "ji",
    "aap", "aapka", "aapki", "aapke", "apna", "apni", "apne", "mera", "meri", "mere", "hum",
    "hamara", "hamari", "hamare", "yeh", "ye", "woh", "wo", "isliye", "kyunki", "lekin",
    "kripya", "karein", "kare", "karna", "kar", "karke", "karo", "diya", "diye", "liya",
    "liye", "sakte", "sakta", "sakti", "chahiye", "chahte", "chahti", "jayega", "jayegi",
    "hua", "hui", "bataiye", "batayein", "bhej", "bheja", "dijiye", "theek", "abhi", "baad",
    "pehle", "sirf", "bhi", "agar", "toh", "wala", "wali", "wale", "mujhe", "hamein",
}
_HINGLISH_SHARE = 0.12  # share of Latin words that must be distinctive Hindi words


def reply_language(text: str) -> str:
    """'hi' | 'hinglish' | 'en' | 'unknown' (no letters)."""
    body = _ID.sub(" ", text or "")
    deva = len(_DEVA.findall(body))
    words = _LATIN_WORD.findall(body)
    latin = sum(len(w) for w in words)
    if deva + latin == 0:
        return "unknown"
    if deva / (deva + latin) >= 0.5:
        return "hi"
    hindi_words = sum(1 for w in words if w.lower() in _HINGLISH)
    if words and hindi_words / len(words) >= _HINGLISH_SHARE:
        return "hinglish"
    return "en"


def matches(text: str, expected: str) -> bool:
    """A reply with no letters (e.g. just an id) can't mismatch."""
    got = reply_language(text)
    return got == "unknown" or got == expected
