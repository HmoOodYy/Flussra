"""Branch administration, readiness, onboarding, and branch payroll setup operations.

Company, status_keys, and status_rate_columns operations live in their named Settings modules; security and audit ownership is in their dedicated modules.
"""
import secrets
import string as _string
from datetime import date as _date
from typing import TYPE_CHECKING

from fastapi import HTTPException, status
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError as SAIntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from app.access.branch_access import check_branch_access
from app.access.permissions import check_permission
from app.access.policy import require_non_driver_subject
from app.db.sql import build_in_clause
from app.payroll_setup import boundaries
from app.payroll_setup.errors import PolicyError
from app.payroll_setup.locks import lock_company, lock_setups
from app.payroll_setup.payroll_policy import assign_setup
from app.payroll_setup.readiness import branch_schedule_readiness, branch_schedule_readiness_detail
from app.payroll_setup.schemas import SetupResponse
from app.settings import audit, security, status_rate_columns
from app.settings.schemas import (
    BranchAdmin,
    BranchCreate,
    BranchUpdate,
    OnboardingOptionsResponse,
)

if TYPE_CHECKING:
    pass


async def _attach_readiness_reasons(
    branches: list[BranchAdmin], company_id: int, user_id: int,
    db: AsyncConnection,
) -> None:
    """Expose reason codes and evaluated dates only to non-drivers with
    payroll.view on each branch."""
    if not branches:
        return
    try:
        await require_non_driver_subject(company_id, user_id, db)
    except HTTPException as exc:
        if exc.status_code == 403:
            return
        raise
    for branch in branches:
        try:
            await check_permission(company_id, user_id, branch.branch_id, "payroll.view", db)
        except HTTPException as exc:
            if exc.status_code == 403:
                continue
            raise
        _, reason, evaluated_date = await branch_schedule_readiness_detail(
            company_id, branch.branch_id, db,
        )
        branch.schedule_readiness_reason = reason
        branch.schedule_readiness_date = evaluated_date


def _generate_branch_code() -> str:
    """
    Generate a random branch code in the format BR_XXXXXXXX where XXXXXXXX
    is 8 uppercase alphanumeric characters chosen via the secrets module.
    """
    alphabet = _string.ascii_uppercase + _string.digits
    suffix = "".join(secrets.choice(alphabet) for _ in range(8))
    return f"BR_{suffix}"


def _handle_branch_integrity_error(exc: SAIntegrityError) -> None:
    """Translate known DB uniqueness violations into 422 HTTPException."""
    msg = str(exc.orig).lower() if exc.orig else str(exc).lower()
    if "uq_branches_company_code" in msg:
        raise HTTPException(
            status_code=422,
            detail="Branch code already exists for this company.",
        )
    if "uq_branches_company_name" in msg:
        raise HTTPException(
            status_code=422,
            detail="Branch name already exists for this company.",
        )
    # Covers ux_Branches_Company_Default (0002 migration) — concurrent
    # set-default race where two transactions both insert/update isdefault=TRUE.
    if "ux_branches_company_default" in msg:
        raise HTTPException(
            status_code=422,
            detail="Another branch is already set as the default. Reload and retry.",
        )
    raise HTTPException(
        status_code=422,
        detail="Branch could not be saved — a uniqueness constraint was violated.",
    )


async def _fetch_branch_metrics(
    company_id: int,
    branch_ids: list[int],
    db: AsyncConnection,
) -> dict[int, dict]:
    """
    Resolve canonical Payroll Setup readiness for each branch and run four
    operational metric queries against the given branch IDs.

    Each operational query is individually wrapped in try/except so a missing
    table or failing index on one metric does not prevent the other three from
    loading.

    Returns dict[branch_id → {metric_name: value}].  A missing key in the
    inner dict means an operational metric query failed; callers substitute
    safe defaults (0 / None). Readiness resolution failures are not suppressed.
    """
    if not branch_ids:
        return {}

    in_clause, in_params = build_in_clause(branch_ids, "bid")
    base: dict = {"cid": company_id, **in_params}
    result: dict[int, dict] = {}

    def _put(bid: int, key: str, val) -> None:
        result.setdefault(bid, {})[key] = val

    # 1 — Canonical Payroll Setup readiness; never consult legacy settings.
    for branch_id in branch_ids:
        ready, _ = await branch_schedule_readiness(company_id, branch_id, db)
        if ready:
            _put(branch_id, "payroll_setup_done", True)

    # 2 — Active status-keys count.
    try:
        r = await db.execute(
            text(f"""
                SELECT branchid, COUNT(*) AS cnt
                FROM   payroll.payrollstatuskeys
                WHERE  companyid = :cid
                  AND  isactive  = TRUE
                  AND  branchid IN ({in_clause})
                GROUP  BY branchid
            """),
            base,
        )
        for row in r.mappings().all():
            _put(row["branchid"], "status_keys_count", int(row["cnt"]))
    except Exception:
        pass

    # 3 — Total people count (core.employees, all employment types).
    try:
        r = await db.execute(
            text(f"""
                SELECT branchid, COUNT(*) AS cnt
                FROM   core.employees
                WHERE  companyid = :cid
                  AND  branchid IN ({in_clause})
                GROUP  BY branchid
            """),
            base,
        )
        for row in r.mappings().all():
            _put(row["branchid"], "total_people_count", int(row["cnt"]))
    except Exception:
        pass

    # 4 — Active drivers count (DriverStatus=Active AND EmploymentStatus=Active).
    try:
        r = await db.execute(
            text(f"""
                SELECT d.branchid, COUNT(*) AS cnt
                FROM   core.drivers   d
                JOIN   core.employees e
                       ON  e.companyid  = d.companyid
                       AND e.employeeid = d.employeeid
                WHERE  d.companyid       = :cid
                  AND  d.driverstatus    = 'Active'
                  AND  e.employmentstatus = 'Active'
                  AND  d.branchid IN ({in_clause})
                GROUP  BY d.branchid
            """),
            base,
        )
        for row in r.mappings().all():
            _put(row["branchid"], "active_drivers_count", int(row["cnt"]))
    except Exception:
        pass

    # 5 — Pending review-items count.
    try:
        r = await db.execute(
            text(f"""
                SELECT branchid, COUNT(*) AS cnt
                FROM   review.managerreviewitems
                WHERE  companyid = :cid
                  AND  status    = 'Pending'
                  AND  branchid IN ({in_clause})
                GROUP  BY branchid
            """),
            base,
        )
        for row in r.mappings().all():
            _put(row["branchid"], "pending_approvals_count", int(row["cnt"]))
    except Exception:
        pass

    return result


_BRANCH_COLS = """
    branchid, companyid, branchcode, branchname,
    status, isdefault,
    addressline1, city, stateprovince, postalcode, country,
    notes, createdatutc, updatedatutc
"""


def _row_to_branch_admin(row, metrics: dict) -> BranchAdmin:
    """Build a BranchAdmin schema object from a DB row and a metrics dict."""
    bid = row["branchid"]
    m = metrics.get(bid, {})
    return BranchAdmin(
        branch_id=bid,
        company_id=row["companyid"],
        branch_code=row["branchcode"],
        branch_name=row["branchname"],
        status=row["status"],
        is_default=bool(row["isdefault"]),
        address_line1=row.get("addressline1"),
        city=row.get("city"),
        state_province=row.get("stateprovince"),
        postal_code=row.get("postalcode"),
        country=row.get("country"),
        notes=row.get("notes"),
        created_at_utc=row["createdatutc"],
        updated_at_utc=row.get("updatedatutc"),
        payroll_setup_done=m.get("payroll_setup_done", False),
        status_keys_count=m.get("status_keys_count"),
        total_people_count=m.get("total_people_count"),
        active_drivers_count=m.get("active_drivers_count"),
        pending_approvals_count=m.get("pending_approvals_count"),
    )


async def get_branches(
    company_id: int,
    user_id: int,
    db: AsyncConnection,
) -> list[BranchAdmin]:
    """
    Return branches visible to the user, ordered default-first, with metrics.

    Company admins see all branches; branch-scoped users see only their own.
    """
    can_see_all, branch_ids = await check_branch_access(company_id, user_id, db)

    filters = ["companyid = :cid"]
    params: dict = {"cid": company_id}

    if not can_see_all:
        if not branch_ids:
            return []
        in_clause, in_params = build_in_clause(branch_ids, "bid")
        filters.append(f"branchid IN ({in_clause})")
        params.update(in_params)

    where = " AND ".join(filters)
    result = await db.execute(
        text(f"""
            SELECT {_BRANCH_COLS}
            FROM   core.branches
            WHERE  {where}
            ORDER  BY isdefault DESC, status, branchname, branchcode
        """),
        params,
    )
    rows = result.mappings().all()
    if not rows:
        return []

    all_ids = [r["branchid"] for r in rows]
    metrics = await _fetch_branch_metrics(company_id, all_ids, db)
    branches = [_row_to_branch_admin(r, metrics) for r in rows]
    await _attach_readiness_reasons(branches, company_id, user_id, db)
    return branches


async def get_branch_by_id(
    branch_id: int,
    company_id: int,
    user_id: int,
    db: AsyncConnection,
) -> BranchAdmin:
    """Fetch a single branch with metrics. Enforces branch-level access."""
    can_see_all, branch_ids = await check_branch_access(company_id, user_id, db)
    if not can_see_all and branch_id not in branch_ids:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have access to this branch.",
        )

    result = await db.execute(
        text(f"""
            SELECT {_BRANCH_COLS}
            FROM   core.branches
            WHERE  branchid  = :bid
              AND  companyid = :cid
        """),
        {"bid": branch_id, "cid": company_id},
    )
    row = result.mappings().first()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Branch {branch_id} not found.",
        )

    metrics = await _fetch_branch_metrics(company_id, [branch_id], db)
    branch = _row_to_branch_admin(row, metrics)
    await _attach_readiness_reasons([branch], company_id, user_id, db)
    return branch


async def create_branch(
    company_id: int,
    user_id: int,
    data: BranchCreate,
    db: AsyncConnection,
) -> BranchAdmin:
    """
    Create a new branch for the company.

    branch_code is auto-generated from branch_name when omitted.
    If is_default=True, all existing defaults are cleared first.
    Requires AllCompanyBranches scope.
    """
    await security.ensure_branch_creator(
        company_id, user_id, db,
        with_payroll_start=data.first_payroll_start_date is not None,
        with_default=data.is_default,
    )

    default_setup_id = None
    if data.first_payroll_start_date is not None:
        await lock_company(company_id, db)
        default_result = await db.execute(text(
            "SELECT DefaultPayrollSetupID FROM core.Companies WHERE CompanyID = :cid"
        ), {"cid": company_id})
        default_setup_id = default_result.scalar_one_or_none()
        if default_setup_id is not None:
            await lock_setups(company_id, [default_setup_id], db)
            setup_result = await db.execute(text("""
                SELECT Status FROM payroll.PayrollSetups
                WHERE CompanyID = :cid AND PayrollSetupID = :sid
            """), {"cid": company_id, "sid": default_setup_id})
            setup_status = setup_result.scalar_one_or_none()
            if setup_status is None:
                raise PolicyError("SETUP_NOT_FOUND", "Company default Payroll Setup does not belong to Company")
            if setup_status != "Active":
                raise PolicyError("SETUP_NOT_ACTIVE", "Company default Payroll Setup is not Active")

    explicit_code = bool(data.branch_code)
    branch_code = data.branch_code.upper() if explicit_code else _generate_branch_code()

    # Clear existing defaults before inserting the new one.
    if data.is_default:
        await db.execute(
            text("""
                UPDATE core.branches
                SET    isdefault    = FALSE,
                       updatedatutc = NOW()
                WHERE  companyid = :cid AND isdefault = TRUE
            """),
            {"cid": company_id},
        )

    _INSERT_BRANCH_SQL = text("""
        INSERT INTO core.branches (
            companyid, branchcode, branchname, status, isdefault,
            addressline1, city, stateprovince, postalcode, country, notes
        ) VALUES (
            :cid, :code, :name, :st, :def,
            :addr, :city, :sp, :pc, :country, :notes
        )
        RETURNING branchid
    """)

    _MAX_RETRIES = 10
    ins = None
    for attempt in range(_MAX_RETRIES):
        try:
            # Use a savepoint so that a constraint violation on the INSERT does
            # not abort the outer transaction, allowing retries to proceed.
            async with db.begin_nested():
                ins = await db.execute(
                    _INSERT_BRANCH_SQL,
                    {
                        "cid":     company_id,
                        "code":    branch_code,
                        "name":    data.branch_name,
                        "st":      data.status,
                        "def":     data.is_default,
                        "addr":    data.address_line1,
                        "city":    data.city,
                        "sp":      data.state_province,
                        "pc":      data.postal_code,
                        "country": data.country,
                        "notes":   data.notes,
                    },
                )
            break  # success — savepoint released
        except SAIntegrityError as exc:
            msg = str(exc.orig).lower() if exc.orig else str(exc).lower()
            if not explicit_code and "uq_branches_company_code" in msg:
                # Auto-generated code collided — retry with a new one.
                if attempt < _MAX_RETRIES - 1:
                    branch_code = _generate_branch_code()
                    continue
                raise HTTPException(
                    status_code=500,
                    detail="Could not generate a unique branch code. Please try again.",
                )
            _handle_branch_integrity_error(exc)

    if ins is None:
        raise HTTPException(
            status_code=500,
            detail="Could not generate a unique branch code. Please try again.",
        )

    new_id = ins.scalar_one()

    await audit.write_settings_audit(
        db,
        company_id=company_id,
        branch_id=new_id,
        user_id=user_id,
        action_code="BRANCH_CREATED",
        entity_name="Branches",
        entity_id=str(new_id),
        new_value={
            "branch_code": branch_code,
            "branch_name": data.branch_name,
            "status":      data.status,
            "is_default":  data.is_default,
        },
    )

    # CP-2D2: every new branch gets a default Status Pay rate column automatically.
    await status_rate_columns.ensure_default_status_rate_column_for_branch(company_id, new_id, db)

    if data.first_payroll_start_date is not None and default_setup_id is not None:
        await assign_setup(
            company_id, user_id, new_id, default_setup_id,
            data.first_payroll_start_date, db,
            reason="Initial branch Payroll Setup assignment",
        )

    created = await get_branch_by_id(new_id, company_id, user_id, db)
    ready, reason = await branch_schedule_readiness(
        company_id, new_id, db, period_start_date=data.first_payroll_start_date,
    )
    created.payroll_setup_done = ready
    if data.first_payroll_start_date is not None:
        # The supplied-date path already passed company-wide payroll_setup.assign.
        created.schedule_readiness_reason = (
            "NO_COMPANY_DEFAULT" if default_setup_id is None else reason
        )
        created.schedule_readiness_date = data.first_payroll_start_date
    return created


async def update_branch(
    branch_id: int,
    company_id: int,
    user_id: int,
    data: BranchUpdate,
    db: AsyncConnection,
) -> BranchAdmin:
    """
    Partially update a branch.  Only non-None fields in BranchUpdate are applied.

    is_default is NOT changed here — use set_default_branch() for that.
    Requires AllCompanyBranches scope.
    Business rule: cannot deactivate the current default branch.
    """
    await security.ensure_company_admin(company_id, user_id, db)

    # Fetch and lock the target row to prevent concurrent updates.
    cur = await db.execute(
        text(f"""
            SELECT {_BRANCH_COLS}
            FROM   core.branches
            WHERE  branchid  = :bid
              AND  companyid = :cid
            FOR UPDATE
        """),
        {"bid": branch_id, "cid": company_id},
    )
    row = cur.mappings().first()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Branch {branch_id} not found.",
        )

    # Merge: use patch value when provided, otherwise keep the current DB value.
    new_name   = data.branch_name    if data.branch_name    is not None else row["branchname"]
    new_code   = data.branch_code    if data.branch_code    is not None else row["branchcode"]
    new_status = data.status         if data.status         is not None else row["status"]
    new_addr   = data.address_line1  if data.address_line1  is not None else row.get("addressline1")
    new_city   = data.city           if data.city           is not None else row.get("city")
    new_sp     = data.state_province if data.state_province is not None else row.get("stateprovince")
    new_pc     = data.postal_code    if data.postal_code    is not None else row.get("postalcode")
    new_co     = data.country        if data.country        is not None else row.get("country")
    new_notes  = data.notes          if data.notes          is not None else row.get("notes")

    # Business rule: the default branch cannot be deactivated.
    if bool(row["isdefault"]) and new_status != "Active":
        raise HTTPException(
            status_code=422,
            detail="Set another branch as default before deactivating this branch.",
        )

    try:
        await db.execute(
            text("""
                UPDATE core.branches
                SET    branchcode    = :code,
                       branchname    = :name,
                       status        = :st,
                       addressline1  = :addr,
                       city          = :city,
                       stateprovince = :sp,
                       postalcode    = :pc,
                       country       = :co,
                       notes         = :notes,
                       updatedatutc  = NOW()
                WHERE  branchid  = :bid
                  AND  companyid = :cid
            """),
            {
                "bid":   branch_id,
                "cid":   company_id,
                "code":  new_code,
                "name":  new_name,
                "st":    new_status,
                "addr":  new_addr,
                "city":  new_city,
                "sp":    new_sp,
                "pc":    new_pc,
                "co":    new_co,
                "notes": new_notes,
            },
        )
    except SAIntegrityError as exc:
        _handle_branch_integrity_error(exc)

    await audit.write_settings_audit(
        db,
        company_id=company_id,
        branch_id=branch_id,
        user_id=user_id,
        action_code="BRANCH_UPDATED",
        entity_name="Branches",
        entity_id=str(branch_id),
        old_value={
            "branch_code":    row["branchcode"],
            "branch_name":    row["branchname"],
            "status":         row["status"],
            "address_line1":  row.get("addressline1"),
            "city":           row.get("city"),
            "state_province": row.get("stateprovince"),
            "postal_code":    row.get("postalcode"),
            "country":        row.get("country"),
            "notes":          row.get("notes"),
        },
        new_value={
            "branch_code":    new_code,
            "branch_name":    new_name,
            "status":         new_status,
            "address_line1":  new_addr,
            "city":           new_city,
            "state_province": new_sp,
            "postal_code":    new_pc,
            "country":        new_co,
            "notes":          new_notes,
        },
    )

    return await get_branch_by_id(branch_id, company_id, user_id, db)


async def set_default_branch(
    branch_id: int,
    company_id: int,
    user_id: int,
    db: AsyncConnection,
) -> BranchAdmin:
    """
    Promote a branch to the company default.

    The branch must be Active.  All other branches have their isdefault
    flag cleared atomically within the same transaction.
    Requires AllCompanyBranches scope.
    """
    await security.ensure_company_admin(company_id, user_id, db)

    # Fetch and lock the target branch.
    cur = await db.execute(
        text(f"""
            SELECT {_BRANCH_COLS}
            FROM   core.branches
            WHERE  branchid  = :bid
              AND  companyid = :cid
            FOR UPDATE
        """),
        {"bid": branch_id, "cid": company_id},
    )
    row = cur.mappings().first()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Branch {branch_id} not found.",
        )

    if row["status"] != "Active":
        raise HTTPException(
            status_code=422,
            detail="Only an active branch can be the default branch.",
        )

    # Idempotent: if already the default, return without unnecessary writes.
    if bool(row["isdefault"]):
        metrics = await _fetch_branch_metrics(company_id, [branch_id], db)
        return _row_to_branch_admin(row, metrics)

    # Clear all current defaults, then promote this branch.
    await db.execute(
        text("""
            UPDATE core.branches
            SET    isdefault    = FALSE,
                   updatedatutc = NOW()
            WHERE  companyid = :cid AND isdefault = TRUE
        """),
        {"cid": company_id},
    )
    try:
        await db.execute(
            text("""
                UPDATE core.branches
                SET    isdefault    = TRUE,
                       updatedatutc = NOW()
                WHERE  branchid  = :bid AND companyid = :cid
            """),
            {"bid": branch_id, "cid": company_id},
        )
    except SAIntegrityError as exc:
        _handle_branch_integrity_error(exc)

    await audit.write_settings_audit(
        db,
        company_id=company_id,
        branch_id=branch_id,
        user_id=user_id,
        action_code="BRANCH_DEFAULT_CHANGED",
        entity_name="Branches",
        entity_id=str(branch_id),
        old_value={"is_default": False},
        new_value={"is_default": True},
    )

    return await get_branch_by_id(branch_id, company_id, user_id, db)


async def get_onboarding_options(
    company_id: int,
    user_id: int,
    db: AsyncConnection,
    *,
    around: _date | None = None,
) -> OnboardingOptionsResponse:
    """
    Return the company's default Payroll Setup together with the canonical
    boundary choices (nearest valid previous/next first-payroll dates and a
    server-suggested date) for onboarding a not-yet-created Branch onto it.

    default_setup is null when the company has no default Payroll Setup, in
    which case choices is also null — an archived default is NOT special-cased
    here; boundaries.onboarding_choices reports it as a SETUP_NOT_ACTIVE
    conflict like any other invalid date.

    Authorization mirrors the create-with-date write this feeds
    (POST /settings/branches with first_payroll_start_date): company-wide
    access, branches.create, and payroll_setup.assign.
    """
    await security.ensure_branch_creator(
        company_id, user_id, db, with_payroll_start=True, with_default=False,
    )
    result = await db.execute(text("""
        SELECT s.PayrollSetupID, s.SetupCode, s.SetupName, s.Description, s.Status
        FROM core.Companies c
        LEFT JOIN payroll.PayrollSetups s
          ON s.CompanyID = c.CompanyID AND s.PayrollSetupID = c.DefaultPayrollSetupID
        WHERE c.CompanyID = :cid
    """), {"cid": company_id})
    row = result.mappings().one_or_none()
    if row is None:
        raise PolicyError("COMPANY_NOT_FOUND", "Company not found")
    if row["payrollsetupid"] is None:
        return OnboardingOptionsResponse(default_setup=None, choices=None)
    default_setup = SetupResponse(
        setup_id=row["payrollsetupid"], setup_code=row["setupcode"],
        setup_name=row["setupname"], description=row["description"],
        status=row["status"],
    )
    choices = await boundaries.onboarding_choices(
        company_id, row["payrollsetupid"], around, db,
    )
    return OnboardingOptionsResponse(default_setup=default_setup, choices=choices)
