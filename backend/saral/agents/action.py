"""Account-Action agent.

Resolves each ACTION intent into a concrete mock-tool call: validates/derives arguments
from extracted entities, runs the (sync) tool under a timeout, and records an ActionRecord.
State-changing calls carry a deterministic idempotency key. Missing required arguments
produce a `needs_clarification` record instead of a guessed call (the re-ask path).

NOTE: In Phase 3 the Compliance gate runs *before* any state-changing call here.
"""

from __future__ import annotations

import asyncio

from saral.config import get_settings
from saral.logging import get_logger
from saral.schemas import ActionRecord, Intent, IntentType
from saral.tools import mock_backend as mb
from saral.tools.mock_backend import ToolError

log = get_logger(__name__)

_STATE_CHANGING = {"update_contact", "raise_ticket", "file_claim"}


class ActionAgent:
    name = "action"

    async def run(
        self, intents: list[Intent], entities: dict, user_id: str, run_id: str, message: str
    ) -> list[ActionRecord]:
        records: list[ActionRecord] = []
        for intent in intents:
            if intent.type != IntentType.ACTION or not intent.action:
                continue
            records.append(
                await self._dispatch(intent.action, entities, user_id, run_id, message)
            )
        return records

    async def _dispatch(
        self, action: str, entities: dict, user_id: str, run_id: str, message: str
    ) -> ActionRecord:
        idem = f"{run_id}:{action}" if action in _STATE_CHANGING else None

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

        elif action == "update_contact":
            field, value = self._resolve_contact_change(entities)
            if not field:
                return ActionRecord(
                    tool=action,
                    needs_clarification=(
                        "What should I update — mobile or email — and to what value?"
                    ),
                )
            args = {"user_id": user_id, "field": field, "value": value}
            call = lambda: mb.update_contact(user_id, field, value, idempotency_key=idem)  # noqa: E731

        elif action == "raise_ticket":
            subject = (message[:60] + "…") if len(message) > 60 else message
            args = {"user_id": user_id, "subject": subject}
            call = lambda: mb.raise_ticket(user_id, subject, message, idempotency_key=idem)  # noqa: E731

        elif action == "file_claim":
            subject = (message[:80] + "…") if len(message) > 80 else message
            args = {"user_id": user_id, "subject": subject}
            call = lambda: mb.file_claim(user_id, subject, idempotency_key=idem)  # noqa: E731

        else:
            return ActionRecord(tool=action, error=f"unknown action '{action}'")

        return await self._invoke(action, args, idem, call)

    @staticmethod
    def _resolve_contact_change(entities: dict) -> tuple[str | None, str | None]:
        if entities.get("mobile"):
            return "mobile", entities["mobile"]
        if entities.get("email"):
            return "email", entities["email"]
        return None, None

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
