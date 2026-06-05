"""Durable store for the mock "core insurer system" — customers, policies, claims, tickets.

This is the system of record for account state: filed claims, raised tickets, and contact
updates must survive restarts and be visible across processes. It is backed by SQLAlchemy so
the SAME code runs on Postgres (live, durable) and on an in-memory SQLite (offline tests),
selected by `store_backend`. Functions are synchronous (the Action agent calls them under a
thread + timeout), so this uses a sync engine.
"""

from __future__ import annotations

from functools import lru_cache

from sqlalchemy import Integer, String, Text, create_engine, func, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column
from sqlalchemy.pool import StaticPool

from saral.config import get_settings
from saral.logging import get_logger
from saral.tools.schemas import ActionResult, ClaimStatus, Policy, Ticket

log = get_logger(__name__)


class ToolError(RuntimeError):
    """Recoverable tool error (not found, invalid arg) — caller may re-ask."""


class MockBase(DeclarativeBase):
    pass


class MUser(MockBase):
    __tablename__ = "mock_users"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    mobile: Mapped[str | None] = mapped_column(String(32), nullable=True)
    email: Mapped[str | None] = mapped_column(String(128), nullable=True)


class MPolicy(MockBase):
    __tablename__ = "mock_policies"
    policy_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    holder_user_id: Mapped[str] = mapped_column(String(32), index=True)
    product: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32))
    premium_inr: Mapped[float] = mapped_column()
    sum_assured_inr: Mapped[float] = mapped_column()
    renewal_date: Mapped[str] = mapped_column(String(32))


class MClaim(MockBase):
    __tablename__ = "mock_claims"
    claim_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    policy_id: Mapped[str] = mapped_column(String(32), index=True)
    status: Mapped[str] = mapped_column(String(32))
    amount_inr: Mapped[float | None] = mapped_column(nullable=True)
    last_updated: Mapped[str] = mapped_column(String(32))
    note: Mapped[str | None] = mapped_column(Text, nullable=True)


class MTicket(MockBase):
    __tablename__ = "mock_tickets"
    ticket_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(32), index=True)
    subject: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="open")


class MIdem(MockBase):
    __tablename__ = "mock_idempotency"
    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    payload: Mapped[str] = mapped_column(Text)


class MMeta(MockBase):
    __tablename__ = "mock_meta"
    name: Mapped[str] = mapped_column(String(32), primary_key=True)
    n: Mapped[int] = mapped_column(Integer)


# --- Seed data (pre-existing customers for cross-user / authorization demos) ---

_SEED_USERS = [
    ("U1001", "Asha Verma", "9876500001", "asha@example.com"),
    ("U1002", "Rahul Singh", "9876500002", "rahul@example.com"),
    ("U1003", "Meena Kumari", "9876500003", "meena@example.com"),
]
_SEED_POLICIES = [
    ("POL1001", "U1001", "health", "active", 14500, 500000, "2026-11-01"),
    ("POL1002", "U1002", "motor", "active", 8200, 300000, "2026-08-15"),
    ("POL1003", "U1003", "term_life", "lapsed", 22000, 5000000, "2026-02-01"),
]
_SEED_CLAIMS = [
    (
        "CLM2001",
        "POL1001",
        "under_review",
        45000,
        "2026-05-28",
        "Awaiting hospital discharge summary.",
    ),
    (
        "CLM2002",
        "POL1002",
        "approved",
        18000,
        "2026-05-30",
        "Approved; disbursal in 3-5 working days.",
    ),
]
_ALLOWED_CONTACT_FIELDS = {"mobile", "email"}


class Store:
    def __init__(self, url: str, *, is_memory: bool) -> None:
        kwargs: dict = {"future": True}
        if is_memory:
            kwargs.update(connect_args={"check_same_thread": False}, poolclass=StaticPool)
        self.engine = create_engine(url, **kwargs)
        MockBase.metadata.create_all(self.engine)
        self.seed()

    def seed(self) -> None:
        with Session(self.engine) as s:
            if s.get(MMeta, "id_seq") is None:
                s.add(MMeta(name="id_seq", n=5000))
            for uid, name, mob, mail in _SEED_USERS:
                if s.get(MUser, uid) is None:
                    s.add(MUser(id=uid, name=name, mobile=mob, email=mail))
            for pid, uid, prod, st, prem, sa, rd in _SEED_POLICIES:
                if s.get(MPolicy, pid) is None:
                    s.add(
                        MPolicy(
                            policy_id=pid,
                            holder_user_id=uid,
                            product=prod,
                            status=st,
                            premium_inr=prem,
                            sum_assured_inr=sa,
                            renewal_date=rd,
                        )
                    )
            for cid, pid, st, amt, lu, note in _SEED_CLAIMS:
                if s.get(MClaim, cid) is None:
                    s.add(
                        MClaim(
                            claim_id=cid,
                            policy_id=pid,
                            status=st,
                            amount_inr=amt,
                            last_updated=lu,
                            note=note,
                        )
                    )
            s.commit()

    def reset(self) -> None:
        """Test helper: wipe + reseed."""
        with Session(self.engine) as s:
            for model in (MIdem, MTicket, MClaim, MPolicy, MUser, MMeta):
                s.query(model).delete()
            s.commit()
        self.seed()

    def _next_id(self, s: Session) -> int:
        meta = s.get(MMeta, "id_seq")
        meta.n += 1
        return meta.n

    # --- conversions ---
    @staticmethod
    def _policy(p: MPolicy) -> Policy:
        return Policy(
            policy_id=p.policy_id,
            holder_user_id=p.holder_user_id,
            product=p.product,
            status=p.status,
            premium_inr=p.premium_inr,
            sum_assured_inr=p.sum_assured_inr,
            renewal_date=p.renewal_date,
        )

    @staticmethod
    def _claim(c: MClaim) -> ClaimStatus:
        return ClaimStatus(
            claim_id=c.claim_id,
            policy_id=c.policy_id,
            status=c.status,
            amount_inr=c.amount_inr,
            last_updated=c.last_updated,
            note=c.note,
        )

    # --- read-only ---
    def get_claim_status(self, claim_id: str) -> ClaimStatus:
        with Session(self.engine) as s:
            c = s.get(MClaim, claim_id.upper())
            if c is None:
                raise ToolError(f"claim {claim_id} not found")
            return self._claim(c)

    def get_policy_details(self, policy_id: str) -> Policy:
        with Session(self.engine) as s:
            p = s.get(MPolicy, policy_id.upper())
            if p is None:
                raise ToolError(f"policy {policy_id} not found")
            return self._policy(p)

    # --- state-changing (idempotent) ---
    def update_contact(
        self, user_id: str, field: str, value: str, idempotency_key: str
    ) -> ActionResult:
        with Session(self.engine) as s:
            if (prior := s.get(MIdem, idempotency_key)) is not None:
                return ActionResult.model_validate_json(prior.payload)
            u = s.get(MUser, user_id)
            if u is None:
                raise ToolError(f"user {user_id} not found")
            if field not in _ALLOWED_CONTACT_FIELDS:
                raise ToolError(
                    f"field '{field}' not updatable; allowed: {_ALLOWED_CONTACT_FIELDS}"
                )
            old = getattr(u, field)
            setattr(u, field, value)
            result = ActionResult(
                ok=True,
                idempotency_key=idempotency_key,
                detail=f"{field} updated",
                changed={"field": field, "old": old, "new": value},
            )
            s.add(MIdem(key=idempotency_key, payload=result.model_dump_json()))
            s.commit()
            return result

    def raise_ticket(self, user_id: str, subject: str, body: str, idempotency_key: str) -> Ticket:
        with Session(self.engine) as s:
            if (prior := s.get(MIdem, idempotency_key)) is not None:
                return Ticket.model_validate_json(prior.payload)
            if s.get(MUser, user_id) is None:
                raise ToolError(f"user {user_id} not found")
            ticket = Ticket(
                ticket_id=f"TKT{self._next_id(s)}",
                user_id=user_id,
                subject=subject,
                idempotency_key=idempotency_key,
            )
            s.add(MTicket(ticket_id=ticket.ticket_id, user_id=user_id, subject=subject))
            s.add(MIdem(key=idempotency_key, payload=ticket.model_dump_json()))
            s.commit()
            return ticket

    def file_claim(self, user_id: str, subject: str, idempotency_key: str) -> ClaimStatus:
        with Session(self.engine) as s:
            if (prior := s.get(MIdem, idempotency_key)) is not None:
                return ClaimStatus.model_validate_json(prior.payload)
            policy = s.scalar(select(MPolicy).where(MPolicy.holder_user_id == user_id))
            if policy is None:
                raise ToolError(f"user {user_id} has no policy to claim against")
            claim_id = f"CLM{self._next_id(s)}"
            c = MClaim(
                claim_id=claim_id,
                policy_id=policy.policy_id,
                status="filed",
                amount_inr=None,
                last_updated="2026-06-05",
                note=f"Filed: {subject[:80]}. Awaiting assessment.",
            )
            s.add(c)
            claim = self._claim(c)
            s.add(MIdem(key=idempotency_key, payload=claim.model_dump_json()))
            s.commit()
            return claim

    # --- identity ---
    def identify_customer(self, name: str, mobile: str | None, email: str | None) -> dict:
        if not name or not (mobile or email):
            raise ToolError("name and a mobile number or email are required")
        with Session(self.engine) as s:
            q = select(MUser)
            existing = None
            if mobile:
                existing = s.scalar(q.where(MUser.mobile == mobile))
            if existing is None and email:
                existing = s.scalar(q.where(func.lower(MUser.email) == email.lower()))
            if existing is not None:
                ctx = self._context(s, existing.id)
                return {"user_id": existing.id, "name": existing.name, "returning": True, **ctx}
            n = self._next_id(s)
            user_id, policy_id = f"U{n}", f"POL{n}"
            s.add(MUser(id=user_id, name=name, mobile=mobile, email=email))
            s.add(
                MPolicy(
                    policy_id=policy_id,
                    holder_user_id=user_id,
                    product="health",
                    status="active",
                    premium_inr=12000,
                    sum_assured_inr=400000,
                    renewal_date="2027-01-01",
                )
            )
            s.commit()
            return {
                "user_id": user_id,
                "name": name,
                "returning": False,
                "policy_id": policy_id,
                "claim_id": None,
            }

    def get_customer_context(self, user_id: str) -> dict:
        with Session(self.engine) as s:
            return self._context(s, user_id)

    @staticmethod
    def _context(s: Session, user_id: str) -> dict:
        ctx: dict = {}
        pol = s.scalar(select(MPolicy).where(MPolicy.holder_user_id == user_id))
        if pol:
            ctx["policy_id"] = pol.policy_id
            claim = s.scalar(select(MClaim).where(MClaim.policy_id == pol.policy_id))
            if claim:
                ctx["claim_id"] = claim.claim_id
        return ctx


@lru_cache
def get_store() -> Store:
    settings = get_settings()
    if settings.store_backend == "postgres":
        url = settings.database_url.replace("+asyncpg", "+psycopg")
        log.info("store.postgres")
        return Store(url, is_memory=False)
    log.info("store.memory")
    return Store("sqlite://", is_memory=True)
