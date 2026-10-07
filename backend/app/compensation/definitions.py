"""Generic PayDefinition governance and provenance.

A PayDefinition is Company-owned. It is created either from a Branch-scoped
request that a Company reviewer approves, or directly by a Company-wide
administrator. Both paths produce the same ordinary definition with its
RateDefinition structure and an immutable provenance record. Neither path
touches Branch applicability, RateTypes, legacy PayItems or legacy rate slots.

Transactions are owned by the get_db dependency: they commit on success and
roll back when an exception propagates.
"""

import uuid as _uuid
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection

from app.access.policy import require_non_driver_subject
from app.compensation.audit import write_audit
from app.compensation.branch_config import apply_config
from app.compensation.errors import compensation_error, translate_database_error
from app.compensation.guards import (
    require_definition_branch_edit,
    require_definition_company_edit,
    require_definition_company_read,
)
from app.compensation.schemas import (
    DecisionAction,
    PayDefinitionDirectCreate,
    PayDefinitionProvenance,
    PayDefinitionRequestCreate,
    PayDefinitionRequestDecision,
    PayDefinitionRequestEvent,
    PayDefinitionRequestSubmit,
    PayDefinitionRequestSummary,
    PayDefinitionRequestUpdate,
    PayDefinitionSummary,
    RateComponentSummary,
)
from app.core.service import _check_branch_access

GOVERNANCE_SCHEMA_VERSION = 1
CALCULATION_METHOD_VERSION = 1

# Methods whose structure and assignment authoring exist in this release.
AUTHORABLE_METHODS = frozenset({"PerUnit"})

_REQUEST_COLUMNS = """
    r.paydefinitionrequestid AS request_id,
    r.companyid              AS company_id,
    r.requestingbranchid     AS requesting_branch_id,
    r.definitioncode         AS definition_code,
    r.definitionname         AS definition_name,
    r.inputtype              AS input_type,
    r.unit                   AS unit,
    r.calculationmethod      AS calculation_method,
    r.notes                  AS notes,
    r.status                 AS status,
    r.revision               AS revision,
    r.approvedpaydefinitionid AS approved_pay_definition_id,
    r.copiedfromrequestid    AS copied_from_request_id,
    r.submittedbyuserid      AS submitted_by_user_id,
    r.submittedatutc         AS submitted_at_utc,
    r.createdbyuserid        AS created_by_user_id,
    r.createdatutc           AS created_at_utc,
    r.updatedbyuserid        AS updated_by_user_id,
    r.updatedatutc           AS updated_at_utc
"""

_DRAFT_COLUMNS = {
    "definition_code": "definitioncode",
    "definition_name": "definitionname",
    "input_type": "inputtype",
    "unit": "unit",
    "calculation_method": "calculationmethod",
    "notes": "notes",
}


def _request_not_found() -> HTTPException:
    return compensation_error("REQUEST_NOT_FOUND", "PayDefinition request not found.", 404)


async def _load_request(
    company_id: int, request_id: UUID, db: AsyncConnection, *, for_update: bool = False,
) -> dict:
    row = (await db.execute(
        text(f"""
            SELECT {_REQUEST_COLUMNS}
            FROM   payroll.paydefinitionrequests r
            WHERE  r.paydefinitionrequestid = :rid AND r.companyid = :cid
            {"FOR UPDATE OF r" if for_update else ""}
        """),
        {"rid": str(request_id), "cid": company_id},
    )).mappings().first()
    if row is None:
        raise _request_not_found()
    return dict(row)


async def _summary(company_id: int, request_id: UUID, db: AsyncConnection) -> PayDefinitionRequestSummary:
    return PayDefinitionRequestSummary.model_validate(await _load_request(company_id, request_id, db))


async def _insert_event(
    db: AsyncConnection, request_id: UUID | str, event_type: str, from_status: str | None,
    to_status: str, user_id: int, revision: int, reason: str | None = None,
) -> None:
    await db.execute(
        text("""
            INSERT INTO payroll.paydefinitionrequestevents
                (paydefinitionrequestid, eventtype, fromstatus, tostatus,
                 actoruserid, reason, requestrevision)
            VALUES (:rid, :etype, :from_status, :to_status, :uid, :reason, :rev)
        """),
        {"rid": str(request_id), "etype": event_type, "from_status": from_status,
         "to_status": to_status, "uid": user_id, "reason": reason, "rev": revision},
    )


async def _require_visible_branch(
    company_id: int, user_id: int, branch_id: int, db: AsyncConnection,
) -> None:
    can_see_all, branch_ids = await _check_branch_access(company_id, user_id, db)
    if not can_see_all and branch_id not in branch_ids:
        raise compensation_error(
            "BRANCH_ACCESS_DENIED",
            "You do not have access to this branch's PayDefinition requests.",
            403,
        )


async def create_draft(
    company_id: int, user_id: int, data: PayDefinitionRequestCreate, db: AsyncConnection,
) -> PayDefinitionRequestSummary:
    await require_definition_branch_edit(company_id, user_id, data.requesting_branch_id, db)
    request_id = (await db.execute(
        text("""
            INSERT INTO payroll.paydefinitionrequests
                (companyid, requestingbranchid, definitioncode, definitionname, inputtype,
                 unit, calculationmethod, notes, createdbyuserid)
            VALUES (:cid, :bid, :code, :name, :input_type, :unit, :method, :notes, :uid)
            RETURNING paydefinitionrequestid
        """),
        {"cid": company_id, "bid": data.requesting_branch_id, "code": data.definition_code,
         "name": data.definition_name, "input_type": data.input_type, "unit": data.unit,
         "method": data.calculation_method, "notes": data.notes, "uid": user_id},
    )).scalar_one()
    await _insert_event(db, request_id, "DraftCreated", None, "Draft", user_id, 1)
    return await _summary(company_id, request_id, db)


async def get_request(
    company_id: int, user_id: int, request_id: UUID, db: AsyncConnection,
) -> PayDefinitionRequestSummary:
    await require_non_driver_subject(company_id, user_id, db)
    row = await _load_request(company_id, request_id, db)
    await _require_visible_branch(company_id, user_id, row["requesting_branch_id"], db)
    return PayDefinitionRequestSummary.model_validate(row)


async def list_requests(
    company_id: int, user_id: int, db: AsyncConnection, *,
    status_filter: str | None = None, branch_id: int | None = None,
) -> list[PayDefinitionRequestSummary]:
    await require_non_driver_subject(company_id, user_id, db)
    can_see_all, branch_ids = await _check_branch_access(company_id, user_id, db)
    where = ["r.companyid = :cid"]
    params: dict = {"cid": company_id}
    if not can_see_all:
        if not branch_ids:
            return []
        where.append("r.requestingbranchid = ANY(:visible)")
        params["visible"] = list(branch_ids)
    if branch_id is not None:
        where.append("r.requestingbranchid = :bid")
        params["bid"] = branch_id
    if status_filter is not None:
        where.append("r.status = :status")
        params["status"] = status_filter
    rows = (await db.execute(
        text(f"""
            SELECT {_REQUEST_COLUMNS}
            FROM   payroll.paydefinitionrequests r
            WHERE  {" AND ".join(where)}
            ORDER  BY r.createdatutc DESC, r.paydefinitionrequestid
        """),
        params,
    )).mappings().all()
    return [PayDefinitionRequestSummary.model_validate(dict(row)) for row in rows]


async def list_events(
    company_id: int, user_id: int, request_id: UUID, db: AsyncConnection,
) -> list[PayDefinitionRequestEvent]:
    request = await get_request(company_id, user_id, request_id, db)
    rows = (await db.execute(
        text("""
            SELECT eventid AS event_id, paydefinitionrequestid AS request_id,
                   eventtype AS event_type, fromstatus AS from_status, tostatus AS to_status,
                   actoruserid AS actor_user_id, reason, requestrevision AS request_revision,
                   occurredatutc AS occurred_at_utc
            FROM   payroll.paydefinitionrequestevents
            WHERE  paydefinitionrequestid = :rid
            ORDER  BY occurredatutc, requestrevision, eventid
        """),
        {"rid": str(request.request_id)},
    )).mappings().all()
    return [PayDefinitionRequestEvent.model_validate(dict(row)) for row in rows]


async def _diagnose_conflict(
    company_id: int, request_id: UUID, db: AsyncConnection, expected_status: str,
    expected_revision: int, action: str,
) -> HTTPException:
    row = (await db.execute(
        text("""
            SELECT status, revision FROM payroll.paydefinitionrequests
            WHERE  paydefinitionrequestid = :rid AND companyid = :cid
        """),
        {"rid": str(request_id), "cid": company_id},
    )).mappings().first()
    if row is None:
        return _request_not_found()
    if row["status"] != expected_status:
        return compensation_error(
            "REQUEST_STATUS_CONFLICT",
            f"Only {expected_status} requests can be {action}. Current status: {row['status']}.",
            422,
        )
    return compensation_error(
        "REQUEST_REVISION_CONFLICT",
        f"Revision mismatch: expected {expected_revision}, current is {row['revision']}. "
        "Reload and retry.",
        409,
    )


async def update_draft(
    company_id: int, user_id: int, request_id: UUID, data: PayDefinitionRequestUpdate,
    db: AsyncConnection,
) -> PayDefinitionRequestSummary:
    pre = await _load_request(company_id, request_id, db)
    await require_definition_branch_edit(company_id, user_id, pre["requesting_branch_id"], db)

    set_parts = ["revision = revision + 1", "updatedbyuserid = :uid", "updatedatutc = now()"]
    params: dict = {"rid": str(request_id), "cid": company_id, "uid": user_id,
                    "expected": data.expected_revision}
    for field, column in _DRAFT_COLUMNS.items():
        if field in data.model_fields_set:
            set_parts.append(f"{column} = :{field}")
            params[field] = getattr(data, field)
    updated = (await db.execute(
        text(f"""
            UPDATE payroll.paydefinitionrequests
            SET    {", ".join(set_parts)}
            WHERE  paydefinitionrequestid = :rid AND companyid = :cid
              AND  status = 'Draft' AND revision = :expected
            RETURNING paydefinitionrequestid
        """),
        params,
    )).scalar_one_or_none()
    if updated is None:
        raise await _diagnose_conflict(
            company_id, request_id, db, "Draft", data.expected_revision, "updated")
    return await _summary(company_id, request_id, db)


def _missing_fields(row: dict) -> list[str]:
    missing = []
    if not (row["definition_name"] or "").strip():
        missing.append("definition_name")
    if not row["input_type"]:
        missing.append("input_type")
    if not row["calculation_method"]:
        missing.append("calculation_method")
    return missing


def _require_authorable(method: str | None, action: str) -> None:
    if method not in AUTHORABLE_METHODS:
        raise compensation_error(
            "CALCULATION_METHOD_NOT_AVAILABLE",
            f"CalculationMethod '{method}' is not available for {action}. "
            "Only PerUnit definitions can be created at this time.",
            422,
        )


async def submit_draft(
    company_id: int, user_id: int, request_id: UUID, data: PayDefinitionRequestSubmit,
    db: AsyncConnection,
) -> PayDefinitionRequestSummary:
    pre = await _load_request(company_id, request_id, db)
    await require_definition_branch_edit(company_id, user_id, pre["requesting_branch_id"], db)
    missing = _missing_fields(pre)
    if missing:
        raise compensation_error(
            "REQUEST_INCOMPLETE",
            f"Cannot submit: missing required fields: {', '.join(missing)}.", 422)
    _require_authorable(pre["calculation_method"], "submission")

    prior = (await db.execute(
        text("""
            SELECT 1 FROM payroll.paydefinitionrequestevents
            WHERE  paydefinitionrequestid = :rid AND eventtype IN ('Submitted', 'Resubmitted')
            LIMIT  1
        """),
        {"rid": str(request_id)},
    )).scalar_one_or_none()
    revision = (await db.execute(
        text("""
            UPDATE payroll.paydefinitionrequests
            SET    status = 'PendingCompanyApproval', submittedbyuserid = :uid,
                   submittedatutc = now(), revision = revision + 1,
                   updatedbyuserid = :uid, updatedatutc = now()
            WHERE  paydefinitionrequestid = :rid AND companyid = :cid
              AND  status = 'Draft' AND revision = :expected
            RETURNING revision
        """),
        {"rid": str(request_id), "cid": company_id, "uid": user_id,
         "expected": data.expected_revision},
    )).scalar_one_or_none()
    if revision is None:
        raise await _diagnose_conflict(
            company_id, request_id, db, "Draft", data.expected_revision, "submitted")
    await _insert_event(
        db, request_id, "Resubmitted" if prior else "Submitted",
        "Draft", "PendingCompanyApproval", user_id, revision)
    return await _summary(company_id, request_id, db)


async def decide_request(
    company_id: int, user_id: int, request_id: UUID, data: PayDefinitionRequestDecision,
    db: AsyncConnection,
) -> PayDefinitionRequestSummary:
    await _load_request(company_id, request_id, db)
    await require_definition_company_edit(company_id, user_id, db)
    if data.action == DecisionAction.Approve:
        return await _approve_request(company_id, user_id, request_id, data, db)

    new_status, event_type = (
        ("Draft", "ReturnedToDraft") if data.action == DecisionAction.ReturnToDraft
        else ("Rejected", "Rejected")
    )
    revision = (await db.execute(
        text("""
            UPDATE payroll.paydefinitionrequests
            SET    status = :new_status, updatedbyuserid = :uid, updatedatutc = now(),
                   revision = revision + 1
            WHERE  paydefinitionrequestid = :rid AND companyid = :cid
              AND  status = 'PendingCompanyApproval' AND revision = :expected
            RETURNING revision
        """),
        {"rid": str(request_id), "cid": company_id, "uid": user_id,
         "new_status": new_status, "expected": data.expected_revision},
    )).scalar_one_or_none()
    if revision is None:
        raise await _diagnose_conflict(
            company_id, request_id, db, "PendingCompanyApproval", data.expected_revision,
            "decided")
    await _insert_event(
        db, request_id, event_type, "PendingCompanyApproval", new_status, user_id,
        revision, data.reason)
    return await _summary(company_id, request_id, db)


def _generated_code() -> str:
    return f"PD{_uuid.uuid4().hex[:12].upper()}"


async def _create_definition_structure(
    db: AsyncConnection, *, company_id: int, user_id: int, code: str | None, name: str,
    input_type: str, unit: str | None, method: str,
) -> tuple[int, int]:
    """Create the PayDefinition with its scalar RateDefinition and required component.

    The governed event is the first authoritative use of the structure, so the
    canonical RateDefinitions.StructureLockedAtUtc is set here and nowhere else.
    """
    pay_definition_id = (await db.execute(
        text("""
            INSERT INTO payroll.paydefinitions
                (companyid, definitioncode, definitionname, inputtype, unit,
                 calculationmethod, createdbyuserid)
            VALUES (:cid, :code, :name, :input_type, :unit, :method, :uid)
            RETURNING paydefinitionid
        """),
        {"cid": company_id, "code": code or _generated_code(), "name": name.strip(),
         "input_type": input_type, "unit": unit, "method": method, "uid": user_id},
    )).scalar_one()
    rate_definition_id = (await db.execute(
        text("""
            INSERT INTO payroll.ratedefinitions (companyid, paydefinitionid, shape)
            VALUES (:cid, :pid, payroll.fn_shapeforcalculationmethod(:method))
            RETURNING ratedefinitionid
        """),
        {"cid": company_id, "pid": pay_definition_id, "method": method},
    )).scalar_one()
    await db.execute(
        text("""
            INSERT INTO payroll.ratecomponentdefinitions (ratedefinitionid, shape, sequenceno)
            VALUES (:rid, 'Scalar', 1)
        """),
        {"rid": rate_definition_id},
    )
    await db.execute(
        text("UPDATE payroll.ratedefinitions SET structurelockedatutc = now() "
             "WHERE ratedefinitionid = :rid"),
        {"rid": rate_definition_id},
    )
    return int(pay_definition_id), int(rate_definition_id)


async def _approve_request(
    company_id: int, user_id: int, request_id: UUID, data: PayDefinitionRequestDecision,
    db: AsyncConnection,
) -> PayDefinitionRequestSummary:
    row = await _load_request(company_id, request_id, db, for_update=True)
    if row["status"] != "PendingCompanyApproval":
        raise await _diagnose_conflict(
            company_id, request_id, db, "PendingCompanyApproval", data.expected_revision,
            "approved")
    if row["revision"] != data.expected_revision:
        raise await _diagnose_conflict(
            company_id, request_id, db, "PendingCompanyApproval", data.expected_revision,
            "approved")
    missing = _missing_fields(row)
    if missing:
        raise compensation_error(
            "REQUEST_INCOMPLETE",
            f"Cannot approve: missing required fields: {', '.join(missing)}.", 422)
    _require_authorable(row["calculation_method"], "approval")

    try:
        pay_definition_id, _ = await _create_definition_structure(
            db, company_id=company_id, user_id=user_id, code=row["definition_code"],
            name=row["definition_name"], input_type=row["input_type"], unit=row["unit"],
            method=row["calculation_method"])
    except DBAPIError as exc:
        translated = translate_database_error(exc)
        if translated is not None:
            raise translated from exc
        raise

    revision = (await db.execute(
        text("""
            UPDATE payroll.paydefinitionrequests
            SET    status = 'Approved', approvedpaydefinitionid = :pid,
                   updatedbyuserid = :uid, updatedatutc = now(), revision = revision + 1
            WHERE  paydefinitionrequestid = :rid AND companyid = :cid
              AND  status = 'PendingCompanyApproval' AND revision = :expected
            RETURNING revision
        """),
        {"rid": str(request_id), "cid": company_id, "uid": user_id,
         "pid": pay_definition_id, "expected": data.expected_revision},
    )).scalar_one_or_none()
    if revision is None:
        raise await _diagnose_conflict(
            company_id, request_id, db, "PendingCompanyApproval", data.expected_revision,
            "approved")

    await db.execute(
        text("""
            INSERT INTO payroll.paydefinitionprovenance
                (paydefinitionid, companyid, creationmode, sourcerequestid, requestingbranchid,
                 createdbyuserid, submittedbyuserid, submittedatutc, approvedbyuserid,
                 approvedatutc, governanceschemaversion, calculationmethodversion)
            VALUES (:pid, :cid, 'Request', :rid, :bid, :creator, :submitter, :submitted_at,
                    :uid, now(), :gv, :mv)
        """),
        {"pid": pay_definition_id, "cid": company_id, "rid": str(request_id),
         "bid": row["requesting_branch_id"], "creator": row["created_by_user_id"],
         "submitter": row["submitted_by_user_id"], "submitted_at": row["submitted_at_utc"],
         "uid": user_id, "gv": GOVERNANCE_SCHEMA_VERSION, "mv": CALCULATION_METHOD_VERSION},
    )
    today = (await db.execute(
        text("SELECT core.fn_CompanyToday(:cid)"), {"cid": company_id})).scalar_one()
    await apply_config(
        db, company_id=company_id, branch_id=row["requesting_branch_id"],
        pay_definition_id=pay_definition_id, user_id=user_id, is_active=True, notes=None,
        effective_from=today)
    await _insert_event(
        db, request_id, "Approved", "PendingCompanyApproval", "Approved", user_id,
        revision, data.reason)
    return await _summary(company_id, request_id, db)


async def copy_rejected(
    company_id: int, user_id: int, request_id: UUID, db: AsyncConnection,
) -> PayDefinitionRequestSummary:
    source = await _load_request(company_id, request_id, db)
    await require_definition_branch_edit(
        company_id, user_id, source["requesting_branch_id"], db)
    if source["status"] != "Rejected":
        raise compensation_error(
            "REQUEST_STATUS_CONFLICT",
            f"Only Rejected requests can be copied. Current status: {source['status']}.", 422)
    new_id = (await db.execute(
        text("""
            INSERT INTO payroll.paydefinitionrequests
                (companyid, requestingbranchid, definitioncode, definitionname, inputtype,
                 unit, calculationmethod, notes, copiedfromrequestid, createdbyuserid)
            VALUES (:cid, :bid, :code, :name, :input_type, :unit, :method, :notes, :src, :uid)
            RETURNING paydefinitionrequestid
        """),
        {"cid": company_id, "bid": source["requesting_branch_id"],
         "code": source["definition_code"], "name": source["definition_name"],
         "input_type": source["input_type"], "unit": source["unit"],
         "method": source["calculation_method"], "notes": source["notes"],
         "src": str(request_id), "uid": user_id},
    )).scalar_one()
    await _insert_event(db, new_id, "CopiedFromRejected", None, "Draft", user_id, 1)
    return await _summary(company_id, new_id, db)


async def create_direct(
    company_id: int, user_id: int, data: PayDefinitionDirectCreate, db: AsyncConnection,
) -> PayDefinitionSummary:
    await require_definition_company_edit(company_id, user_id, db)
    _require_authorable(data.calculation_method, "direct creation")
    try:
        pay_definition_id, _ = await _create_definition_structure(
            db, company_id=company_id, user_id=user_id, code=data.definition_code,
            name=data.definition_name, input_type=data.input_type, unit=data.unit,
            method=data.calculation_method)
        await db.execute(
            text("""
                INSERT INTO payroll.paydefinitionprovenance
                    (paydefinitionid, companyid, creationmode, createdbyuserid,
                     governanceschemaversion, calculationmethodversion)
                VALUES (:pid, :cid, 'DirectCreate', :uid, :gv, :mv)
            """),
            {"pid": pay_definition_id, "cid": company_id, "uid": user_id,
             "gv": GOVERNANCE_SCHEMA_VERSION, "mv": CALCULATION_METHOD_VERSION},
        )
    except DBAPIError as exc:
        translated = translate_database_error(exc)
        if translated is not None:
            raise translated from exc
        raise
    return await _load_definition(company_id, pay_definition_id, db)


async def _load_definition(
    company_id: int, pay_definition_id: int, db: AsyncConnection,
) -> PayDefinitionSummary:
    row = (await db.execute(
        text("""
            SELECT pd.paydefinitionid AS pay_definition_id, pd.companyid AS company_id,
                   pd.definitioncode AS definition_code, pd.definitionname AS definition_name,
                   pd.inputtype AS input_type, pd.unit AS unit,
                   pd.calculationmethod AS calculation_method, pd.status AS status,
                   rd.ratedefinitionid AS rate_definition_id, rd.shape AS rate_shape,
                   rd.structurelockedatutc AS structure_locked_at_utc
            FROM   payroll.paydefinitions pd
            LEFT JOIN payroll.ratedefinitions rd ON rd.paydefinitionid = pd.paydefinitionid
            WHERE  pd.paydefinitionid = :pid AND pd.companyid = :cid
        """),
        {"pid": pay_definition_id, "cid": company_id},
    )).mappings().first()
    if row is None:
        raise compensation_error("DEFINITION_NOT_FOUND", "PayDefinition not found.", 404)
    summary = dict(row)
    if summary["rate_definition_id"] is not None:
        components = (await db.execute(
            text("""
                SELECT ratecomponentdefinitionid AS rate_component_definition_id,
                       sequenceno AS sequence_no, ordinalfrom AS ordinal_from,
                       ordinalto AS ordinal_to
                FROM   payroll.ratecomponentdefinitions
                WHERE  ratedefinitionid = :rid
                ORDER  BY sequenceno
            """),
            {"rid": summary["rate_definition_id"]},
        )).mappings().all()
        summary["components"] = [RateComponentSummary.model_validate(dict(c)) for c in components]
    provenance = (await db.execute(
        text("""
            SELECT creationmode AS creation_mode, sourcerequestid AS source_request_id,
                   requestingbranchid AS requesting_branch_id,
                   createdbyuserid AS created_by_user_id,
                   submittedbyuserid AS submitted_by_user_id,
                   submittedatutc AS submitted_at_utc,
                   approvedbyuserid AS approved_by_user_id, approvedatutc AS approved_at_utc,
                   governanceschemaversion AS governance_schema_version,
                   calculationmethodversion AS calculation_method_version,
                   createdatutc AS created_at_utc
            FROM   payroll.paydefinitionprovenance
            WHERE  paydefinitionid = :pid
        """),
        {"pid": pay_definition_id},
    )).mappings().first()
    if provenance is not None:
        summary["provenance"] = PayDefinitionProvenance.model_validate(dict(provenance))
    return PayDefinitionSummary.model_validate(summary)


async def get_definition(
    company_id: int, user_id: int, pay_definition_id: int, db: AsyncConnection,
) -> PayDefinitionSummary:
    await require_definition_company_read(company_id, user_id, db)
    return await _load_definition(company_id, pay_definition_id, db)


async def list_definitions(
    company_id: int, user_id: int, db: AsyncConnection, *, include_retired: bool = True,
) -> list[PayDefinitionSummary]:
    await require_definition_company_read(company_id, user_id, db)
    ids = (await db.execute(
        text("SELECT paydefinitionid FROM payroll.paydefinitions "
             "WHERE companyid = :cid AND (:include_retired OR status <> 'Retired') "
             "ORDER BY lower(definitionname), definitioncode, paydefinitionid"),
        {"cid": company_id, "include_retired": include_retired},
    )).scalars().all()
    return [await _load_definition(company_id, int(i), db) for i in ids]


async def retire_definition(
    company_id: int, user_id: int, pay_definition_id: int, db: AsyncConnection,
) -> PayDefinitionSummary:
    """Retire a PayDefinition. Nothing is deleted: structure, provenance, rate
    assignments and branch configuration history are preserved."""
    await require_definition_company_edit(company_id, user_id, db)
    row = (await db.execute(
        text("""
            SELECT status FROM payroll.paydefinitions
            WHERE  paydefinitionid = :pid AND companyid = :cid
            FOR UPDATE
        """),
        {"pid": pay_definition_id, "cid": company_id},
    )).mappings().first()
    if row is None:
        raise compensation_error("DEFINITION_NOT_FOUND", "PayDefinition not found.", 404)
    if row["status"] != "Retired":
        await db.execute(
            text("""
                UPDATE payroll.paydefinitions
                SET    status = 'Retired', updatedbyuserid = :uid, updatedatutc = now()
                WHERE  paydefinitionid = :pid
            """),
            {"uid": user_id, "pid": pay_definition_id},
        )
        await write_audit(
            db, company_id=company_id, branch_id=None, user_id=user_id,
            action_code="PAY_DEFINITION_RETIRED", entity_name="PayDefinitions",
            entity_id=str(pay_definition_id), old_value={"status": row["status"]},
            new_value={"status": "Retired"})
    return await _load_definition(company_id, pay_definition_id, db)
