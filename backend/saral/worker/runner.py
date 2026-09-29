"""Execute a single run through the agent graph and publish trace events.

Kept separate from the consume loop so it can be unit-tested without Redis Streams.
"""

from __future__ import annotations

import asyncio
import time

from saral import metrics
from saral.compliance.pii import redact_pii
from saral.graph.build import build_checkpointed_graph
from saral.graph.state import RunState
from saral.logging import get_logger
from saral.runbus import publish_trace
from saral.schemas import Language, RunRequest, TraceEvent

log = get_logger(__name__)


async def execute_run(req: RunRequest) -> RunState:
    graph = build_checkpointed_graph()
    config = {"configurable": {"thread_id": req.run_id}}
    init = RunState(
        run_id=req.run_id,
        conversation_id=req.conversation_id,
        tenant_id=req.tenant_id,
        user_id=req.user_id,
        auth_level=req.auth_level,
        raw_message=req.message,
        history=req.history,
        known_entities=req.known_entities,
        pending_write=req.pending_write,
        intent_nonce=req.intent_nonce,
        resume_reply=req.resume_reply,
        challenge_response=req.challenge_response,
        challenge_id=req.challenge_id,
        otp_attempts_left=req.otp_attempts_left,
        reply_text=req.reply_text,
        language_hint=Language(req.prev_language) if req.prev_language else None,
    )
    # Resume from a checkpoint if this run was interrupted mid-flight (worker crash).
    snapshot = await graph.aget_state(config)
    graph_input = None if getattr(snapshot, "next", None) else init
    if graph_input is None:
        log.info("run.resuming", run_id=req.run_id)

    def ev(type_, agent=None, data=None) -> TraceEvent:
        return TraceEvent(
            type=type_,
            run_id=req.run_id,
            conversation_id=req.conversation_id,
            agent=agent,
            data=data or {},
        )

    await publish_trace(ev("run_started", data={"message": redact_pii(req.message)}))

    final_state = init
    t_start = time.monotonic()
    prev_ts = t_start
    try:
        async for chunk in graph.astream(graph_input, config, stream_mode="updates"):
            for node_name, update in chunk.items():
                if update is None:
                    continue
                now = time.monotonic()
                elapsed_ms = int((now - prev_ts) * 1000)  # this node's wall-clock
                prev_ts = now
                metrics.NODE_SECONDS.labels(node_name).observe(elapsed_ms / 1000)
                await publish_trace(ev("agent_started", agent=node_name))
                final_state = final_state.model_copy(update=update)

                if "intents" in update:
                    await publish_trace(
                        ev(
                            "intent",
                            agent=node_name,
                            data={
                                "language": update.get("language"),
                                "intents": [i.model_dump() for i in update.get("intents", [])],
                                "entities": _redacted(update.get("entities", {})),
                            },
                        )
                    )
                if "route" in update:
                    await publish_trace(
                        ev("route", agent=node_name, data={"route": update["route"]})
                    )
                if "retrieved" in update:
                    await publish_trace(
                        ev(
                            "retrieval",
                            agent=node_name,
                            data={
                                "citations": [p.citation for p in update["retrieved"]],
                                "passages": [p.model_dump() for p in update["retrieved"]],
                            },
                        )
                    )
                if "compliance_decisions" in update:
                    await publish_trace(
                        ev(
                            "compliance",
                            agent=node_name,
                            data={
                                "decisions": [
                                    d.model_dump() for d in update["compliance_decisions"]
                                ]
                            },
                        )
                    )
                if "actions" in update:
                    await publish_trace(
                        ev(
                            "action",
                            agent=node_name,
                            data={"actions": [a.model_dump() for a in update["actions"]]},
                        )
                    )
                if update.get("final_response") is not None:
                    await publish_trace(
                        ev(
                            "synthesis",
                            agent=node_name,
                            data=update["final_response"].model_dump(),
                        )
                    )
                await publish_trace(
                    ev(
                        "agent_finished",
                        agent=node_name,
                        data={
                            "elapsed_ms": elapsed_ms,
                            "tokens": int(update.get("tokens_used") or 0),
                        },
                    )
                )

        # Authoritative final state from the checkpoint (correct even after a resume).
        snap = await graph.aget_state(config)
        if snap and snap.values:
            final_state = RunState.model_validate(snap.values)

        response = final_state.final_response
        await publish_trace(
            ev(
                "final",
                data={
                    "status": final_state.status,
                    "language": final_state.language,
                    "route": final_state.route,
                    "response": response.model_dump() if response else None,
                    "citations": [p.citation for p in final_state.retrieved],
                    "elapsed_ms": int((time.monotonic() - t_start) * 1000),
                    "tokens": int(final_state.tokens_used or 0),
                },
            )
        )
        # Simulated OTP "SMS" arrives AFTER the assistant's "I've sent a code" reply — taken from
        # the FINAL state, so a code dropped later in the run (e.g. escalation) is never shown.
        pending_otp = final_state.challenge_otp
        if pending_otp:
            await publish_trace(
                ev("otp", agent="identity", data={"code": pending_otp, "channel": "sms"})
            )
        metrics.RUNS.labels(str(final_state.status)).inc()
        await _persist_or_warn(final_state)
        await _drop_checkpoint(graph, req.run_id)
    except TimeoutError as e:
        log.error("run.timeout", run_id=req.run_id, error=str(e))
        await publish_trace(ev("error", data={"error": str(e)}))
        final_state.status = "timed_out"  # terminal close, recorded with closed_at
        await _persist_or_warn(final_state)
        await _drop_checkpoint(graph, req.run_id)
    except Exception as e:  # noqa: BLE001 — never hard-crash a run
        log.error("run.error", run_id=req.run_id, error=str(e))
        await publish_trace(ev("error", data={"error": str(e)}))
        final_state.status = "crashed"  # terminal close: the entry is acked, never resumed
        await _persist_or_warn(final_state)
        await _drop_checkpoint(graph, req.run_id)
    finally:
        await publish_trace(ev("run_finished", data={"status": final_state.status}))

    return final_state


async def _drop_checkpoint(graph, run_id: str) -> None:
    """A run that reached an outcome is acked and never resumed, so its checkpoint (the full
    run state, raw message included) is deleted. Only a worker that DIES mid-run leaves one
    behind — exactly the case crash-resume needs."""
    try:
        await graph.checkpointer.adelete_thread(run_id)
    except Exception as e:  # noqa: BLE001
        log.warning("run.checkpoint_drop_failed", run_id=run_id, error=str(e))


def _redacted(entities: dict) -> dict:
    """Trace events are a developer view: mask contact values (mobile/email) in them."""
    return {k: redact_pii(v) if isinstance(v, str) else v for k, v in entities.items()}


_PERSIST_ATTEMPTS = 3
_PERSIST_BACKOFF_S = (0.2, 0.5, 1.0)


async def _persist_or_warn(state: RunState) -> None:
    if not await _persist(state):
        # The reply was shown, but the conversation state (e.g. a pending confirmation) was not
        # saved: tell the client instead of failing silently on its next reply.
        await publish_trace(
            TraceEvent(
                type="warning",
                run_id=state.run_id,
                conversation_id=state.conversation_id,
                data={"warning": "persist_failed"},
            )
        )


async def _persist(state: RunState) -> bool:
    """Persist the run + audit chain + suspend custody, retrying transient failures.

    persist_run commits once at the end, so a failed attempt rolls back entirely and a retry is
    safe; if a commit landed but its ack was lost, the run row exists and we stop. Never crashes
    a run, but a final failure is an ERROR and is reported to the client (returns False).
    """
    from saral.config import get_settings

    if get_settings().app_env == "test":
        return True
    from saral.db.repository import persist_run, run_persisted

    last: Exception | None = None
    for attempt in range(_PERSIST_ATTEMPTS):
        try:
            await persist_run(state)
            return True
        except Exception as e:  # noqa: BLE001
            last = e
            log.warning("run.persist_retry", run_id=state.run_id, attempt=attempt, error=str(e))
            try:
                if await run_persisted(state.run_id):
                    return True
            except Exception:  # noqa: BLE001, S110 — DB still down; keep retrying
                pass
            await asyncio.sleep(_PERSIST_BACKOFF_S[attempt])
    log.error("run.persist_failed", run_id=state.run_id, error=str(last))
    return False
