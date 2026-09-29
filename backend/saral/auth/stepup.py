"""Step-up authentication (`request_step_up`).

Synthetic mechanism, stated openly: `request_step_up` mints a 6-digit code and, in DEMO_MODE,
returns it in a `test_otp` field (there is no SMS channel). The customer echoes it back as
`challenge_response`; `verify_step_up` checks it single-use, TTL-bounded, and ownership-bound.
A verifier, not a stub — it cannot raise auth level without a valid challenge.
"""

from __future__ import annotations

import secrets

from saral.config import get_settings
from saral.logging import get_logger
from saral.tools.store import get_store

log = get_logger(__name__)


def request_step_up(user_id: str, purpose: str = "step_up") -> dict:
    """Mint a challenge. Returns {challenge_id, test_otp?}; test_otp only in DEMO_MODE."""
    s = get_settings()
    code = f"{secrets.randbelow(1_000_000):06d}"
    challenge_id = get_store().create_challenge(user_id, code, s.step_up_ttl_s, purpose=purpose)
    log.info("stepup.requested", user_id=user_id, challenge_id=challenge_id, purpose=purpose)
    out: dict = {"challenge_id": challenge_id}
    # No real SMS channel: in DEMO_MODE the code is delivered as a simulated SMS popup.
    if s.demo_mode:
        out["test_otp"] = code
    return out


def verify_step_up(
    challenge_id: str, response: str, user_id: str, purpose: str = "step_up"
) -> bool:
    ok = get_store().verify_challenge(challenge_id, response, user_id, purpose=purpose)
    log.info("stepup.verified", user_id=user_id, challenge_id=challenge_id, ok=ok)
    return ok


def attempts_left(challenge_id: str) -> int:
    return get_store().challenge_attempts_left(challenge_id)
