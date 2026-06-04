"""Evaluation API: run the suite and fetch reports (TRD §17)."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException

from saral.config import get_settings
from saral.eval import store
from saral.eval.runner import run_eval
from saral.eval.schemas import EvalReport

router = APIRouter(prefix="/eval", tags=["eval"])


@router.post("/run", response_model=EvalReport)
async def run() -> EvalReport:
    return await run_eval()


@router.get("/report", response_model=EvalReport)
async def latest_report() -> EvalReport:
    report = await asyncio.to_thread(store.latest_report, get_settings().eval_reports_dir)
    if report is None:
        raise HTTPException(status_code=404, detail="no eval report yet; POST /eval/run")
    return report


@router.get("/reports", response_model=list[EvalReport])
async def all_reports() -> list[EvalReport]:
    return await asyncio.to_thread(store.list_reports, get_settings().eval_reports_dir)
