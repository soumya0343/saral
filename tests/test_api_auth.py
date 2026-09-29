"""API auth integration tests (S1/S2/S3/S6): bearer identity, ownership, login OTP, refresh
rotation, self-service erasure, server-to-server endpoints, and the step-up grant.

Needs a real, migrated Postgres: set SARAL_TEST_DATABASE_URL (asyncpg DSN). Skipped otherwise.
The run bus is replaced by an in-memory list, so no Redis or worker is involved.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

pytestmark = pytest.mark.skipif(
    not os.environ.get("SARAL_TEST_DATABASE_URL"),
    reason="needs a migrated Postgres (SARAL_TEST_DATABASE_URL)",
)


def _truncate_app_tables() -> None:
    url = os.environ["SARAL_TEST_DATABASE_URL"].replace("+asyncpg", "+psycopg")
    eng = create_engine(url)
    with eng.begin() as c:
        tables = c.execute(
            text("SELECT tablename FROM pg_tables WHERE schemaname='public'"
                 " AND tablename <> 'alembic_version'")
        ).scalars().all()
        c.execute(text(f"TRUNCATE {', '.join(tables)} CASCADE"))
    eng.dispose()


@pytest.fixture
def api(monkeypatch):
    from saral.api import routes
    from saral.api.app import create_app
    from saral.db import session as dbs
    from saral.tools import mock_backend as mb

    mb._reset_state()
    _truncate_app_tables()  # the mock store reuses user ids after a reset; start clean
    dbs.get_engine.cache_clear()  # new event loop per TestClient -> fresh async engine
    dbs.get_sessionmaker.cache_clear()
    enqueued: list = []

    async def fake_enqueue(req):
        enqueued.append(req)
        return "0-1"

    monkeypatch.setattr(routes, "enqueue_run", fake_enqueue)
    with TestClient(create_app()) as client:
        client.enqueued = enqueued  # type: ignore[attr-defined]
        yield client


@pytest.fixture
def sql():
    url = os.environ["SARAL_TEST_DATABASE_URL"].replace("+asyncpg", "+psycopg")
    eng = create_engine(url)
    yield eng
    eng.dispose()


def _dev_token(api, user_id: str) -> dict:
    tok = api.post("/auth/session", json={"user_id": user_id}).json()["token"]
    return {"Authorization": f"Bearer {tok}"}


def _new_convo(api, headers) -> str:
    r = api.post("/conversations", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["id"]


# --- identity comes only from the token ------------------------------------------------------


def test_customer_routes_require_a_token(api):
    assert api.post("/conversations").status_code == 401
    assert api.get("/me/conversations").status_code == 401
    assert api.get("/me").status_code == 401
    assert api.delete("/me/data").status_code == 401
    bad = {"Authorization": "Bearer not-a-jwt"}
    assert api.post("/conversations", headers=bad).status_code == 401


def test_conversation_owner_comes_from_token_not_body(api):
    h = _dev_token(api, "U1001")
    r = api.post("/conversations", headers=h, json={"user_id": "U1003"})
    assert r.json()["user_id"] == "U1001"


def test_other_customers_conversation_is_invisible(api):
    meena = _dev_token(api, "U1003")
    cid = _new_convo(api, meena)
    asha = _dev_token(api, "U1001")
    msg = {"content": "what is my claim status?"}
    assert api.post(f"/conversations/{cid}/messages", headers=asha, json=msg).status_code == 404
    assert api.post(f"/conversations/{cid}/reply", headers=asha, json=msg).status_code == 404
    assert api.get(f"/conversations/{cid}", headers=asha).status_code == 404
    assert api.get(f"/conversations/{cid}/stream", headers=asha).status_code == 404
    assert all(c["id"] != cid for c in api.get("/me/conversations", headers=asha).json())
    # The owner can use it; the enqueued run carries the TOKEN's identity.
    assert api.post(f"/conversations/{cid}/messages", headers=meena, json=msg).status_code == 200
    assert api.enqueued[-1].user_id == "U1003"


# --- sign-up / login -------------------------------------------------------------------------


def test_new_customer_signs_up_directly(api):
    r = api.post("/customers", json={"name": "Priya Nair", "mobile": "9123456780"}).json()
    assert r["status"] == "signed_in"
    s = r["session"]
    assert s["returning"] is False and s["policy_id"]
    me = api.get("/me", headers={"Authorization": f"Bearer {s['tokens']['access_token']}"})
    assert me.json()["user_id"] == s["user_id"]


def test_existing_account_needs_login_otp(api):
    r = api.post("/customers", json={"name": "anyone", "mobile": "9876500001"}).json()
    assert r["status"] == "otp_required"
    assert r["session"] is None  # no account data before the OTP
    assert r["sent_to"].endswith("0001") and "98765" not in r["sent_to"]

    wrong = "000000" if r["test_otp"] != "000000" else "111111"
    bad = api.post("/auth/login/verify", json={"challenge_id": r["challenge_id"], "code": wrong})
    assert bad.status_code == 401
    assert bad.json()["detail"]["attempts_left"] == 4

    ok = api.post(
        "/auth/login/verify", json={"challenge_id": r["challenge_id"], "code": r["test_otp"]}
    ).json()
    assert ok["user_id"] == "U1001" and ok["returning"] is True
    assert ok["tokens"]["access_token"] and ok["tokens"]["refresh_token"]


def test_dev_token_minting_disabled_in_prod(api, monkeypatch):
    from saral.config import get_settings

    monkeypatch.setattr(get_settings(), "app_env", "prod")
    assert api.post("/auth/session", json={"user_id": "U1001"}).status_code == 404


# --- sessions: expiry, refresh rotation, history kept -----------------------------------------


def test_expired_access_token_refreshes_and_history_survives(api, monkeypatch):
    from saral.auth.tokens import mint_session_token

    login = api.post("/customers", json={"name": "Ravi", "mobile": "9123456781"}).json()
    tokens = login["session"]["tokens"]
    h = {"Authorization": f"Bearer {tokens['access_token']}"}
    cid = _new_convo(api, h)
    api.post(f"/conversations/{cid}/messages", headers=h, json={"content": "hello"})

    expired = mint_session_token(login["session"]["user_id"], ttl_min=-1)
    stale = {"Authorization": f"Bearer {expired}"}
    assert api.get("/me/conversations", headers=stale).status_code == 401

    new = api.post("/auth/refresh", json={"refresh_token": tokens["refresh_token"]}).json()
    h2 = {"Authorization": f"Bearer {new['access_token']}"}
    assert [c["id"] for c in api.get("/me/conversations", headers=h2).json()] == [cid]
    history = api.get(f"/conversations/{cid}", headers=h2).json()
    assert [m["content"] for m in history] == ["hello"]


def test_refresh_token_reuse_revokes_the_family(api):
    login = api.post("/customers", json={"name": "Sita", "mobile": "9123456782"}).json()
    first = login["session"]["tokens"]["refresh_token"]
    second = api.post("/auth/refresh", json={"refresh_token": first}).json()["refresh_token"]
    # Replaying the rotated token = theft signal -> whole family revoked, incl. the new one.
    assert api.post("/auth/refresh", json={"refresh_token": first}).status_code == 401
    assert api.post("/auth/refresh", json={"refresh_token": second}).status_code == 401


def test_logout_revokes_refresh(api):
    rt = api.post("/customers", json={"name": "Om", "mobile": "9123456783"}).json()["session"][
        "tokens"
    ]["refresh_token"]
    assert api.post("/auth/logout", json={"refresh_token": rt}).status_code == 204
    assert api.post("/auth/refresh", json={"refresh_token": rt}).status_code == 401


# --- self-service erasure + server-to-server endpoints --------------------------------------


def test_erasure_is_self_service_and_signs_out(api):
    login = api.post("/customers", json={"name": "Zoya", "mobile": "9123456784"}).json()
    s = login["session"]
    h = {"Authorization": f"Bearer {s['tokens']['access_token']}"}
    cid = _new_convo(api, h)
    api.post(f"/conversations/{cid}/messages", headers=h, json={"content": "my email is z@x.io"})

    r = api.delete("/me/data", headers=h)
    assert r.status_code == 200 and r.json()["consent_status"] == "withdrawn"
    assert api.get(f"/conversations/{cid}", headers=h).json()[0]["content"] == "[erased]"
    refresh = {"refresh_token": s["tokens"]["refresh_token"]}
    assert api.post("/auth/refresh", json=refresh).status_code == 401
    assert api.delete("/users/U1001/data").status_code in (404, 405)  # old route is gone


def test_rm_endpoint_needs_service_key(api, monkeypatch):
    from saral.config import get_settings

    body = {"operator_id": "rm-7"}
    assert api.post("/escalations/x/resolve", json=body).status_code == 404  # no key: disabled
    monkeypatch.setattr(get_settings(), "service_api_key", "s3cret")
    assert api.post("/escalations/x/resolve", json=body).status_code == 401
    wrong = {"X-Service-Key": "nope"}
    assert api.post("/escalations/x/resolve", json=body, headers=wrong).status_code == 401
    right = {"X-Service-Key": "s3cret"}
    assert api.post("/escalations/x/resolve", json=body, headers=right).status_code == 404


def test_eval_trigger_locked_in_prod(api, monkeypatch):
    from saral.config import get_settings

    monkeypatch.setattr(get_settings(), "app_env", "prod")
    assert api.post("/eval/run").status_code == 404  # no service key configured
    assert api.get("/eval/reports").status_code == 200  # read-only reports stay public


# --- step-up is a short grant bound to one pending write -------------------------------------


def _suspend(sql, cid: str, status: str, pending: dict, challenge_id: str | None = None):
    with sql.begin() as c:
        c.execute(
            text(
                "UPDATE conversations SET suspend_status=:s, pending_write=CAST(:p AS JSONB),"
                " challenge_id=:c, original_message=:m WHERE id=:id"
            ),
            {"s": status, "p": json.dumps(pending), "c": challenge_id,
             "m": "update my number to 9811111111", "id": cid},
        )


def _pending(key: str = "idem-1") -> dict:
    return {"tool": "update_contact", "field": "mobile", "value": "9811111111",
            "args": {"field": "mobile", "value": "9811111111"}, "read_back": "confirm?",
            "idempotency_key": key, "intent_nonce": 1}


def test_otp_grants_step_up_for_that_pending_write_only(api, sql):
    from saral.auth import request_step_up

    h = _dev_token(api, "U1001")
    cid = _new_convo(api, h)
    chal = request_step_up("U1001")
    _suspend(sql, cid, "awaiting_input", _pending("idem-1"), chal["challenge_id"])

    api.post(f"/conversations/{cid}/reply", headers=h, json={"content": chal["test_otp"]})
    assert str(api.enqueued[-1].auth_level) == "step_up"
    with sql.connect() as c:
        row = c.execute(text("SELECT step_up_for, step_up_until FROM conversations WHERE id=:i"),
                        {"i": cid}).one()
    assert row.step_up_for == "idem-1"

    # Confirmation for the SAME write: still step_up.
    _suspend(sql, cid, "awaiting_confirmation", _pending("idem-1"))
    api.post(f"/conversations/{cid}/reply", headers=h, json={"content": "haan"})
    assert str(api.enqueued[-1].auth_level) == "step_up"
    assert api.enqueued[-1].resume_reply == "yes"

    # A DIFFERENT pending write does not inherit it.
    _suspend(sql, cid, "awaiting_confirmation", _pending("idem-2"))
    api.post(f"/conversations/{cid}/reply", headers=h, json={"content": "haan"})
    assert str(api.enqueued[-1].auth_level) == "session"

    # A fresh message never carries step-up forward.
    api.post(f"/conversations/{cid}/messages", headers=h, json={"content": "hi"})
    assert str(api.enqueued[-1].auth_level) == "session"


def test_step_up_grant_expires(api, sql):
    h = _dev_token(api, "U1001")
    cid = _new_convo(api, h)
    _suspend(sql, cid, "awaiting_confirmation", _pending("idem-9"))
    with sql.begin() as c:
        c.execute(
            text("UPDATE conversations SET step_up_for='idem-9', step_up_until=:t WHERE id=:i"),
            {"t": datetime.now(UTC) - timedelta(seconds=1), "i": cid},
        )
    api.post(f"/conversations/{cid}/reply", headers=h, json={"content": "yes"})
    assert str(api.enqueued[-1].auth_level) == "session"  # expired -> OTP must be re-earned


# --- Phase 4: escalation rows link to their RM request; erasure reaches the RM queue ---------


def test_escalation_row_links_to_its_rm_request(api):
    import asyncio

    from sqlalchemy import select

    from saral.db.models import Escalation
    from saral.db.repository import persist_run
    from saral.db.session import get_sessionmaker
    from saral.graph.state import RunState
    from saral.schemas import EscalationRecord, ResponsePayload

    login = api.post("/customers", json={"name": "Ira", "mobile": "9123456783"}).json()
    s = login["session"]
    h = {"Authorization": f"Bearer {s['tokens']['access_token']}"}
    cid = _new_convo(api, h)
    state = RunState(
        run_id="run-rm-1",
        conversation_id=cid,
        user_id=s["user_id"],
        raw_message="update my number",
        status="escalated",
        final_response=ResponsePayload(resolution_status="escalated", message="raised"),
        escalation=EscalationRecord(
            conversation_id=cid, tenant_id="t_demo", detected_intent="update_contact",
            blocking_reason="awaiting_manager_approval", transcript_ref=cid, sla_target="24h",
            user_id=s["user_id"], request_id="REQ424242",
        ),
    )

    from saral.db.session import get_engine

    async def check():
        try:
            await persist_run(state)
            async with get_sessionmaker()() as session:
                return (
                    await session.scalars(
                        select(Escalation).where(Escalation.conversation_id == cid)
                    )
                ).one()
        finally:
            await get_engine().dispose()

    # The cached engine belongs to the API client's loop; use a fresh one on this loop.
    get_sessionmaker.cache_clear()
    get_engine.cache_clear()
    try:
        row = asyncio.run(check())
    finally:
        get_sessionmaker.cache_clear()
        get_engine.cache_clear()
    assert row.request_id == "REQ424242" and row.user_id == s["user_id"]


def test_erasure_reaches_the_rm_request_queue(api):
    from saral.rm.requests import get_requests

    login = api.post("/customers", json={"name": "Neel", "mobile": "9123456782"}).json()
    s = login["session"]
    h = {"Authorization": f"Bearer {s['tokens']['access_token']}"}
    _new_convo(api, h)
    req, _ = get_requests().raise_request(
        tenant_id="t_demo", user_id=s["user_id"], conversation_id="c", kind="update_contact",
        reason="awaiting_manager_approval", key="erase-me",
        change={"field": "mobile", "value": "9000000001"}, summary="asked to change mobile",
        language="en", sla_target="24h",
    )
    r = api.delete("/me/data", headers=h)
    assert r.status_code == 200 and r.json()["erased"]["rm_requests"] == 1
    row = get_requests().get_for_rm(req.id)
    assert row["requested_change"] is None and row["summary"] == "[erased]"
