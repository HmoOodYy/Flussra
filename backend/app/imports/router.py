"""PayrollWork source staging and bounded preview routes; no decision/apply API."""
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncEngine

from app.dependencies import get_current_user, get_engine
from app.imports.batch_service import (
    InterpretationRequest,
    MappingRequest,
    RegroupRequest,
    RevisionRequest,
    accept_mapping,
    build_preview,
    cancel_import,
    create_import,
    create_layout,
    read_import,
    update_interpretation,
)
from app.imports.readmodels import (
    ISSUE_DEFAULT,
    ISSUE_MAX,
    ROW_DEFAULT,
    ROW_MAX,
    SOURCE_DEFAULT,
    SOURCE_MAX,
    read_issues,
    read_rows,
    read_sources,
)
from app.imports.template import period_template

router = APIRouter(prefix="/payroll", tags=["Imports"])


@router.post("/periods/{period_id}/imports")
async def upload(period_id: int, request: Request,
                 idempotency_key: UUID = Header(alias="Idempotency-Key"),
                 user: dict = Depends(get_current_user), engine: AsyncEngine = Depends(get_engine)):
    return await create_import(request, engine, int(user["cid"]), int(user["sub"]), period_id, idempotency_key)


@router.get("/imports/{import_id}")
async def get_import(import_id: int, target_offset: int = Query(default=0, ge=0),
                     target_limit: int = Query(default=50, ge=1, le=100),
                     header_layout_id: int | None = Query(default=None, ge=1),
                     header_offset: int = Query(default=0, ge=0),
                     header_limit: int = Query(default=25, ge=1, le=25),
                     user: dict = Depends(get_current_user),
                     engine: AsyncEngine = Depends(get_engine)):
    return await read_import(engine, int(user["cid"]), int(user["sub"]), import_id,
                             target_offset=target_offset, target_limit=target_limit,
                             header_layout_id=header_layout_id, header_offset=header_offset,
                             header_limit=header_limit)


@router.put("/imports/{import_id}/interpretation")
async def change_interpretation(import_id: int, body: InterpretationRequest, request: Request,
                                user: dict = Depends(get_current_user), engine: AsyncEngine = Depends(get_engine)):
    return await update_interpretation(request, engine, int(user["cid"]), int(user["sub"]), import_id, body)


@router.post("/imports/{import_id}/layouts")
async def regroup_layout(import_id: int, body: RegroupRequest, request: Request,
                         user: dict = Depends(get_current_user), engine: AsyncEngine = Depends(get_engine)):
    return await create_layout(request, engine, int(user["cid"]), int(user["sub"]), import_id, body)


@router.put("/imports/{import_id}/layouts/{layout_id}/mapping")
async def change_mapping(import_id: int, layout_id: int, body: MappingRequest, request: Request,
                         user: dict = Depends(get_current_user), engine: AsyncEngine = Depends(get_engine)):
    return await accept_mapping(request, engine, int(user["cid"]), int(user["sub"]), import_id, layout_id, body)


@router.post("/imports/{import_id}/preview")
async def refresh_preview(import_id: int, body: RevisionRequest, request: Request,
                          user: dict = Depends(get_current_user), engine: AsyncEngine = Depends(get_engine)):
    return await build_preview(request, engine, int(user["cid"]), int(user["sub"]), import_id, body)


@router.get("/imports/{import_id}/rows")
async def rows(import_id: int, expected_revision: int = Query(ge=1), plan_fingerprint: str = Query(min_length=64, max_length=64),
               offset: int = Query(default=0, ge=0), limit: int = Query(default=ROW_DEFAULT, ge=1, le=ROW_MAX),
               severity: str | None = None, user: dict = Depends(get_current_user), engine: AsyncEngine = Depends(get_engine)):
    return await read_rows(engine, int(user["cid"]), int(user["sub"]), import_id, expected_revision,
                           plan_fingerprint, offset, limit, severity)


@router.get("/imports/{import_id}/issues")
async def issues(import_id: int, expected_revision: int = Query(ge=1), plan_fingerprint: str = Query(min_length=64, max_length=64),
                 offset: int = Query(default=0, ge=0), limit: int = Query(default=ISSUE_DEFAULT, ge=1, le=ISSUE_MAX),
                 severity: str | None = None, user: dict = Depends(get_current_user), engine: AsyncEngine = Depends(get_engine)):
    return await read_issues(engine, int(user["cid"]), int(user["sub"]), import_id, expected_revision,
                             plan_fingerprint, offset, limit, severity)


@router.get("/imports/{import_id}/source-records")
async def source_records(import_id: int, expected_revision: int = Query(ge=1), plan_fingerprint: str = Query(min_length=64, max_length=64),
                         offset: int = Query(default=0, ge=0), limit: int = Query(default=SOURCE_DEFAULT, ge=1, le=SOURCE_MAX),
                         cell_offset: int = Query(default=0, ge=0), cell_limit: int = Query(default=32, ge=1, le=50),
                         user: dict = Depends(get_current_user), engine: AsyncEngine = Depends(get_engine)):
    return await read_sources(engine, int(user["cid"]), int(user["sub"]), import_id, expected_revision,
                              plan_fingerprint, offset, limit, cell_offset, cell_limit)


@router.get("/periods/{period_id}/imports/template.xlsx")
async def template(period_id: int, request: Request,
                   definition_ids: list[int] | None = Query(default=None),
                   user: dict = Depends(get_current_user), engine: AsyncEngine = Depends(get_engine)):
    data = await period_template(request, engine, int(user["cid"]), int(user["sub"]), period_id, definition_ids)
    return Response(data, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": 'attachment; filename="payroll-work-template.xlsx"'})


@router.post("/imports/{import_id}/cancel")
async def cancel(import_id: int, body: RevisionRequest, user: dict = Depends(get_current_user),
                 engine: AsyncEngine = Depends(get_engine)):
    return await cancel_import(engine, int(user["cid"]), int(user["sub"]), import_id, body.expected_revision)
