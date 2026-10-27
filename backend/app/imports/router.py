"""PayrollWork source staging and bounded preview routes; no decision/apply API."""
from uuid import UUID

import anyio
from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import Response
from pydantic import ValidationError
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
    load_import,
    read_import,
    update_interpretation,
)
from app.imports.errors import ImportFailure
from app.imports.policy_metadata import ImportPolicyMetadataResponse, read_period_import_policy
from app.imports.readmodels import (
    read_issues,
    read_rows,
    read_sources,
)
from app.imports.runtime import admission, authorize
from app.imports.template import period_template
from app.imports.transport import receive_mapping_json

router = APIRouter(prefix="/payroll", tags=["Imports"])


def _inline_mapping_schema_refs(schema: dict, definitions: dict, active: frozenset[str] = frozenset()) -> dict:
    reference = schema.get("$ref")
    if isinstance(reference, str) and reference.startswith("#/$defs/"):
        name = reference.rsplit("/", 1)[-1]
        if name in active:
            return schema
        return _inline_mapping_schema_refs(definitions[name], definitions, active | {name})
    return {key: (_inline_mapping_schema_refs(value, definitions, active) if isinstance(value, dict)
                  else [_inline_mapping_schema_refs(item, definitions, active) if isinstance(item, dict) else item
                        for item in value] if isinstance(value, list) else value)
            for key, value in schema.items()}


def _mapping_request_body() -> dict:
    schema = MappingRequest.model_json_schema()
    definitions = schema.pop("$defs", {})
    return {"required": True, "content": {"application/json": {
        "schema": _inline_mapping_schema_refs(schema, definitions)}}}


@router.post("/periods/{period_id}/imports")
async def upload(period_id: int, request: Request,
                 idempotency_key: UUID = Header(alias="Idempotency-Key"),
                 user: dict = Depends(get_current_user), engine: AsyncEngine = Depends(get_engine)):
    return await create_import(request, engine, int(user["cid"]), int(user["sub"]), period_id, idempotency_key)


@router.get("/periods/{period_id}/imports/policy", response_model=ImportPolicyMetadataResponse)
async def effective_policy(period_id: int, request: Request,
                           user: dict = Depends(get_current_user), engine: AsyncEngine = Depends(get_engine)):
    settings = request.app.state.settings
    return await read_period_import_policy(
        engine, int(user["cid"]), int(user["sub"]), period_id,
        settings.product_policy, settings.import_resource_policy,
    )


@router.get("/imports/{import_id}")
async def get_import(import_id: int, request: Request, target_offset: int = Query(default=0, ge=0),
                     target_limit: int | None = Query(default=None, ge=1),
                     header_layout_id: int | None = Query(default=None, ge=1),
                     header_offset: int = Query(default=0, ge=0),
                     header_limit: int | None = Query(default=None, ge=1),
                     user: dict = Depends(get_current_user),
                     engine: AsyncEngine = Depends(get_engine)):
    policy = request.app.state.settings.import_resource_policy
    return await read_import(engine, int(user["cid"]), int(user["sub"]), import_id,
                             target_offset=target_offset, target_limit=target_limit,
                             header_layout_id=header_layout_id, header_offset=header_offset,
                             header_limit=header_limit, policy=policy)


@router.put("/imports/{import_id}/interpretation")
async def change_interpretation(import_id: int, body: InterpretationRequest, request: Request,
                                user: dict = Depends(get_current_user), engine: AsyncEngine = Depends(get_engine)):
    return await update_interpretation(request, engine, int(user["cid"]), int(user["sub"]), import_id, body)


@router.post("/imports/{import_id}/layouts")
async def regroup_layout(import_id: int, body: RegroupRequest, request: Request,
                         user: dict = Depends(get_current_user), engine: AsyncEngine = Depends(get_engine)):
    return await create_layout(request, engine, int(user["cid"]), int(user["sub"]), import_id, body)


@router.put("/imports/{import_id}/mapping", openapi_extra={"requestBody": _mapping_request_body()})
async def change_mapping(import_id: int, request: Request,
                         user: dict = Depends(get_current_user), engine: AsyncEngine = Depends(get_engine)):
    cid, uid = int(user["cid"]), int(user["sub"])
    async with engine.begin() as db:
        row = await load_import(db, cid, import_id)
        await authorize(db, cid, uid, row["payrollperiodid"], mutate=True)
    policy = request.app.state.settings.import_resource_policy
    async with admission(request):
        payload = await receive_mapping_json(request, policy.max_mapping_request_bytes)
        try:
            body = await anyio.to_thread.run_sync(MappingRequest.model_validate_json, payload)
        except ValidationError as exc:
            raise ImportFailure("IMPORT_MAPPING_REQUEST_INVALID",
                                "Mapping request does not match the supported target mapping format.", 422) from exc
        return await accept_mapping(request, engine, cid, uid, import_id, body)


@router.post("/imports/{import_id}/preview")
async def refresh_preview(import_id: int, body: RevisionRequest, request: Request,
                          user: dict = Depends(get_current_user), engine: AsyncEngine = Depends(get_engine)):
    return await build_preview(request, engine, int(user["cid"]), int(user["sub"]), import_id, body)


@router.get("/imports/{import_id}/rows")
async def rows(import_id: int, request: Request, expected_revision: int = Query(ge=1),
               plan_fingerprint: str = Query(min_length=64, max_length=64),
               offset: int = Query(default=0, ge=0), limit: int | None = Query(default=None, ge=1),
               severity: str | None = None, user: dict = Depends(get_current_user), engine: AsyncEngine = Depends(get_engine)):
    policy = request.app.state.settings.import_resource_policy
    limit = policy.row_page_default if limit is None else limit
    return await read_rows(engine, int(user["cid"]), int(user["sub"]), import_id, expected_revision,
                           plan_fingerprint, offset, limit, severity, policy=policy)


@router.get("/imports/{import_id}/issues")
async def issues(import_id: int, request: Request, expected_revision: int = Query(ge=1),
                 plan_fingerprint: str = Query(min_length=64, max_length=64),
                 offset: int = Query(default=0, ge=0), limit: int | None = Query(default=None, ge=1),
                 severity: str | None = None, user: dict = Depends(get_current_user), engine: AsyncEngine = Depends(get_engine)):
    policy = request.app.state.settings.import_resource_policy
    limit = policy.issue_page_default if limit is None else limit
    return await read_issues(engine, int(user["cid"]), int(user["sub"]), import_id, expected_revision,
                             plan_fingerprint, offset, limit, severity, policy=policy)


@router.get("/imports/{import_id}/source-records")
async def source_records(import_id: int, request: Request, expected_revision: int = Query(ge=1),
                         plan_fingerprint: str = Query(min_length=64, max_length=64),
                         offset: int = Query(default=0, ge=0), limit: int | None = Query(default=None, ge=1),
                         cell_offset: int = Query(default=0, ge=0), cell_limit: int | None = Query(default=None, ge=1),
                         user: dict = Depends(get_current_user), engine: AsyncEngine = Depends(get_engine)):
    policy = request.app.state.settings.import_resource_policy
    limit = policy.source_page_default if limit is None else limit
    cell_limit = policy.source_cell_page_default if cell_limit is None else cell_limit
    return await read_sources(engine, int(user["cid"]), int(user["sub"]), import_id, expected_revision,
                              plan_fingerprint, offset, limit, cell_offset, cell_limit, policy=policy)


@router.get("/periods/{period_id}/imports/template.xlsx")
async def template(period_id: int, request: Request,
                   definition_ids: list[int] | None = Query(default=None),
                   user: dict = Depends(get_current_user), engine: AsyncEngine = Depends(get_engine)):
    data = await period_template(request, engine, int(user["cid"]), int(user["sub"]), period_id, definition_ids)
    return Response(data, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": 'attachment; filename="payroll-work-template.xlsx"'})


@router.post("/imports/{import_id}/cancel")
async def cancel(import_id: int, body: RevisionRequest, request: Request, user: dict = Depends(get_current_user),
                 engine: AsyncEngine = Depends(get_engine)):
    policy = request.app.state.settings.import_resource_policy
    return await cancel_import(engine, int(user["cid"]), int(user["sub"]), import_id,
                               body.expected_revision, policy=policy)
