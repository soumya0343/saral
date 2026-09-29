"""Session issuance for the mock IdP: short access token + rotating refresh token.

The client holds both. The access token (JWT, `session_ttl_min`) is sent on every request; when
it expires the client exchanges the refresh token (opaque, `refresh_ttl_days`, stored hashed,
rotated on use) for a new pair. Conversation history is keyed by customer id, not by token, so
expiry never loses history — it only asks the client to prove identity again.
"""

from __future__ import annotations

from pydantic import BaseModel

from saral.auth.tokens import mint_session_token
from saral.config import get_settings
from saral.tools.store import get_store


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int  # seconds until the access token expires


def issue_tokens(user_id: str, tenant_id: str | None = None) -> TokenPair:
    s = get_settings()
    tenant = tenant_id or s.default_tenant
    return TokenPair(
        access_token=mint_session_token(user_id, tenant_id=tenant),
        refresh_token=get_store().issue_refresh(user_id, tenant),
        expires_in=s.session_ttl_min * 60,
    )


def refresh_tokens(refresh_token: str) -> TokenPair:
    """Rotate: consume the refresh token, return a new pair. Raises ToolError if invalid."""
    user_id, tenant_id, new_refresh = get_store().rotate_refresh(refresh_token)
    return TokenPair(
        access_token=mint_session_token(user_id, tenant_id=tenant_id),
        refresh_token=new_refresh,
        expires_in=get_settings().session_ttl_min * 60,
    )
