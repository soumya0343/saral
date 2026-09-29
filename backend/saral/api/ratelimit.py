"""Fixed-window rate limits on the API edge (Redis INCR + EXPIRE).

Keys are per client IP for unauthenticated endpoints (sign-up/login, OTP verify, refresh) and
per customer for authenticated ones (messages, replies, step-up). Brute-forcing a 6-digit OTP
or burning free-tier LLM quota both need volume; this caps it. Fails OPEN if Redis is down —
availability over strictness for a demo; the OTP attempt cap still holds on its own.
"""

from __future__ import annotations

from fastapi import Depends, HTTPException, Request

from saral.api.deps import current_customer
from saral.auth import SessionClaims
from saral.config import get_settings
from saral.logging import get_logger
from saral.runbus import get_redis

log = get_logger(__name__)


def client_ip(request: Request) -> str:
    header = get_settings().client_ip_header
    if header and (value := request.headers.get(header)):
        return value.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


async def hit(bucket: str, subject: str, limit: int, window_s: int) -> None:
    """Count one request; raise 429 once `limit` is exceeded within the window."""
    if not get_settings().rate_limit_enabled:
        return
    key = f"rl:{bucket}:{subject}"
    try:
        r = get_redis()
        count = await r.incr(key)
        if count == 1:
            await r.expire(key, window_s)
    except Exception as e:  # noqa: BLE001 — fail open, but loudly
        log.warning("ratelimit.unavailable", bucket=bucket, error=str(e))
        return
    if count > limit:
        raise HTTPException(
            status_code=429,
            detail="too many requests; slow down",
            headers={"Retry-After": str(window_s)},
        )


def per_ip(bucket: str, limit: int, window_s: int = 60):
    async def dep(request: Request) -> None:
        await hit(bucket, client_ip(request), limit, window_s)

    return Depends(dep)


def per_customer(bucket: str, limit: int, window_s: int = 60):
    async def dep(claims: SessionClaims = Depends(current_customer)) -> None:
        await hit(bucket, claims.user_id, limit, window_s)

    return Depends(dep)
