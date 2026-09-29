"""FastAPI application factory.

Phase 0: app skeleton + /health. Routes for conversations, runs, audit, and eval are
added in later phases.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response

from saral import __version__
from saral.api.auth_routes import router as auth_router
from saral.api.eval_routes import router as eval_router
from saral.api.middleware import RequestIDMiddleware
from saral.api.rm_routes import router as rm_router
from saral.api.routes import router
from saral.config import get_settings
from saral.logging import configure_logging, get_logger

log = get_logger(__name__)


@contextlib.asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    task: asyncio.Task | None = None
    if get_settings().run_worker_inproc:
        from saral.worker.main import run as worker_run

        task = asyncio.create_task(worker_run())
        log.info("worker.inproc_started")
    try:
        yield
    finally:
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


def create_app() -> FastAPI:
    configure_logging()
    settings = get_settings()

    app = FastAPI(
        title="Saral",
        version=__version__,
        description="Multilingual multi-agent customer-support resolution engine",
        lifespan=_lifespan,
    )
    app.add_middleware(RequestIDMiddleware)
    # Only the frontend may call from a browser. Auth is a bearer header (no cookies), so
    # credentials stay off.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["GET", "POST", "DELETE"],
        allow_headers=["Authorization", "Content-Type", "Accept"],
    )

    @app.get("/health", tags=["meta"])
    async def health() -> dict[str, str | bool]:
        # demo_mode lets the UI label the simulated OTP delivery honestly.
        return {
            "status": "ok",
            "env": settings.app_env,
            "version": __version__,
            "demo_mode": settings.demo_mode,
        }

    @app.get("/ready", tags=["meta"])
    async def ready() -> Response:
        """Readiness: the database and Redis answer. /health stays a liveness check."""
        import asyncio

        from sqlalchemy import text as sql

        from saral.db.session import get_engine
        from saral.runbus import get_redis

        checks: dict[str, str] = {}
        try:
            async with get_engine().connect() as conn:
                await asyncio.wait_for(conn.execute(sql("SELECT 1")), 2)
            checks["database"] = "ok"
        except Exception as e:  # noqa: BLE001
            checks["database"] = f"error: {type(e).__name__}"
        try:
            await asyncio.wait_for(get_redis().ping(), 2)
            checks["redis"] = "ok"
        except Exception as e:  # noqa: BLE001
            checks["redis"] = f"error: {type(e).__name__}"
        ok = all(v == "ok" for v in checks.values())
        return JSONResponse({"ready": ok, **checks}, status_code=200 if ok else 503)

    @app.get("/metrics", tags=["meta"], include_in_schema=False)
    async def prometheus_metrics() -> Response:
        from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    app.include_router(auth_router)
    app.include_router(router)
    app.include_router(eval_router)
    app.include_router(rm_router)

    log.info("app.created", env=settings.app_env)
    return app


app = create_app()
