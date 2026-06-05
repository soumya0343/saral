"""Write-flow helpers: confirmation parsing + conversation-anchored idempotency keys."""

from saral.actions.confirm import (
    build_pending_write,
    idempotency_key,
    parse_confirmation,
)

__all__ = ["build_pending_write", "idempotency_key", "parse_confirmation"]
