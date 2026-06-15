"""
CDPI (Custom Daily Pay Item) -- Draft CRUD service layer.

Covers: create draft, read request, list requests, update draft.

All functions use raw parameterized SQL via sqlalchemy.text().
Transactions are managed by the get_db() dependency (engine.begin()),
which auto-commits on success and rolls back on exception.

Scope model:
  - AllCompanyBranches users see all requests for the company.
  - SpecificBranch users see only requests for their own branch(es).

Optimistic concurrency (update_draft):
  - Caller supplies expected_revision.
  - Service reads current Revision from the DB.
  - If they do not match, raises HTTP 409 (stale data).
  - On match, increments Revision by 1 and updates UpdatedByUserID/UpdatedAtUtc.
"""
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.service import _check_branch_access
from app.cdpi.guards import require_cdpi_branch_edit
from app.cdpi.schemas import CdpiRequestCreate, CdpiRequestSummary, CdpiRequestUpdate


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

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
