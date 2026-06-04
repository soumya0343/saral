"""PII detection + redaction.

Default `RegexRedactor` is deterministic and dependency-free — it covers the high-risk
Indian PII types (mobile, email, Aadhaar, PAN, card) and is reproducible for eval. A
`PresidioRedactor` (NER-backed, catches names/locations) is available via the `pii` extra.

Redaction runs before storage and before any logged preview (FR-6, NFR-4).
"""

from __future__ import annotations

import re
from typing import Protocol

from saral.config import get_settings

_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("EMAIL", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")),
    ("AADHAAR", re.compile(r"\b\d{4}\s?\d{4}\s?\d{4}\b")),
    ("PAN", re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b")),
    ("CARD", re.compile(r"\b(?:\d[ -]?){13,16}\b")),
    ("MOBILE", re.compile(r"\b(?:\+?91[-\s]?)?[6-9]\d{9}\b")),
]


class PIIRedactor(Protocol):
    def redact(self, text: str) -> tuple[str, list[str]]: ...


class RegexRedactor:
    def redact(self, text: str) -> tuple[str, list[str]]:
        found: list[str] = []
        out = text
        # Order matters: Aadhaar/card (longer digit runs) before mobile.
        for label, pattern in _PATTERNS:
            if pattern.search(out):
                found.append(label)
                out = pattern.sub(f"<{label}>", out)
        return out, found


class PresidioRedactor:
    """NER-backed redactor (requires the `pii` extra: presidio-analyzer/anonymizer)."""

    def __init__(self) -> None:
        from presidio_analyzer import AnalyzerEngine
        from presidio_anonymizer import AnonymizerEngine

        self._analyzer = AnalyzerEngine()
        self._anonymizer = AnonymizerEngine()

    def redact(self, text: str) -> tuple[str, list[str]]:
        results = self._analyzer.analyze(text=text, language="en")
        found = sorted({r.entity_type for r in results})
        anonymized = self._anonymizer.anonymize(text=text, analyzer_results=results)
        return anonymized.text, found


_redactor: PIIRedactor | None = None


def get_redactor() -> PIIRedactor:
    global _redactor
    if _redactor is None:
        _redactor = (
            PresidioRedactor() if get_settings().pii_backend == "presidio" else RegexRedactor()
        )
    return _redactor


def redact_pii(text: str) -> str:
    return get_redactor().redact(text)[0]
