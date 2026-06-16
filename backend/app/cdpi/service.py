"""
CDPI (Custom Daily Pay Item) -- service layer.

All functions use raw parameterized SQL via sqlalchemy.text().
Transactions are managed by the get_db() dependency (engine.begin()),
which auto-commits on success and rolls back on exception.
"""
import uuid as _uuid
from datetime import date as _date, timedelta as _timedelta
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.service import _check_branch_access
from app.cdpi.guards import require_cdpi_branch_edit, require_cdpi_company_edit
from app.cdpi.methods import get_adapter
from app.cdpi.schemas import (
    CdpiRequestCreate,
    CdpiRequestSummary,
    CdpiRequestUpdate,
    CdpiSubmitRequest,
    CdpiDecideRequest,
    CdpiDecideAction,
    CdpiDirectCreateRequest,
    CdpiDirectCreateSummary,
    CdpiBranchItemState,
    CdpiBranchItemUpdate,
)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

# Maps CdpiRequests.InputType to the corresponding PayItems.DataType value.
# InputType='Time' items store time-based quantities; InputType='Number' items
# store dimensionless decimal quantities.
_INPUT_TYPE_TO_DATATYPE: dict[str, str] = {
    "Time":   "Time",
    "Number": "Decimal",
}


def _datatype_for_input_type(input_type: str) -> str:
    """Return the PayItems.DataType value for a CDPI InputType string."""
    return _INPUT_TYPE_TO_DATATYPE.get(input_type, "Decimal")


_SELECT_COLS = """
    r.requestid            AS request_id,
    r.companyid            AS company_id,
    r.requestingbranchid   AS requesting_branch_id,
    r.itemname             AS item_name,
    r.inputtype            AS input_type,
    r.unit                 AS unit,
    r.calcmethodkey        AS calc_method_key,
    r.notes                AS notes,
    r.status               AS status,
    r.revision             AS revision,
    r.approvedpayitemid    AS approved_pay_item_id,
    r.copiedfromrequestid  AS copied_from_request_id,
    r.submittedbyuserid    AS submitted_by_user_id,
    r.submittedatutc       AS submitted_at_utc,
    r.createdbyuserid      AS created_by_user_id,
    r.createdatutc         AS created_at_utc,
    r.updatedbyuserid      AS updated_by_user_id,
    r.updatedatutc         AS updated_at_utc
""".strip()


async def _load_request(request_id: UUID, db: AsyncConnection) -> CdpiRequestSummary:
    result = await db.execute(
        text(f"""
            SELECT {_SELECT_COLS}
            FROM   payroll.cdpirequests r
            WHERE  r.requestid = :rid
        """),
        {"rid": str(request_id)},
    )
    row = result.mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail="CDPI request not found.")
    return CdpiRequestSummary.model_validate(dict(row))


# ---------------------------------------------------------------------------
# Create Draft
# ---------------------------------------------------------------------------

async def create_draft(
    company_id: int,
    user_id: int,
    data: CdpiRequestCreate,
    db: AsyncConnection,
) -> CdpiRequestSummary:
    """
    Insert a new Draft CdpiRequest plus the mandatory DraftCreated event.

    Both inserts are performed within the same transaction (get_db uses
    engine.begin(); a raised exception rolls back both).

    Raises:
      HTTP 403 -- caller lacks payitems.edit on the requesting branch.
    """
    await require_cdpi_branch_edit(company_id, user_id, data.requesting_branch_id, db)

    result = await db.execute(
        text("""
            INSERT INTO payroll.cdpirequests (
                companyid,
                requestingbranchid,
                itemname,
                inputtype,
                unit,
                calcmethodkey,
                notes,
                status,
                revision,
                createdbyuserid
            ) VALUES (
                :cid,
                :branch_id,
                :item_name,
                :input_type,
                :unit,
                :calc_method_key,
                :notes,
                'Draft',
                1,
                :uid
            )
            RETURNING requestid
        """),
        {
            "cid":             company_id,
            "branch_id":       data.requesting_branch_id,
            "item_name":       data.item_name,
            "input_type":      data.input_type,
            "unit":            data.unit,
            "calc_method_key": data.calc_method_key,
            "notes":           data.notes,
            "uid":             user_id,
        },
    )
    new_id: UUID = result.scalar_one()

    await db.execute(
        text("""
            INSERT INTO payroll.cdpirequestevents (
                requestid,
                eventtype,
                tostatus,
                actoruserid,
                requestrevision
            ) VALUES (
                :rid,
                'DraftCreated',
                'Draft',
                :uid,
                1
            )
        """),
        {"rid": str(new_id), "uid": user_id},
    )

    return await _load_request(new_id, db)


# ---------------------------------------------------------------------------
# Read single request
# ---------------------------------------------------------------------------

async def get_request(
    company_id: int,
    user_id: int,
    request_id: UUID,
    db: AsyncConnection,
) -> CdpiRequestSummary:
    """
    Return a single CdpiRequest visible to the caller.

    Raises:
      HTTP 404 -- request not found or belongs to a different company.
      HTTP 403 -- request belongs to a branch the caller cannot see.
    """
    row = await _load_request(request_id, db)

    if row.company_id != company_id:
        raise HTTPException(status_code=404, detail="CDPI request not found.")

    can_see_all, branch_ids = await _check_branch_access(company_id, user_id, db)
    if not can_see_all and row.requesting_branch_id not in branch_ids:
        raise HTTPException(
            status_code=403,
            detail="You do not have access to this branch's CDPI requests.",
        )

    return row


# ---------------------------------------------------------------------------
# List requests
# ---------------------------------------------------------------------------

async def list_requests(
    company_id: int,
    user_id: int,
    db: AsyncConnection,
    *,
    status_filter: str | None = None,
    branch_id: int | None = None,
) -> list[CdpiRequestSummary]:
    """
    Return CDPI requests visible to the caller, with optional filters.

    Scope:
      AllCompanyBranches users: see all requests for the company.
      SpecificBranch users: restricted to their own branch(es).

    Optional filters:
      status_filter -- restrict to a single Status value.
      branch_id     -- restrict to a single requesting branch (must also be
                       in the caller's scope; silently returns empty list if
                       the caller cannot see that branch).
    """
    can_see_all, branch_ids = await _check_branch_access(company_id, user_id, db)

    where_parts = ["r.companyid = :cid"]
    params: dict = {"cid": company_id}

    if not can_see_all:
        if not branch_ids:
            return []
        in_clause = ", ".join(f":b{i}" for i in range(len(branch_ids)))
        where_parts.append(f"r.requestingbranchid IN ({in_clause})")
        for i, bid in enumerate(branch_ids):
            params[f"b{i}"] = bid

    if branch_id is not None:
        where_parts.append("r.requestingbranchid = :filter_branch")
        params["filter_branch"] = branch_id

    if status_filter is not None:
        where_parts.append("r.status = :filter_status")
        params["filter_status"] = status_filter

    where_sql = " AND ".join(where_parts)

    result = await db.execute(
        text(f"""
            SELECT {_SELECT_COLS}
            FROM   payroll.cdpirequests r
            WHERE  {where_sql}
            ORDER  BY r.createdatutc DESC
        """),
        params,
    )
    rows = result.mappings().all()
    return [CdpiRequestSummary.model_validate(dict(r)) for r in rows]


# ---------------------------------------------------------------------------
# Update Draft (optimistic concurrency)
# ---------------------------------------------------------------------------

async def update_draft(
    company_id: int,
    user_id: int,
    request_id: UUID,
    data: CdpiRequestUpdate,
    db: AsyncConnection,
) -> CdpiRequestSummary:
    """
    Apply a partial update to a Draft CdpiRequest using race-safe optimistic
    concurrency.

    Flow:
      1. Pre-read: load company_id + requestingbranchid for existence and
         permission checks (branch cannot change, so a pre-read is safe here).
      2. Permission check against requestingbranchid (HTTP 403 if denied).
      3. Atomic conditional UPDATE:
           WHERE requestid = :rid
             AND companyid = :cid
             AND status    = 'Draft'
             AND revision  = :expected_revision
         with RETURNING requestid.
         If two concurrent callers both pass step 1-2 with the same revision,
         exactly one UPDATE wins; the other returns 0 rows and is rejected.
      4. If 0 rows returned, diagnose with a second SELECT to distinguish
         not-found, non-Draft, and stale-revision cases and return the
         appropriate HTTP error.
      5. On success return the updated row. No event is written.

    Only fields explicitly provided in data (non-None) are written; omitted
    fields are left unchanged.
    """
    # Step 1: pre-read for existence + permission check.
    pre = (await db.execute(
        text("""
            SELECT companyid, requestingbranchid
            FROM   payroll.cdpirequests
            WHERE  requestid = :rid
        """),
        {"rid": str(request_id)},
    )).mappings().first()

    if pre is None or pre["companyid"] != company_id:
        raise HTTPException(status_code=404, detail="CDPI request not found.")

    # Step 2: permission check uses branch from pre-read (branch is immutable).
    await require_cdpi_branch_edit(company_id, user_id, pre["requestingbranchid"], db)

    # Step 3: build the SET clause for the conditional atomic UPDATE.
    set_parts = [
        "revision        = revision + 1",
        "updatedbyuserid = :uid",
        "updatedatutc    = now()",
    ]
    params: dict = {
        "rid":              str(request_id),
        "cid":              company_id,
        "expected_revision": data.expected_revision,
        "uid":              user_id,
    }

    if data.item_name is not None:
        set_parts.append("itemname = :item_name")
        params["item_name"] = data.item_name
    if data.input_type is not None:
        set_parts.append("inputtype = :input_type")
        params["input_type"] = data.input_type
    if data.unit is not None:
        set_parts.append("unit = :unit")
        params["unit"] = data.unit
    if data.calc_method_key is not None:
        set_parts.append("calcmethodkey = :calc_method_key")
        params["calc_method_key"] = data.calc_method_key
    if data.notes is not None:
        set_parts.append("notes = :notes")
        params["notes"] = data.notes

    set_sql = ", ".join(set_parts)
    updated = (await db.execute(
        text(f"""
            UPDATE payroll.cdpirequests
            SET    {set_sql}
            WHERE  requestid = :rid
              AND  companyid = :cid
              AND  status    = 'Draft'
              AND  revision  = :expected_revision
            RETURNING requestid
        """),
        params,
    )).scalar_one_or_none()

    if updated is not None:
        return await _load_request(request_id, db)

    # Step 4: 0 rows updated -- diagnose why.
    diag = (await db.execute(
        text("""
            SELECT status, revision
            FROM   payroll.cdpirequests
            WHERE  requestid = :rid AND companyid = :cid
        """),
        {"rid": str(request_id), "cid": company_id},
    )).mappings().first()

    if diag is None:
        # Deleted between pre-read and UPDATE (extremely rare).
        raise HTTPException(status_code=404, detail="CDPI request not found.")

    if diag["status"] != "Draft":
        raise HTTPException(
            status_code=422,
            detail=(
                f"Only Draft requests can be updated. "
                f"Current status: {diag['status']}."
            ),
        )

    # Status is Draft but revision did not match -- concurrent update won.
    raise HTTPException(
        status_code=409,
        detail=(
            f"Revision mismatch: expected {data.expected_revision}, "
            f"current is {diag['revision']}. Reload and retry."
        ),
    )


# ---------------------------------------------------------------------------
# Submit Draft
# ---------------------------------------------------------------------------


async def submit_draft(
    company_id: int,
    user_id: int,
    request_id: UUID,
    data: CdpiSubmitRequest,
    db: AsyncConnection,
) -> CdpiRequestSummary:
    """
    Transition a Draft request to PendingCompanyApproval.

    Flow:
      1. Pre-read: load company, branch, and current definition fields
         needed for completeness validation and the event type decision.
      2. Verify existence and company scope.
      3. Branch-edit permission check.
      4. Validate completeness: ItemName, InputType, CalcMethodKey all required.
      5. Validate CalcMethodKey is a submittable method (PerUnit only for Task 4).
      6. Determine event type: Submitted (first time) vs Resubmitted (returned
         request re-submitted -- detected by non-null SubmittedAtUtc in Draft).
      7. Atomic conditional UPDATE:
           WHERE requestid, companyid, status='Draft', revision=expected
         Sets Status='PendingCompanyApproval', SubmittedByUserID, SubmittedAtUtc,
         revision+1.  Race: only one concurrent caller wins.
      8. Diagnose 0-row result (stale revision or not Draft).
      9. Insert Submitted/Resubmitted event (same transaction; rolled back on failure).
     10. Return updated row.

    Raises:
      404 -- not found.
      403 -- no branch-edit permission.
      422 -- not Draft, incomplete definition, or unsupported calc method.
      409 -- stale revision.
    """
    # Step 1: pre-read.
    pre = (await db.execute(
        text("""
            SELECT companyid, requestingbranchid,
                   itemname, inputtype, calcmethodkey,
                   submittedatutc
            FROM   payroll.cdpirequests
            WHERE  requestid = :rid
        """),
        {"rid": str(request_id)},
    )).mappings().first()

    if pre is None or pre["companyid"] != company_id:
        raise HTTPException(status_code=404, detail="CDPI request not found.")

    # Step 3: permission check.
    await require_cdpi_branch_edit(company_id, user_id, pre["requestingbranchid"], db)

    # Step 4: CalcMethodKey presence check -- must be set before adapter lookup.
    if not pre["calcmethodkey"]:
        raise HTTPException(
            status_code=422,
            detail="Cannot submit: missing required fields: CalcMethodKey.",
        )

    # Step 5: method adapter gate -- delegates completeness and support checks.
    adapter = get_adapter(pre["calcmethodkey"])
    if adapter is None:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown CalcMethodKey: '{pre['calcmethodkey']}'.",
        )
    if not adapter.is_implemented():
        raise HTTPException(
            status_code=422,
            detail=(
                f"CalcMethodKey '{pre['calcmethodkey']}' is not yet supported for "
                "submission. Only PerUnit requests may be submitted at this time."
            ),
        )
    missing = adapter.validate_submit(pre["itemname"], pre["inputtype"])
    if missing:
        raise HTTPException(
            status_code=422,
            detail=f"Cannot submit: missing required fields: {', '.join(missing)}.",
        )

    # Step 6: first submit vs resubmit.
    event_type = "Resubmitted" if pre["submittedatutc"] is not None else "Submitted"

    # Step 7: atomic conditional UPDATE.
    updated = (await db.execute(
        text("""
            UPDATE payroll.cdpirequests
            SET    status            = 'PendingCompanyApproval',
                   submittedbyuserid = :uid,
                   submittedatutc    = now(),
                   revision          = revision + 1,
                   updatedbyuserid   = :uid,
                   updatedatutc      = now()
            WHERE  requestid = :rid
              AND  companyid = :cid
              AND  status    = 'Draft'
              AND  revision  = :expected_revision
            RETURNING revision
        """),
        {
            "rid":               str(request_id),
            "cid":               company_id,
            "expected_revision": data.expected_revision,
            "uid":               user_id,
        },
    )).scalar_one_or_none()

    if updated is None:
        diag = (await db.execute(
            text("""
                SELECT status, revision
                FROM   payroll.cdpirequests
                WHERE  requestid = :rid AND companyid = :cid
            """),
            {"rid": str(request_id), "cid": company_id},
        )).mappings().first()
        if diag is None:
            raise HTTPException(status_code=404, detail="CDPI request not found.")
        if diag["status"] != "Draft":
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Only Draft requests can be submitted. "
                    f"Current status: {diag['status']}."
                ),
            )
        raise HTTPException(
            status_code=409,
            detail=(
                f"Revision mismatch: expected {data.expected_revision}, "
                f"current is {diag['revision']}. Reload and retry."
            ),
        )

    # Step 9: insert event (same transaction).
    await db.execute(
        text("""
            INSERT INTO payroll.cdpirequestevents (
                requestid, eventtype, fromstatus, tostatus,
                actoruserid, requestrevision
            ) VALUES (
                :rid, :etype, 'Draft', 'PendingCompanyApproval',
                :uid, :rev
            )
        """),
        {
            "rid":   str(request_id),
            "etype": event_type,
            "uid":   user_id,
            "rev":   updated,
        },
    )

    return await _load_request(request_id, db)


# ---------------------------------------------------------------------------
# Decide: ReturnToDraft, Reject, or Approve
# ---------------------------------------------------------------------------

async def decide_request(
    company_id: int,
    user_id: int,
    request_id: UUID,
    data: CdpiDecideRequest,
    db: AsyncConnection,
) -> CdpiRequestSummary:
    """
    Company reviewer action on a PendingCompanyApproval request.

    Raises:
      404 -- not found.
      403 -- caller is not a company-wide reviewer.
      422 -- not PendingCompanyApproval, incomplete definition, or unsupported method.
      409 -- stale revision.
    """
    if data.action == CdpiDecideAction.Approve:
        return await _approve_request(company_id, user_id, request_id, data, db)

    if data.action == CdpiDecideAction.ReturnToDraft:
        new_status = "Draft"
        event_type = "ReturnedToDraft"
    elif data.action == CdpiDecideAction.Reject:
        new_status = "Rejected"
        event_type = "Rejected"
    else:
        raise HTTPException(
            status_code=422,
            detail=f"Unsupported action: '{data.action}'.",
        )

    # Pre-read for existence.
    pre = (await db.execute(
        text("SELECT companyid FROM payroll.cdpirequests WHERE requestid = :rid"),
        {"rid": str(request_id)},
    )).mappings().first()

    if pre is None or pre["companyid"] != company_id:
        raise HTTPException(status_code=404, detail="CDPI request not found.")

    await require_cdpi_company_edit(company_id, user_id, db)

    updated = (await db.execute(
        text("""
            UPDATE payroll.cdpirequests
            SET    status           = :new_status,
                   updatedbyuserid  = :uid,
                   updatedatutc     = now(),
                   revision         = revision + 1
            WHERE  requestid = :rid
              AND  companyid = :cid
              AND  status    = 'PendingCompanyApproval'
              AND  revision  = :expected_revision
            RETURNING revision
        """),
        {
            "rid":               str(request_id),
            "cid":               company_id,
            "expected_revision": data.expected_revision,
            "uid":               user_id,
            "new_status":        new_status,
        },
    )).scalar_one_or_none()

    if updated is None:
        diag = (await db.execute(
            text("""
                SELECT status, revision FROM payroll.cdpirequests
                WHERE requestid = :rid AND companyid = :cid
            """),
            {"rid": str(request_id), "cid": company_id},
        )).mappings().first()
        if diag is None:
            raise HTTPException(status_code=404, detail="CDPI request not found.")
        if diag["status"] != "PendingCompanyApproval":
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Only PendingCompanyApproval requests can be decided. "
                    f"Current status: {diag['status']}."
                ),
            )
        raise HTTPException(
            status_code=409,
            detail=(
                f"Revision mismatch: expected {data.expected_revision}, "
                f"current is {diag['revision']}. Reload and retry."
            ),
        )

    await db.execute(
        text("""
            INSERT INTO payroll.cdpirequestevents (
                requestid, eventtype, fromstatus, tostatus,
                actoruserid, reason, requestrevision
            ) VALUES (
                :rid, :etype, 'PendingCompanyApproval', :to_status,
                :uid, :reason, :rev
            )
        """),
        {
            "rid":      str(request_id),
            "etype":    event_type,
            "to_status": new_status,
            "uid":      user_id,
            "reason":   data.reason,
            "rev":      updated,
        },
    )

    return await _load_request(request_id, db)


async def _approve_request(
    company_id: int,
    user_id: int,
    request_id: UUID,
    data: CdpiDecideRequest,
    db: AsyncConnection,
) -> CdpiRequestSummary:
    """
    Atomically approve a PendingCompanyApproval request.

    Within a single transaction:
      1. Pre-read: load branch + item fields.
      2. Company-wide permission check.
      3. Validate via adapter (must be implemented + complete).
      4. Atomic UPDATE: status='Approved', revision+1.
      5. INSERT PayItems (company-level Daily PerUnit item).
      6. INSERT CdpiDefinitions (SourceRequestID = request UUID).
      7. INSERT BranchPayItemConfig (requesting branch, IsActive=TRUE).
      8. UPDATE CdpiRequests.ApprovedPayItemID.
      9. INSERT Approved event.
    """
    # Step 1: pre-read.
    pre = (await db.execute(
        text("""
            SELECT companyid, requestingbranchid,
                   itemname, inputtype, calcmethodkey, unit, notes
            FROM   payroll.cdpirequests
            WHERE  requestid = :rid
        """),
        {"rid": str(request_id)},
    )).mappings().first()

    if pre is None or pre["companyid"] != company_id:
        raise HTTPException(status_code=404, detail="CDPI request not found.")

    # Step 2: company-wide permission.
    await require_cdpi_company_edit(company_id, user_id, db)

    # Step 3: adapter validation.
    if not pre["calcmethodkey"]:
        raise HTTPException(
            status_code=422,
            detail="Cannot approve: missing required fields: CalcMethodKey.",
        )
    adapter = get_adapter(pre["calcmethodkey"])
    if adapter is None or not adapter.is_implemented():
        raise HTTPException(
            status_code=422,
            detail=(
                f"CalcMethodKey '{pre['calcmethodkey']}' is not supported for approval. "
                "Only PerUnit requests may be approved at this time."
            ),
        )
    missing = adapter.validate_submit(pre["itemname"], pre["inputtype"])
    if missing:
        raise HTTPException(
            status_code=422,
            detail=f"Cannot approve: missing required fields: {', '.join(missing)}.",
        )

    # Step 4 (early validation): confirm the request is still in the expected state
    # before doing expensive inserts.  If we are in engine.begin() mode (production),
    # the inserts below are rolled back on any error, so correctness is guaranteed.
    diag_pre = (await db.execute(
        text("""
            SELECT status, revision FROM payroll.cdpirequests
            WHERE requestid = :rid AND companyid = :cid
        """),
        {"rid": str(request_id), "cid": company_id},
    )).mappings().first()
    if diag_pre is None:
        raise HTTPException(status_code=404, detail="CDPI request not found.")
    if diag_pre["status"] != "PendingCompanyApproval":
        raise HTTPException(
            status_code=422,
            detail=(
                f"Only PendingCompanyApproval requests can be approved. "
                f"Current status: {diag_pre['status']}."
            ),
        )
    if diag_pre["revision"] != data.expected_revision:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Revision mismatch: expected {data.expected_revision}, "
                f"current is {diag_pre['revision']}. Reload and retry."
            ),
        )

    # Step 5: INSERT PayItems (must precede the status UPDATE so we can include
    # ApprovedPayItemID in the same statement — the check constraint requires
    # Status='Approved' and ApprovedPayItemID IS NOT NULL to be set together).
    pay_item_code = f"CDPI{_uuid.uuid4().hex[:12].upper()}"
    pay_item_datatype = _datatype_for_input_type(pre["inputtype"] or "")
    pay_item_id: int = (await db.execute(
        text("""
            INSERT INTO payroll.payitems (
                companyid, branchid, payitemcode, displaylabel, payitemname,
                category, datatype, unit, status, sortorder,
                appearsinpayrollentry, appearsinledger, appearsinreports,
                requiresrate, issystemstandard,
                itemscope, ratebehavior, isdefaultbranchactive,
                requestingbranchid, createdbyuserid, notes
            ) VALUES (
                :cid, NULL, :code, :label, :name,
                'Custom', :datatype, :unit, 'Active', 0,
                TRUE, TRUE, TRUE,
                FALSE, FALSE,
                'Daily', 'PerUnit', FALSE,
                :req_branch_id, :uid, :notes
            )
            RETURNING payitemid
        """),
        {
            "cid":           company_id,
            "code":          pay_item_code,
            "label":         pre["itemname"],
            "name":          pre["itemname"],
            "datatype":      pay_item_datatype,
            "unit":          pre["unit"],
            "req_branch_id": pre["requestingbranchid"],
            "uid":           user_id,
            "notes":         pre["notes"],
        },
    )).scalar_one()

    # Step 6: INSERT CdpiDefinitions.
    await db.execute(
        text("""
            INSERT INTO payroll.cdpidefinitions (
                payitemid, definitionschemaversion, lockedatutc, createdbyuserid
            ) VALUES (
                :pid, 1, now(), :uid
            )
        """),
        {"pid": pay_item_id, "uid": user_id},
    )

    # Step 7: INSERT BranchPayItemConfig (requesting branch, active).
    await db.execute(
        text("""
            INSERT INTO payroll.branchpayitemconfig (
                companyid, branchid, payitemid,
                isactive, effectivefrom, createdbyuserid
            ) VALUES (
                :cid, :bid, :pid,
                TRUE, CURRENT_DATE, :uid
            )
        """),
        {
            "cid": company_id,
            "bid": pre["requestingbranchid"],
            "pid": pay_item_id,
            "uid": user_id,
        },
    )

    # Step 8: Atomic UPDATE — sets Status='Approved' and ApprovedPayItemID together
    # (check constraint ck_cdpirequests_approvallink requires both non-null simultaneously).
    new_rev = (await db.execute(
        text("""
            UPDATE payroll.cdpirequests
            SET    status            = 'Approved',
                   approvedpayitemid = :pid,
                   updatedbyuserid   = :uid,
                   updatedatutc      = now(),
                   revision          = revision + 1
            WHERE  requestid = :rid
              AND  companyid = :cid
              AND  status    = 'PendingCompanyApproval'
              AND  revision  = :expected_revision
            RETURNING revision
        """),
        {
            "rid":               str(request_id),
            "cid":               company_id,
            "expected_revision": data.expected_revision,
            "uid":               user_id,
            "pid":               pay_item_id,
        },
    )).scalar_one_or_none()

    if new_rev is None:
        # In engine.begin() mode the transaction rolls back the PayItem inserts above.
        # Diagnose the conflict for the caller.
        diag = (await db.execute(
            text("""
                SELECT status, revision FROM payroll.cdpirequests
                WHERE requestid = :rid AND companyid = :cid
            """),
            {"rid": str(request_id), "cid": company_id},
        )).mappings().first()
        if diag is None:
            raise HTTPException(status_code=404, detail="CDPI request not found.")
        if diag["status"] != "PendingCompanyApproval":
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Only PendingCompanyApproval requests can be approved. "
                    f"Current status: {diag['status']}."
                ),
            )
        raise HTTPException(
            status_code=409,
            detail=(
                f"Revision mismatch: expected {data.expected_revision}, "
                f"current is {diag['revision']}. Reload and retry."
            ),
        )

    # Step 9: INSERT Approved event.
    await db.execute(
        text("""
            INSERT INTO payroll.cdpirequestevents (
                requestid, eventtype, fromstatus, tostatus,
                actoruserid, reason, requestrevision
            ) VALUES (
                :rid, 'Approved', 'PendingCompanyApproval', 'Approved',
                :uid, :reason, :rev
            )
        """),
        {
            "rid":    str(request_id),
            "uid":    user_id,
            "reason": data.reason,
            "rev":    new_rev,
        },
    )

    return await _load_request(request_id, db)


# ---------------------------------------------------------------------------
# Direct company item creation (no request workflow)
# ---------------------------------------------------------------------------

async def create_direct_company_item(
    company_id: int,
    user_id: int,
    data: CdpiDirectCreateRequest,
    db: AsyncConnection,
) -> CdpiDirectCreateSummary:
    """
    Admin-direct creation of a CDPI PayItem without the request workflow.

    Creates a PayItem + CdpiDefinition in one transaction.
    No CdpiRequest row is created.  No BranchPayItemConfig rows are created —
    the item starts inactive for all branches.

    Raises:
      403 -- caller lacks AllCompanyBranches scope + payitems.edit.
      422 -- calc_method_key is not yet supported (only PerUnit).
    """
    await require_cdpi_company_edit(company_id, user_id, db)

    # Validate via adapter.
    adapter = get_adapter(data.calc_method_key)
    if adapter is None or not adapter.is_implemented():
        raise HTTPException(
            status_code=422,
            detail=(
                f"CalcMethodKey '{data.calc_method_key}' is not supported for direct creation. "
                "Only PerUnit items may be created at this time."
            ),
        )
    missing = adapter.validate_submit(data.item_name, data.input_type)
    if missing:
        raise HTTPException(
            status_code=422,
            detail=f"Cannot create: missing required fields: {', '.join(missing)}.",
        )

    # INSERT PayItems (no requesting branch — company-level direct create).
    pay_item_code = f"CDPI{_uuid.uuid4().hex[:12].upper()}"
    pay_item_datatype = _datatype_for_input_type(data.input_type)
    pay_item_id: int = (await db.execute(
        text("""
            INSERT INTO payroll.payitems (
                companyid, branchid, payitemcode, displaylabel, payitemname,
                category, datatype, unit, status, sortorder,
                appearsinpayrollentry, appearsinledger, appearsinreports,
                requiresrate, issystemstandard,
                itemscope, ratebehavior, isdefaultbranchactive,
                requestingbranchid, createdbyuserid, notes
            ) VALUES (
                :cid, NULL, :code, :label, :name,
                'Custom', :datatype, :unit, 'Active', 0,
                TRUE, TRUE, TRUE,
                FALSE, FALSE,
                'Daily', 'PerUnit', FALSE,
                NULL, :uid, :notes
            )
            RETURNING payitemid
        """),
        {
            "cid":      company_id,
            "code":     pay_item_code,
            "label":    data.item_name,
            "name":     data.item_name,
            "datatype": pay_item_datatype,
            "unit":     data.unit,
            "uid":      user_id,
            "notes":    data.notes,
        },
    )).scalar_one()

    # INSERT CdpiDefinitions.
    await db.execute(
        text("""
            INSERT INTO payroll.cdpidefinitions (
                payitemid, definitionschemaversion, lockedatutc, createdbyuserid
            ) VALUES (
                :pid, 1, now(), :uid
            )
        """),
        {"pid": pay_item_id, "uid": user_id},
    )

    return CdpiDirectCreateSummary(
        pay_item_id=pay_item_id,
        pay_item_code=pay_item_code,
        company_id=company_id,
        item_name=data.item_name,
        input_type=data.input_type,
        unit=data.unit,
        calc_method_key=data.calc_method_key,
        notes=data.notes,
        created_by_user_id=user_id,
    )


# ---------------------------------------------------------------------------
# Copy Rejected request to new Draft
# ---------------------------------------------------------------------------

async def copy_rejected(
    company_id: int,
    user_id: int,
    request_id: UUID,
    db: AsyncConnection,
) -> CdpiRequestSummary:
    """
    Create a new Draft from a Rejected request.

    The new request gets a fresh UUID, Revision=1, and CopiedFromRequestID set
    to the source.  Only editable definition fields are copied; approval/
    submission metadata is not.  The original rejected request is not modified.

    A CopiedFromRejected event is inserted for the new request in the same
    transaction.

    Raises:
      404 -- source not found or wrong company.
      403 -- no branch-edit permission on source's branch.
      422 -- source is not Rejected.
    """
    # Load source.
    src = (await db.execute(
        text("""
            SELECT companyid, requestingbranchid, status,
                   itemname, inputtype, unit, calcmethodkey, notes
            FROM   payroll.cdpirequests
            WHERE  requestid = :rid
        """),
        {"rid": str(request_id)},
    )).mappings().first()

    if src is None or src["companyid"] != company_id:
        raise HTTPException(status_code=404, detail="CDPI request not found.")

    # Permission enforced before status check to prevent cross-branch state leaks.
    # A scoped user must not learn that a request exists or what status it has
    # before their branch access is verified.
    await require_cdpi_branch_edit(company_id, user_id, src["requestingbranchid"], db)

    if src["status"] != "Rejected":
        raise HTTPException(
            status_code=422,
            detail=(
                f"Only Rejected requests can be copied. "
                f"Current status: {src['status']}."
            ),
        )

    # Insert new request.
    new_id = (await db.execute(
        text("""
            INSERT INTO payroll.cdpirequests (
                companyid,
                requestingbranchid,
                itemname,
                inputtype,
                unit,
                calcmethodkey,
                notes,
                status,
                revision,
                copiedfromrequestid,
                createdbyuserid
            ) VALUES (
                :cid,
                :branch_id,
                :item_name,
                :input_type,
                :unit,
                :calc_method_key,
                :notes,
                'Draft',
                1,
                :source_id,
                :uid
            )
            RETURNING requestid
        """),
        {
            "cid":             company_id,
            "branch_id":       src["requestingbranchid"],
            "item_name":       src["itemname"],
            "input_type":      src["inputtype"],
            "unit":            src["unit"],
            "calc_method_key": src["calcmethodkey"],
            "notes":           src["notes"],
            "source_id":       str(request_id),
            "uid":             user_id,
        },
    )).scalar_one()

    # Insert CopiedFromRejected event for the new request.
    await db.execute(
        text("""
            INSERT INTO payroll.cdpirequestevents (
                requestid, eventtype, fromstatus, tostatus,
                actoruserid, requestrevision
            ) VALUES (
                :rid, 'CopiedFromRejected', NULL, 'Draft',
                :uid, 1
            )
        """),
        {"rid": str(new_id), "uid": user_id},
    )

    return await _load_request(new_id, db)


# ---------------------------------------------------------------------------
# Task 7: Branch-level CDPI controls
# ---------------------------------------------------------------------------


def _build_branch_item_state(
    *,
    payitemid: int,
    payitemcode: str,
    payitemname: str,
    datatype: str,
    unit: "str | None",
    ratebehavior: str,
    isdefaultbranchactive: bool,
    cfg_isactive: "bool | None",
    cfg_branchdisplayname: "str | None",
) -> CdpiBranchItemState:
    is_active = bool(cfg_isactive) if cfg_isactive is not None else bool(isdefaultbranchactive)
    override = cfg_branchdisplayname or None
    return CdpiBranchItemState(
        pay_item_id=payitemid,
        pay_item_code=payitemcode,
        item_name=payitemname,
        branch_display_name_override=override,
        effective_display_name=override if override else payitemname,
        is_active=is_active,
        data_type=datatype,
        unit=unit,
        rate_behavior=ratebehavior,
        is_cdpi=True,
    )


async def _cdpi_get_open_period_end(
    branch_id: int,
    company_id: int,
    db: AsyncConnection,
) -> "_date | None":
    today = _date.today()
    r = await db.execute(
        text("""
            SELECT MAX(enddate)
            FROM   payroll.payrollperiods
            WHERE  branchid   = :bid
              AND  companyid  = :cid
              AND  status    IN ('Draft', 'Open', 'InReview', 'Approved')
              AND  startdate <= :today
              AND  enddate   >= :today
        """),
        {"bid": branch_id, "cid": company_id, "today": today},
    )
    return r.scalar_one()


async def _load_branch_cdpi_item(
    company_id: int,
    branch_id: int,
    pay_item_id: int,
    db: AsyncConnection,
) -> CdpiBranchItemState:
    today = _date.today()
    row = (await db.execute(
        text("""
            SELECT
                pi.payitemid, pi.payitemcode, pi.payitemname,
                pi.datatype, pi.unit, pi.ratebehavior, pi.isdefaultbranchactive,
                cfg.isactive           AS cfg_isactive,
                cfg.branchdisplayname  AS cfg_branchdisplayname
            FROM  payroll.payitems pi
            JOIN  payroll.cdpidefinitions cd ON cd.payitemid = pi.payitemid
            LEFT JOIN LATERAL (
                SELECT isactive, branchdisplayname
                FROM   payroll.branchpayitemconfig
                WHERE  payitemid      = pi.payitemid
                  AND  companyid      = :cid
                  AND  branchid       = :bid
                  AND  effectivefrom <= :today
                  AND  (effectiveto IS NULL OR effectiveto >= :today)
                ORDER  BY effectivefrom DESC
                LIMIT  1
            ) cfg ON TRUE
            WHERE pi.payitemid = :pid
              AND pi.companyid = :cid
              AND pi.status   != 'Retired'
        """),
        {"pid": pay_item_id, "cid": company_id, "bid": branch_id, "today": today},
    )).mappings().first()

    if row is None:
        raise HTTPException(status_code=404, detail="CDPI pay item not found for this company.")

    return _build_branch_item_state(
        payitemid=row["payitemid"],
        payitemcode=row["payitemcode"],
        payitemname=row["payitemname"],
        datatype=row["datatype"],
        unit=row["unit"],
        ratebehavior=row["ratebehavior"],
        isdefaultbranchactive=row["isdefaultbranchactive"],
        cfg_isactive=row["cfg_isactive"],
        cfg_branchdisplayname=row["cfg_branchdisplayname"],
    )


async def list_branch_cdpi_items(
    company_id: int,
    user_id: int,
    branch_id: int,
    db: AsyncConnection,
) -> list[CdpiBranchItemState]:
    br = (await db.execute(
        text("SELECT 1 FROM core.branches WHERE branchid = :bid AND companyid = :cid"),
        {"bid": branch_id, "cid": company_id},
    )).first()
    if br is None:
        raise HTTPException(status_code=404, detail=f"Branch {branch_id} not found.")

    await require_cdpi_branch_edit(company_id, user_id, branch_id, db)

    today = _date.today()
    rows = (await db.execute(
        text("""
            SELECT
                pi.payitemid, pi.payitemcode, pi.payitemname,
                pi.datatype, pi.unit, pi.ratebehavior, pi.isdefaultbranchactive,
                cfg.isactive           AS cfg_isactive,
                cfg.branchdisplayname  AS cfg_branchdisplayname
            FROM  payroll.payitems pi
            JOIN  payroll.cdpidefinitions cd ON cd.payitemid = pi.payitemid
            LEFT JOIN LATERAL (
                SELECT isactive, branchdisplayname
                FROM   payroll.branchpayitemconfig
                WHERE  payitemid      = pi.payitemid
                  AND  companyid      = :cid
                  AND  branchid       = :bid
                  AND  effectivefrom <= :today
                  AND  (effectiveto IS NULL OR effectiveto >= :today)
                ORDER  BY effectivefrom DESC
                LIMIT  1
            ) cfg ON TRUE
            WHERE pi.companyid = :cid
              AND pi.status   != 'Retired'
            ORDER BY pi.payitemname
        """),
        {"cid": company_id, "bid": branch_id, "today": today},
    )).mappings().all()

    return [
        _build_branch_item_state(
            payitemid=r["payitemid"],
            payitemcode=r["payitemcode"],
            payitemname=r["payitemname"],
            datatype=r["datatype"],
            unit=r["unit"],
            ratebehavior=r["ratebehavior"],
            isdefaultbranchactive=r["isdefaultbranchactive"],
            cfg_isactive=r["cfg_isactive"],
            cfg_branchdisplayname=r["cfg_branchdisplayname"],
        )
        for r in rows
    ]


async def update_branch_cdpi_item(
    company_id: int,
    user_id: int,
    branch_id: int,
    pay_item_id: int,
    data: CdpiBranchItemUpdate,
    db: AsyncConnection,
) -> CdpiBranchItemState:
    br = (await db.execute(
        text("SELECT 1 FROM core.branches WHERE branchid = :bid AND companyid = :cid"),
        {"bid": branch_id, "cid": company_id},
    )).first()
    if br is None:
        raise HTTPException(status_code=404, detail=f"Branch {branch_id} not found.")

    await require_cdpi_branch_edit(company_id, user_id, branch_id, db)

    pi = (await db.execute(
        text("""
            SELECT pi.payitemid
            FROM   payroll.payitems pi
            JOIN   payroll.cdpidefinitions cd ON cd.payitemid = pi.payitemid
            WHERE  pi.payitemid = :pid
              AND  pi.companyid = :cid
              AND  pi.status   != 'Retired'
        """),
        {"pid": pay_item_id, "cid": company_id},
    )).first()
    if pi is None:
        raise HTTPException(
            status_code=404,
            detail="CDPI pay item not found for this company.",
        )

    update_is_active    = "is_active" in data.model_fields_set
    update_display_name = "branch_display_name_override" in data.model_fields_set

    if not update_is_active and not update_display_name:
        return await _load_branch_cdpi_item(company_id, branch_id, pay_item_id, db)

    period_max_end = await _cdpi_get_open_period_end(branch_id, company_id, db)
    effective_from: _date = (
        period_max_end + _timedelta(days=1) if period_max_end is not None else _date.today()
    )

    await _write_cdpi_branch_config(
        pay_item_id=pay_item_id,
        branch_id=branch_id,
        company_id=company_id,
        user_id=user_id,
        effective_from=effective_from,
        update_is_active=update_is_active,
        new_is_active=data.is_active,
        update_display_name=update_display_name,
        new_display_name=data.branch_display_name_override,
        db=db,
    )

    return await _load_branch_cdpi_item(company_id, branch_id, pay_item_id, db)


async def _write_cdpi_branch_config(
    pay_item_id: int,
    branch_id: int,
    company_id: int,
    user_id: int,
    effective_from: _date,
    *,
    update_is_active: bool,
    new_is_active: "bool | None",
    update_display_name: bool,
    new_display_name: "str | None",
    db: AsyncConnection,
) -> None:
    open_row = (await db.execute(
        text("""
            SELECT configid, isactive, branchdisplayname, effectivefrom
            FROM   payroll.branchpayitemconfig
            WHERE  payitemid   = :pid
              AND  companyid   = :cid
              AND  branchid    = :bid
              AND  effectiveto IS NULL
            FOR UPDATE
        """),
        {"pid": pay_item_id, "cid": company_id, "bid": branch_id},
    )).mappings().first()

    if open_row is None:
        # No existing config row — INSERT fresh.
        is_active_val    = bool(new_is_active) if (update_is_active and new_is_active is not None) else False
        display_name_val = new_display_name if update_display_name else None
        await db.execute(
            text("""
                INSERT INTO payroll.branchpayitemconfig
                    (companyid, branchid, payitemid, isactive, branchdisplayname,
                     effectivefrom, createdbyuserid)
                VALUES
                    (:cid, :bid, :pid, :active, :dname, :eff_from, :uid)
            """),
            {
                "cid": company_id, "bid": branch_id, "pid": pay_item_id,
                "active": is_active_val, "dname": display_name_val,
                "eff_from": effective_from, "uid": user_id,
            },
        )

    elif open_row["effectivefrom"] >= effective_from:
        # Open row starts on or after the resolved effective date — safe to UPDATE in place.
        # This covers both same-day edits (no period protection) and future-dated pending rows
        # that are already beyond the period boundary.
        set_parts: list[str] = []
        params: dict = {"cid_row": open_row["configid"]}

        if update_is_active and new_is_active is not None:
            set_parts.append("isactive = :active")
            params["active"] = bool(new_is_active)

        if update_display_name:
            set_parts.append("branchdisplayname = :dname")
            params["dname"] = new_display_name

        if set_parts:
            await db.execute(
                text(f"UPDATE payroll.branchpayitemconfig SET {', '.join(set_parts)} WHERE configid = :cid_row"),
                params,
            )

    else:
        # Open row starts before the resolved effective date — close it and INSERT a new open row.
        # This handles both the normal "row started yesterday" case and the period-protected case
        # where effective_from is pushed to the future: a row starting today must not be mutated
        # in place while it belongs to a protected open payroll period.
        await db.execute(
            text("""
                UPDATE payroll.branchpayitemconfig
                SET    effectiveto = :close_date
                WHERE  configid    = :cid_row
            """),
            {
                "close_date": effective_from - _timedelta(days=1),
                "cid_row":    open_row["configid"],
            },
        )
        is_active_val = (
            bool(new_is_active) if (update_is_active and new_is_active is not None)
            else bool(open_row["isactive"])
        )
        display_name_val = (
            new_display_name if update_display_name
            else open_row["branchdisplayname"]
        )
        await db.execute(
            text("""
                INSERT INTO payroll.branchpayitemconfig
                    (companyid, branchid, payitemid, isactive, branchdisplayname,
                     effectivefrom, createdbyuserid)
                VALUES
                    (:cid, :bid, :pid, :active, :dname, :eff_from, :uid)
            """),
            {
                "cid": company_id, "bid": branch_id, "pid": pay_item_id,
                "active": is_active_val, "dname": display_name_val,
                "eff_from": effective_from, "uid": user_id,
            },
        )
