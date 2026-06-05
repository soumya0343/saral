"""Identity & authentication (TRD §11.5).

`user_id` is never trusted from the message body. The mock IdP mints a signed, short-TTL
session token; Saral validates it and reads `sub` / `tenant_id` / `auth_level` from the
token only. State-changing actions require step-up (OTP). Every resume re-validates.
"""

from saral.auth.identity import IdentityGate, SessionClaims, identity_gate
from saral.auth.stepup import request_step_up, verify_step_up
from saral.auth.tokens import (
    AuthError,
    decode_token,
    mint_session_token,
    raise_auth_level,
)

__all__ = [
    "AuthError",
    "IdentityGate",
    "SessionClaims",
    "decode_token",
    "identity_gate",
    "mint_session_token",
    "raise_auth_level",
    "request_step_up",
    "verify_step_up",
]
