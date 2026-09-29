"""The RM request queue — every escalation becomes one of these.

Saral never writes customer data: a confirmed change (new mobile, a claim, a ticket) and every
other escalation (failed OTP, repeated complaint, compliance block, …) is recorded here for the
customer's relationship manager, a human who acts outside Saral. What this table guarantees:

- **complete** — the requested change itself (field + new value, claim facts) is stored,
  encrypted at rest (`crypto.py`); only the service-key RM endpoints decrypt it.
- **deduplicated** — one OPEN request per (customer, kind, requested change): asking twice
  returns the existing request (unique partial index on `dedup_key WHERE status='open'`).
- **queryable** — the customer can ask "what happened to my request?" (`list_for_user`), and
  the RM's system sets `done` / `rejected` (+ a note for the customer) via `set_status`.
- **erasable** — erasure tombstones the change and the summary, keeping the audit shape.

Lives beside the core-system store (same database / engine, own table), so it works on
Postgres live and in-memory SQLite for tests and the offline eval.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import time
from dataclasses import dataclass
from functools import lru_cache

from sqlalchemy import Index, Integer, String, Text, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from saral.logging import get_logger
from saral.rm.crypto import decrypt, encrypt

log = get_logger(__name__)

STATUSES = ("open", "done", "rejected")
_TOMBSTONE = "[erased]"


class _Base(DeclarativeBase):
    pass


class RMRequest(_Base):
    __tablename__ = "rm_requests"
    id: Mapped[str] = mapped_column(String(16), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64))
    user_id: Mapped[str] = mapped_column(String(32), index=True)
    conversation_id: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(32))  # update_contact | file_claim | … | handoff
    reason: Mapped[str] = mapped_column(String(64))  # why it went to a human
    status: Mapped[str] = mapped_column(String(16), default="open")
    dedup_key: Mapped[str] = mapped_column(String(64))
    change_enc: Mapped[str | None] = mapped_column(Text, nullable=True)  # Fernet
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)  # English, for the RM
    language: Mapped[str] = mapped_column(String(16), default="en")
    sla_target: Mapped[str] = mapped_column(String(16), default="24h")
    created_epoch: Mapped[int] = mapped_column(Integer)
    updated_epoch: Mapped[int] = mapped_column(Integer)
    handled_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    rm_note: Mapped[str | None] = mapped_column(Text, nullable=True)  # shown to the customer

    __table_args__ = (
        Index(
            "uq_rm_requests_open_dedup",
            "dedup_key",
            unique=True,
            postgresql_where=text("status = 'open'"),
            sqlite_where=text("status = 'open'"),
        ),
    )


@dataclass
class RequestView:
    """What the customer (and the graph) may see: no change payload, no summary."""

    id: str
    kind: str
    reason: str
    status: str
    sla_target: str
    created_epoch: int
    updated_epoch: int
    rm_note: str | None = None


def dedup_key(user_id: str, kind: str, identity: dict) -> str:
    """Same customer + same kind + same requested change -> same key."""
    canon = json.dumps(identity, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(f"{user_id}|{kind}|{canon.lower()}".encode()).hexdigest()


def _view(r: RMRequest) -> RequestView:
    return RequestView(
        id=r.id,
        kind=r.kind,
        reason=r.reason,
        status=r.status,
        sla_target=r.sla_target,
        created_epoch=r.created_epoch,
        updated_epoch=r.updated_epoch,
        rm_note=r.rm_note,
    )


def _rm_dict(r: RMRequest) -> dict:
    return {
        **_view(r).__dict__,
        "tenant_id": r.tenant_id,
        "user_id": r.user_id,
        "conversation_id": r.conversation_id,
        "language": r.language,
        "requested_change": decrypt(r.change_enc),
        "summary": r.summary,
        "handled_by": r.handled_by,
    }


class RequestStore:
    def __init__(self, engine) -> None:
        self.engine = engine
        _Base.metadata.create_all(engine)

    def _open_by_key(self, s: Session, key: str) -> RMRequest | None:
        return s.scalar(
            select(RMRequest).where(RMRequest.dedup_key == key, RMRequest.status == "open")
        )

    def find_open(self, key: str) -> RequestView | None:
        with Session(self.engine) as s:
            r = self._open_by_key(s, key)
            return _view(r) if r else None

    def raise_request(
        self,
        *,
        tenant_id: str,
        user_id: str,
        conversation_id: str,
        kind: str,
        reason: str,
        key: str,
        change: dict | None,
        summary: str | None,
        language: str,
        sla_target: str,
    ) -> tuple[RequestView, bool]:
        """(request, created). An open request with the same key is returned, not duplicated."""
        now = int(time.time())
        for _ in range(3):
            with Session(self.engine) as s:
                existing = self._open_by_key(s, key)
                if existing is not None:
                    return _view(existing), False
                row = RMRequest(
                    id=f"REQ{secrets.randbelow(900_000) + 100_000}",
                    tenant_id=tenant_id,
                    user_id=user_id,
                    conversation_id=conversation_id,
                    kind=kind,
                    reason=reason,
                    status="open",
                    dedup_key=key,
                    change_enc=encrypt(change) if change else None,
                    summary=summary,
                    language=language,
                    sla_target=sla_target,
                    created_epoch=now,
                    updated_epoch=now,
                )
                s.add(row)
                try:
                    s.commit()
                except IntegrityError:
                    s.rollback()  # lost a race on the open key (or an id clash): re-read
                    continue
                log.info("rm.request_raised", request_id=row.id, kind=kind, reason=reason)
                return _view(row), True
        raise RuntimeError("could not raise RM request")

    def set_summary(self, request_id: str, summary: str) -> None:
        with Session(self.engine) as s:
            r = s.get(RMRequest, request_id)
            if r is not None and not r.summary:
                r.summary = summary
                s.commit()

    def list_for_user(self, user_id: str, limit: int = 5) -> list[RequestView]:
        with Session(self.engine) as s:
            rows = s.scalars(
                select(RMRequest)
                .where(RMRequest.user_id == user_id)
                .order_by(RMRequest.created_epoch.desc(), RMRequest.id)
                .limit(limit)
            ).all()
            return [_view(r) for r in rows]

    # --- the RM's system (service key only) ---

    def list_for_rm(self, status: str | None = "open", limit: int = 50) -> list[dict]:
        with Session(self.engine) as s:
            q = select(RMRequest).order_by(RMRequest.created_epoch).limit(limit)
            if status:
                q = q.where(RMRequest.status == status)
            return [_rm_dict(r) for r in s.scalars(q).all()]

    def get_for_rm(self, request_id: str) -> dict | None:
        with Session(self.engine) as s:
            r = s.get(RMRequest, request_id)
            return _rm_dict(r) if r else None

    def set_status(
        self, request_id: str, status: str, handled_by: str, note: str | None = None
    ) -> dict | None:
        if status not in STATUSES:
            raise ValueError(f"status must be one of {STATUSES}")
        with Session(self.engine) as s:
            r = s.get(RMRequest, request_id)
            if r is None:
                return None
            r.status = status
            r.handled_by = handled_by
            r.updated_epoch = int(time.time())
            if note is not None:
                from saral.compliance.pii import redact_pii

                r.rm_note = redact_pii(note)
            s.commit()
            return _rm_dict(r)

    def erase_user(self, user_id: str) -> int:
        with Session(self.engine) as s:
            rows = s.scalars(select(RMRequest).where(RMRequest.user_id == user_id)).all()
            for r in rows:
                r.change_enc = None
                r.summary = _TOMBSTONE
                r.rm_note = None
            s.commit()
            return len(rows)

    def reset(self) -> None:
        with Session(self.engine) as s:
            s.query(RMRequest).delete()
            s.commit()


@lru_cache
def get_requests() -> RequestStore:
    from saral.tools.store import get_store

    return RequestStore(get_store().engine)
