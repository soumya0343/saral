"""Mock IdP endpoints: login OTP, token refresh/logout, and the step-up harness.

These simulate the host bank/insurer app's auth (ADR-0001) — labelled as a harness, not a
production IdP. The client holds a short access token + a rotating refresh token; identity
downstream comes ONLY from the access token.

- `POST /auth/login/verify`: an existing customer proves the account with the login OTP.
- `POST /auth/refresh`: rotate the refresh token for a new pair (silent re-auth; history kept).
- `POST /auth/logout`: revoke the refresh-token family.
- `POST /auth/session`: dev/test-only shortcut that mints a token for a user id.
- `POST /auth/step-up`: standalone step-up harness (the chat flow uses `/reply` instead).
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from saral.api.deps import current_customer
from saral.api.ratelimit import per_customer, per_ip
from saral.api.routes import CustomerSession
from saral.auth import (
    SessionClaims,
    decode_token,
    mint_session_token,
    request_step_up,
    verify_step_up,
)
from saral.auth.sessions import TokenPair, issue_tokens, refresh_tokens
from saral.auth.stepup import attempts_left
from saral.config import get_settings
from saral.schemas import AuthLevel
from saral.tools import mock_backend as mb
from saral.tools.store import get_store

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginVerify(BaseModel):
    challenge_id: str
    code: str


@router.post(
    "/login/verify",
    response_model=CustomerSession,
    dependencies=[per_ip("login_verify", limit=10)],
)
async def login_verify(body: LoginVerify) -> CustomerSession:
    """Existing customer: exchange the login OTP for a session. The challenge alone decides
    whose account this is — the request can't name a user."""
    user_id = await asyncio.to_thread(get_store().challenge_owner, body.challenge_id, "login")
    if user_id is None or not await asyncio.to_thread(
        verify_step_up, body.challenge_id, body.code, user_id, "login"
    ):
        left = await asyncio.to_thread(attempts_left, body.challenge_id) if user_id else 0
        raise HTTPException(
            status_code=401,
            detail={"error": "invalid or expired code", "attempts_left": left},
        )
    profile = await asyncio.to_thread(mb.get_customer, user_id) or {"name": user_id}
    ctx = await asyncio.to_thread(mb.get_customer_context, user_id)
    return CustomerSession(
        user_id=user_id,
        name=profile["name"],
        returning=True,
        policy_id=ctx.get("policy_id"),
        claim_id=ctx.get("claim_id"),
        tokens=await asyncio.to_thread(issue_tokens, user_id),
    )


class RefreshRequest(BaseModel):
    refresh_token: str


@router.post("/refresh", response_model=TokenPair, dependencies=[per_ip("refresh", limit=30)])
async def refresh(body: RefreshRequest) -> TokenPair:
    try:
        return await asyncio.to_thread(refresh_tokens, body.refresh_token)
    except mb.ToolError as e:
        raise HTTPException(status_code=401, detail=str(e)) from e


@router.post("/logout", status_code=204)
async def logout(body: RefreshRequest) -> None:
    await asyncio.to_thread(get_store().revoke_refresh, body.refresh_token)


class SessionRequest(BaseModel):
    user_id: str


class SessionToken(BaseModel):
    token: str
    user_id: str
    tenant_id: str
    auth_level: str


@router.post("/session", response_model=SessionToken)
async def mint_session(body: SessionRequest) -> SessionToken:
    """DEV/TEST ONLY: mint a token for a known customer without proof (smoke tests, scripts).
    Disabled when APP_ENV=prod — there, sessions come only from sign-up or the login OTP."""
    if get_settings().app_env == "prod":
        raise HTTPException(status_code=404, detail="not found")
    if not await asyncio.to_thread(mb.user_exists, body.user_id):
        raise HTTPException(status_code=404, detail=f"customer {body.user_id} not found")
    token = mint_session_token(body.user_id)
    claims = decode_token(token)
    return SessionToken(
        token=token,
        user_id=claims.user_id,
        tenant_id=claims.tenant_id,
        auth_level=str(claims.auth_level),
    )


class StepUpRequest(BaseModel):
    # On a verify call, supply both:
    challenge_id: str | None = None
    response: str | None = None


class StepUpResult(BaseModel):
    mode: str  # "requested" | "verified" | "failed"
    challenge_id: str | None = None
    test_otp: str | None = None  # DEMO_MODE only
    token: str | None = None  # a short-lived step_up token on success
    auth_level: str | None = None
    attempts_left: int | None = None


@router.post(
    "/step-up",
    response_model=StepUpResult,
    dependencies=[per_customer("step_up", limit=10)],
)
async def step_up(
    body: StepUpRequest, claims: SessionClaims = Depends(current_customer)
) -> StepUpResult:
    """Standalone step-up harness. The chat flow does NOT honour client step-up tokens — it
    grants step-up server-side, bound to one pending write (see /conversations/{id}/reply)."""
    if body.challenge_id and body.response is not None:
        if await asyncio.to_thread(
            verify_step_up, body.challenge_id, body.response, claims.user_id
        ):
            ttl_min = max(get_settings().step_up_grant_ttl_s // 60, 1)
            token = mint_session_token(
                claims.user_id,
                tenant_id=claims.tenant_id,
                auth_level=AuthLevel.STEP_UP,
                ttl_min=ttl_min,
            )
            return StepUpResult(mode="verified", token=token, auth_level=str(AuthLevel.STEP_UP))
        return StepUpResult(
            mode="failed",
            auth_level=str(claims.auth_level),
            attempts_left=await asyncio.to_thread(attempts_left, body.challenge_id),
        )

    chal = await asyncio.to_thread(request_step_up, claims.user_id)
    return StepUpResult(
        mode="requested", challenge_id=chal["challenge_id"], test_otp=chal.get("test_otp")
    )
