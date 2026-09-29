"""Evaluation API: run the suite and fetch reports."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, Header, HTTPException

from saral.api.deps import require_service_key
from saral.config import get_settings
from saral.eval import store
from saral.eval.runner import detect_tier, run_eval
from saral.eval.schemas import EvalReport

router = APIRouter(prefix="/eval", tags=["eval"])


def _eval_trigger_allowed(x_service_key: str | None = Header(default=None)) -> None:
    """Running the suite spends free-tier LLM quota, so it is not a public endpoint: in prod it
    needs the service key (or use `make eval`); in dev/test it is open for convenience."""
    if get_settings().app_env != "prod":
        return
    require_service_key(x_service_key)


@router.post("/run", response_model=EvalReport, dependencies=[Depends(_eval_trigger_allowed)])
async def run() -> EvalReport:
    """Runs the OFFLINE tier only. With real model keys configured the suite would spend
    free-tier quota for ~150 scenarios, so the live tier is CLI-only (`make eval-live`)."""
    if await asyncio.to_thread(detect_tier) != "offline":
        raise HTTPException(
            status_code=409,
            detail="this server has real model keys: run the live eval with `make eval-live`, "
            "or the offline eval with `make eval`",
        )
    return await run_eval(expect_tier="offline")


@router.get("/report", response_model=EvalReport)
async def latest_report() -> EvalReport:
    report = await asyncio.to_thread(store.latest_report, get_settings().eval_reports_dir)
    if report is None:
        raise HTTPException(status_code=404, detail="no eval report yet; POST /eval/run")
    return report


@router.get("/reports", response_model=list[EvalReport])
async def all_reports() -> list[EvalReport]:
    return await asyncio.to_thread(store.list_reports, get_settings().eval_reports_dir)
