"""Session tokens — the mock IdP (TRD §11.5).

The mock auth service IS the IdP for this build: it mints a signed, short-TTL JWT carrying
`sub` (user_id), `tenant_id`, `auth_level`, and `exp`. Saral validates the signature + expiry
and reads identity from the token ONLY — never from message content. Step-up raises the
`auth_level` claim by re-minting the token after a verified challenge.

HS256 with a shared secret is sufficient for a mock IdP; a real deployment would use the host
app's asymmetric keys. The secret comes from config (insecure dev default, set in prod).
"""

from __future__ import annotations

import datetime as _dt
from typing import TYPE_CHECKING

import jwt

from saral.config import get_settings
from saral.schemas import AuthLevel

if TYPE_CHECKING:
    from saral.auth.identity import SessionClaims

_ALGO = "HS256"
_ISS = "saral-mock-idp"


class AuthError(Exception):
    """Token invalid, expired, or insufficient. Maps to HTTP 401."""


def _now() -> _dt.datetime:
    # Stamped at call time; injected separately in tests if determinism is needed.
    return _dt.datetime.now(tz=_dt.UTC)


def mint_session_token(
    user_id: str,
    *,
    tenant_id: str | None = None,
    auth_level: AuthLevel = AuthLevel.SESSION,
    ttl_min: int | None = None,
) -> str:
    """Mint a signed session token. Identity flows ONLY through this token thereafter."""
    s = get_settings()
    iat = _now()
    exp = iat + _dt.timedelta(minutes=ttl_min if ttl_min is not None else s.session_ttl_min)
    payload = {
        "iss": _ISS,
        "sub": user_id,
        "tenant_id": tenant_id or s.default_tenant,
        "auth_level": str(auth_level),
        "iat": iat,
        "exp": exp,
    }
    return jwt.encode(payload, s.session_secret, algorithm=_ALGO)


def raise_auth_level(token: str, new_level: AuthLevel) -> str:
    """Re-mint the SAME identity at a higher auth level after a verified step-up.

    Never raises the level on its own — the caller must have validated a challenge first
    (TRD §11.5 invariant: never raise level without a valid challenge_response).
    """
    claims = decode_token(token)
    return mint_session_token(
        claims.user_id, tenant_id=claims.tenant_id, auth_level=new_level
    )


def decode_token(token: str) -> SessionClaims:
    """Validate signature + expiry and return the claims. Raises AuthError on any failure."""
    from saral.auth.identity import SessionClaims

    s = get_settings()
    try:
        data = jwt.decode(
            token,
            s.session_secret,
            algorithms=[_ALGO],
            issuer=_ISS,
            options={"require": ["exp", "sub", "tenant_id"]},
        )
    except jwt.ExpiredSignatureError as e:
        raise AuthError("session token expired") from e
    except jwt.InvalidTokenError as e:
        raise AuthError(f"invalid session token: {e}") from e
    try:
        level = AuthLevel(data.get("auth_level", "session"))
    except ValueError:
        level = AuthLevel.SESSION
    return SessionClaims(
        user_id=str(data["sub"]),
        tenant_id=str(data["tenant_id"]),
        auth_level=level,
    )
