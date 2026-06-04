"""Execute a single run through the agent graph and publish trace events.

Kept separate from the consume loop so it can be unit-tested without Redis Streams.
"""

from __future__ import annotations

from saral.graph.build import build_graph
from saral.graph.state import RunState
from saral.logging import get_logger
from saral.runbus import publish_trace
from saral.schemas import RunRequest, TraceEvent

log = get_logger(__name__)


async def execute_run(req: RunRequest) -> RunState:
    graph = build_graph()
    init = RunState(
        run_id=req.run_id,
        conversation_id=req.conversation_id,
        user_id=req.user_id,
        raw_message=req.message,
    )

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
    try:
        async for chunk in graph.astream(init, stream_mode="updates"):
            for node_name, update in chunk.items():
                if update is None:
                    continue
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
                if "actions" in update:
                    await publish_trace(
                        ev(
                            "action",
                            agent=node_name,
                            data={"actions": [a.model_dump() for a in update["actions"]]},
                        )
                    )
                await publish_trace(ev("agent_finished", agent=node_name))

        await publish_trace(
            ev(
                "final",
                data={
                    "status": final_state.status,
                    "language": final_state.language,
                    "intents": [i.model_dump() for i in final_state.intents],
                    "entities": final_state.entities,
                    "route": final_state.route,
                    "citations": [p.citation for p in final_state.retrieved],
                    "actions": [a.model_dump() for a in final_state.actions],
                },
            )
        )
    except Exception as e:  # noqa: BLE001 — never hard-crash a run
        log.error("run.error", run_id=req.run_id, error=str(e))
        await publish_trace(ev("error", data={"error": str(e)}))
        final_state.status = "degraded"
    finally:
        await publish_trace(ev("run_finished", data={"status": final_state.status}))

    return final_state
