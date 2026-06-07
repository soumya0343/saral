"""Account-Action agent.

Resolves each ACTION intent into a concrete mock-tool call: validates/derives arguments
from extracted entities, runs the (sync) tool under a timeout, and records an ActionRecord.
State-changing calls carry a deterministic idempotency key. Missing required arguments
produce a `needs_clarification` record instead of a guessed call (the re-ask path).

NOTE: In Phase 3 the Compliance gate runs *before* any state-changing call here.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from saral.config import get_settings
from saral.logging import get_logger
from saral.schemas import ActionRecord, Intent, IntentType, PendingWrite
from saral.tools import mock_backend as mb
from saral.tools.mock_backend import ToolError

log = get_logger(__name__)

_STATE_CHANGING = {"update_contact", "raise_ticket", "file_claim"}


class ActionAgent:
    name = "action"

    async def run(
        self,
        intents: list[Intent],
        entities: dict,
        user_id: str,
        run_id: str,
        message: str,
        pending_write: PendingWrite | None = None,
) -> list[ActionRecord]:
        records: list[ActionRecord] = []
        for intent in intents:
            if intent.type != IntentType.ACTION or not intent.action:
                continue
            # A gated write only executes via its confirmed, conversation-anchored PendingWrite
            # — never directly off a fresh intent.
            if intent.action in _STATE_CHANGING:
                if pending_write is not None and pending_write.tool == intent.action:
                    records.append(await self._execute_pending(pending_write, user_id, message))
                continue
            records.append(await self._dispatch(intent.action, entities))
        return records

    async def _execute_pending(
        self, pw: PendingWrite, user_id: str, message: str
) -> ActionRecord:
        """Execute a confirmed write under its idempotent key (exactly-once on resume)."""
        idem: str = pw.idempotency_key
        call: Callable[[], Any]
        if pw.tool == "update_contact":
            field, value = pw.field or "", pw.value or ""
            args = {"user_id": user_id, "field": field, "value": value}
            call = lambda: mb.update_contact(user_id, field, value, idempotency_key=idem)  # noqa: E731
        elif pw.tool == "raise_ticket":
            subject = pw.value or message[:60]
            args = {"user_id": user_id, "subject": subject}
            call = lambda: mb.raise_ticket(user_id, subject, message, idempotency_key=idem)  # noqa: E731
        elif pw.tool == "file_claim":
            subject = pw.value or message[:80]
            args = {"user_id": user_id, "subject": subject}
            call = lambda: mb.file_claim(user_id, subject, idempotency_key=idem)  # noqa: E731
        else:
            return ActionRecord(tool=pw.tool, error=f"unknown write '{pw.tool}'")
        return await self._invoke(pw.tool, args, idem, call)

    async def _dispatch(self, action: str, entities: dict) -> ActionRecord:
        # Reads only: state-changing actions never reach here — they execute via
        # _execute_pending() under the conversation-anchored idempotency key (run() guard
        # above). Reads need no idempotency key.
        idem = None

        # Resolve arguments / required-arg checks.
        if action == "get_claim_status":
            claim_id = entities.get("claim_id")
            if not claim_id:
                return ActionRecord(
                    tool=action,
                    needs_clarification=(
                        "You don't have any claims on file yet. Would you like to file one?"
),
)
            args = {"claim_id": claim_id}
            call = lambda: mb.get_claim_status(claim_id)  # noqa: E731

        elif action == "get_policy_details":
            policy_id = entities.get("policy_id")
            if not policy_id:
                return ActionRecord(
                    tool=action, needs_clarification="Which policy ID should I look up?"
)
            args = {"policy_id": policy_id}
            call = lambda: mb.get_policy_details(policy_id)  # noqa: E731

        else:
            return ActionRecord(tool=action, error=f"unknown action '{action}'")

        return await self._invoke(action, args, idem, call)

    async def _invoke(self, action, args, idem, call) -> ActionRecord:
        timeout = get_settings().tool_timeout_s
        try:
            result = await asyncio.wait_for(asyncio.to_thread(call), timeout=timeout)
            return ActionRecord(
                tool=action,
                args=args,
                idempotency_key=idem,
                ok=True,
                result=result.model_dump(),
)
        except ToolError as e:
            log.info("action.tool_error", tool=action, error=str(e))
            return ActionRecord(tool=action, args=args, idempotency_key=idem, error=str(e))
        except TimeoutError:
            log.warning("action.timeout", tool=action)
            return ActionRecord(
                tool=action, args=args, idempotency_key=idem, error="tool timed out"
)
