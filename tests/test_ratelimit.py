"""Rate limiter + insecure-prod-config guard."""

from __future__ import annotations

import os

import pytest
from fastapi import HTTPException


def test_prod_refuses_default_session_secret(monkeypatch):
    from saral.config import Settings

    monkeypatch.setenv("APP_ENV", "prod")
    monkeypatch.delenv("SESSION_SECRET", raising=False)
    with pytest.raises(ValueError, match="SESSION_SECRET"):
        Settings(_env_file=None)
    monkeypatch.setenv("SESSION_SECRET", "x" * 16)
    with pytest.raises(ValueError):
        Settings(_env_file=None)
    monkeypatch.setenv("SESSION_SECRET", "k" * 48)
    monkeypatch.delenv("RM_REQUEST_KEY", raising=False)
    with pytest.raises(ValueError, match="RM_REQUEST_KEY"):
        Settings(_env_file=None)  # the requested changes would be encrypted with a dev key
    from cryptography.fernet import Fernet

    monkeypatch.setenv("RM_REQUEST_KEY", Fernet.generate_key().decode())
    assert Settings(_env_file=None).app_env == "prod"


def test_cors_origins_parsed():
    from saral.config import Settings

    s = Settings(_env_file=None, frontend_origins="https://a.app, https://b.app")
    assert s.cors_origins == ["https://a.app", "https://b.app"]


@pytest.mark.skipif(
    not os.environ.get("SARAL_TEST_REDIS_URL"), reason="needs Redis (SARAL_TEST_REDIS_URL)"
)
async def test_fixed_window_limit(monkeypatch):
    import uuid

    from saral import runbus
    from saral.api import ratelimit
    from saral.config import get_settings

    monkeypatch.setattr(get_settings(), "rate_limit_enabled", True)
    monkeypatch.setattr(get_settings(), "redis_url", os.environ["SARAL_TEST_REDIS_URL"])
    runbus.get_redis.cache_clear()
    who = uuid.uuid4().hex
    for _ in range(3):
        await ratelimit.hit("t", who, limit=3, window_s=30)
    with pytest.raises(HTTPException) as e:
        await ratelimit.hit("t", who, limit=3, window_s=30)
    assert e.value.status_code == 429
    runbus.get_redis.cache_clear()


async def test_limiter_fails_open_without_redis(monkeypatch):
    from saral import runbus
    from saral.api import ratelimit
    from saral.config import get_settings

    monkeypatch.setattr(get_settings(), "rate_limit_enabled", True)
    monkeypatch.setattr(get_settings(), "redis_url", "redis://localhost:1/0")
    runbus.get_redis.cache_clear()
    await ratelimit.hit("t", "someone", limit=0, window_s=30)  # no exception: fail open
    runbus.get_redis.cache_clear()


@pytest.mark.parametrize(
    "header,from_right,expected",
    [
        ("6.6.6.6, 49.238.50.215", 1, "49.238.50.215"),  # appending proxy: forged entry ignored
        ("49.238.50.215", 1, "49.238.50.215"),
        ("6.6.6.6, 49.238.50.215, 10.0.0.1", 2, "49.238.50.215"),  # two proxies of ours
        ("49.238.50.215", 3, "49.238.50.215"),  # fewer hops than configured: leftmost
        ("203.0.113.9, 10.0.0.1", 0, "203.0.113.9"),  # 0 = first entry (overwriting proxies)
    ],
)
def test_client_ip_reads_the_right_hop(monkeypatch, header, from_right, expected):
    from starlette.requests import Request

    from saral.api.ratelimit import client_ip
    from saral.config import get_settings

    monkeypatch.setattr(get_settings(), "client_ip_header", "X-Forwarded-For")
    monkeypatch.setattr(get_settings(), "client_ip_from_right", from_right)
    req = Request({"type": "http", "headers": [(b"x-forwarded-for", header.encode())],
                   "client": ("10.16.34.130", 1)})
    assert client_ip(req) == expected
