"""Request authentication dependencies.

- `current_customer`: the bearer access token is the ONLY source of the customer's identity.
  Nothing in a request body or path can name a different customer.
- `owned_conversation`: loads a conversation and 404s unless it belongs to that customer
  (404, not 403, so conversation ids can't be probed).
- `require_service_key`: server-to-server calls (the RM's system, ops) — no UI involved.
"""

from __future__ import annotations

import hmac

from fastapi import Depends, Header, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from saral.auth import SessionClaims, decode_token
from saral.auth.tokens import AuthError
from saral.config import get_settings
from saral.db.models import Conversation
from saral.db.session import get_session


def current_customer(authorization: str | None = Header(default=None)) -> SessionClaims:
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(
            status_code=401,
            detail="missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        return decode_token(token)
    except AuthError as e:
        # Expired -> the client refreshes via /auth/refresh and retries.
        raise HTTPException(
            status_code=401, detail=str(e), headers={"WWW-Authenticate": "Bearer"}
        ) from e


async def owned_conversation(
    conversation_id: str,
    claims: SessionClaims = Depends(current_customer),
    db: AsyncSession = Depends(get_session),
) -> Conversation:
    convo = await db.get(Conversation, conversation_id)
    if convo is None or convo.user_id != claims.user_id or convo.tenant_id != claims.tenant_id:
        raise HTTPException(status_code=404, detail="conversation not found")
    return convo


def require_service_key(x_service_key: str | None = Header(default=None)) -> None:
    expected = get_settings().service_api_key
    if not expected:
        raise HTTPException(status_code=404, detail="not found")  # endpoint disabled
    if not x_service_key or not hmac.compare_digest(x_service_key, expected):
        raise HTTPException(status_code=401, detail="invalid service key")
