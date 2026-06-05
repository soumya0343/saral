"""Mock IdP + step-up HTTP endpoints (TRD §17, §11.5)."""

from fastapi.testclient import TestClient

from saral.api.app import create_app
from saral.tools import mock_backend as mb


def setup_function():
    mb._reset_state()


def _client() -> TestClient:
    return TestClient(create_app())


def test_session_token_minted_and_identity_token_derived():
    c = _client()
    r = c.post("/auth/session", json={"user_id": "U1001"})
    assert r.status_code == 200
    body = r.json()
    assert body["user_id"] == "U1001"
    assert body["tenant_id"] == "t_demo"
    assert body["auth_level"] == "session"
    assert body["token"]


def test_session_unknown_customer_404():
    assert _client().post("/auth/session", json={"user_id": "NOPE"}).status_code == 404


def test_step_up_request_then_verify_raises_level():
    c = _client()
    token = c.post("/auth/session", json={"user_id": "U1001"}).json()["token"]

    req = c.post("/auth/step-up", json={"token": token}).json()
    assert req["mode"] == "requested"
    assert req["challenge_id"] and req["test_otp"]

    ok = c.post(
        "/auth/step-up",
        json={"token": token, "challenge_id": req["challenge_id"], "response": req["test_otp"]},
    ).json()
    assert ok["mode"] == "verified"
    assert ok["auth_level"] == "step_up"
    assert ok["token"]

    # Single-use: replaying the same OTP fails.
    reuse = c.post(
        "/auth/step-up",
        json={"token": token, "challenge_id": req["challenge_id"], "response": req["test_otp"]},
    ).json()
    assert reuse["mode"] == "failed"


def test_step_up_wrong_otp_fails():
    c = _client()
    token = c.post("/auth/session", json={"user_id": "U1001"}).json()["token"]
    req = c.post("/auth/step-up", json={"token": token}).json()
    bad = c.post(
        "/auth/step-up",
        json={"token": token, "challenge_id": req["challenge_id"], "response": "000000"},
    ).json()
    assert bad["mode"] == "failed"


def test_step_up_rejects_invalid_token():
    assert _client().post("/auth/step-up", json={"token": "garbage"}).status_code == 401
