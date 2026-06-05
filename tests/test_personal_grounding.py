"""Per-customer document grounding — the differentiator (FR-16, §12.3, §18.2).

Proves: (1) an explanation grounds in the customer's OWN clause, (2) the per-customer filter is
a hard pre-filter that never leaks across customers, (3) the intent→domain map bounds it, and
(4) Hindi queries ground in Hindi clause text.
"""

from saral.graph.build import build_graph
from saral.graph.state import RunState
from saral.rag.customer_index import search_customer
from saral.tools import mock_backend as mb


def setup_function():
    mb._reset_state()


async def _run(message: str, user_id: str) -> RunState:
    init = RunState(run_id="r", conversation_id="c", user_id=user_id, raw_message=message)
    return RunState.model_validate(await build_graph().ainvoke(init))


def test_explanation_grounds_in_own_clause():
    hits = search_customer(
        "why was my claim reduced proportionate deduction", "U1001", ["claims", "policy_coverage"]
    )
    assert hits
    assert any("4.1" in h.text or "proportionate" in h.text.lower() for h in hits)
    assert all(h.is_personal for h in hits)


def test_no_cross_customer_leak():
    # A customer with no documents of their own gets nothing, even with a matching query.
    assert search_customer("proportionate deduction clause 4.1", "U1002", None) == []


def test_intent_domain_map_bounds_retrieval():
    # Restricting to a domain the docs aren't tagged with returns nothing for that scope.
    assert search_customer("claim deduction", "U1001", ["profile_account"]) == []


def test_hindi_query_grounds_in_hindi_clause():
    hits = search_customer(
        "मेरा दावा क्यों अस्वीकृत हुआ पॉलिसी लैप्स", "U1003", ["claims", "policy_coverage"]
    )
    assert hits
    assert any("6.2" in h.text or "लैप्स" in h.text for h in hits)


def test_rider_clause_grounding():
    hits = search_customer(
        "diabetes claim reject rider R1.3", "U1010", ["claims", "policy_coverage"]
    )
    assert any("R1.3" in h.text for h in hits)


async def test_graph_surfaces_personal_citation():
    state = await _run("Why was my claim partially reduced?", "U1001")
    assert state.route == "rag"
    assert any(p.is_personal for p in state.retrieved)
    assert state.final_response is not None
    assert state.final_response.citations
