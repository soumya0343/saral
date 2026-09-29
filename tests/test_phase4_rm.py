"""Phase 4 — the RM request is complete, deduplicated, queryable and erasable; every escalation
becomes one; claims carry structured facts; the RM's system talks to Saral with a service key."""

from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from saral.actions import claim_intake
from saral.api.app import create_app
from saral.config import get_settings
from saral.eval.conversation import ConversationDriver
from saral.eval.schemas import Scenario
from saral.rm.requests import dedup_key, get_requests
from saral.tools import mock_backend as mb


def setup_function():
    mb._reset_state()


def _driver(user_id: str, language: str = "en") -> ConversationDriver:
    sc = Scenario(
        id=f"t-{user_id}", category="action", language=language, user_id=user_id,
        message="x",
    )
    return ConversationDriver(sc)


async def _change_mobile(d: ConversationDriver, number: str = "9811122233"):
    await d.say(f"Please update my mobile number to {number}")
    await d.say("{otp}")
    return (await d.say("yes"))[1]


# --- the request store ------------------------------------------------------------------


def _raise(key: str, **kw):
    args = {
        "tenant_id": "t", "user_id": "U1001", "conversation_id": "c", "kind": "update_contact",
        "reason": "awaiting_manager_approval", "key": key,
        "change": {"field": "mobile", "value": "9811122233"}, "summary": None, "language": "en",
        "sla_target": "24h",
    }
    return get_requests().raise_request(**{**args, **kw})


def test_same_open_request_is_not_duplicated_but_a_closed_one_can_be_reopened():
    first, created = _raise("k1")
    again, created_again = _raise("k1")
    assert created and not created_again and again.id == first.id
    get_requests().set_status(first.id, "done", "rm-7", "Updated in core system")
    third, created_third = _raise("k1")
    assert created_third and third.id != first.id


def test_requested_change_is_encrypted_at_rest_and_readable_only_for_the_rm():
    req, _ = _raise("k2")
    with get_requests().engine.connect() as conn:
        raw = conn.execute(
            text("SELECT change_enc FROM rm_requests WHERE id = :i"), {"i": req.id}
        ).scalar()
    assert raw and "9811122233" not in raw
    assert get_requests().get_for_rm(req.id)["requested_change"]["value"] == "9811122233"
    # The customer-facing view never carries the change.
    view = get_requests().list_for_user("U1001")[0]
    assert not hasattr(view, "requested_change")


def test_erasure_tombstones_change_and_summary():
    req, _ = _raise("k3", summary="Customer asked to change mobile.")
    assert get_requests().erase_user("U1001") == 1
    row = get_requests().get_for_rm(req.id)
    assert row["requested_change"] is None and row["summary"] == "[erased]"


def test_dedup_key_depends_on_the_change():
    a = dedup_key("U1", "update_contact", {"field": "mobile", "value": "9811122233"})
    assert a == dedup_key("U1", "update_contact", {"value": "9811122233", "field": "mobile"})
    assert a != dedup_key("U1", "update_contact", {"field": "mobile", "value": "9811122234"})
    assert a != dedup_key("U2", "update_contact", {"field": "mobile", "value": "9811122233"})


# --- through the graph ------------------------------------------------------------------


async def test_confirmed_change_becomes_a_complete_rm_request():
    state = await _change_mobile(_driver("U1001"))
    assert state.status == "escalated" and state.rm_request_created
    assert state.escalation.request_id == state.rm_request_id
    assert state.rm_request_id in state.final_response.message  # customer gets the reference
    row = get_requests().get_for_rm(state.rm_request_id)
    assert row["kind"] == "update_contact" and row["user_id"] == "U1001"
    assert row["requested_change"]["args"] == {"field": "mobile", "value": "9811122233"}
    # Case summary: English, says why, and never carries the new number.
    assert "Escalated because" in row["summary"] and "9811122233" not in row["summary"]


async def test_asking_twice_gives_one_request_and_no_second_otp():
    d = _driver("U1001")
    first = await _change_mobile(d)
    _, again = await d.say("Please update my mobile number to 9811122233")
    assert again.status == "resolved" and again.challenge_id is None  # no OTP minted
    assert again.rm_request_id == first.rm_request_id and not again.rm_request_created
    assert len(get_requests().list_for_user("U1001")) == 1


async def test_customer_asks_status_in_hindi_and_sees_the_rms_outcome():
    d = _driver("U1003", "hi")
    await d.say("मेरा मोबाइल नंबर 9811122233 में बदल दो")
    await d.say("{otp}")
    _, raised = await d.say("हाँ")
    _, status = await d.say("मेरे अनुरोध की स्थिति क्या है?")
    assert "get_request_status" in [a.tool for a in status.actions if a.ok]
    assert raised.rm_request_id in status.final_response.message
    assert "खुला" in status.final_response.message
    get_requests().set_status(raised.rm_request_id, "done", "rm-3", "नंबर अपडेट कर दिया गया")
    _, after = await d.say("मेरे अनुरोध की स्थिति क्या है?")
    assert "पूरा" in after.final_response.message
    assert "नंबर अपडेट कर दिया गया" in after.final_response.message


async def test_other_customers_request_id_is_not_found():
    req, _ = _raise("k4")  # belongs to U1001
    _, state = await _driver("U1020").say(f"What is the status of request {req.id}?")
    rec = next(a for a in state.actions if a.tool == "get_request_status")
    assert not rec.ok and req.id not in str(rec.result)


async def test_every_escalation_becomes_a_handoff_request():
    d = _driver("U1001")
    await d.say("Your app is the worst, nothing is working")
    _, state = await d.say("Still not working, horrible")
    assert state.status == "escalated" and state.rm_request_kind == "handoff"
    row = get_requests().get_for_rm(state.rm_request_id)
    assert row["reason"] == "repeated_complaint" and row["requested_change"] is None


# --- claim intake -------------------------------------------------------------------------


async def test_claim_is_filed_with_structured_facts_not_the_chat_message():
    d = _driver("U1001")
    _, s1 = await d.say("I want to file a claim")
    assert s1.status == "awaiting_input" and "date" in s1.final_response.message
    _, s2 = await d.say("It was a knee surgery at Apollo Hospital yesterday")
    assert s2.status == "awaiting_input" and "amount" in s2.final_response.message
    await d.say("45000")
    _, s4 = await d.say("{otp}")
    assert s4.status == "awaiting_confirmation" and "₹45,000" in s4.final_response.message
    _, s5 = await d.say("yes")
    assert s5.rm_request_kind == "file_claim"
    change = get_requests().get_for_rm(s5.rm_request_id)["requested_change"]["args"]
    assert change == {
        "policy_id": "POL1001",
        "incident_date": (date.today() - timedelta(days=1)).isoformat(),
        "amount_inr": 45000.0,
        "provider": "Apollo Hospital",
        "description": "knee surgery",
    }
    assert not any(a.tool == "file_claim" for a in s5.actions)  # Saral never files it itself


async def test_claim_intake_asks_which_policy_when_the_customer_has_two():
    _, s = await _driver("U1002").say(
        "File a claim: car accident yesterday, repaired at Sharma Motors for Rs 18,500"
    )
    assert "POL1002" in s.final_response.message and "POL1012" in s.final_response.message


async def test_claim_intake_gives_up_to_a_human_after_repeated_rounds():
    d = _driver("U1001")
    await d.say("I want to file a claim")
    for _ in range(claim_intake.MAX_ROUNDS):
        _, s = await d.say("Apollo Hospital")
    assert s.status == "escalated" and s.rm_request_kind == "handoff"


@pytest.mark.parametrize(
    "text,slots",
    [
        ("bill 1.2 lakh", {"amount_inr": 120000.0}),
        ("₹45,000 at City Hospital", {"amount_inr": 45000.0, "provider": "City Hospital"}),
        ("सिटी अस्पताल में 30000 रुपये", {"provider": "सिटी अस्पताल", "amount_inr": 30000.0}),
        ("kal Fortis hospital mein", {"provider": "Fortis hospital"}),
    ],
)
def test_slot_extraction(text, slots):
    got = claim_intake.extract(text, missing=set(slots))
    assert {k: got.get(k) for k in slots} == slots


def test_future_or_impossible_dates_are_rejected():
    assert claim_intake.parse_date("31/02/2026") is None
    future = (date.today() + timedelta(days=10)).strftime("%d/%m/%Y")
    assert claim_intake.parse_date(future) is None
    assert claim_intake.parse_date("कल") == (date.today() - timedelta(days=1)).isoformat()


# --- the RM's system --------------------------------------------------------------------


def test_rm_endpoints_need_the_service_key(monkeypatch):
    req, _ = _raise("k5")
    client = TestClient(create_app())
    assert client.get("/rm/requests").status_code == 404  # disabled without a configured key
    monkeypatch.setattr(get_settings(), "service_api_key", "s3cret")
    assert client.get("/rm/requests", headers={"X-Service-Key": "nope"}).status_code == 401
    ok = client.get("/rm/requests", headers={"X-Service-Key": "s3cret"}).json()["requests"]
    assert ok[0]["requested_change"]["value"] == "9811122233"
    upd = client.post(
        f"/rm/requests/{req.id}/status",
        headers={"X-Service-Key": "s3cret"},
        json={"status": "done", "handled_by": "rm-1", "note": "Done, call 9876500001 if needed"},
    )
    assert upd.status_code == 200 and upd.json()["status"] == "done"
    assert "9876500001" not in (get_requests().get_for_rm(req.id)["rm_note"] or "")


# --- observability ------------------------------------------------------------------------


async def test_metrics_count_escalations_and_rm_requests():
    from prometheus_client import REGISTRY

    before = REGISTRY.get_sample_value(
        "saral_rm_requests_total", {"kind": "update_contact", "created": "true"}
    ) or 0.0
    await _change_mobile(_driver("U1001"))
    after = REGISTRY.get_sample_value(
        "saral_rm_requests_total", {"kind": "update_contact", "created": "true"}
    )
    assert after == before + 1
    body = TestClient(create_app()).get("/metrics").text
    assert "saral_escalations_total" in body and "saral_llm_calls_total" in body


def test_ready_reports_dependencies():
    r = TestClient(create_app()).get("/ready")
    assert r.status_code in (200, 503)
    assert set(r.json()) >= {"ready", "database", "redis"}
