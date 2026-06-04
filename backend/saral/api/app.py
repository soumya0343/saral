"""FastAPI application factory.

Phase 0: app skeleton + /health. Routes for conversations, runs, audit, and eval are
added in later phases.
"""

from __future__ import annotations

from fastapi import FastAPI

from saral import __version__
from saral.api.middleware import RequestIDMiddleware
from saral.config import get_settings
from saral.logging import configure_logging, get_logger

log = get_logger(__name__)


def create_app() -> FastAPI:
    configure_logging()
    settings = get_settings()

    app = FastAPI(
        title="Saral",
        version=__version__,
        description="Multilingual multi-agent customer-support resolution engine",
    )
    app.add_middleware(RequestIDMiddleware)

    @app.get("/health", tags=["meta"])
    async def health() -> dict[str, str]:
        return {"status": "ok", "env": settings.app_env, "version": __version__}

    log.info("app.created", env=settings.app_env)
    return app


app = create_app()
