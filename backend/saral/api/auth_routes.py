"""Mock IdP + step-up endpoints (TRD §17, §11.5).

`/auth/session` mints a short-TTL session token (the host app's job; simulated here). The
token is the ONLY source of identity downstream. `/auth/step-up` runs the OTP challenge: a
request mints a code (returned as `test_otp` in non-prod — no SMS), a verify raises the token
to `step_up`.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from saral.auth import (
    decode_token,
    mint_session_token,
    raise_auth_level,
    request_step_up,
    verify_step_up,
)
from saral.auth.tokens import AuthError
from saral.schemas import AuthLevel
from saral.tools import mock_backend as mb

router = APIRouter(prefix="/auth", tags=["auth"])


class SessionRequest(BaseModel):
    user_id: str


class SessionToken(BaseModel):
    token: str
    user_id: str
    tenant_id: str
    auth_level: str


@router.post("/session", response_model=SessionToken)
async def mint_session(body: SessionRequest) -> SessionToken:
    """Mint a session token for a known customer (the mock IdP)."""
    if not mb.user_exists(body.user_id):
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
    token: str  # the current session token (identity is token-derived, never from the body)
    # On a verify call, supply both:
    challenge_id: str | None = None
    response: str | None = None


class StepUpResult(BaseModel):
    mode: str  # "requested" | "verified" | "failed"
    challenge_id: str | None = None
    test_otp: str | None = None  # non-prod only
    token: str | None = None  # a re-minted step_up token on success
    auth_level: str | None = None


@router.post("/step-up", response_model=StepUpResult)
async def step_up(body: StepUpRequest) -> StepUpResult:
    try:
        claims = decode_token(body.token)
    except AuthError as e:
        raise HTTPException(status_code=401, detail=str(e)) from e

    # Verify mode: a challenge_id + response are present.
    if body.challenge_id and body.response is not None:
        if verify_step_up(body.challenge_id, body.response, claims.user_id):
            new_token = raise_auth_level(body.token, AuthLevel.STEP_UP)
            return StepUpResult(
                mode="verified", token=new_token, auth_level=str(AuthLevel.STEP_UP)
            )
        return StepUpResult(mode="failed", auth_level=str(claims.auth_level))

    # Request mode: mint a fresh challenge.
    chal = request_step_up(claims.user_id)
    return StepUpResult(
        mode="requested", challenge_id=chal["challenge_id"], test_otp=chal.get("test_otp")
    )
