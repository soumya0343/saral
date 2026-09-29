"""Customer, conversation, message, resume and SSE stream routes.

Identity is token-derived: every customer route reads the customer from the bearer access
token (`api.deps.current_customer`) and only ever touches conversations that customer owns.
Nothing in a path or body can name another customer. A write suspends the run; `/reply`
resumes it. Step-up is a short server-side grant bound to the pending write's idempotency key,
so a verified OTP authorizes that one write only, and only briefly.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette.sse import EventSourceResponse

from saral.actions.turns import SuspendState, plan_reply
from saral.api.deps import current_customer, owned_conversation, require_service_key
from saral.api.ratelimit import per_customer, per_ip
from saral.auth import SessionClaims, request_step_up
from saral.auth.sessions import TokenPair, issue_tokens
from saral.compliance.pii import redact_pii
from saral.config import get_settings
from saral.db.models import Conversation, Message
from saral.db.repository import allocate_seq
from saral.db.session import get_session
from saral.runbus import enqueue_run, subscribe_trace
from saral.schemas import AuthLevel, HistoryTurn, PendingWrite, RunRequest
from saral.tools import mock_backend as mb

router = APIRouter()


# --- customers (sign-up / login) -------------------------------------------------------------


class IdentifyCustomer(BaseModel):
    name: str
    mobile: str | None = None
    email: str | None = None


class CustomerSession(BaseModel):
    """A signed-in customer: profile + the client-held token pair."""

    user_id: str
    name: str
    returning: bool
    policy_id: str | None = None  # set only when the customer has exactly one
    claim_id: str | None = None  # set only when the customer has exactly one
    policy_ids: list[str] = []
    claim_ids: list[str] = []
    tokens: TokenPair


class IdentifyOut(BaseModel):
    status: Literal["signed_in", "otp_required"]
    # signed_in: a NEW sandbox customer was created and is signed in straight away.
    session: CustomerSession | None = None
    # otp_required: the contact belongs to an EXISTING account — prove it with a login OTP
    # (POST /auth/login/verify). Knowing a phone number alone never opens an account.
    challenge_id: str | None = None
    sent_to: str | None = None  # masked contact the code was "sent" to
    test_otp: str | None = None  # DEMO_MODE only (simulated SMS)


def _mask(mobile: str | None, email: str | None) -> str:
    if mobile:
        return "*" * max(len(mobile) - 4, 0) + mobile[-4:]
    if email and "@" in email:
        local, _, domain = email.partition("@")
        return f"{local[:1]}***@{domain}"
    return "your registered contact"


@router.post(
    "/customers",
    response_model=IdentifyOut,
    tags=["customers"],
    dependencies=[per_ip("identify", limit=10)],
)
async def identify_customer(body: IdentifyCustomer) -> IdentifyOut:
    if not body.name.strip() or not (body.mobile or body.email):
        raise HTTPException(
            status_code=400, detail="name and a mobile number or email are required"
        )
    existing = await asyncio.to_thread(mb.find_customer, body.mobile, body.email)
    if existing is not None:
        chal = await asyncio.to_thread(request_step_up, existing["user_id"], "login")
        return IdentifyOut(
            status="otp_required",
            challenge_id=chal["challenge_id"],
            sent_to=_mask(existing.get("mobile"), existing.get("email")),
            test_otp=chal.get("test_otp"),
        )
    try:
        created = await asyncio.to_thread(mb.identify_customer, body.name, body.mobile, body.email)
    except mb.ToolError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return IdentifyOut(
        status="signed_in",
        session=CustomerSession(
            user_id=created["user_id"],
            name=created["name"],
            returning=False,
            policy_id=created.get("policy_id"),
            claim_id=created.get("claim_id"),
            policy_ids=[created["policy_id"]] if created.get("policy_id") else [],
            tokens=await asyncio.to_thread(issue_tokens, created["user_id"]),
        ),
    )


class CustomerProfile(BaseModel):
    user_id: str
    name: str
    policy_id: str | None = None  # set only when the customer has exactly one
    claim_id: str | None = None  # set only when the customer has exactly one
    policy_ids: list[str] = []
    claim_ids: list[str] = []


@router.get("/me", response_model=CustomerProfile, tags=["customers"])
async def me(claims: SessionClaims = Depends(current_customer)) -> CustomerProfile:
    profile = await asyncio.to_thread(mb.get_customer, claims.user_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="customer not found")
    ctx = await asyncio.to_thread(mb.get_customer_context, claims.user_id)
    return CustomerProfile(
        user_id=claims.user_id,
        name=profile["name"],
        policy_id=ctx.get("policy_id"),
        claim_id=ctx.get("claim_id"),
        policy_ids=ctx.get("policy_ids", []),
        claim_ids=ctx.get("claim_ids", []),
    )


# --- conversations ---------------------------------------------------------------------------


class ConversationOut(BaseModel):
    id: str
    user_id: str
    status: str
    language: str | None = None


class SendMessage(BaseModel):
    content: str


class MessageAccepted(BaseModel):
    conversation_id: str
    message_id: str
    run_id: str


class MessageOut(BaseModel):
    id: str
    role: str
    content: str
    sequence_num: int


class ConversationSummary(BaseModel):
    id: str
    status: str
    suspend_status: str | None = None
    preview: str  # first user message, for the sidebar list
    updated_at: str


@router.get(
    "/me/conversations",
    response_model=list[ConversationSummary],
    tags=["conversations"],
)
async def list_conversations(
    claims: SessionClaims = Depends(current_customer),
    db: AsyncSession = Depends(get_session),
) -> list[ConversationSummary]:
    """The signed-in customer's past conversations (most-recent first) — the sidebar history.
    History is keyed by customer, not by token: a new session sees every past conversation."""
    convos = (
        await db.scalars(
            select(Conversation)
            .where(
                Conversation.user_id == claims.user_id,
                Conversation.tenant_id == claims.tenant_id,
            )
            .order_by(Conversation.updated_at.desc())
            .limit(50)
        )
    ).all()
    out: list[ConversationSummary] = []
    for c in convos:
        first = await db.scalar(
            select(Message.content)
            .where(Message.conversation_id == c.id, Message.role == "user")
            .order_by(Message.sequence_num)
            .limit(1)
        )
        out.append(
            ConversationSummary(
                id=c.id,
                status=c.status,
                suspend_status=c.suspend_status,
                preview=(first or "New conversation")[:60],
                updated_at=c.updated_at.isoformat() if c.updated_at else "",
            )
        )
    return out


@router.post("/conversations", response_model=ConversationOut, tags=["conversations"])
async def start_conversation(
    claims: SessionClaims = Depends(current_customer),
    db: AsyncSession = Depends(get_session),
) -> ConversationOut:
    convo = Conversation(user_id=claims.user_id, tenant_id=claims.tenant_id)
    db.add(convo)
    await db.flush()
    return ConversationOut(id=convo.id, user_id=convo.user_id, status=convo.status)


async def _append_message(
    db: AsyncSession, convo: Conversation, role: str, content: str
) -> Message:
    next_seq = await allocate_seq(db, convo.id)
    msg = Message(
        tenant_id=convo.tenant_id,
        conversation_id=convo.id,
        role=role,
        content=redact_pii(content),  # redact PII before store
        sequence_num=next_seq,
    )
    db.add(msg)
    await db.flush()
    return msg


async def _history(db: AsyncSession, conversation_id: str) -> list[HistoryTurn]:
    n = get_settings().conversation_memory_turns
    recent = (
        await db.scalars(
            select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.sequence_num.desc())
            .limit(n)
        )
    ).all()
    return [HistoryTurn(role=m.role, content=m.content) for m in reversed(recent)]


def _step_up_granted(convo: Conversation, pending: PendingWrite | None) -> bool:
    """A verified OTP authorizes exactly the pending write it was earned for, briefly."""
    return (
        pending is not None
        and convo.step_up_for == pending.idempotency_key
        and convo.step_up_until is not None
        and convo.step_up_until > datetime.now(UTC)
    )


@router.post(
    "/conversations/{conversation_id}/messages",
    response_model=MessageAccepted,
    tags=["conversations"],
    dependencies=[per_customer("turns", limit=30)],
)
async def send_message(
    body: SendMessage,
    convo: Conversation = Depends(owned_conversation),
    db: AsyncSession = Depends(get_session),
) -> MessageAccepted:
    # Consent gate (DPDP): processing proceeds only while consent is granted.
    if convo.consent_status == "withdrawn":
        raise HTTPException(status_code=403, detail="consent withdrawn; processing halted")

    history = await _history(db, convo.id)
    msg = await _append_message(db, convo, "user", body.content)
    req = RunRequest(
        run_id=uuid.uuid4().hex,
        conversation_id=convo.id,
        user_id=convo.user_id,
        tenant_id=convo.tenant_id,
        auth_level=AuthLevel.SESSION,  # a fresh message never carries step-up forward
        message=body.content,
        history=history,
        known_entities=await asyncio.to_thread(mb.get_customer_context, convo.user_id),
        prev_language=convo.language,
    )
    # Commit BEFORE enqueueing so the worker never races ahead of this turn's rows.
    await db.commit()
    await enqueue_run(req)
    return MessageAccepted(conversation_id=convo.id, message_id=msg.id, run_id=req.run_id)


@router.post(
    "/conversations/{conversation_id}/reply",
    response_model=MessageAccepted,
    tags=["conversations"],
    dependencies=[per_customer("turns", limit=30)],
)
async def reply(
    body: SendMessage,
    convo: Conversation = Depends(owned_conversation),
    db: AsyncSession = Depends(get_session),
) -> MessageAccepted:
    """Resume a suspended run (clarification / step-up OTP / write confirmation).

    Identity comes from the bearer token (ownership already checked). An OTP reply is verified
    deterministically and, on success, grants step-up for THIS pending write only; a
    confirmation carries the persisted pending write and the yes/no answer into a fresh run.
    """
    if not convo.suspend_status:
        raise HTTPException(status_code=409, detail="conversation is not awaiting a reply")

    pending = PendingWrite(**convo.pending_write) if convo.pending_write else None
    history = await _history(db, convo.id)
    await _append_message(db, convo, "user", body.content)

    plan = await plan_reply(
        SuspendState(
            suspend_status=convo.suspend_status,
            pending_write=pending,
            challenge_id=convo.challenge_id,
            original_message=convo.original_message,
            step_up_granted=_step_up_granted(convo, pending),
        ),
        body.content,
        convo.user_id,
        history,
    )
    if plan.grant_step_up and pending is not None:
        convo.step_up_for = pending.idempotency_key
        convo.step_up_until = datetime.now(UTC) + timedelta(
            seconds=get_settings().step_up_grant_ttl_s
        )
    req = RunRequest(
        run_id=uuid.uuid4().hex,
        conversation_id=convo.id,
        user_id=convo.user_id,
        tenant_id=convo.tenant_id,
        auth_level=plan.auth_level,
        message=plan.message,
        history=history,
        known_entities=await asyncio.to_thread(mb.get_customer_context, convo.user_id),
        pending_write=plan.pending_write,
        intent_nonce=plan.intent_nonce,
        prev_language=convo.language,
        reply_text=body.content,  # the customer's actual words this turn (language detection)
        resume_reply=plan.resume_reply,
        challenge_response=plan.challenge_response,
        challenge_id=plan.challenge_id,
        otp_attempts_left=plan.otp_attempts_left,
    )

    # Clear suspend custody now; the worker re-sets it if the run suspends again. Commit BEFORE
    # enqueueing so this write can't land after (and overwrite) the worker's new suspend state.
    convo.suspend_status = None
    await db.commit()
    await enqueue_run(req)
    return MessageAccepted(conversation_id=convo.id, message_id="", run_id=req.run_id)


@router.get("/conversations/{conversation_id}/stream", tags=["conversations"])
async def stream(
    run_id: str | None = None,
    last_event_id: str | None = Header(default=None),
    convo: Conversation = Depends(owned_conversation),
):
    """SSE stream of agent trace + final response (owner only).

    Pass `run_id` (returned by /messages or /reply) to replay that run from its first event —
    subscribing after posting loses nothing. `Last-Event-ID` resumes a dropped connection.
    """
    conversation_id = convo.id

    async def event_gen():
        async for event_id, raw in subscribe_trace(conversation_id, run_id, last_event_id):
            yield {"id": event_id, "data": raw}

    return EventSourceResponse(event_gen())


@router.get(
    "/conversations/{conversation_id}",
    response_model=list[MessageOut],
    tags=["conversations"],
)
async def get_history(
    convo: Conversation = Depends(owned_conversation),
    db: AsyncSession = Depends(get_session),
) -> list[MessageOut]:
    rows = (
        await db.scalars(
            select(Message)
            .where(Message.conversation_id == convo.id)
            .order_by(Message.sequence_num)
        )
    ).all()
    return [
        MessageOut(id=m.id, role=m.role, content=m.content, sequence_num=m.sequence_num)
        for m in rows
    ]


# --- compliance ------------------------------------------------------------------------------


@router.delete("/me/data", tags=["compliance"])
async def erase_my_data(claims: SessionClaims = Depends(current_customer)) -> dict:
    """Right-to-erasure (DPDP), self-service: the signed-in customer erases THEIR OWN data.
    Tombstones PII, withdraws consent, and signs out every session. The hash-chained audit log
    is left intact (it holds no raw PII), so the 7-year hold stays erasure-compatible."""
    from saral.compliance.erasure import erase_user_data

    counts = await erase_user_data(claims.user_id)
    await asyncio.to_thread(mb.revoke_user_sessions, claims.user_id)
    return {"erased": counts, "consent_status": "withdrawn"}


# --- server-to-server (the RM's system) --------------------------------------------------------


class ResolveEscalation(BaseModel):
    operator_id: str  # which RM handled it, asserted by the authenticated RM system
    reply: str | None = None


@router.post(
    "/escalations/{escalation_id}/resolve",
    tags=["escalations"],
    dependencies=[Depends(require_service_key)],
)
async def resolve_escalation_route(escalation_id: str, body: ResolveEscalation) -> dict:
    """Called by the RM's own system (X-Service-Key), never by a browser: records which RM
    handled the request and when. The RM performs the actual change outside Saral."""
    from saral.db.repository import resolve_escalation

    ok = await resolve_escalation(escalation_id, body.operator_id, body.reply)
    if not ok:
        raise HTTPException(status_code=404, detail="escalation not found")
    return {"escalation_id": escalation_id, "handled_by": body.operator_id, "status": "resolved"}
