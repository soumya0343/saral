"""Durable store for the mock "core insurer system" — customers, policies, claims, tickets.

This is the system of record for account state: filed claims, raised tickets, and contact
updates must survive restarts and be visible across processes. It is backed by SQLAlchemy so
the SAME code runs on Postgres (live, durable) and on an in-memory SQLite (offline tests),
selected by `store_backend`. Functions are synchronous (the Action agent calls them under a
thread + timeout), so this uses a sync engine.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from functools import lru_cache
from pathlib import Path

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
    # Dedup window is tied to the pending-write TTL: entries past TTL are purged
    # together (a genuinely new intent gets a fresh nonce -> fresh key, so this never re-fires).
    created_epoch: Mapped[int] = mapped_column(Integer, default=0)


class MMeta(MockBase):
    __tablename__ = "mock_meta"
    name: Mapped[str] = mapped_column(String(32), primary_key=True)
    n: Mapped[int] = mapped_column(Integer)


class MChallenge(MockBase):
    """OTP challenge (step-up or login). The code itself is never stored: only an HMAC keyed by
    the server secret, so a leaked table can't be brute-forced offline. Single-use, TTL-bound,
    and burned after `otp_max_attempts` wrong guesses. (Supersedes the plaintext
    `mock_challenges` table, which is left unused.)"""

    __tablename__ = "mock_otp_challenges"
    challenge_id: Mapped[str] = mapped_column(String(48), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(32), index=True)
    purpose: Mapped[str] = mapped_column(String(16), default="step_up")
    code_hash: Mapped[str] = mapped_column(String(64))
    expires_epoch: Mapped[int] = mapped_column(Integer)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    used: Mapped[int] = mapped_column(Integer, default=0)


class MRefreshToken(MockBase):
    """Mock-IdP refresh token. Only a SHA-256 of the opaque token is stored. Rotated on every
    use; presenting an already-rotated token revokes its whole family (theft signal)."""

    __tablename__ = "mock_refresh_tokens"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(32), index=True)
    tenant_id: Mapped[str] = mapped_column(String(64))
    family_id: Mapped[str] = mapped_column(String(32), index=True)
    expires_epoch: Mapped[int] = mapped_column(Integer)
    revoked: Mapped[int] = mapped_column(Integer, default=0)


def _sha256(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _code_hash(challenge_id: str, code: str) -> str:
    key = get_settings().session_secret.encode("utf-8")
    return hmac.new(key, f"{challenge_id}|{code}".encode(), hashlib.sha256).hexdigest()


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
        self._ensure_columns()
        self.seed()

    def _ensure_columns(self) -> None:
        """Best-effort add of columns introduced after a table already exists. The mock store
        uses create_all (not alembic), so an existing Postgres mock DB won't pick up new columns
        automatically; this keeps demo DBs working without a manual reset."""
        from sqlalchemy import text

        with self.engine.begin() as conn:
            try:
                conn.execute(
                    text("ALTER TABLE mock_idempotency ADD COLUMN IF NOT EXISTS created_epoch INTEGER DEFAULT 0")  # noqa: E501
)
            except Exception as e:  # noqa: BLE001 — sqlite lacks IF NOT EXISTS; fresh tables already have it
                log.debug("store.ensure_columns_skipped", error=str(e))

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
            self._seed_from_manifest(s)
            s.commit()

    def _seed_from_manifest(self, s: Session) -> None:
        """Seed hero + thin customers from data/customers/manifest.yaml (additive, skip-if-exists).

        Keeps the structured store consistent with the per-customer documents the RAG index cites.
        """
        import yaml

        from saral.config import get_settings

        path = Path(get_settings().customers_dir) / "manifest.yaml"
        if not path.exists():
            return
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for cust in data.get("customers", []):
            cid = cust["customer_id"]
            if s.get(MUser, cid) is None:
                s.add(
                    MUser(
                        id=cid,
                        name=cust.get("name", cid),
                        mobile=cust.get("registered_mobile"),
                        email=cust.get("registered_email"),
)
)
            for p in cust.get("policies", []):
                if s.get(MPolicy, p["policy_id"]) is None:
                    s.add(
                        MPolicy(
                            policy_id=p["policy_id"],
                            holder_user_id=cid,
                            product=p.get("product", "health"),
                            status=p.get("status", "active"),
                            premium_inr=p.get("premium_inr", 0),
                            sum_assured_inr=p.get("sum_assured_inr", 0),
                            renewal_date=p.get("renewal_date", ""),
)
)
            for c in cust.get("claims", []):
                if s.get(MClaim, c["claim_id"]) is None:
                    s.add(
                        MClaim(
                            claim_id=c["claim_id"],
                            policy_id=c["policy_id"],
                            status=c.get("status", "under_review"),
                            amount_inr=c.get("amount_inr"),
                            last_updated=c.get("last_updated", ""),
                            note=c.get("note"),
)
)

    def reset(self) -> None:
        """Test helper: wipe + reseed."""
        with Session(self.engine) as s:
            for model in (
                MChallenge, MRefreshToken, MIdem, MTicket, MClaim, MPolicy, MUser, MMeta
            ):
                s.query(model).delete()
            s.commit()
        self.seed()

    # --- OTP challenges (step-up + login) ---
    def create_challenge(
        self, user_id: str, code: str, ttl_s: int, purpose: str = "step_up"
    ) -> str:
        challenge_id = f"chl_{secrets.token_urlsafe(18)}"  # unguessable, not user/time derived
        with Session(self.engine) as s:
            s.add(
                MChallenge(
                    challenge_id=challenge_id,
                    user_id=user_id,
                    purpose=purpose,
                    code_hash=_code_hash(challenge_id, code),
                    expires_epoch=int(time.time()) + ttl_s,
                )
            )
            s.commit()
        return challenge_id

    def verify_challenge(
        self, challenge_id: str, response: str, user_id: str, purpose: str = "step_up"
    ) -> bool:
        """Single-use, TTL-bounded, ownership- and purpose-bound, attempt-limited."""
        max_attempts = get_settings().otp_max_attempts
        with Session(self.engine) as s:
            ch = s.get(MChallenge, challenge_id)
            if ch is None or ch.used or ch.user_id != user_id or ch.purpose != purpose:
                return False
            if int(time.time()) > ch.expires_epoch or ch.attempts >= max_attempts:
                return False
            if not hmac.compare_digest(
                ch.code_hash, _code_hash(challenge_id, (response or "").strip())
            ):
                ch.attempts += 1
                if ch.attempts >= max_attempts:
                    ch.used = 1  # burned: too many wrong guesses
                s.commit()
                return False
            ch.used = 1  # single-use: burn the challenge on success
            s.commit()
            return True

    def challenge_attempts_left(self, challenge_id: str) -> int:
        with Session(self.engine) as s:
            ch = s.get(MChallenge, challenge_id)
            if ch is None or ch.used or int(time.time()) > ch.expires_epoch:
                return 0
            return max(get_settings().otp_max_attempts - ch.attempts, 0)

    def challenge_owner(self, challenge_id: str, purpose: str) -> str | None:
        """user_id a live challenge was issued to (login verify knows only the challenge)."""
        with Session(self.engine) as s:
            ch = s.get(MChallenge, challenge_id)
            if ch is None or ch.purpose != purpose:
                return None
            return ch.user_id

    # --- refresh tokens (mock IdP) ---
    def issue_refresh(self, user_id: str, tenant_id: str, family_id: str | None = None) -> str:
        raw = secrets.token_urlsafe(32)
        ttl_s = get_settings().refresh_ttl_days * 86400
        with Session(self.engine) as s:
            s.add(
                MRefreshToken(
                    token_hash=_sha256(raw),
                    user_id=user_id,
                    tenant_id=tenant_id,
                    family_id=family_id or secrets.token_hex(16),
                    expires_epoch=int(time.time()) + ttl_s,
                )
            )
            s.commit()
        return raw

    def rotate_refresh(self, raw: str) -> tuple[str, str, str]:
        """Consume a refresh token -> (user_id, tenant_id, new_raw). Raises ToolError if invalid.
        Reuse of an already-rotated token revokes the whole family."""
        with Session(self.engine) as s:
            row = s.get(MRefreshToken, _sha256(raw or ""))
            if row is None:
                raise ToolError("invalid refresh token")
            if row.revoked:
                self._revoke_where(s, MRefreshToken.family_id == row.family_id)
                s.commit()
                log.warning("auth.refresh_reuse_detected", family=row.family_id)
                raise ToolError("refresh token reused; session revoked")
            if int(time.time()) > row.expires_epoch:
                raise ToolError("refresh token expired")
            row.revoked = 1
            user_id, tenant_id, family = row.user_id, row.tenant_id, row.family_id
            s.commit()
        return user_id, tenant_id, self.issue_refresh(user_id, tenant_id, family_id=family)

    def revoke_refresh(self, raw: str) -> None:
        with Session(self.engine) as s:
            row = s.get(MRefreshToken, _sha256(raw or ""))
            if row is not None:
                self._revoke_where(s, MRefreshToken.family_id == row.family_id)
                s.commit()

    def revoke_user_sessions(self, user_id: str) -> None:
        with Session(self.engine) as s:
            self._revoke_where(s, MRefreshToken.user_id == user_id)
            s.commit()

    @staticmethod
    def _revoke_where(s: Session, cond) -> None:
        for row in s.scalars(select(MRefreshToken).where(cond)).all():
            row.revoked = 1

    def _next_id(self, s: Session) -> int:
        meta = s.get(MMeta, "id_seq")
        if meta is None:  # seeded at startup; absent only if the table was wiped externally
            meta = MMeta(name="id_seq", n=5000)
            s.add(meta)
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
            s.add(
                MIdem(
                    key=idempotency_key,
                    payload=result.model_dump_json(),
                    created_epoch=int(time.time()),
)
)
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
            s.add(
                MIdem(
                    key=idempotency_key,
                    payload=ticket.model_dump_json(),
                    created_epoch=int(time.time()),
)
)
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
            s.add(
                MIdem(
                    key=idempotency_key,
                    payload=claim.model_dump_json(),
                    created_epoch=int(time.time()),
)
)
            s.commit()
            return claim

    # --- identity ---
    def find_customer(self, mobile: str | None, email: str | None) -> dict | None:
        """Existing customer by registered mobile/email, or None. Does NOT authenticate."""
        with Session(self.engine) as s:
            q = select(MUser)
            existing = None
            if mobile:
                existing = s.scalar(q.where(MUser.mobile == mobile))
            if existing is None and email:
                existing = s.scalar(q.where(func.lower(MUser.email) == email.lower()))
            if existing is None:
                return None
            return {"user_id": existing.id, "name": existing.name,
                    "mobile": existing.mobile, "email": existing.email}

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

    def get_customer(self, user_id: str) -> dict | None:
        with Session(self.engine) as s:
            u = s.get(MUser, user_id)
            return None if u is None else {"user_id": u.id, "name": u.name}

    def get_customer_context(self, user_id: str) -> dict:
        with Session(self.engine) as s:
            return self._context(s, user_id)

    def user_exists(self, user_id: str) -> bool:
        with Session(self.engine) as s:
            return s.get(MUser, user_id) is not None

    def purge_idempotency(self, older_than_s: int, now: float | None = None) -> int:
        """Purge dedup entries past the TTL (dedup window tied to pending-write TTL)."""
        cutoff = int((now or time.time()) - older_than_s)
        with Session(self.engine) as s:
            n = (
                s.query(MIdem)
                .filter(MIdem.created_epoch > 0, MIdem.created_epoch < cutoff)
                .delete()
)
            s.commit()
            return n

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
