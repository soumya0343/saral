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


def _bearer(c: TestClient, user_id: str = "U1001") -> dict:
    token = c.post("/auth/session", json={"user_id": user_id}).json()["token"]
    return {"Authorization": f"Bearer {token}"}


def test_step_up_request_then_verify_raises_level():
    c = _client()
    h = _bearer(c)

    req = c.post("/auth/step-up", headers=h, json={}).json()
    assert req["mode"] == "requested"
    assert req["challenge_id"] and req["test_otp"]

    ok = c.post(
        "/auth/step-up",
        headers=h,
        json={"challenge_id": req["challenge_id"], "response": req["test_otp"]},
    ).json()
    assert ok["mode"] == "verified"
    assert ok["auth_level"] == "step_up"
    assert ok["token"]

    # Single-use: replaying the same OTP fails.
    reuse = c.post(
        "/auth/step-up",
        headers=h,
        json={"challenge_id": req["challenge_id"], "response": req["test_otp"]},
    ).json()
    assert reuse["mode"] == "failed"


def test_step_up_wrong_otp_fails():
    c = _client()
    h = _bearer(c)
    req = c.post("/auth/step-up", headers=h, json={}).json()
    wrong = "000000" if req["test_otp"] != "000000" else "111111"
    bad = c.post(
        "/auth/step-up",
        headers=h,
        json={"challenge_id": req["challenge_id"], "response": wrong},
    ).json()
    assert bad["mode"] == "failed"
    assert bad["attempts_left"] == 4


def test_step_up_rejects_invalid_token():
    bad = {"Authorization": "Bearer garbage"}
    assert _client().post("/auth/step-up", headers=bad, json={}).status_code == 401


def test_otp_is_hashed_random_and_attempt_limited():
    from sqlalchemy.orm import Session

    from saral.auth.stepup import attempts_left, request_step_up, verify_step_up
    from saral.tools.store import MChallenge, get_store

    a = request_step_up("U1001")
    b = request_step_up("U1001")
    assert a["challenge_id"] != b["challenge_id"]
    assert "U1001" not in a["challenge_id"]  # not derived from user id / time

    with Session(get_store().engine) as s:
        row = s.get(MChallenge, a["challenge_id"])
        assert a["test_otp"] not in row.code_hash  # code never stored in plaintext

    wrong = "000000" if a["test_otp"] != "000000" else "111111"
    for i in range(4):
        assert verify_step_up(a["challenge_id"], wrong, "U1001") is False
        assert attempts_left(a["challenge_id"]) == 4 - i
    assert verify_step_up(a["challenge_id"], wrong, "U1001") is False  # 5th miss burns it
    assert attempts_left(a["challenge_id"]) == 0
    # Even the right code no longer works once burned.
    assert verify_step_up(a["challenge_id"], a["test_otp"], "U1001") is False


def test_otp_is_bound_to_user_and_purpose():
    from saral.auth.stepup import request_step_up, verify_step_up

    c = request_step_up("U1001", purpose="login")
    assert verify_step_up(c["challenge_id"], c["test_otp"], "U1003", purpose="login") is False
    assert verify_step_up(c["challenge_id"], c["test_otp"], "U1001", purpose="step_up") is False
    assert verify_step_up(c["challenge_id"], c["test_otp"], "U1001", purpose="login") is True


def test_otp_hidden_without_demo_mode(monkeypatch):
    from saral.auth.stepup import request_step_up
    from saral.config import get_settings

    monkeypatch.setattr(get_settings(), "demo_mode", False)
    assert "test_otp" not in request_step_up("U1001")
