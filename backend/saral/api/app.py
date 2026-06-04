"""FastAPI application factory.

Phase 0: app skeleton + /health. Routes for conversations, runs, audit, and eval are
added in later phases.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from saral import __version__
from saral.api.eval_routes import router as eval_router
from saral.api.middleware import RequestIDMiddleware
from saral.api.routes import router
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
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/health", tags=["meta"])
    async def health() -> dict[str, str]:
        return {"status": "ok", "env": settings.app_env, "version": __version__}

    app.include_router(router)
    app.include_router(eval_router)

    log.info("app.created", env=settings.app_env)
    return app


app = create_app()
