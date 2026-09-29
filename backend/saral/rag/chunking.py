"""Heading-aware, clause-aware markdown chunking shared by the generic and per-customer indexes.

A paragraph (blank-line separated block) is the retrieval unit. Headings are NOT chunks of
their own: they are tracked as a path (`Policy Schedule › 6. Grace Period and Lapse`) that is
attached to every paragraph under them, so a chunk keeps the context the heading gave it. A
paragraph that opens with a clause number (`6.2 …`, `**Clause R1.3**`, `खंड 6.2 …`) records it
as `clause_id`, which becomes the citation anchor (`CLM2030.md#6.2` instead of `#3`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")
# A clause number at the start of a paragraph: "6.2 ", "**4.1 Proportionate", "Clause R1.3",
# "खंड 6.2". Requires a dot (6.2 / R1.3) so a plain list number ("1 ") is not a clause.
_CLAUSE_START_RE = re.compile(
    r"^\W{0,3}(?:clause\s+|खंड\s+)?(R?\d+(?:\.\d+)+)(?=[\s.:)*—-])", re.IGNORECASE
)
# Clause references anywhere in text ("Clause 6.2", "खंड 4.1", "Clause R1.3").
CLAUSE_REF_RE = re.compile(r"(?:clause|खंड|khand)\s*(R?\d+(?:\.\d+)+)", re.IGNORECASE)
PATH_SEP = " › "


@dataclass
class Chunk:
    text: str  # the paragraph, whitespace-normalized (what is shown / quoted)
    section: str = ""  # heading path, e.g. "Policy Schedule › 4. Claim Settlement"
    clause_id: str | None = None  # clause this paragraph IS (not one it merely mentions)
    refs: list[str] = field(default_factory=list)  # clauses the paragraph mentions

    @property
    def indexed_text(self) -> str:
        """What gets embedded / BM25-tokenized: heading path + body, so a paragraph under
        '## 6. Lapse' matches a lapse question even if it never says 'lapse' itself."""
        return f"{self.section}: {self.text}" if self.section else self.text


def _clean_heading(raw: str) -> str:
    return raw.replace("**", "").strip()


def chunk_markdown(text: str) -> list[Chunk]:
    stack: list[tuple[int, str]] = []  # (level, heading)
    chunks: list[Chunk] = []
    for block in (b.strip() for b in text.split("\n\n")):
        if not block:
            continue
        body_lines: list[str] = []
        for line in block.splitlines():
            m = _HEADING_RE.match(line.strip())
            if m and not body_lines:
                level, title = len(m.group(1)), _clean_heading(m.group(2))
                while stack and stack[-1][0] >= level:
                    stack.pop()
                stack.append((level, title))
            else:
                body_lines.append(line)
        body = " ".join(" ".join(body_lines).split())
        if not body:
            continue
        clause = _CLAUSE_START_RE.match(body)
        chunks.append(
            Chunk(
                text=body,
                section=PATH_SEP.join(h for _, h in stack),
                clause_id=clause.group(1).upper() if clause else None,
                refs=sorted({r.upper() for r in CLAUSE_REF_RE.findall(body)}),
            )
        )
    return chunks
