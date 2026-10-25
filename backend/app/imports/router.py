"""PayrollWork File staging, mapping and read-only preview; no decision/apply routes."""
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from sqlalchemy.ext.asyncio import AsyncEngine

from app.dependencies import get_current_user, get_engine
from app.imports.preview_service import (
    ISSUE_PAGE_DEFAULT,
    ISSUE_PAGE_MAX,
    ROW_PAGE_DEFAULT,
    ROW_PAGE_MAX,
    map_or_refresh,
    read_page,
)
from app.imports.service import (
    InterpretationRequest,
    RevisionRequest,
    cancel_import,
    create_import,
    read_import,
    reinterpret_import,
)
from app.imports.template import period_template

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


@router.post("/imports/{import_id}/refresh")
async def refresh(import_id: int, body: RevisionRequest, request: Request,
                  user: dict = Depends(get_current_user), engine: AsyncEngine = Depends(get_engine)):
    return await map_or_refresh(request, engine, int(user["cid"]), int(user["sub"]), import_id, body, refresh=True)


@router.get("/imports/{import_id}/rows")
async def preview_rows(import_id: int, expected_revision: int = Query(ge=1),
                       plan_fingerprint: str = Query(min_length=64, max_length=64, pattern="^[0-9a-f]{64}$"),
                       offset: int = Query(default=0, ge=0, le=2_147_483_647),
                       limit: int = Query(default=ROW_PAGE_DEFAULT, ge=1, le=ROW_PAGE_MAX),
                       severity: Literal["Error", "Warning", "Conflict"] | None = None,
                       user: dict = Depends(get_current_user), engine: AsyncEngine = Depends(get_engine)):
    return await read_page(engine, int(user["cid"]), int(user["sub"]), import_id,
                           expected_revision, plan_fingerprint, offset, limit, severity)


@router.get("/imports/{import_id}/issues")
async def preview_issues(import_id: int, expected_revision: int = Query(ge=1),
                         plan_fingerprint: str = Query(min_length=64, max_length=64, pattern="^[0-9a-f]{64}$"),
                         offset: int = Query(default=0, ge=0, le=2_147_483_647),
                         limit: int = Query(default=ISSUE_PAGE_DEFAULT, ge=1, le=ISSUE_PAGE_MAX),
                         severity: Literal["Error", "Warning", "Conflict"] | None = None,
                         user: dict = Depends(get_current_user), engine: AsyncEngine = Depends(get_engine)):
    return await read_page(engine, int(user["cid"]), int(user["sub"]), import_id,
                           expected_revision, plan_fingerprint, offset, limit, severity, issues=True)


@router.get("/periods/{period_id}/imports/template.xlsx")
async def download_template(period_id: int, request: Request,
                            user: dict = Depends(get_current_user), engine: AsyncEngine = Depends(get_engine)):
    data = await period_template(request, engine, int(user["cid"]), int(user["sub"]), period_id)
    return Response(content=data, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": 'attachment; filename="payroll-template.xlsx"'})
