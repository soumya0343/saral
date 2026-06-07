"""PII detection + redaction (the Redact gate, CONTEXT 'Redact gate').

`PresidioRedactor` (NER-backed: catches names/locations) is the production redact gate, layered
over the regex patterns so India-specific PII (Aadhaar/PAN/mobile) Presidio's default NER misses
is still caught. It needs the `pii` extra; if that import fails, `get_redactor()` falls back to
the dependency-free `RegexRedactor`. CI/eval default to regex (deterministic + reproducible).

Redaction runs before storage and before any logged preview (FR-6, NFR-4).
"""

from __future__ import annotations

import re
from typing import Protocol

from saral.config import get_settings
from saral.logging import get_logger

log = get_logger(__name__)

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
    """NER-backed redactor (requires the `pii` extra: presidio-analyzer/anonymizer).

    Layered over the regex patterns: the regex pass first catches India-specific PII
    (Aadhaar/PAN/mobile) that Presidio's default English NER does not, then Presidio adds
    names/locations/etc.
    """

    def __init__(self) -> None:
        from presidio_analyzer import AnalyzerEngine
        from presidio_anonymizer import AnonymizerEngine

        self._analyzer = AnalyzerEngine()
        self._anonymizer = AnonymizerEngine()
        self._regex = RegexRedactor()

    def redact(self, text: str) -> tuple[str, list[str]]:
        out, found = self._regex.redact(text)  # India-specific PII first
        results = self._analyzer.analyze(text=out, language="en")
        if results:
            found = sorted(set(found) | {r.entity_type for r in results})
            out = self._anonymizer.anonymize(text=out, analyzer_results=results).text
        return out, found


_redactor: PIIRedactor | None = None


def get_redactor() -> PIIRedactor:
    global _redactor
    if _redactor is None:
        if get_settings().pii_backend == "presidio":
            try:
                _redactor = PresidioRedactor()
            except Exception as e:  # noqa: BLE001 — missing `pii` extra / model
                log.warning("pii.presidio_unavailable", error=str(e), fallback="regex")
                _redactor = RegexRedactor()
        else:
            _redactor = RegexRedactor()
    return _redactor


def redact_pii(text: str) -> str:
    return get_redactor().redact(text)[0]
