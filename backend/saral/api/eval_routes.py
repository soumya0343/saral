"""Evaluation API: run the suite and fetch reports."""

from __future__ import annotations

import asyncio
import hmac
import os
import sys
from pathlib import Path

from fastapi import APIRouter, Depends, Header, HTTPException, Request

import saral
from saral.api.deps import require_service_key
from saral.api.ratelimit import client_ip, hit
from saral.config import get_settings
from saral.eval import store
from saral.eval.schemas import EvalReport
from saral.logging import get_logger

log = get_logger(__name__)

router = APIRouter(prefix="/eval", tags=["eval"])

# The offline tier, whatever this server is configured with (mirrors EVAL_OFFLINE_ENV in the
# Makefile): stub models, hashing embedder, and an in-memory store.
_OFFLINE_ENV = {
    "APP_ENV": "test",
    "STORE_BACKEND": "memory",
    "EMBEDDER": "hashing",
    "LLM_PROVIDER_ORDER": "stub",
    "LLM_ROLE_TRIAGE": "stub",
    "LLM_ROLE_SYNTHESIS": "stub",
    "LLM_ROLE_JUDGE": "stub",
    "GROQ_API_KEY": "",
    "GEMINI_API_KEY": "",
    "CEREBRAS_API_KEY": "",
    "SARVAM_API_KEY": "",
    "ANTHROPIC_API_KEY": "",
    "LOG_LEVEL": "ERROR",
}
_BACKEND_DIR = str(Path(saral.__file__).resolve().parent.parent)  # so the child can import saral
_RUN_TIMEOUT_S = 300
_run_lock = asyncio.Lock()


async def _eval_trigger_allowed(
    request: Request, x_service_key: str | None = Header(default=None)
) -> None:
    """Open in dev/test. In prod it needs the service key, unless EVAL_RUN_PUBLIC (a public
    demo): then anyone may run it, rate-limited per client IP."""
    s = get_settings()
    if s.app_env != "prod":
        return
    if not s.eval_run_public:
        require_service_key(x_service_key)
        return
    key = s.service_api_key
    if x_service_key and key and hmac.compare_digest(x_service_key, key):
        return  # the operator is not rate-limited
    await hit("eval_run", client_ip(request), s.eval_run_public_per_hour, 3600)


@router.post("/run", response_model=EvalReport, dependencies=[Depends(_eval_trigger_allowed)])
async def run() -> EvalReport:
    """Runs the OFFLINE tier in a child process and returns its report (in prod, the oldest
    offline reports beyond EVAL_REPORTS_KEEP are deleted).

    A child process, because the suite wipes and reseeds the mock store before every scenario
    and needs stub models: in this process that would delete the server's data and, with real
    keys configured, spend free-tier quota. The live tier stays CLI-only (`make eval-live`).
    """
    if _run_lock.locked():
        raise HTTPException(status_code=409, detail="an eval run is already in progress")
    async with _run_lock:
        env = {**os.environ, **_OFFLINE_ENV}
        env["PYTHONPATH"] = os.pathsep.join(filter(None, [_BACKEND_DIR, env.get("PYTHONPATH")]))
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "saral.eval.runner", "--tier", "offline",
            env=env,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, err = await asyncio.wait_for(proc.communicate(), _RUN_TIMEOUT_S)
        except TimeoutError as e:
            proc.kill()
            raise HTTPException(status_code=504, detail="eval run timed out") from e
        if proc.returncode != 0:
            tail = err.decode(errors="replace").strip().splitlines()[-1:] or ["unknown error"]
            log.error("eval.run_failed", code=proc.returncode, error=tail[0])
            raise HTTPException(status_code=500, detail=f"eval run failed: {tail[0]}")
        settings = get_settings()
        report = await asyncio.to_thread(store.latest_report, settings.eval_reports_dir, "offline")
        if report is None:
            raise HTTPException(status_code=500, detail="eval ran but wrote no report")
        if settings.app_env == "prod":  # a deployment's disk; local report history is left alone
            await asyncio.to_thread(
                store.prune_reports, settings.eval_reports_dir, settings.eval_reports_keep
            )
    return report


@router.get("/report", response_model=EvalReport)
async def latest_report() -> EvalReport:
    report = await asyncio.to_thread(store.latest_report, get_settings().eval_reports_dir)
    if report is None:
        raise HTTPException(status_code=404, detail="no eval report yet; POST /eval/run")
    return report


@router.get("/reports", response_model=list[EvalReport])
async def all_reports() -> list[EvalReport]:
    return await asyncio.to_thread(store.list_reports, get_settings().eval_reports_dir)
