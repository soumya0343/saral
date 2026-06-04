from fastapi.testclient import TestClient

from saral.api.app import create_app


def test_health_ok() -> None:
    client = TestClient(create_app())
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert "version" in body
    # request-id echoed back
    assert resp.headers.get("X-Request-ID")
