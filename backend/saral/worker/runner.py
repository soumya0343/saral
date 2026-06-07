"""Execute a single run through the agent graph and publish trace events.

Kept separate from the consume loop so it can be unit-tested without Redis Streams.
"""

from __future__ import annotations

import time

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

    await publish_trace(ev("run_started", data={"message": req.message}))

    final_state = init
    pending_otp: str | None = None
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
                await publish_trace(ev("agent_started", agent=node_name))
                final_state = final_state.model_copy(update=update)
                # Capture a simulated OTP; emitted AFTER the reply so the message shows first.
                if update.get("challenge_otp"):
                    pending_otp = update["challenge_otp"]

                if "intents" in update:
                    await publish_trace(
                        ev(
                            "intent",
                            agent=node_name,
                            data={
                                "language": update.get("language"),
                                "intents": [i.model_dump() for i in update.get("intents", [])],
                                "entities": update.get("entities", {}),
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
        # Simulated OTP "SMS" arrives AFTER the assistant's "I've sent a code" reply.
        if pending_otp:
            await publish_trace(
                ev("otp", agent="identity", data={"code": pending_otp, "channel": "sms"})
            )
        await _persist(final_state)
    except TimeoutError as e:
        log.error("run.timeout", run_id=req.run_id, error=str(e))
        await publish_trace(ev("error", data={"error": str(e)}))
        final_state.status = "timed_out" # terminal close, recorded with closed_at
        await _persist(final_state)
    except Exception as e:  # noqa: BLE001 — never hard-crash a run
        log.error("run.error", run_id=req.run_id, error=str(e))
        await publish_trace(ev("error", data={"error": str(e)}))
        final_state.status = "crashed"  # terminal close; crash resume re-runs from checkpoint
        await _persist(final_state)
    finally:
        await publish_trace(ev("run_finished", data={"status": final_state.status}))

    return final_state


async def _persist(state: RunState) -> None:
    """Best-effort persistence of the run + audit chain. Never crashes a run."""
    from saral.config import get_settings

    if get_settings().app_env == "test":
        return
    try:
        from saral.db.repository import persist_run

        await persist_run(state)
    except Exception as e:  # noqa: BLE001
        log.warning("run.persist_failed", run_id=state.run_id, error=str(e))
