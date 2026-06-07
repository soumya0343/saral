"""Conversation + message + resume + SSE stream routes.

Identity is token-derived: the conversation holds a session token (mock-IdP
custody for the demo) that is re-validated on every message and every resume. A write
suspends the run; `/reply` resumes it — re-validating identity and, if the session expired
during suspension, re-earning step-up before the write fires.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette.sse import EventSourceResponse

from saral.auth import decode_token, mint_session_token, verify_step_up
from saral.auth.tokens import AuthError
from saral.compliance.pii import redact_pii
from saral.config import get_settings
from saral.db.models import Conversation, Message
from saral.db.session import get_session
from saral.runbus import enqueue_run, subscribe_trace
from saral.schemas import AuthLevel, HistoryTurn, PendingWrite, RunRequest
from saral.tools import mock_backend as mb

router = APIRouter()


class IdentifyCustomer(BaseModel):
    name: str
    mobile: str | None = None
    email: str | None = None


class CustomerOut(BaseModel):
    user_id: str
    name: str
    returning: bool
    policy_id: str | None = None
    claim_id: str | None = None


class StartConversation(BaseModel):
    user_id: str


@router.post("/customers", response_model=CustomerOut, tags=["customers"])
async def identify_customer(body: IdentifyCustomer) -> CustomerOut:
    try:
        result = mb.identify_customer(body.name, body.mobile, body.email)
    except mb.ToolError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return CustomerOut(**result)


class ConversationOut(BaseModel):
    id: str
    user_id: str
    status: str
    language: str | None = None
    token: str | None = None  # session token minted for this conversation (mock IdP)


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
    "/users/{user_id}/conversations",
    response_model=list[ConversationSummary],
    tags=["conversations"],
)
async def list_conversations(
    user_id: str, db: AsyncSession = Depends(get_session)
) -> list[ConversationSummary]:
    """Past conversations for a customer (most-recent first) — powers the sidebar history."""
    convos = (
        await db.scalars(
            select(Conversation)
            .where(Conversation.user_id == user_id)
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
    body: StartConversation, db: AsyncSession = Depends(get_session)
) -> ConversationOut:
    # Mint a session token for this customer (the host app's job; simulated here).
    token = mint_session_token(body.user_id)
    claims = decode_token(token)
    convo = Conversation(
        user_id=claims.user_id, tenant_id=claims.tenant_id, session_token=token
    )
    db.add(convo)
    await db.flush()
    return ConversationOut(
        id=convo.id, user_id=convo.user_id, status=convo.status, token=token
    )


def _claims_or_refresh(convo: Conversation) -> tuple[str, str, AuthLevel, str]:
    """Re-validate the conversation token. On expiry, re-mint a SESSION token —
    a write that suspended at step_up will then re-earn step-up on resume.
    Returns (user_id, tenant_id, auth_level, token)."""
    token = convo.session_token
    if token:
        try:
            c = decode_token(token)
            return c.user_id, c.tenant_id, c.auth_level, token
        except AuthError:
            pass
    fresh = mint_session_token(convo.user_id, tenant_id=convo.tenant_id)
    c = decode_token(fresh)
    convo.session_token = fresh
    return c.user_id, c.tenant_id, c.auth_level, fresh


async def _append_message(
    db: AsyncSession, convo: Conversation, role: str, content: str
) -> Message:
    next_seq = (
        await db.scalar(
            select(func.coalesce(func.max(Message.sequence_num), 0) + 1).where(
                Message.conversation_id == convo.id
            )
        )
    ) or 1
    msg = Message(
        tenant_id=convo.tenant_id,
        conversation_id=convo.id,
        role=role,
        content=redact_pii(content), #: redact PII before store
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


@router.post(
    "/conversations/{conversation_id}/messages",
    response_model=MessageAccepted,
    tags=["conversations"],
)
async def send_message(
    conversation_id: str, body: SendMessage, db: AsyncSession = Depends(get_session)
) -> MessageAccepted:
    convo = await db.get(Conversation, conversation_id)
    if convo is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    # Consent gate (DPDP): processing proceeds only while consent is granted.
    # A withdrawn conversation is closed to further processing.
    if convo.consent_status == "withdrawn":
        raise HTTPException(status_code=403, detail="consent withdrawn; processing halted")

    user_id, tenant_id, auth_level, _ = _claims_or_refresh(convo)
    history = await _history(db, conversation_id)
    msg = await _append_message(db, convo, "user", body.content)

    run_id = uuid.uuid4().hex
    await enqueue_run(
        RunRequest(
            run_id=run_id,
            conversation_id=conversation_id,
            user_id=user_id,
            tenant_id=tenant_id,
            auth_level=auth_level,
            message=body.content,
            history=history,
            known_entities=mb.get_customer_context(user_id),
            prev_language=convo.language,
        )
    )
    return MessageAccepted(conversation_id=conversation_id, message_id=msg.id, run_id=run_id)


@router.post(
    "/conversations/{conversation_id}/reply",
    response_model=MessageAccepted,
    tags=["conversations"],
)
async def reply(
    conversation_id: str, body: SendMessage, db: AsyncSession = Depends(get_session)
) -> MessageAccepted:
    """Resume a suspended run (clarification / step-up OTP / write confirmation).

    Re-validates identity first. For an OTP reply, verifies the challenge and
    re-mints the token at step_up; for a confirmation, carries the persisted pending write
    and the yes/no answer into a fresh run.
    """
    convo = await db.get(Conversation, conversation_id)
    if convo is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    if not convo.suspend_status:
        raise HTTPException(status_code=409, detail="conversation is not awaiting a reply")

    user_id, tenant_id, auth_level, token = _claims_or_refresh(convo)
    pending = PendingWrite(**convo.pending_write) if convo.pending_write else None
    original = convo.original_message or body.content
    await _append_message(db, convo, "user", body.content)

    req = RunRequest(
        run_id=uuid.uuid4().hex,
        conversation_id=conversation_id,
        user_id=user_id,
        tenant_id=tenant_id,
        auth_level=auth_level,
        message=original,  # re-send the original so the write re-evaluates (not the reply)
        history=await _history(db, conversation_id),
        known_entities=mb.get_customer_context(user_id),
        pending_write=pending,
        intent_nonce=pending.intent_nonce if pending else 0,
        prev_language=convo.language,
    )

    if convo.suspend_status == "awaiting_input" and convo.challenge_id:
        # Step-up OTP reply. Verify, then re-mint at step_up (or escalate on failure).
        if verify_step_up(convo.challenge_id, body.content, user_id):
            fresh = mint_session_token(user_id, tenant_id=tenant_id, auth_level=AuthLevel.STEP_UP)
            convo.session_token = fresh
            req.auth_level = AuthLevel.STEP_UP
        else:
            req.challenge_response = body.content  # wrong OTP -> graph escalates
    elif convo.suspend_status == "awaiting_input":
        # Clarification (missing arg): fold the answer into the original so triage re-extracts.
        req.message = f"{original} {body.content}".strip()
    elif convo.suspend_status == "awaiting_confirmation":
        req.resume_reply = body.content  # yes/no; identity re-validates before any write

    # Clear suspend custody now; the worker will re-set it if the run suspends again.
    convo.suspend_status = None
    await enqueue_run(req)
    return MessageAccepted(
        conversation_id=conversation_id, message_id="", run_id=req.run_id
    )


@router.get("/conversations/{conversation_id}/stream", tags=["conversations"])
async def stream(conversation_id: str):
    """SSE stream of agent trace + final response for the latest run."""

    async def event_gen():
        async for raw in subscribe_trace(conversation_id):
            yield {"data": raw}

    return EventSourceResponse(event_gen())


@router.get(
    "/conversations/{conversation_id}",
    response_model=list[MessageOut],
    tags=["conversations"],
)
async def get_history(
    conversation_id: str, db: AsyncSession = Depends(get_session)
) -> list[MessageOut]:
    convo = await db.get(Conversation, conversation_id)
    if convo is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    rows = (
        await db.scalars(
            select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.sequence_num)
        )
    ).all()
    return [
        MessageOut(id=m.id, role=m.role, content=m.content, sequence_num=m.sequence_num)
        for m in rows
    ]


@router.delete("/users/{user_id}/data", tags=["compliance"])
async def erase_user(user_id: str) -> dict:
    """Right-to-erasure (DPDP): tombstone the customer's PII, withdraw consent. The hash-chained
    audit log is left intact (it holds no raw PII), so the 7-year hold stays erasure-compatible."""
    from saral.compliance.erasure import erase_user_data

    counts = await erase_user_data(user_id)
    return {"user_id": user_id, "erased": counts, "consent_status": "withdrawn"}


class ResolveEscalation(BaseModel):
    operator_id: str
    reply: str | None = None


@router.post("/escalations/{escalation_id}/resolve", tags=["escalations"])
async def resolve_escalation_route(escalation_id: str, body: ResolveEscalation) -> dict:
    """Record which operator handled an escalation, and when (: operator is audit-only —
    this writes audit metadata; an operator never drives a turn or fires a tool)."""
    from saral.db.repository import resolve_escalation

    ok = await resolve_escalation(escalation_id, body.operator_id, body.reply)
    if not ok:
        raise HTTPException(status_code=404, detail="escalation not found")
    return {"escalation_id": escalation_id, "handled_by": body.operator_id, "status": "resolved"}
