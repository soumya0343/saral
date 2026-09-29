"""Server-to-server endpoints for the relationship manager's own system (X-Service-Key).

The RM works outside Saral: their system pulls open requests (with the decrypted requested
change and the case summary), performs the change in the core system itself, and reports the
outcome back here so the customer can ask "what happened to my request?". There is no Saral UI
for this, and no endpoint here writes customer data — only the request's status.
"""

from __future__ import annotations

import asyncio
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from saral.api.deps import require_service_key
from saral.rm.requests import get_requests

router = APIRouter(prefix="/rm", tags=["rm"], dependencies=[Depends(require_service_key)])


@router.get("/requests")
async def list_requests(
    status: Literal["open", "done", "rejected", "all"] = "open",
    limit: int = Query(default=50, ge=1, le=200),
) -> dict:
    rows = await asyncio.to_thread(
        get_requests().list_for_rm, None if status == "all" else status, limit
    )
    return {"requests": rows}


@router.get("/requests/{request_id}")
async def get_request(request_id: str) -> dict:
    row = await asyncio.to_thread(get_requests().get_for_rm, request_id)
    if row is None:
        raise HTTPException(status_code=404, detail="request not found")
    return row


class StatusUpdate(BaseModel):
    status: Literal["done", "rejected", "open"]
    handled_by: str  # which RM, asserted by the authenticated RM system
    note: str | None = None  # shown to the customer when they ask (PII-redacted)


@router.post("/requests/{request_id}/status")
async def set_status(request_id: str, body: StatusUpdate) -> dict:
    row = await asyncio.to_thread(
        get_requests().set_status, request_id, body.status, body.handled_by, body.note
    )
    if row is None:
        raise HTTPException(status_code=404, detail="request not found")
    return {"id": row["id"], "status": row["status"], "handled_by": row["handled_by"]}
