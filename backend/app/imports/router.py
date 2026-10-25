"""PayrollWork File staging only; no mapping, decisions, preview or apply routes."""
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request
from sqlalchemy.ext.asyncio import AsyncEngine

from app.dependencies import get_current_user, get_engine
from app.imports.service import (
    InterpretationRequest,
    RevisionRequest,
    cancel_import,
    create_import,
    read_import,
    reinterpret_import,
)

router = APIRouter(prefix="/payroll", tags=["Imports"])


@router.post("/periods/{period_id}/imports")
async def upload(period_id: int, request: Request,
                 idempotency_key: UUID = Header(alias="Idempotency-Key"),
                 user: dict = Depends(get_current_user), engine: AsyncEngine = Depends(get_engine)):
    return await create_import(request, engine, int(user["cid"]), int(user["sub"]), period_id, idempotency_key)


@router.get("/imports/{import_id}")
async def get_import(import_id: int, user: dict = Depends(get_current_user),
                     engine: AsyncEngine = Depends(get_engine)):
    return await read_import(engine, int(user["cid"]), int(user["sub"]), import_id)


@router.put("/imports/{import_id}/mapping")
async def change_interpretation(import_id: int, body: InterpretationRequest, request: Request,
                                user: dict = Depends(get_current_user), engine: AsyncEngine = Depends(get_engine)):
    return await reinterpret_import(request, engine, int(user["cid"]), int(user["sub"]), import_id, body)


@router.post("/imports/{import_id}/cancel")
async def cancel(import_id: int, body: RevisionRequest, user: dict = Depends(get_current_user),
                 engine: AsyncEngine = Depends(get_engine)):
    return await cancel_import(engine, int(user["cid"]), int(user["sub"]), import_id, body.expected_revision)
