"""Conversation + message + SSE stream routes (TRD §17, Phase 1 subset)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette.sse import EventSourceResponse

from saral.db.models import Conversation, Message
from saral.db.session import get_session
from saral.runbus import enqueue_run, subscribe_trace
from saral.schemas import RunRequest

router = APIRouter()


class StartConversation(BaseModel):
    user_id: str


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


@router.post("/conversations", response_model=ConversationOut, tags=["conversations"])
async def start_conversation(
    body: StartConversation, db: AsyncSession = Depends(get_session)
) -> ConversationOut:
    convo = Conversation(user_id=body.user_id)
    db.add(convo)
    await db.flush()
    return ConversationOut(id=convo.id, user_id=convo.user_id, status=convo.status)


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

    next_seq = (
        await db.scalar(
            select(func.coalesce(func.max(Message.sequence_num), 0) + 1).where(
                Message.conversation_id == conversation_id
            )
        )
    ) or 1
    msg = Message(
        conversation_id=conversation_id,
        role="user",
        content=body.content,  # Phase 3: redact PII before store
        sequence_num=next_seq,
    )
    db.add(msg)
    await db.flush()

    run_id = uuid.uuid4().hex
    await enqueue_run(
        RunRequest(
            run_id=run_id,
            conversation_id=conversation_id,
            user_id=convo.user_id,
            message=body.content,
        )
    )
    return MessageAccepted(
        conversation_id=conversation_id, message_id=msg.id, run_id=run_id
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
