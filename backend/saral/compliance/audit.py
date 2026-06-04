"""Tamper-evident audit hash-chain.

Each entry's `hash_self = sha256(hash_prev || seq || decision || actor || reason)`. Any
post-hoc edit breaks the chain from that point on, which `verify_chain` detects. The chain
logic is pure (no DB) so it is unit-testable and reusable by the persistence layer.
"""

from __future__ import annotations

import hashlib

from pydantic import BaseModel

GENESIS = "0" * 64


class AuditEntry(BaseModel):
    seq: int
    decision: str
    reason: str
    actor: str
    hash_prev: str
    hash_self: str


def compute_hash(hash_prev: str, seq: int, decision: str, actor: str, reason: str) -> str:
    payload = f"{hash_prev}|{seq}|{decision}|{actor}|{reason}".encode()
    return hashlib.sha256(payload).hexdigest()


def append(prev: AuditEntry | None, *, decision: str, actor: str, reason: str) -> AuditEntry:
    seq = 0 if prev is None else prev.seq + 1
    hash_prev = GENESIS if prev is None else prev.hash_self
    hash_self = compute_hash(hash_prev, seq, decision, actor, reason)
    return AuditEntry(
        seq=seq,
        decision=decision,
        reason=reason,
        actor=actor,
        hash_prev=hash_prev,
        hash_self=hash_self,
    )


def build_chain(decisions: list[tuple[str, str, str]]) -> list[AuditEntry]:
    """decisions: list of (decision, actor, reason) -> chained entries."""
    entries: list[AuditEntry] = []
    prev: AuditEntry | None = None
    for decision, actor, reason in decisions:
        entry = append(prev, decision=decision, actor=actor, reason=reason)
        entries.append(entry)
        prev = entry
    return entries


def verify_chain(entries: list[AuditEntry]) -> bool:
    prev_hash = GENESIS
    for i, e in enumerate(entries):
        if e.seq != i or e.hash_prev != prev_hash:
            return False
        if compute_hash(e.hash_prev, e.seq, e.decision, e.actor, e.reason) != e.hash_self:
            return False
        prev_hash = e.hash_self
    return True
