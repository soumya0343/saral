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
