"""
Settings domain service — company profile, branch administration,
branch payroll setup, and payroll status keys.

Security model:
  - Reading company profile: any authenticated user in this company.
  - Reading branches / payroll setup / status keys: branch-scoped.
  - All writes require AllCompanyBranches scope (_ensure_company_admin).

Audit logging:
  - All writes emit events to audit.auditlog inside the same transaction.
  - A failure in _write_settings_audit rolls back the primary write too.

Race safety:
  - update_branch, set_default_branch, and update_status_key use
    SELECT … FOR UPDATE to prevent concurrent modifications.

All database access is raw parameterised SQL via sqlalchemy.text().
"""
import json
import re
import secrets
import string as _string

from fastapi import HTTPException, status
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError as SAIntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.service import _check_branch_access, _check_permission, _build_in_clause
from app.settings.schemas import (
    BranchAdmin,
    BranchCreate,
    BranchUpdate,
    CompanyProfile,
    CompanyUpdate,
    PayrollSetup,
    PayrollSetupUpsert,
    StatusKey,
    StatusKeyCreate,
    StatusKeyUpdate,
    StatusRateColumn,
    StatusRateColumnCreate,
    PayItemRateTypeMapCreate,
    PayItemRateTypeMapSummary,
)


# ---------------------------------------------------------------------------
# Custom pay item code generation
# ---------------------------------------------------------------------------

# Characters used in auto-generated CPI_ codes.
# Ambiguous characters (O / 0 / I / 1 / L) are excluded so printed codes
# are easy to read and transcribe without error.
_CPI_CHARSET: str = "".join(
    c for c in (_string.ascii_uppercase + _string.digits)
    if c not in "O0I1L"
)
_CPI_PREFIX:   str = "CPI_"
_CPI_SUFFIX_LEN: int = 8
_CPI_MAX_RETRIES: int = 10  # collision is astronomically unlikely; 10 gives a clean ceiling

_SRC_SUFFIX_LEN: int = 8  # SRC_{company_id}_{8 chars}


def _generate_pay_item_code() -> str:
    """
    Return a new candidate custom pay item code: CPI_ + 8 random characters.

    Characters are drawn from uppercase letters + digits, excluding visually
    ambiguous characters (O, 0, I, 1, L).  With ~32 usable characters and an
    8-character suffix, the keyspace is 32^8 ≈ 1 trillion, so collisions are
    practically impossible in any real deployment.

    Separated into its own function so tests can monkeypatch it to exercise
    the retry path without actually creating database rows.
    """
    return _CPI_PREFIX + "".join(
        secrets.choice(_CPI_CHARSET) for _ in range(_CPI_SUFFIX_LEN)
    )


def _generate_src_rate_code(company_id: int) -> str:
    """Return a new candidate StatusRateColumn RateCode: SRC_{company_id}_{8 chars}."""
    suffix = "".join(secrets.choice(_CPI_CHARSET) for _ in range(_SRC_SUFFIX_LEN))
    return f"SRC_{company_id}_{suffix}"


# ---------------------------------------------------------------------------
# Audit helper
# ---------------------------------------------------------------------------

_SETTINGS_AUDIT_REASONS: dict[str, str] = {
    "COMPANY_UPDATED":           "Company profile updated",
    "BRANCH_CREATED":            "Branch created",
    "BRANCH_UPDATED":            "Branch updated",
    "BRANCH_DEFAULT_CHANGED":    "Default branch changed",
    "PAYROLL_SETUP_SAVED":       "Branch payroll setup saved",
    "STATUS_KEY_CREATED":        "Payroll status key created",
    "STATUS_KEY_UPDATED":        "Payroll status key updated",
    "STATUS_KEY_DEACTIVATED":    "Payroll status key deactivated",
    "PAY_ITEM_RATE_TYPE_ASSIGNED": "Rate type mapped to pay item",
}


async def _write_settings_audit(
    db: AsyncConnection,
    *,
    company_id: int,
    branch_id: int | None,
    user_id: int,
    action_code: str,
    entity_name: str,
    entity_id: str,
    old_value: dict | None = None,
    new_value: dict | None = None,
) -> None:
    """
    Insert one row into audit.AuditLog for a settings-domain event.

    Extracted as a module-level function so tests can monkeypatch it to verify
    that all preceding writes roll back when this raises.
    """
    await db.execute(
        text("""
            INSERT INTO audit.auditlog
                (companyid, branchid, actoruserid, actioncode,
                 entityschema, entityname, entityid,
                 oldvaluejson, newvaluejson, reason, sourcetype)
            VALUES
                (:company_id, :branch_id, :actor_id, :action_code,
                 'core', :entity_name, :entity_id,
                 :old_val, :new_val, :reason, 'Application')
        """),
        {
            "company_id":  company_id,
            "branch_id":   branch_id,
            "actor_id":    user_id,
            "action_code": action_code,
            "entity_name": entity_name,
            "entity_id":   entity_id,
            "old_val":     json.dumps(old_value) if old_value is not None else None,
            "new_val":     json.dumps(new_value) if new_value is not None else None,
            "reason":      _SETTINGS_AUDIT_REASONS.get(action_code, action_code),
        },
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

async def _ensure_company_admin(
    company_id: int,
    user_id: int,
    db: AsyncConnection,
) -> None:
    """
    Raise HTTP 403 unless the user has AllCompanyBranches scope AND the
    ``setup.manage`` permission.

    Two gates:
      1. Scope gate — _check_branch_access raises 403 if no access at all;
         then we verify the scope is company-wide, not just a single branch.
      2. Permission gate — _check_permission verifies setup.manage is granted.
         branch_id=None means only AllCompanyBranches assignments are matched,
         so SpecificBranch users cannot satisfy this even if they somehow pass
         gate 1 (they won't, but the defense-in-depth is intentional).
    """
    can_see_all, _ = await _check_branch_access(company_id, user_id, db)
    if not can_see_all:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This action requires company-level (all-branches) access.",
        )
    await _check_permission(company_id, user_id, None, "setup.manage", db)


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
    Run 5 branch-metric queries against the given branch IDs.

    Each query is individually wrapped in try/except so a missing table or
    failing index on one metric does not prevent the other four from loading.

    Returns dict[branch_id → {metric_name: value}].  A missing key in the
    inner dict means that metric query failed; callers substitute safe
    defaults (False / 0 / None).
    """
    if not branch_ids:
        return {}

    in_clause, in_params = _build_in_clause(branch_ids, "bid")
    base: dict = {"cid": company_id, **in_params}
    result: dict[int, dict] = {}

    def _put(bid: int, key: str, val) -> None:
        result.setdefault(bid, {})[key] = val

    # 1 — Payroll setup done: branch has an active BranchPayrollSettings row.
    try:
        r = await db.execute(
            text(f"""
                SELECT branchid
                FROM   payroll.branchpayrollsettings
                WHERE  companyid = :cid
                  AND  isactive  = TRUE
                  AND  branchid IN ({in_clause})
            """),
            base,
        )
        for row in r.mappings().all():
            _put(row["branchid"], "payroll_setup_done", True)
    except Exception:
        pass  # graceful degradation — key stays absent

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


# Full branch column list reused across all SELECT statements.
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


# ---------------------------------------------------------------------------
# Public service functions
# ---------------------------------------------------------------------------

async def get_company_profile(
    company_id: int,
    user_id: int,
    db: AsyncConnection,
) -> CompanyProfile:
    """
    Return the company profile including the default branch name.

    Any authenticated user in the company can read this.
    """
    # Verifies the user has at least some access to this company.
    await _check_branch_access(company_id, user_id, db)

    result = await db.execute(
        text("""
            SELECT
                c.companyid,
                c.companycode,
                c.companyname,
                c.legalname,
                c.status,
                c.issuspended,
                c.timezonename,
                c.notes,
                c.allowselfapproval,
                c.createdatutc,
                c.updatedatutc,
                b.branchid   AS default_branch_id,
                b.branchname AS default_branch_name
            FROM  core.companies c
            LEFT  JOIN core.branches b
                   ON  b.companyid = c.companyid
                   AND b.isdefault = TRUE
            WHERE  c.companyid = :cid
            ORDER  BY b.branchname NULLS LAST
            LIMIT  1
        """),
        {"cid": company_id},
    )
    row = result.mappings().first()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Company not found.",
        )

    return CompanyProfile(
        company_id=row["companyid"],
        company_code=row["companycode"],
        company_name=row["companyname"],
        legal_name=row.get("legalname"),
        status=row["status"],
        is_suspended=bool(row["issuspended"]),
        timezone_name=row["timezonename"],
        notes=row.get("notes"),
        allow_self_approval=bool(row.get("allowselfapproval", True)),
        default_branch_id=row.get("default_branch_id"),
        default_branch_name=row.get("default_branch_name"),
        created_at_utc=row["createdatutc"],
        updated_at_utc=row.get("updatedatutc"),
    )


async def update_company_profile(
    company_id: int,
    user_id: int,
    data: CompanyUpdate,
    db: AsyncConnection,
) -> CompanyProfile:
    """
    Update editable company profile fields (name, legal name, timezone, notes).

    Status and IsSuspended are system-controlled and cannot be changed here.
    Requires AllCompanyBranches scope.
    """
    await _ensure_company_admin(company_id, user_id, db)

    # Read current values for the audit snapshot.
    current = await get_company_profile(company_id, user_id, db)

    # Preserve existing values when none are supplied in the patch.
    new_tz             = data.timezone_name      if data.timezone_name      is not None else current.timezone_name
    new_self_approval  = data.allow_self_approval if data.allow_self_approval is not None else current.allow_self_approval

    await db.execute(
        text("""
            UPDATE core.companies
            SET    companyname       = :name,
                   legalname         = :legal,
                   timezonename      = :tz,
                   notes             = :notes,
                   allowselfapproval = :allow_self,
                   updatedatutc      = NOW()
            WHERE  companyid = :cid
        """),
        {
            "cid":        company_id,
            "name":       data.company_name,
            "legal":      data.legal_name,
            "tz":         new_tz,
            "notes":      data.notes,
            "allow_self": new_self_approval,
        },
    )

    await _write_settings_audit(
        db,
        company_id=company_id,
        branch_id=None,
        user_id=user_id,
        action_code="COMPANY_UPDATED",
        entity_name="Companies",
        entity_id=str(company_id),
        old_value={
            "company_name":       current.company_name,
            "legal_name":         current.legal_name,
            "timezone_name":      current.timezone_name,
            "notes":              current.notes,
            "allow_self_approval": current.allow_self_approval,
        },
        new_value={
            "company_name":       data.company_name,
            "legal_name":         data.legal_name,
            "timezone_name":      new_tz,
            "notes":              data.notes,
            "allow_self_approval": new_self_approval,
        },
    )

    return await get_company_profile(company_id, user_id, db)


async def get_branches(
    company_id: int,
    user_id: int,
    db: AsyncConnection,
) -> list[BranchAdmin]:
    """
    Return branches visible to the user, ordered default-first, with metrics.

    Company admins see all branches; branch-scoped users see only their own.
    """
    can_see_all, branch_ids = await _check_branch_access(company_id, user_id, db)

    filters = ["companyid = :cid"]
    params: dict = {"cid": company_id}

    if not can_see_all:
        if not branch_ids:
            return []
        in_clause, in_params = _build_in_clause(branch_ids, "bid")
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
    return [_row_to_branch_admin(r, metrics) for r in rows]


async def get_branch_by_id(
    branch_id: int,
    company_id: int,
    user_id: int,
    db: AsyncConnection,
) -> BranchAdmin:
    """Fetch a single branch with metrics. Enforces branch-level access."""
    can_see_all, branch_ids = await _check_branch_access(company_id, user_id, db)
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
    return _row_to_branch_admin(row, metrics)


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
    await _ensure_company_admin(company_id, user_id, db)

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

    await _write_settings_audit(
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
    await _ensure_default_status_rate_column_for_branch(company_id, new_id, db)

    return await get_branch_by_id(new_id, company_id, user_id, db)


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
    await _ensure_company_admin(company_id, user_id, db)

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

    await _write_settings_audit(
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
    await _ensure_company_admin(company_id, user_id, db)

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

    await _write_settings_audit(
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


# ===========================================================================
# Branch payroll setup
# ===========================================================================

_SETUP_COLS = """
    s.branchpayrollsettingsid, s.companyid, s.branchid,
    b.branchname,
    s.payrollfrequency, s.anchorstartdate, s.paydateoffsetdays,
    s.paydayofweek, s.firstpaydate, s.includepaydayasworkday,
    s.normaldaysoffmask, s.customintervaldays, s.isactive, s.notes,
    s.createdatutc, s.updatedatutc,
    s.currentscheduleversionid
"""


def _row_to_payroll_setup(row) -> PayrollSetup:
    return PayrollSetup(
        settings_id=row["branchpayrollsettingsid"],
        company_id=row["companyid"],
        branch_id=row["branchid"],
        branch_name=row.get("branchname"),
        payroll_frequency=row["payrollfrequency"],
        anchor_start_date=row["anchorstartdate"],
        pay_date_offset_days=row.get("paydateoffsetdays") or 0,
        pay_day_of_week=row.get("paydayofweek"),
        first_pay_date=row.get("firstpaydate"),
        include_pay_day_as_work_day=bool(row.get("includepaydayasworkday", False)),
        normal_days_off_mask=row.get("normaldaysoffmask"),
        custom_interval_days=row.get("customintervaldays"),
        is_active=bool(row.get("isactive", True)),
        notes=row.get("notes"),
        created_at_utc=row["createdatutc"],
        updated_at_utc=row.get("updatedatutc"),
        schedule_version_id=row.get("currentscheduleversionid"),
    )


async def create_schedule_version_for_setup(
    company_id: int,
    branch_id: int,
    user_id: int,
    data: "PayrollSetupUpsert",
    db: AsyncConnection,
) -> int:
    """
    Insert an immutable PayrollScheduleVersions row for a setup upsert and
    update BranchPayrollSettings.CurrentScheduleVersionID.

    Must be called inside the same transaction as the settings upsert, after
    the upsert has already committed the new settings values, while still
    holding the branch workflow advisory lock.

    Returns the new ScheduleVersionID.
    """
    # Canonical config hash: same format as payroll._setup_fingerprint
    config_hash = json.dumps(
        {
            "anchor":   str(data.anchor_start_date),
            "freq":     data.payroll_frequency,
            "interval": data.custom_interval_days,
        },
        sort_keys=True,
        separators=(",", ":"),
    )

    # Next version number = max(existing) + 1 for this branch
    max_row = await db.execute(
        text("""
            SELECT COALESCE(MAX(versionnumber), 0) AS maxver
            FROM   payroll.PayrollScheduleVersions
            WHERE  companyid = :cid AND branchid = :bid
        """),
        {"cid": company_id, "bid": branch_id},
    )
    next_version = (max_row.scalar_one() or 0) + 1

    ins = await db.execute(
        text("""
            INSERT INTO payroll.PayrollScheduleVersions
                (CompanyID, BranchID, VersionNumber,
                 PayrollFrequency, AnchorStartDate,
                 CustomIntervalDays, NormalDaysOffMask,
                 PayDayOfWeek, FirstPayDate, IncludePayDayAsWorkDay,
                 EffectiveFromDate, EffectiveToDate,
                 CreatedByUserID, SourceAction, ConfigHash)
            VALUES
                (:cid, :bid, :vnum,
                 :freq, :anchor,
                 :interval_days, :mask,
                 :pdow, :fpd, :incl,
                 :anchor, NULL,
                 :uid, 'SETUP_UPDATED', :chash)
            RETURNING ScheduleVersionID
        """),
        {
            "cid":           company_id,
            "bid":           branch_id,
            "vnum":          next_version,
            "freq":          data.payroll_frequency,
            "anchor":        data.anchor_start_date,
            "interval_days": data.custom_interval_days,
            "mask":          data.normal_days_off_mask,
            "pdow":          data.pay_day_of_week,
            "fpd":           data.first_pay_date,
            "incl":          bool(data.include_pay_day_as_work_day),
            "uid":           user_id,
            "chash":         config_hash,
        },
    )
    new_sv_id: int = ins.scalar_one()

    await db.execute(
        text("""
            UPDATE payroll.BranchPayrollSettings
            SET    CurrentScheduleVersionID = :sv_id
            WHERE  CompanyID = :cid AND BranchID = :bid
        """),
        {"sv_id": new_sv_id, "cid": company_id, "bid": branch_id},
    )

    return new_sv_id


async def get_payroll_setup(
    branch_id: int,
    company_id: int,
    user_id: int,
    db: AsyncConnection,
) -> PayrollSetup:
    """
    Return the payroll-schedule configuration for a branch.
    Raises 404 if no configuration has been saved yet.
    Any authenticated user with branch access can read this.
    """
    can_see_all, branch_ids = await _check_branch_access(company_id, user_id, db)
    if not can_see_all and branch_id not in branch_ids:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have access to this branch.",
        )

    result = await db.execute(
        text(f"""
            SELECT {_SETUP_COLS}
            FROM   payroll.branchpayrollsettings s
            JOIN   core.branches b
                   ON  b.branchid  = s.branchid
                   AND b.companyid = s.companyid
            WHERE  s.branchid  = :bid
              AND  s.companyid = :cid
        """),
        {"bid": branch_id, "cid": company_id},
    )
    row = result.mappings().first()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No payroll setup found for branch {branch_id}.",
        )
    return _row_to_payroll_setup(row)


async def upsert_payroll_setup(
    branch_id: int,
    company_id: int,
    user_id: int,
    data: PayrollSetupUpsert,
    db: AsyncConnection,
) -> PayrollSetup:
    """
    Create or fully replace the payroll-schedule configuration for a branch.

    Uses PostgreSQL ON CONFLICT … DO UPDATE for an atomic upsert.
    Requires AllCompanyBranches scope.
    """
    await _ensure_company_admin(company_id, user_id, db)

    # Verify the branch belongs to this company.
    branch_check = await db.execute(
        text("SELECT branchid FROM core.branches WHERE branchid=:bid AND companyid=:cid"),
        {"bid": branch_id, "cid": company_id},
    )
    if branch_check.first() is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Branch {branch_id} not found.",
        )

    # Acquire the branch workflow advisory lock before reading or mutating
    # BranchPayrollSettings.  This is the same transaction-level lock used by
    # CP-1C candidate creation, ensuring setup changes and period creation are
    # fully serialized per branch.  Lock is released when the transaction
    # commits or rolls back.
    await db.execute(
        text("SELECT pg_advisory_xact_lock(:cid, :bid)"),
        {"cid": company_id, "bid": branch_id},
    )

    # Safety guard: the new anchor_start_date must not fall inside or before
    # any existing non-cancelled payroll period for this branch.
    # Changing the anchor to a date ≤ the last existing period would cause the
    # next period calculation to generate dates that overlap historical records.
    max_end_row = await db.execute(
        text("""
            SELECT MAX(enddate) AS max_end
            FROM   payroll.payrollperiods
            WHERE  branchid  = :bid
              AND  companyid = :cid
              AND  status    != 'Cancelled'
        """),
        {"bid": branch_id, "cid": company_id},
    )
    max_end = max_end_row.scalar_one_or_none()
    if max_end is not None and data.anchor_start_date <= max_end:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Cannot set anchor_start_date to {data.anchor_start_date}: "
                f"existing payroll periods for this branch extend to {max_end}. "
                f"The new anchor must be after {max_end}. "
                f"Cancel all active periods first, or choose a later start date."
            ),
        )

    await db.execute(
        text("""
            INSERT INTO payroll.branchpayrollsettings (
                companyid, branchid,
                payrollfrequency, anchorstartdate, paydateoffsetdays,
                paydayofweek, firstpaydate, includepaydayasworkday,
                normaldaysoffmask, customintervaldays, isactive, createdbyuserid, notes
            ) VALUES (
                :cid, :bid,
                :freq, :anchor, 0,
                :pdow, :fpd, :incl,
                :mask, :interval_days, TRUE, :uid, :notes
            )
            ON CONFLICT (companyid, branchid) DO UPDATE
                SET payrollfrequency       = EXCLUDED.payrollfrequency,
                    anchorstartdate        = EXCLUDED.anchorstartdate,
                    paydateoffsetdays      = 0,
                    paydayofweek           = EXCLUDED.paydayofweek,
                    firstpaydate           = EXCLUDED.firstpaydate,
                    includepaydayasworkday = EXCLUDED.includepaydayasworkday,
                    normaldaysoffmask      = EXCLUDED.normaldaysoffmask,
                    customintervaldays     = EXCLUDED.customintervaldays,
                    isactive               = TRUE,
                    notes                  = EXCLUDED.notes,
                    updatedatutc           = NOW()
        """),
        {
            "cid":           company_id,
            "bid":           branch_id,
            "uid":           user_id,
            "freq":          data.payroll_frequency,
            "anchor":        data.anchor_start_date,
            "pdow":          data.pay_day_of_week,
            "fpd":           data.first_pay_date,
            "incl":          data.include_pay_day_as_work_day,
            "mask":          data.normal_days_off_mask,
            "interval_days": data.custom_interval_days,
            "notes":         data.notes,
        },
    )

    await _write_settings_audit(
        db,
        company_id=company_id,
        branch_id=branch_id,
        user_id=user_id,
        action_code="PAYROLL_SETUP_SAVED",
        entity_name="BranchPayrollSettings",
        entity_id=f"{company_id}:{branch_id}",
        new_value={
            "payroll_frequency":           data.payroll_frequency,
            "anchor_start_date":           str(data.anchor_start_date),
            "pay_day_of_week":             data.pay_day_of_week,
            "first_pay_date":              str(data.first_pay_date) if data.first_pay_date else None,
            "include_pay_day_as_work_day": data.include_pay_day_as_work_day,
            "normal_days_off_mask":        data.normal_days_off_mask,
            "custom_interval_days":        data.custom_interval_days,
        },
    )

    # CP-2A: create an immutable schedule version record for this setup upsert.
    # Runs inside the same transaction and branch advisory lock as the upsert above.
    await create_schedule_version_for_setup(company_id, branch_id, user_id, data, db)

    return await get_payroll_setup(branch_id, company_id, user_id, db)


# ===========================================================================
# Payroll status keys
# ===========================================================================

import random as _random
import string as _string

_SK_CODE_CHARS = _string.ascii_uppercase + _string.digits


def _generate_status_key_code() -> str:
    """Generate a random SK_XXXXXXXX status key code (8 uppercase alphanumerics)."""
    suffix = "".join(_random.choices(_SK_CODE_CHARS, k=8))
    return f"SK_{suffix}"


def _normalize_status_code(code: str) -> str:
    """Uppercase and strip — used for the NormalizedStatusCode uniqueness check."""
    return code.strip().upper()


def _row_to_status_key(row) -> StatusKey:
    return StatusKey(
        status_key_id=row["statuskeyid"],
        company_id=row["companyid"],
        branch_id=row["branchid"],
        key_name=row["keyname"],
        status_code=row["statuscode"],
        normalized_status_code=row["normalizedstatuscode"],
        hours_value=row["hoursvalue"],
        is_off_reason=bool(row["isoffreason"]),
        deducts_from_yearly_allowance=bool(row["deductsfromyearlyallowance"]),
        allowance_category=row.get("allowancecategory"),
        is_active=bool(row["isactive"]),
        display_order=int(row.get("displayorder") or 0),
        status_rate_column_id=row.get("statusratecolumnid"),
        # Usage limits
        limit_uses_per_period_enabled=bool(row.get("limitusesperperiodenabled") or False),
        limit_uses_per_period=row.get("limitusesperperiod"),
        limit_uses_per_driver_enabled=bool(row.get("limitusesperdriverenabled") or False),
        limit_uses_per_driver=row.get("limitusesperdriver"),
        limit_uses_across_drivers_enabled=bool(row.get("limitusesacrossdriversenabled") or False),
        limit_uses_across_drivers=row.get("limitusesacrossdrivers"),
        limit_uses_per_day_enabled=bool(row.get("limitusesperdayenabled") or False),
        limit_uses_per_day=row.get("limitusesperday"),
        created_at_utc=row["createdatutc"],
        updated_at_utc=row.get("updatedatutc"),
    )


_STATUS_KEY_COLS = """
    statuskeyid, companyid, branchid,
    keyname, statuscode, normalizedstatuscode,
    hoursvalue, isoffreason, deductsfromyearlyallowance,
    allowancecategory, isactive, displayorder,
    limitusesperperiodenabled, limitusesperperiod,
    limitusesperdriverenabled, limitusesperdriver,
    limitusesacrossdriversenabled, limitusesacrossdrivers,
    limitusesperdayenabled, limitusesperday,
    statusratecolumnid,
    createdatutc, updatedatutc
"""


def _validate_deduction_rules(
    deducts: bool,
    is_off_reason: bool,
    allowance_category: str | None,
) -> None:
    """Raise 422 if the deduction-related fields are inconsistent."""
    if deducts:
        raise HTTPException(
            status_code=422,
            detail="Yearly allowance tracking is not available yet.",
        )


async def _check_status_key_code_unique(
    branch_id: int,
    company_id: int,
    normalized_code: str,
    db: AsyncConnection,
    *,
    exclude_key_id: int | None = None,
) -> None:
    """Raise 422 if an active status key with the same normalized code exists."""
    params: dict = {
        "bid":  branch_id,
        "cid":  company_id,
        "code": normalized_code,
    }
    extra = ""
    if exclude_key_id is not None:
        extra = " AND statuskeyid <> :kid"
        params["kid"] = exclude_key_id

    result = await db.execute(
        text(f"""
            SELECT statuskeyid
            FROM   payroll.payrollstatuskeys
            WHERE  branchid             = :bid
              AND  companyid            = :cid
              AND  normalizedstatuscode = :code
              AND  isactive             = TRUE
              {extra}
        """),
        params,
    )
    if result.first() is not None:
        raise HTTPException(
            status_code=422,
            detail=(
                f"An active status key with code '{normalized_code}' "
                "already exists for this branch."
            ),
        )


async def get_status_keys(
    branch_id: int,
    company_id: int,
    user_id: int,
    db: AsyncConnection,
    *,
    include_inactive: bool = False,
) -> list[StatusKey]:
    """
    List status keys for a branch, ordered by display_order then code.
    By default returns only active keys; pass include_inactive=True for all.
    Any authenticated user with branch access can read this.
    """
    can_see_all, branch_ids = await _check_branch_access(company_id, user_id, db)
    if not can_see_all and branch_id not in branch_ids:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have access to this branch.",
        )

    filters = ["branchid = :bid", "companyid = :cid"]
    params: dict = {"bid": branch_id, "cid": company_id}
    if not include_inactive:
        filters.append("isactive = TRUE")

    where = " AND ".join(filters)
    result = await db.execute(
        text(f"""
            SELECT {_STATUS_KEY_COLS}
            FROM   payroll.payrollstatuskeys
            WHERE  {where}
            ORDER  BY keyname ASC
        """),
        params,
    )
    return [_row_to_status_key(r) for r in result.mappings().all()]


async def create_status_key(
    branch_id: int,
    company_id: int,
    user_id: int,
    data: StatusKeyCreate,
    db: AsyncConnection,
) -> StatusKey:
    """
    Create a new payroll status key for a branch.

    The status_code is generated server-side (SK_XXXXXXXX); the user provides
    key_name (the human-readable label).  Up to 5 retries on code collision.
    Requires AllCompanyBranches scope.
    """
    await _ensure_company_admin(company_id, user_id, db)

    # Verify the branch belongs to this company.
    branch_check = await db.execute(
        text("SELECT branchid FROM core.branches WHERE branchid=:bid AND companyid=:cid"),
        {"bid": branch_id, "cid": company_id},
    )
    if branch_check.first() is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Branch {branch_id} not found.",
        )

    _validate_deduction_rules(
        data.deducts_from_yearly_allowance,
        data.is_off_reason,
        data.allowance_category,
    )

    # Validate StatusRateColumnID if provided
    if data.status_rate_column_id is not None:
        await _validate_status_rate_column_for_branch(
            data.status_rate_column_id, branch_id, company_id, db,
        )
        if data.hours_value <= 0:
            raise HTTPException(
                status_code=422,
                detail="hours_value must be greater than 0 when status_rate_column_id is set.",
            )

    # Validate usage limits
    if data.limit_uses_per_period_enabled and not (data.limit_uses_per_period and data.limit_uses_per_period > 0):
        raise HTTPException(status_code=422, detail="limit_uses_per_period must be a positive integer when enabled.")
    if data.limit_uses_per_driver_enabled and not (data.limit_uses_per_driver and data.limit_uses_per_driver > 0):
        raise HTTPException(status_code=422, detail="limit_uses_per_driver must be a positive integer when enabled.")
    if data.limit_uses_across_drivers_enabled and not (data.limit_uses_across_drivers and data.limit_uses_across_drivers > 0):
        raise HTTPException(status_code=422, detail="limit_uses_across_drivers must be a positive integer when enabled.")
    if data.limit_uses_per_day_enabled and not (data.limit_uses_per_day and data.limit_uses_per_day > 0):
        raise HTTPException(status_code=422, detail="limit_uses_per_day must be a positive integer when enabled.")

    # Generate a unique SK_ code with up to 5 retries on collision.
    new_id: int | None = None
    generated_code: str = ""
    for _ in range(5):
        generated_code = _generate_status_key_code()
        normalized = _normalize_status_code(generated_code)
        try:
            ins = await db.execute(
                text("""
                    INSERT INTO payroll.payrollstatuskeys (
                        companyid, branchid,
                        keyname, statuscode, normalizedstatuscode,
                        hoursvalue, isoffreason, deductsfromyearlyallowance,
                        allowancecategory, isactive, displayorder,
                        limitusesperperiodenabled, limitusesperperiod,
                        limitusesperdriverenabled, limitusesperdriver,
                        limitusesacrossdriversenabled, limitusesacrossdrivers,
                        limitusesperdayenabled, limitusesperday,
                        statusratecolumnid,
                        createdbyuserid
                    ) VALUES (
                        :cid, :bid,
                        :kname, :code, :ncode,
                        :hours, :off, :deducts,
                        :cat, :active, 0,
                        :lpp_en, :lpp,
                        :lpd_en, :lpd,
                        :lad_en, :lad,
                        :lpday_en, :lpday,
                        :src_col_id,
                        :uid
                    )
                    RETURNING statuskeyid
                """),
                {
                    "cid":       company_id,
                    "bid":       branch_id,
                    "kname":     data.key_name.strip(),
                    "code":      generated_code,
                    "ncode":     normalized,
                    "hours":     data.hours_value,
                    "off":       data.is_off_reason,
                    "deducts":   data.deducts_from_yearly_allowance,
                    "cat":       data.allowance_category,
                    "active":    data.is_active,
                    "lpp_en":    data.limit_uses_per_period_enabled,
                    "lpp":       data.limit_uses_per_period,
                    "lpd_en":    data.limit_uses_per_driver_enabled,
                    "lpd":       data.limit_uses_per_driver,
                    "lad_en":    data.limit_uses_across_drivers_enabled,
                    "lad":       data.limit_uses_across_drivers,
                    "lpday_en":  data.limit_uses_per_day_enabled,
                    "lpday":     data.limit_uses_per_day,
                    "src_col_id": data.status_rate_column_id,
                    "uid":       user_id,
                },
            )
            new_id = ins.scalar_one()
            break
        except SAIntegrityError:
            # Unique violation on NormalizedStatusCode — retry with new code
            continue

    if new_id is None:
        raise HTTPException(
            status_code=500,
            detail="Could not generate a unique status key code after 5 attempts. Please try again.",
        )

    await _write_settings_audit(
        db,
        company_id=company_id,
        branch_id=branch_id,
        user_id=user_id,
        action_code="STATUS_KEY_CREATED",
        entity_name="PayrollStatusKeys",
        entity_id=str(new_id),
        new_value={
            "key_name":      data.key_name,
            "status_code":   generated_code,
            "hours_value":   str(data.hours_value),
            "is_off_reason": data.is_off_reason,
            "deducts_from_yearly_allowance": data.deducts_from_yearly_allowance,
            "allowance_category": data.allowance_category,
        },
    )

    row_result = await db.execute(
        text(f"SELECT {_STATUS_KEY_COLS} FROM payroll.payrollstatuskeys WHERE statuskeyid=:kid"),
        {"kid": new_id},
    )
    return _row_to_status_key(row_result.mappings().one())


async def update_status_key(
    key_id: int,
    branch_id: int,
    company_id: int,
    user_id: int,
    data: StatusKeyUpdate,
    db: AsyncConnection,
) -> StatusKey:
    """
    Partially update a status key — only non-None fields are applied.
    Requires AllCompanyBranches scope.
    """
    await _ensure_company_admin(company_id, user_id, db)

    # Fetch and lock the target row.
    cur = await db.execute(
        text(f"""
            SELECT {_STATUS_KEY_COLS}
            FROM   payroll.payrollstatuskeys
            WHERE  statuskeyid = :kid
              AND  branchid    = :bid
              AND  companyid   = :cid
            FOR UPDATE
        """),
        {"kid": key_id, "bid": branch_id, "cid": company_id},
    )
    row = cur.mappings().first()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Status key {key_id} not found.",
        )

    # Merge: apply patch where non-None, otherwise keep current.
    new_key_name = data.key_name.strip() if data.key_name is not None else row["keyname"]
    new_hours    = data.hours_value      if data.hours_value  is not None else row["hoursvalue"]
    new_off      = data.is_off_reason    if data.is_off_reason is not None else row["isoffreason"]
    new_deducts  = (
        data.deducts_from_yearly_allowance
        if data.deducts_from_yearly_allowance is not None
        else row["deductsfromyearlyallowance"]
    )
    new_cat      = data.allowance_category  if data.allowance_category  is not None else row.get("allowancecategory")
    new_active   = data.is_active           if data.is_active           is not None else row["isactive"]
    new_order    = data.display_order       if data.display_order       is not None else int(row.get("displayorder") or 0)

    # status_rate_column_id: -1 is the clear sentinel (set to NULL)
    if data.status_rate_column_id is not None:
        if data.status_rate_column_id == -1:
            new_src_col_id: int | None = None
        else:
            new_src_col_id = data.status_rate_column_id
    else:
        new_src_col_id = row.get("statusratecolumnid")

    # Usage limits
    new_lpp_en  = data.limit_uses_per_period_enabled    if data.limit_uses_per_period_enabled    is not None else bool(row.get("limitusesperperiodenabled") or False)
    new_lpp     = data.limit_uses_per_period            if data.limit_uses_per_period            is not None else row.get("limitusesperperiod")
    new_lpd_en  = data.limit_uses_per_driver_enabled    if data.limit_uses_per_driver_enabled    is not None else bool(row.get("limitusesperdriverenabled") or False)
    new_lpd     = data.limit_uses_per_driver            if data.limit_uses_per_driver            is not None else row.get("limitusesperdriver")
    new_lad_en  = data.limit_uses_across_drivers_enabled if data.limit_uses_across_drivers_enabled is not None else bool(row.get("limitusesacrossdriversenabled") or False)
    new_lad     = data.limit_uses_across_drivers        if data.limit_uses_across_drivers        is not None else row.get("limitusesacrossdrivers")
    new_lpday_en = data.limit_uses_per_day_enabled      if data.limit_uses_per_day_enabled       is not None else bool(row.get("limitusesperdayenabled") or False)
    new_lpday   = data.limit_uses_per_day               if data.limit_uses_per_day               is not None else row.get("limitusesperday")

    # StatusCode is immutable — always keep the existing generated code.
    existing_code       = row["statuscode"]
    existing_normalized = row["normalizedstatuscode"]

    _validate_deduction_rules(new_deducts, new_off, new_cat)

    # Validate status_rate_column_id if it is being set (not cleared)
    if new_src_col_id is not None:
        await _validate_status_rate_column_for_branch(
            new_src_col_id, branch_id, company_id, db,
        )
        if new_hours <= 0:
            raise HTTPException(
                status_code=422,
                detail="hours_value must be greater than 0 when status_rate_column_id is set.",
            )

    # Validate merged usage limits
    if new_lpp_en and not (new_lpp and new_lpp > 0):
        raise HTTPException(status_code=422, detail="limit_uses_per_period must be a positive integer when enabled.")
    if new_lpd_en and not (new_lpd and new_lpd > 0):
        raise HTTPException(status_code=422, detail="limit_uses_per_driver must be a positive integer when enabled.")
    if new_lad_en and not (new_lad and new_lad > 0):
        raise HTTPException(status_code=422, detail="limit_uses_across_drivers must be a positive integer when enabled.")
    if new_lpday_en and not (new_lpday and new_lpday > 0):
        raise HTTPException(status_code=422, detail="limit_uses_per_day must be a positive integer when enabled.")

    # Reactivation uniqueness check: if key is being activated and another
    # active key already holds the same normalized code, reject.
    being_activated = (not bool(row["isactive"])) and new_active
    if being_activated:
        await _check_status_key_code_unique(
            branch_id, company_id, existing_normalized, db, exclude_key_id=key_id
        )

    try:
        await db.execute(
            text("""
                UPDATE payroll.payrollstatuskeys
                SET    keyname                    = :kname,
                       hoursvalue                = :hours,
                       isoffreason               = :off,
                       deductsfromyearlyallowance = :deducts,
                       allowancecategory         = :cat,
                       isactive                  = :active,
                       displayorder              = :order,
                       limitusesperperiodenabled  = :lpp_en,
                       limitusesperperiod         = :lpp,
                       limitusesperdriverenabled  = :lpd_en,
                       limitusesperdriver         = :lpd,
                       limitusesacrossdriversenabled = :lad_en,
                       limitusesacrossdrivers     = :lad,
                       limitusesperdayenabled     = :lpday_en,
                       limitusesperday            = :lpday,
                       statusratecolumnid        = :src_col_id,
                       updatedbyuserid           = :uid,
                       updatedatutc              = NOW()
                WHERE  statuskeyid = :kid
            """),
            {
                "kid":     key_id,
                "kname":   new_key_name,
                "hours":   new_hours,
                "off":     new_off,
                "deducts": new_deducts,
                "cat":     new_cat,
                "active":  new_active,
                "order":   new_order,
                "lpp_en":  new_lpp_en,
                "lpp":     new_lpp,
                "lpd_en":  new_lpd_en,
                "lpd":     new_lpd,
                "lad_en":  new_lad_en,
                "lad":     new_lad,
                "lpday_en":   new_lpday_en,
                "lpday":      new_lpday,
                "src_col_id": new_src_col_id,
                "uid":        user_id,
            },
        )
    except SAIntegrityError as exc:
        msg = str(exc.orig).lower() if exc.orig else str(exc).lower()
        if "ux_payrollstatuskeys_activecode" in msg:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"A status key with code '{existing_normalized}' "
                    "is already active for this branch."
                ),
            )
        if "ck_statuskeys_" in msg:
            raise HTTPException(
                status_code=422,
                detail="Usage limit value must be a positive integer when the limit is enabled.",
            )
        raise HTTPException(
            status_code=422,
            detail="Status key could not be saved — a uniqueness constraint was violated.",
        )

    await _write_settings_audit(
        db,
        company_id=company_id,
        branch_id=branch_id,
        user_id=user_id,
        action_code="STATUS_KEY_UPDATED",
        entity_name="PayrollStatusKeys",
        entity_id=str(key_id),
        old_value={
            "key_name":    row["keyname"],
            "hours_value": str(row["hoursvalue"]),
            "is_off_reason": row["isoffreason"],
            "is_active":   row["isactive"],
        },
        new_value={
            "key_name":    new_key_name,
            "hours_value": str(new_hours),
            "is_off_reason": new_off,
            "is_active":   new_active,
        },
    )

    row_result = await db.execute(
        text(f"SELECT {_STATUS_KEY_COLS} FROM payroll.payrollstatuskeys WHERE statuskeyid=:kid"),
        {"kid": key_id},
    )
    return _row_to_status_key(row_result.mappings().one())


async def delete_status_key(
    key_id: int,
    branch_id: int,
    company_id: int,
    user_id: int,
    db: AsyncConnection,
) -> StatusKey:
    """
    Soft-delete (deactivate) a status key.

    The key is set to is_active=FALSE; it stays in the database for historical
    reference and is visible with include_inactive=true.
    Idempotent: if the key is already inactive, returns it unchanged.
    Requires AllCompanyBranches scope.
    """
    await _ensure_company_admin(company_id, user_id, db)

    cur = await db.execute(
        text(f"""
            SELECT {_STATUS_KEY_COLS}
            FROM   payroll.payrollstatuskeys
            WHERE  statuskeyid = :kid
              AND  branchid    = :bid
              AND  companyid   = :cid
            FOR UPDATE
        """),
        {"kid": key_id, "bid": branch_id, "cid": company_id},
    )
    row = cur.mappings().first()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Status key {key_id} not found.",
        )

    # Idempotent: already inactive → return as-is (no audit write).
    if not bool(row["isactive"]):
        return _row_to_status_key(row)

    await db.execute(
        text("""
            UPDATE payroll.payrollstatuskeys
            SET    isactive        = FALSE,
                   updatedbyuserid = :uid,
                   updatedatutc    = NOW()
            WHERE  statuskeyid = :kid
        """),
        {"kid": key_id, "uid": user_id},
    )

    await _write_settings_audit(
        db,
        company_id=company_id,
        branch_id=branch_id,
        user_id=user_id,
        action_code="STATUS_KEY_DEACTIVATED",
        entity_name="PayrollStatusKeys",
        entity_id=str(key_id),
        old_value={"is_active": True,  "status_code": row["statuscode"]},
        new_value={"is_active": False},
    )

    row_result = await db.execute(
        text(f"SELECT {_STATUS_KEY_COLS} FROM payroll.payrollstatuskeys WHERE statuskeyid=:kid"),
        {"kid": key_id},
    )
    return _row_to_status_key(row_result.mappings().one())


# ===========================================================================
# Status Rate Columns (CP-2D2)
# ===========================================================================

def _row_to_status_rate_column(row) -> StatusRateColumn:
    return StatusRateColumn(
        status_rate_column_id=row["statusratecolumnid"],
        company_id=row["companyid"],
        branch_id=row["branchid"],
        rate_type_id=row["ratetypeid"],
        rate_type_code=row["ratecode"],
        column_name=row["columnname"],
        is_default=bool(row["isdefault"]),
        is_active=bool(row["isactive"]),
        created_at_utc=row["createdatutc"],
    )


async def list_status_rate_columns(
    branch_id: int,
    company_id: int,
    user_id: int,
    db: AsyncConnection,
    *,
    active_only: bool = True,
) -> list[StatusRateColumn]:
    """Return all StatusRateColumns for a branch.  Enforces branch-level access."""
    can_see_all, branch_ids = await _check_branch_access(company_id, user_id, db)
    if not can_see_all and branch_id not in branch_ids:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have access to this branch.",
        )

    where = "WHERE src.branchid = :bid AND src.companyid = :cid"
    if active_only:
        where += " AND src.isactive = TRUE"
    result = await db.execute(
        text(f"""
            SELECT src.statusratecolumnid, src.companyid, src.branchid,
                   src.ratetypeid, rt.ratecode, src.columnname,
                   src.isdefault, src.isactive, src.createdatutc
            FROM   payroll.statusratecolumns src
            JOIN   payroll.ratetypes rt ON rt.ratetypeid = src.ratetypeid
            {where}
            ORDER BY src.isdefault DESC, src.statusratecolumnid
        """),
        {"bid": branch_id, "cid": company_id},
    )
    return [_row_to_status_rate_column(r) for r in result.mappings().all()]


async def create_status_rate_column(
    branch_id: int,
    company_id: int,
    user_id: int,
    data: StatusRateColumnCreate,
    db: AsyncConnection,
) -> StatusRateColumn:
    """
    Create a new custom StatusRateColumn for a branch.

    Automatically creates a new company-owned RateType (SRC_{company_id}_{random})
    to back the column.  Callers do NOT supply rate_type_id.
    """
    await _ensure_company_admin(company_id, user_id, db)

    # Verify branch belongs to this company
    branch_check = await db.execute(
        text("SELECT branchid FROM core.branches WHERE branchid=:bid AND companyid=:cid"),
        {"bid": branch_id, "cid": company_id},
    )
    if branch_check.first() is None:
        raise HTTPException(status_code=404, detail=f"Branch {branch_id} not found.")

    normalized_name = data.column_name.strip().upper()

    # Reject duplicate active column name for this branch
    dup_check = await db.execute(
        text("""
            SELECT statusratecolumnid FROM payroll.statusratecolumns
            WHERE  companyid             = :cid
              AND  branchid              = :bid
              AND  normalizedcolumnname  = :norm
              AND  isactive              = TRUE
        """),
        {"cid": company_id, "bid": branch_id, "norm": normalized_name},
    )
    if dup_check.first() is not None:
        raise HTTPException(
            status_code=422,
            detail=f"A status rate column named {data.column_name!r} already exists for this branch.",
        )

    # Create a new company-owned RateType backing this column
    rate_type_id: int | None = None
    for attempt in range(_CPI_MAX_RETRIES):
        rate_code = _generate_src_rate_code(company_id)
        try:
            async with db.begin_nested():
                rt_ins = await db.execute(
                    text("""
                        INSERT INTO payroll.ratetypes
                            (companyid, ratecode, ratename, unitname, isactive)
                        VALUES (:cid, :code, :name, :unit, TRUE)
                        RETURNING ratetypeid
                    """),
                    {
                        "cid":  company_id,
                        "code": rate_code,
                        "name": data.column_name.strip(),
                        "unit": data.unit_name,
                    },
                )
                rate_type_id = rt_ins.scalar_one()
            break
        except SAIntegrityError as exc:
            msg = str(exc.orig).lower() if exc.orig else str(exc).lower()
            if "ratecode" in msg and attempt < _CPI_MAX_RETRIES - 1:
                continue
            raise HTTPException(status_code=500, detail="Could not generate unique rate code.")

    if rate_type_id is None:
        raise HTTPException(status_code=500, detail="Could not generate unique rate code.")

    # If is_default, uniqueness is enforced by ux_StatusRateColumns_BranchDefault
    try:
        ins = await db.execute(
            text("""
                INSERT INTO payroll.statusratecolumns
                    (companyid, branchid, ratetypeid, columnname, normalizedcolumnname,
                     isdefault, isactive, createdbyuserid)
                VALUES (:cid, :bid, :rtid, :name, :norm, :def, TRUE, :uid)
                RETURNING statusratecolumnid
            """),
            {
                "cid":  company_id,
                "bid":  branch_id,
                "rtid": rate_type_id,
                "name": data.column_name.strip(),
                "norm": normalized_name,
                "def":  data.is_default,
                "uid":  user_id,
            },
        )
        new_id = ins.scalar_one()
    except SAIntegrityError as exc:
        msg = str(exc.orig).lower() if exc.orig else str(exc).lower()
        if "ux_statusratecolumns_branchdefault" in msg:
            raise HTTPException(
                status_code=422,
                detail="This branch already has a default status rate column. Set is_default=false or deactivate the existing default first.",
            )
        raise HTTPException(status_code=422, detail="Could not create status rate column.")

    row = (await db.execute(
        text("""
            SELECT src.statusratecolumnid, src.companyid, src.branchid,
                   src.ratetypeid, rt.ratecode, src.columnname,
                   src.isdefault, src.isactive, src.createdatutc
            FROM   payroll.statusratecolumns src
            JOIN   payroll.ratetypes rt ON rt.ratetypeid = src.ratetypeid
            WHERE  src.statusratecolumnid = :sid
        """),
        {"sid": new_id},
    )).mappings().one()
    return _row_to_status_rate_column(row)


async def _validate_status_rate_column_for_branch(
    status_rate_column_id: int,
    branch_id: int,
    company_id: int,
    db: AsyncConnection,
) -> None:
    """Raise 422 if the StatusRateColumnID doesn't exist for this branch."""
    check = await db.execute(
        text("""
            SELECT statusratecolumnid FROM payroll.statusratecolumns
            WHERE  statusratecolumnid = :sid
              AND  branchid           = :bid
              AND  companyid          = :cid
              AND  isactive           = TRUE
        """),
        {"sid": status_rate_column_id, "bid": branch_id, "cid": company_id},
    )
    if check.first() is None:
        raise HTTPException(
            status_code=422,
            detail=f"StatusRateColumn {status_rate_column_id} not found or not active for this branch.",
        )


async def _ensure_default_status_rate_column_for_branch(
    company_id: int,
    branch_id: int,
    db: AsyncConnection,
) -> None:
    """
    Idempotently ensure one default Status Pay column exists for a branch.

    Uses STATUS_PAY system RateType (CompanyID=NULL).  Skips silently if
    STATUS_PAY has not been seeded yet (should not happen in production) or
    if a default already exists.

    Called from create_branch so every new branch gets the default automatically.
    """
    rt_row = (await db.execute(
        text("SELECT ratetypeid FROM payroll.ratetypes WHERE ratecode = 'STATUS_PAY'"),
    )).mappings().first()
    if rt_row is None:
        return

    existing = (await db.execute(
        text("""
            SELECT statusratecolumnid FROM payroll.statusratecolumns
            WHERE  companyid = :cid AND branchid = :bid
              AND  isdefault = TRUE AND isactive  = TRUE
        """),
        {"cid": company_id, "bid": branch_id},
    )).mappings().first()
    if existing:
        return

    try:
        await db.execute(
            text("""
                INSERT INTO payroll.statusratecolumns
                    (companyid, branchid, ratetypeid, columnname, normalizedcolumnname,
                     isdefault, isactive)
                VALUES (:cid, :bid, :rtid, 'Status Pay', 'STATUS PAY', TRUE, TRUE)
                ON CONFLICT DO NOTHING
            """),
            {"cid": company_id, "bid": branch_id, "rtid": rt_row["ratetypeid"]},
        )
    except Exception:
        pass


# ===========================================================================
# Pay items & branch configuration
# ===========================================================================

from datetime import date as _date, timedelta as _timedelta               # noqa: E402
from app.settings.schemas import (                                        # noqa: E402
    BranchPayItemState,
    BranchPayItemConfigVersion,
    PayItemConfigUpdate,
    CustomPayItem,
    CustomPayItemCreate,
    CustomPayItemUpdate,
    CustomPayItemUsage,
    CustomPayItemDeleteResult,
    CustomPayItemRequest,
    CustomPayItemRequestCreate,
    CustomPayItemRequestDecide,
)

_SETTINGS_AUDIT_REASONS.update({
    "PAY_ITEM_CONFIG_CREATED":        "Branch pay item configuration created",
    "PAY_ITEM_CONFIG_UPDATED":        "Branch pay item configuration updated (same-day amendment)",
    "PAY_ITEM_CONFIG_VERSIONED":      "Branch pay item configuration versioned (new effective date)",
    "PAY_ITEM_BULK_CONFIG":           "Bulk branch pay item configuration applied",
    # M12 custom pay item audit codes
    "CUSTOM_PAY_ITEM_CREATED":           "Custom pay item created (admin direct)",
    "CUSTOM_PAY_ITEM_UPDATED":           "Custom pay item metadata updated",
    "CUSTOM_PAY_ITEM_DELETED":           "Custom pay item physically deleted",
    "CUSTOM_PAY_ITEM_RETIRED":           "Custom pay item retired (meaningful usage preserved)",
    "CUSTOM_PAY_ITEM_REQUESTED":         "Custom pay item request submitted by branch",
    "CUSTOM_PAY_ITEM_APPROVED":          "Custom pay item request approved — item created",
    "CUSTOM_PAY_ITEM_REJECTED":          "Custom pay item request rejected",
    "PAY_ITEM_ORDER_UPDATED":            "Pay item display order saved",
})

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

async def _check_branch_belongs_to_company(
    branch_id: int,
    company_id: int,
    db: AsyncConnection,
) -> None:
    """Raise 404 if the branch does not exist or does not belong to this company."""
    r = await db.execute(
        text("SELECT 1 FROM core.branches WHERE branchid = :bid AND companyid = :cid"),
        {"bid": branch_id, "cid": company_id},
    )
    if not r.fetchone():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Branch {branch_id} not found for this company.",
        )


def _make_config_version(row) -> BranchPayItemConfigVersion:
    return BranchPayItemConfigVersion(
        config_id=row["configid"],
        is_active=bool(row["cfgisactive"]),
        notes=row["cfgnotes"],
        effective_from=row["effectivefrom"],
        effective_to=row["effectiveto"],
        created_at_utc=row["createdatutc"],
    )


def _build_pay_item_state(
    item_row,
    current_cfg: BranchPayItemConfigVersion | None,
    pending_cfg: BranchPayItemConfigVersion | None,
    *,
    has_open_periods: bool = False,
    line_types: list[str] | None = None,
    rate_codes: list[str] | None = None,
) -> BranchPayItemState:
    """
    Build a BranchPayItemState from an item row + separate current/pending configs.

    current_cfg: the config whose date range covers today
                 (effectivefrom <= today AND (effectiveto IS NULL OR effectiveto >= today))
    pending_cfg: the open future-dated config (effectivefrom > today, effectiveto IS NULL)

    These are derived independently so that after a versioning write the
    freshly-closed row still shows as the current config until its effectiveto
    date passes.
    """
    if current_cfg is not None:
        is_active = bool(current_cfg.is_active)
        notes = current_cfg.notes
        is_using_default = False
    else:
        is_active = bool(item_row["isdefaultbranchactive"])
        notes = None
        is_using_default = True

    return BranchPayItemState(
        pay_item_id=item_row["payitemid"],
        pay_item_code=item_row["payitemcode"],
        pay_item_name=item_row["payitemname"],
        category=item_row["category"],
        data_type=item_row["datatype"],
        unit=item_row["unit"],
        sort_order=item_row["sortorder"],
        appears_in_payroll_entry=bool(item_row["appearsinpayrollentry"]),
        appears_in_ledger=bool(item_row["appearsinledger"]),
        appears_in_reports=bool(item_row["appearsinreports"]),
        requires_rate=bool(item_row["requiresrate"]),
        is_system_standard=bool(item_row["issystemstandard"]),
        item_scope=item_row["itemscope"],
        rate_behavior=item_row["ratebehavior"],
        item_status=item_row["itemstatus"],
        is_active=is_active,
        notes=notes,
        is_using_default=is_using_default,
        current_config=current_cfg,
        pending_config=pending_cfg,
        has_open_periods=has_open_periods,
        line_type_mappings=line_types or [],
        rate_type_mappings=rate_codes or [],
    )


_PI_ITEM_COLS = """
    pi.payitemid, pi.payitemcode, pi.payitemname, pi.category,
    pi.datatype, pi.unit, pi.sortorder,
    pi.appearsinpayrollentry, pi.appearsinledger, pi.appearsinreports,
    pi.requiresrate, pi.issystemstandard,
    pi.itemscope, pi.ratebehavior, pi.isdefaultbranchactive,
    pi.status AS itemstatus
"""

_PI_CFG_COLS = """
    bpic.configid,
    bpic.isactive    AS cfgisactive,
    bpic.notes       AS cfgnotes,
    bpic.effectivefrom,
    bpic.effectiveto,
    bpic.createdatutc
"""


async def _fetch_config_pair(
    company_id: int,
    branch_id: int,
    db: AsyncConnection,
) -> tuple[dict[int, BranchPayItemConfigVersion], dict[int, BranchPayItemConfigVersion]]:
    """
    Return (current_configs, pending_configs), each keyed by payitemid.

    current:  the config whose date range covers today —
              effectivefrom <= today AND (effectiveto IS NULL OR effectiveto >= today).
              Uses DISTINCT ON so only the latest-starting row is returned when
              multiple closed rows overlap today (shouldn't happen, but safe).

    pending:  the open future-dated config —
              effectiveto IS NULL AND effectivefrom > today.

    Separating the two queries means a freshly-closed row (effectivefrom <= today,
    effectiveto = some future date) still shows as current_config after a
    versioning write, even though its effectiveto is no longer NULL.
    """
    today = _date.today()

    r_current = await db.execute(
        text(f"""
            SELECT DISTINCT ON (payitemid) payitemid, {_PI_CFG_COLS}
            FROM   payroll.branchpayitemconfig bpic
            WHERE  companyid     = :cid
              AND  branchid      = :bid
              AND  effectivefrom <= :today
              AND  (effectiveto IS NULL OR effectiveto >= :today)
            ORDER  BY payitemid, effectivefrom DESC
        """),
        {"cid": company_id, "bid": branch_id, "today": today},
    )
    current_cfgs: dict[int, BranchPayItemConfigVersion] = {
        row["payitemid"]: _make_config_version(row)
        for row in r_current.mappings().all()
    }

    r_pending = await db.execute(
        text(f"""
            SELECT payitemid, {_PI_CFG_COLS}
            FROM   payroll.branchpayitemconfig bpic
            WHERE  companyid     = :cid
              AND  branchid      = :bid
              AND  effectiveto   IS NULL
              AND  effectivefrom >  :today
        """),
        {"cid": company_id, "bid": branch_id, "today": today},
    )
    pending_cfgs: dict[int, BranchPayItemConfigVersion] = {
        row["payitemid"]: _make_config_version(row)
        for row in r_pending.mappings().all()
    }

    return current_cfgs, pending_cfgs


async def _fetch_type_maps(
    company_id: int,
    db: AsyncConnection,
) -> tuple[dict[int, list[str]], dict[int, list[str]]]:
    """
    Return line_type_mappings and rate_type_mappings keyed by payitemid.
    Queries are wrapped individually — a missing table or empty catalog
    silently returns empty maps rather than failing the whole page load.
    """
    line_map: dict[int, list[str]] = {}
    rate_map: dict[int, list[str]] = {}
    try:
        r = await db.execute(
            text("""
                SELECT piltm.payitemid, piltm.linetype
                FROM   payroll.payitemlinetypemap piltm
                JOIN   payroll.payitems pi ON pi.payitemid = piltm.payitemid
                WHERE  piltm.status = 'Active'
                  AND  (pi.companyid IS NULL OR pi.companyid = :cid)
            """),
            {"cid": company_id},
        )
        for row in r.mappings().all():
            line_map.setdefault(row["payitemid"], []).append(row["linetype"])
    except Exception:
        pass

    try:
        r = await db.execute(
            text("""
                SELECT pirtm.payitemid, rt.ratecode
                FROM   payroll.payitemratetypemap pirtm
                JOIN   payroll.ratetypes rt    ON rt.ratetypeid  = pirtm.ratetypeid
                JOIN   payroll.payitems  pi    ON pi.payitemid   = pirtm.payitemid
                WHERE  pirtm.status = 'Active'
                  AND  rt.isactive  = TRUE
                  AND  (pi.companyid IS NULL OR pi.companyid = :cid)
            """),
            {"cid": company_id},
        )
        for row in r.mappings().all():
            rate_map.setdefault(row["payitemid"], []).append(row["ratecode"])
    except Exception:
        pass

    return line_map, rate_map


async def _get_current_open_period_max_end(
    branch_id: int,
    company_id: int,
    db: AsyncConnection,
) -> "_date | None":
    """
    Return the latest end_date among open (non-finalized) payroll periods whose
    date range CONTAINS today (startdate <= today <= enddate).

    Returns None if no currently-running open period exists.

    A future open period (startdate > today) does NOT block a change applied
    today — only a period that the admin is actively working in right now does.
    """
    today = _date.today()
    r = await db.execute(
        text("""
            SELECT MAX(enddate) AS max_end
            FROM   payroll.payrollperiods
            WHERE  branchid   = :bid
              AND  companyid  = :cid
              AND  status    IN ('Draft', 'Open', 'InReview', 'Approved')
              AND  startdate <= :today
              AND  enddate   >= :today
        """),
        {"bid": branch_id, "cid": company_id, "today": today},
    )
    return r.scalar_one()  # None when no matching row


async def _get_pay_item_state_internal(
    pay_item_id: int,
    branch_id: int,
    company_id: int,
    db: AsyncConnection,
    *,
    has_open_periods: bool = False,
) -> BranchPayItemState:
    """Post-write read — fetches item + current/pending configs + type maps without auth gate."""
    item_result = await db.execute(
        text(f"SELECT {_PI_ITEM_COLS} FROM payroll.payitems pi WHERE pi.payitemid = :piid"),
        {"piid": pay_item_id},
    )
    item_row = item_result.mappings().first()
    if item_row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail=f"Pay item {pay_item_id} not found.")

    current_cfgs, pending_cfgs = await _fetch_config_pair(company_id, branch_id, db)
    line_map, rate_map = await _fetch_type_maps(company_id, db)

    return _build_pay_item_state(
        item_row,
        current_cfgs.get(pay_item_id),
        pending_cfgs.get(pay_item_id),
        has_open_periods=has_open_periods,
        line_types=line_map.get(pay_item_id, []),
        rate_codes=rate_map.get(pay_item_id, []),
    )


# ---------------------------------------------------------------------------
# Public service functions
# ---------------------------------------------------------------------------

async def get_pay_items(
    branch_id: int,
    company_id: int,
    user_id: int,
    db: AsyncConnection,
) -> list[BranchPayItemState]:
    """
    List all non-Retired pay items with their branch-level config state.

    Readable by any authenticated user with access to this branch.
    Branch ownership is validated (branch must belong to this company).
    """
    # Access gate: user must have access to this branch
    can_see_all, branch_ids = await _check_branch_access(company_id, user_id, db)
    if not can_see_all and branch_id not in branch_ids:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="You do not have access to this branch.")

    # Branch-ownership validation: branch must belong to this company
    await _check_branch_belongs_to_company(branch_id, company_id, db)

    items_result = await db.execute(
        text(f"""
            SELECT {_PI_ITEM_COLS}
            FROM   payroll.payitems pi
            WHERE  pi.status != 'Retired'
              AND  (pi.companyid IS NULL OR pi.companyid = :cid)
            ORDER  BY pi.sortorder, pi.payitemname
        """),
        {"cid": company_id},
    )
    item_rows = items_result.mappings().all()
    if not item_rows:
        return []

    current_cfgs, pending_cfgs = await _fetch_config_pair(company_id, branch_id, db)
    line_map, rate_map = await _fetch_type_maps(company_id, db)

    return [
        _build_pay_item_state(
            row,
            current_cfgs.get(row["payitemid"]),
            pending_cfgs.get(row["payitemid"]),
            line_types=line_map.get(row["payitemid"], []),
            rate_codes=rate_map.get(row["payitemid"], []),
        )
        for row in item_rows
    ]


# ---------------------------------------------------------------------------
# Shared helpers — period-protection resolution + single-branch write
# ---------------------------------------------------------------------------

def _resolve_effective_from(
    effective_from_input: "_date | None",
    period_max_end: "_date | None",
) -> "_date":
    """
    Resolve the effective_from date given the current open-period boundary.

    - period_max_end is None  → no open period; use effective_from_input or today.
    - period_max_end is set   →
        * effective_from_input is None  → auto-schedule to day after period ends.
        * effective_from_input ≤ end    → HTTP 422 (cannot write inside open period).
        * effective_from_input > end    → allowed, use as-is.

    Pure function (no DB access).  Raises HTTPException on invalid dates.
    """
    today = _date.today()
    if period_max_end is not None:
        min_allowed: _date = period_max_end + _timedelta(days=1)
        if effective_from_input is None:
            return min_allowed
        if effective_from_input <= period_max_end:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"A payroll period is open until {period_max_end}. "
                    f"Pay item changes cannot take effect during an open period. "
                    f"The earliest allowed effective_from is {min_allowed}."
                ),
            )
        return effective_from_input
    return effective_from_input or today


async def _apply_pay_item_config_to_branch(
    pay_item_id: int,
    branch_id: int,
    company_id: int,
    user_id: int,
    is_active: bool,
    notes: "str | None",
    effective_from: "_date",
    db: AsyncConnection,
) -> "tuple[str, int]":
    """
    Write one branch's pay item configuration using full effective-dated versioning.

    ``effective_from`` must already be resolved (period-protection applied by the
    caller via _resolve_effective_from).

    Returns ``(action_code, config_id)`` where action_code is one of:
      - "PAY_ITEM_CONFIG_CREATED"  — fresh INSERT, no prior config row
      - "PAY_ITEM_CONFIG_UPDATED"  — in-place UPDATE (same date or pending replace)
      - "PAY_ITEM_CONFIG_VERSIONED" — existing row closed, new row inserted

    Shared by update_pay_item_config (single branch) and bulk_update_pay_item_config.
    """
    # Row-level lock prevents concurrent mutations on the same branch+item
    open_result = await db.execute(
        text(f"""
            SELECT {_PI_CFG_COLS}
            FROM   payroll.branchpayitemconfig bpic
            WHERE  payitemid   = :piid
              AND  companyid   = :cid
              AND  branchid    = :bid
              AND  effectiveto IS NULL
            FOR UPDATE
        """),
        {"piid": pay_item_id, "cid": company_id, "bid": branch_id},
    )
    open_row = open_result.mappings().first()

    action_code: str
    config_id: int
    old_value: "dict | None" = None

    if open_row is None:
        # No existing config: INSERT fresh row
        action_code = "PAY_ITEM_CONFIG_CREATED"
        try:
            ins = await db.execute(
                text("""
                    INSERT INTO payroll.branchpayitemconfig
                        (companyid, branchid, payitemid,
                         isactive, effectivefrom, notes, createdbyuserid)
                    VALUES
                        (:cid, :bid, :piid, :active, :eff_from, :notes, :uid)
                    RETURNING configid
                """),
                {"cid": company_id, "bid": branch_id, "piid": pay_item_id,
                 "active": is_active, "eff_from": effective_from,
                 "notes": notes, "uid": user_id},
            )
            config_id = ins.scalar_one()
        except SAIntegrityError:
            raise HTTPException(
                status_code=422,
                detail=(
                    "A concurrent request already created the config for this item. "
                    "Please retry — the existing config is now visible."
                ),
            )

    elif open_row["effectivefrom"] == effective_from:
        # Same-day amendment: UPDATE in place
        action_code = "PAY_ITEM_CONFIG_UPDATED"
        config_id   = open_row["configid"]
        old_value   = {
            "is_active":     bool(open_row["cfgisactive"]),
            "notes":         open_row["cfgnotes"],
            "effective_from": str(open_row["effectivefrom"]),
        }
        await db.execute(
            text("""
                UPDATE payroll.branchpayitemconfig
                SET    isactive = :active, notes = :notes
                WHERE  configid = :cid_row
            """),
            {"active": is_active, "notes": notes, "cid_row": config_id},
        )

    elif open_row["effectivefrom"] < effective_from:
        # New future version: close existing row, INSERT new open row
        action_code = "PAY_ITEM_CONFIG_VERSIONED"
        old_value   = {
            "is_active":           bool(open_row["cfgisactive"]),
            "notes":               open_row["cfgnotes"],
            "effective_from":      str(open_row["effectivefrom"]),
            "closed_effective_to": str(effective_from - _timedelta(days=1)),
        }
        await db.execute(
            text("""
                UPDATE payroll.branchpayitemconfig
                SET    effectiveto = :close_date
                WHERE  configid    = :cid_row
            """),
            {"close_date": effective_from - _timedelta(days=1),
             "cid_row":    open_row["configid"]},
        )
        try:
            ins = await db.execute(
                text("""
                    INSERT INTO payroll.branchpayitemconfig
                        (companyid, branchid, payitemid,
                         isactive, effectivefrom, notes, createdbyuserid)
                    VALUES
                        (:cid, :bid, :piid, :active, :eff_from, :notes, :uid)
                    RETURNING configid
                """),
                {"cid": company_id, "bid": branch_id, "piid": pay_item_id,
                 "active": is_active, "eff_from": effective_from,
                 "notes": notes, "uid": user_id},
            )
            config_id = ins.scalar_one()
        except SAIntegrityError:
            raise HTTPException(
                status_code=422,
                detail="Concurrent config update conflict. Please retry.",
            )

    else:
        # Pending row (effectivefrom > effective_from): replace in place
        action_code = "PAY_ITEM_CONFIG_UPDATED"
        config_id   = open_row["configid"]
        old_value   = {
            "is_active":     bool(open_row["cfgisactive"]),
            "notes":         open_row["cfgnotes"],
            "effective_from": str(open_row["effectivefrom"]),
        }
        await db.execute(
            text("""
                UPDATE payroll.branchpayitemconfig
                SET    isactive      = :active,
                       notes         = :notes,
                       effectivefrom = :eff_from
                WHERE  configid      = :cid_row
            """),
            {"active": is_active, "notes": notes,
             "eff_from": effective_from, "cid_row": config_id},
        )

    await _write_settings_audit(
        db,
        company_id=company_id,
        branch_id=branch_id,
        user_id=user_id,
        action_code=action_code,
        entity_name="BranchPayItemConfig",
        entity_id=f"{branch_id}:{pay_item_id}",
        old_value=old_value,
        new_value={
            "is_active":      is_active,
            "notes":          notes,
            "effective_from": str(effective_from),
        },
    )
    return action_code, config_id


# ---------------------------------------------------------------------------
# Single-branch config update (delegates to shared helpers)
# ---------------------------------------------------------------------------

async def update_pay_item_config(
    pay_item_id: int,
    branch_id: int,
    company_id: int,
    user_id: int,
    data: "PayItemConfigUpdate",
    db: AsyncConnection,
) -> BranchPayItemState:
    """
    Set the branch-level activation + notes for a pay item, with full
    effective-dated versioning.

    Versioning semantics (see PayItemConfigUpdate docstring for details):
    - Same effective date as existing open row → UPDATE in place (amendment)
    - Later effective date → close existing row, INSERT new open row
    - Earlier effective date than a pending row → UPDATE pending row in place
    - No open row → INSERT new open row

    If effective_from <= today AND open payroll periods exist,
    has_open_periods=True is set in the response as a warning.

    Requires AllCompanyBranches scope + setup.manage permission.
    """
    await _ensure_company_admin(company_id, user_id, db)
    await _check_branch_belongs_to_company(branch_id, company_id, db)

    pi_result = await db.execute(
        text("""
            SELECT payitemid, payitemcode FROM payroll.payitems
            WHERE  payitemid = :piid
              AND  status   != 'Retired'
              AND  (companyid IS NULL OR companyid = :cid)
        """),
        {"piid": pay_item_id, "cid": company_id},
    )
    pi_row = pi_result.mappings().first()
    if pi_row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail=f"Pay item {pay_item_id} not found or retired.")

    period_max_end   = await _get_current_open_period_max_end(branch_id, company_id, db)
    has_open_periods = period_max_end is not None

    # Raises HTTP 422 when effective_from falls inside an active open period
    effective_from = _resolve_effective_from(data.effective_from, period_max_end)

    await _apply_pay_item_config_to_branch(
        pay_item_id=pay_item_id, branch_id=branch_id,
        company_id=company_id, user_id=user_id,
        is_active=data.is_active, notes=data.notes,
        effective_from=effective_from, db=db,
    )

    return await _get_pay_item_state_internal(
        pay_item_id, branch_id, company_id, db, has_open_periods=has_open_periods
    )


# ---------------------------------------------------------------------------
# Bulk branch config update — atomic, single transaction
# ---------------------------------------------------------------------------

async def bulk_update_pay_item_config(
    pay_item_id: int,
    company_id: int,
    user_id: int,
    data: "BulkPayItemConfigUpdate",
    db: AsyncConnection,
) -> "BulkPayItemConfigResult":
    """
    Apply a single pay item configuration change atomically across one or more
    branches within the same transaction.

    Design:
      Phase 1 (VALIDATION) — resolve effective_from for every target branch and
        collect period-protection errors.  No writes occur.  If any branch fails
        validation the entire request is rejected with HTTP 422 and the full list
        of per-branch errors is returned.

      Phase 2 (WRITE) — call _apply_pay_item_config_to_branch for every branch
        in the same SQLAlchemy transaction (opened by engine.begin() in get_db).
        If any write raises an exception the transaction rolls back automatically,
        leaving every branch unchanged.

    Requires AllCompanyBranches scope + setup.manage permission.
    """
    from app.settings.schemas import (
        BulkPayItemTarget,
        BulkPayItemConfigResult,
        BulkPayItemBranchResult,
    )

    await _ensure_company_admin(company_id, user_id, db)

    # Pay item existence check
    pi_result = await db.execute(
        text("""
            SELECT payitemid, payitemcode FROM payroll.payitems
            WHERE  payitemid = :piid
              AND  status   != 'Retired'
              AND  (companyid IS NULL OR companyid = :cid)
        """),
        {"piid": pay_item_id, "cid": company_id},
    )
    pi_row = pi_result.mappings().first()
    if pi_row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail=f"Pay item {pay_item_id} not found or retired.")

    # Resolve target branch list
    if data.target == BulkPayItemTarget.AllBranches:
        br_result = await db.execute(
            text("""
                SELECT branchid, branchname
                FROM   core.branches
                WHERE  companyid = :cid AND status = 'Active'
                ORDER  BY branchname
            """),
            {"cid": company_id},
        )
        target_branches = [
            {"branch_id": r["branchid"], "branch_name": r["branchname"]}
            for r in br_result.mappings().all()
        ]
        if not target_branches:
            raise HTTPException(
                status_code=422,
                detail="No active branches found for this company.",
            )
    else:
        # SelectedBranches — validate that every requested branch_id belongs to company
        br_result = await db.execute(
            text("""
                SELECT branchid, branchname
                FROM   core.branches
                WHERE  companyid = :cid
                  AND  branchid  = ANY(:bids)
                ORDER  BY branchname
            """),
            {"cid": company_id, "bids": data.branch_ids},
        )
        found = {r["branchid"]: r["branchname"] for r in br_result.mappings().all()}
        missing = [bid for bid in (data.branch_ids or []) if bid not in found]
        if missing:
            raise HTTPException(
                status_code=422,
                detail=f"Branch ID(s) not found or not in this company: {missing}",
            )
        target_branches = [
            {"branch_id": bid, "branch_name": found[bid]}
            for bid in (data.branch_ids or [])
        ]

    # Phase 1 — VALIDATION (no DB writes)
    validated: list[dict] = []
    validation_errors: list[dict] = []

    for branch in target_branches:
        period_end = await _get_current_open_period_max_end(
            branch["branch_id"], company_id, db
        )
        try:
            eff = _resolve_effective_from(data.effective_from, period_end)
            validated.append({**branch, "effective_from": eff})
        except HTTPException as exc:
            validation_errors.append({
                "branch_id":   branch["branch_id"],
                "branch_name": branch["branch_name"],
                "error":       exc.detail,
            })

    if validation_errors:
        # Fail fast — return all errors, write nothing
        raise HTTPException(
            status_code=422,
            detail={
                "message": (
                    "Validation failed for one or more target branches. "
                    "No changes were applied."
                ),
                "branch_errors": validation_errors,
            },
        )

    # Phase 2 — WRITE (all within engine.begin() transaction)
    _STATUS_MAP: dict[str, str] = {
        "PAY_ITEM_CONFIG_CREATED":   "Created",
        "PAY_ITEM_CONFIG_UPDATED":   "Updated",
        "PAY_ITEM_CONFIG_VERSIONED": "Versioned",
    }
    results: list[BulkPayItemBranchResult] = []

    for branch_data in validated:
        action_code, config_id = await _apply_pay_item_config_to_branch(
            pay_item_id=pay_item_id,
            branch_id=branch_data["branch_id"],
            company_id=company_id,
            user_id=user_id,
            is_active=data.is_active,
            notes=data.notes,
            effective_from=branch_data["effective_from"],
            db=db,
        )
        results.append(BulkPayItemBranchResult(
            branch_id=branch_data["branch_id"],
            branch_name=branch_data["branch_name"],
            status=_STATUS_MAP.get(action_code, "Updated"),  # type: ignore[arg-type]
            config_id=config_id,
            effective_from=branch_data["effective_from"],
        ))

    # One company-level audit record summarising the bulk operation.
    # Per-branch audit records are already written by _apply_pay_item_config_to_branch.
    await _write_settings_audit(
        db,
        company_id=company_id,
        branch_id=None,
        user_id=user_id,
        action_code="PAY_ITEM_BULK_CONFIG",
        entity_name="BranchPayItemConfig",
        entity_id=f"bulk:{pay_item_id}",
        old_value=None,
        new_value={
            "is_active":      data.is_active,
            "notes":          data.notes,
            "effective_from": str(data.effective_from) if data.effective_from else None,
            "target":         data.target,
            "branch_count":   len(results),
        },
    )

    return BulkPayItemConfigResult(
        pay_item_id=pay_item_id,
        pay_item_code=pi_row["payitemcode"],
        target=data.target,
        requested_branch_count=len(target_branches),
        updated_branch_count=len(results),
        results=results,
    )


async def get_pay_item_config_history(
    pay_item_id: int,
    branch_id: int,
    company_id: int,
    user_id: int,
    db: AsyncConnection,
) -> list[BranchPayItemConfigVersion]:
    """
    Return all config versions (open + closed) for one pay item on this branch,
    ordered newest-first.

    Readable by any authenticated user with access to this branch.
    """
    can_see_all, branch_ids = await _check_branch_access(company_id, user_id, db)
    if not can_see_all and branch_id not in branch_ids:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="You do not have access to this branch.")

    await _check_branch_belongs_to_company(branch_id, company_id, db)

    result = await db.execute(
        text(f"""
            SELECT {_PI_CFG_COLS}
            FROM   payroll.branchpayitemconfig bpic
            WHERE  payitemid  = :piid
              AND  companyid  = :cid
              AND  branchid   = :bid
            ORDER  BY effectivefrom DESC, configid DESC
        """),
        {"piid": pay_item_id, "cid": company_id, "bid": branch_id},
    )
    return [_make_config_version(r) for r in result.mappings().all()]


async def get_missing_pay_item_configs(
    branch_id: int,
    company_id: int,
    user_id: int,
    db: AsyncConnection,
) -> list[str]:
    """
    Return pay item codes that have no open BranchPayItemConfig row for
    this branch.  An empty list means every item is explicitly configured.

    Requires AllCompanyBranches scope.
    """
    await _ensure_company_admin(company_id, user_id, db)
    await _check_branch_belongs_to_company(branch_id, company_id, db)

    result = await db.execute(
        text("""
            SELECT pi.payitemcode
            FROM   payroll.payitems pi
            WHERE  pi.status != 'Retired'
              AND  (pi.companyid IS NULL OR pi.companyid = :cid)
              AND  NOT EXISTS (
                       SELECT 1
                       FROM   payroll.branchpayitemconfig bpic
                       WHERE  bpic.payitemid  = pi.payitemid
                         AND  bpic.companyid  = :cid
                         AND  bpic.branchid   = :bid
                         AND  bpic.effectiveto IS NULL
                   )
            ORDER  BY pi.payitemcode
        """),
        {"cid": company_id, "bid": branch_id},
    )
    return [r["payitemcode"] for r in result.mappings().all()]


# ===========================================================================
# M12: Custom Pay Items — admin catalog + branch request/approval flow
# ===========================================================================
"""
Custom Pay Items design (M12):
  - Items are company-level after approval: CompanyID set, BranchID = NULL.
  - RequestingBranchID records provenance (which branch requested the item).
  - Admin-direct creates have RequestingBranchID = None.
  - On approval, BranchPayItemConfig is created for the requesting branch
    (IsActive=TRUE) so the item starts active there and inactive everywhere else.
  - System items (IsSystemStandard=TRUE) cannot be deleted via these endpoints.

M12 item scope / rate behavior constraints:
  - Daily items  → RateBehavior = 'PerUnit'       (quantity × driver rate)
  - Period items → RateBehavior = 'EnteredAmount'  (user enters dollar amount)
  Additional rate behaviors (OrdinalTier, RangeBracket, Block …) are M13 work.

Smart delete logic:
  - Never used              → physical delete (rows removed from DB).
  - Only non-meaningful use → clean up empty/voided draft lines, physical delete.
  - Meaningful use          → retire (Status = 'Retired'); code permanently locked.
  Meaningful = any active draft line with Quantity>0, RateAmount, or CalculatedAmount≠0,
               OR any final payroll line.
"""

# ---------------------------------------------------------------------------
# Internal helpers (M12)
# ---------------------------------------------------------------------------

_CUSTOM_ITEM_COLS = """
    pi.payitemid, pi.companyid, pi.payitemcode, pi.displaylabel,
    pi.payitemname, pi.category, pi.datatype, pi.unit, pi.status, pi.sortorder,
    pi.appearsinpayrollentry, pi.appearsinledger, pi.appearsinreports,
    pi.requiresrate, pi.issystemstandard,
    pi.itemscope, pi.ratebehavior,
    pi.requestingbranchid,
    pi.createdbyuserid, pi.createdatutc, pi.updatedatutc,
    pi.notes
"""


def _row_to_custom_pay_item(
    row,
    rate_names: "list[str] | None" = None,
) -> "CustomPayItem":
    return CustomPayItem(
        pay_item_id=row["payitemid"],
        company_id=row["companyid"],
        pay_item_code=row["payitemcode"],
        display_label=row.get("displaylabel"),
        pay_item_name=row["payitemname"],
        category=row["category"],
        data_type=row["datatype"],
        unit=row.get("unit"),
        item_scope=row["itemscope"],
        rate_behavior=row["ratebehavior"],
        status=row["status"],
        sort_order=row["sortorder"],
        appears_in_payroll_entry=bool(row["appearsinpayrollentry"]),
        appears_in_ledger=bool(row["appearsinledger"]),
        appears_in_reports=bool(row["appearsinreports"]),
        requires_rate=bool(row["requiresrate"]),
        is_system_standard=bool(row["issystemstandard"]),
        requesting_branch_id=row.get("requestingbranchid"),
        notes=row.get("notes"),
        created_at_utc=row["createdatutc"],
        updated_at_utc=row.get("updatedatutc"),
        rate_names=rate_names or [],
    )


async def _fetch_rate_names_for_items(
    item_ids: "list[int]",
    company_id: int,
    db: AsyncConnection,
) -> "dict[int, list[str]]":
    """
    Return ordered rate_name lists keyed by payitemid.

    Rate names are stored in payroll.payitemsettings with
    settingkey = 'rate_name_1', 'rate_name_2', etc.
    """
    if not item_ids:
        return {}
    in_clause, in_params = _build_in_clause(item_ids, "sid")
    result = await db.execute(
        text(f"""
            SELECT payitemid, settingkey, settingvaluetext
            FROM   payroll.payitemsettings
            WHERE  payitemid IN ({in_clause})
              AND  companyid  = :cid
              AND  settingkey LIKE 'rate_name_%'
              AND  status     = 'Active'
              AND  settingvaluetext IS NOT NULL
            ORDER  BY payitemid, settingkey
        """),
        {"cid": company_id, **in_params},
    )
    result_dict: dict[int, list[str]] = {}
    for row in result.mappings().all():
        result_dict.setdefault(row["payitemid"], []).append(row["settingvaluetext"])
    return result_dict


_REQUEST_COLS = """
    r.requestid, r.companyid, r.requestingbranchid,
    b.branchname AS requestingbranchname,
    r.requestedbyuserid,
    u.displayname AS requestedby,
    r.requestedatutc,
    r.payitemcode, r.displaylabel, r.payitemname,
    r.itemscope, r.ratebehavior, r.category, r.unit, r.notes, r.sortorder,
    r.status,
    r.decidedbyuserid,
    du.displayname AS decidedby,
    r.decidedatutc, r.decisionreason, r.approvedpayitemid
"""


def _row_to_request(row) -> CustomPayItemRequest:
    return CustomPayItemRequest(
        request_id=row["requestid"],
        company_id=row["companyid"],
        requesting_branch_id=row["requestingbranchid"],
        requesting_branch_name=row.get("requestingbranchname"),
        requested_by_user_id=row["requestedbyuserid"],
        requested_by=row.get("requestedby"),
        requested_at_utc=row["requestedatutc"],
        pay_item_code=row["payitemcode"],
        display_label=row.get("displaylabel"),
        pay_item_name=row["payitemname"],
        item_scope=row["itemscope"],
        rate_behavior=row["ratebehavior"],
        category=row["category"],
        unit=row.get("unit"),
        notes=row.get("notes"),
        sort_order=row["sortorder"],
        status=row["status"],
        decided_by_user_id=row.get("decidedbyuserid"),
        decided_by=row.get("decidedby"),
        decided_at_utc=row.get("decidedatutc"),
        decision_reason=row.get("decisionreason"),
        approved_pay_item_id=row.get("approvedpayitemid"),
    )


async def _block_if_code_taken(
    company_id: int,
    pay_item_code: str,
    db: AsyncConnection,
    *,
    exclude_request_id: int | None = None,
) -> None:
    """
    Raise HTTP 422 if any of these conditions are true:
      1. A SYSTEM item (CompanyID IS NULL) has this code — explicit service-layer
         block, independent of DB constraints (requirement #9).
      2. An Active or Inactive custom item for this company has this code.
      3. A Retired custom item for this company has this code (code permanently locked).
      4. A PendingApproval or Approved request for this company has this code
         (prevents racing duplicate submissions).
    """
    # 1. System item check (explicit — do not rely only on DB constraints)
    sys_row = await db.execute(
        text("""
            SELECT payitemid FROM payroll.payitems
            WHERE  companyid IS NULL AND payitemcode = :code
        """),
        {"code": pay_item_code},
    )
    if sys_row.first() is not None:
        raise HTTPException(
            status_code=422,
            detail=(
                f"'{pay_item_code}' is a system pay item code and cannot be used "
                "for a custom item."
            ),
        )

    # 2 & 3. Existing custom item (Active, Inactive, or Retired)
    item_row = await db.execute(
        text("""
            SELECT payitemid, status FROM payroll.payitems
            WHERE  companyid = :cid AND payitemcode = :code
        """),
        {"cid": company_id, "code": pay_item_code},
    )
    existing = item_row.mappings().first()
    if existing is not None:
        if existing["status"] == "Retired":
            raise HTTPException(
                status_code=422,
                detail=(
                    f"'{pay_item_code}' was previously used and has been retired. "
                    "Retired codes cannot be reused to protect historical payroll records."
                ),
            )
        raise HTTPException(
            status_code=422,
            detail=f"A custom pay item with code '{pay_item_code}' already exists for this company.",
        )

    # 4. Pending or approved request for same code
    req_params: dict = {"cid": company_id, "code": pay_item_code}
    extra = ""
    if exclude_request_id is not None:
        extra = " AND r.requestid != :excl"
        req_params["excl"] = exclude_request_id

    req_row = await db.execute(
        text(f"""
            SELECT requestid FROM payroll.custompayitemrequests r
            WHERE  companyid = :cid
              AND  payitemcode = :code
              AND  status IN ('PendingApproval', 'Approved')
              {extra}
        """),
        req_params,
    )
    if req_row.first() is not None:
        raise HTTPException(
            status_code=422,
            detail=(
                f"A pending or approved request for code '{pay_item_code}' already exists "
                "for this company."
            ),
        )


async def _get_custom_item_or_404(
    item_id: int,
    company_id: int,
    db: AsyncConnection,
) -> dict:
    """Fetch a custom pay item by ID and company. Raises 404 if not found."""
    result = await db.execute(
        text(f"""
            SELECT {_CUSTOM_ITEM_COLS}
            FROM   payroll.payitems pi
            WHERE  pi.payitemid = :iid
              AND  pi.companyid = :cid
        """),
        {"iid": item_id, "cid": company_id},
    )
    row = result.mappings().first()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Custom pay item {item_id} not found.",
        )
    return row


async def _compute_usage(
    item_id: int,
    company_id: int,
    pay_item_code: str,
    db: AsyncConnection,
) -> CustomPayItemUsage:
    """
    Return usage counts for a custom pay item.

    Meaningful draft line: Status='Active' AND
        (Quantity > 0 OR RateAmount IS NOT NULL
         OR (CalculatedAmount IS NOT NULL AND CalculatedAmount != 0))

    Non-meaningful draft line: Status='Voided' OR all numeric fields are zero/null.
    """
    draft_result = await db.execute(
        text("""
            SELECT
                SUM(CASE
                    WHEN status != 'Void'
                     AND (quantity > 0
                          OR rateamount IS NOT NULL
                          OR (calculatedamount IS NOT NULL AND calculatedamount != 0))
                    THEN 1 ELSE 0
                END)  AS meaningful_count,
                SUM(CASE
                    WHEN status = 'Void'
                      OR (quantity = 0
                          AND rateamount IS NULL
                          AND (calculatedamount IS NULL OR calculatedamount = 0))
                    THEN 1 ELSE 0
                END)  AS non_meaningful_count
            FROM payroll.payrolldraftlines
            WHERE companyid = :cid AND linetype = :code
        """),
        {"cid": company_id, "code": pay_item_code},
    )
    draft_row = draft_result.mappings().first()
    meaningful = int(draft_row["meaningful_count"] or 0)
    non_meaningful = int(draft_row["non_meaningful_count"] or 0)

    final_result = await db.execute(
        text("""
            SELECT COUNT(*) AS cnt
            FROM   payroll.payrollfinallines
            WHERE  companyid = :cid AND linetype = :code
        """),
        {"cid": company_id, "code": pay_item_code},
    )
    final_count = int((final_result.scalar_one() or 0))

    # Count DriverRates rows linked to this Pay Item via PayItemRateTypeMap.
    # A custom pay item with existing driver rates must be retired, not physically
    # deleted, to avoid orphaning rate records that reference a deleted rate type mapping.
    driver_rates_result = await db.execute(
        text("""
            SELECT COUNT(*) AS cnt
            FROM   payroll.driverrates dr
            JOIN   payroll.payitemratetypemap pirm
                   ON pirm.ratetypeid = dr.ratetypeid
            WHERE  pirm.payitemid = :item_id
              AND  dr.companyid   = :company_id
        """),
        {"item_id": item_id, "company_id": company_id},
    )
    driver_rates_count = int(driver_rates_result.scalar_one() or 0)

    has_meaningful = meaningful > 0
    has_final = final_count > 0
    has_driver_rates = driver_rates_count > 0

    return CustomPayItemUsage(
        pay_item_id=item_id,
        pay_item_code=pay_item_code,
        has_meaningful_usage=has_meaningful,
        has_final_lines=has_final,
        meaningful_draft_line_count=meaningful,
        final_line_count=final_count,
        non_meaningful_draft_line_count=non_meaningful,
        driver_rates_count=driver_rates_count,
        can_physical_delete=not has_meaningful and not has_final and not has_driver_rates,
        deletion_would_retire=has_meaningful or has_final or has_driver_rates,
    )


# ---------------------------------------------------------------------------
# Public service functions — admin direct catalog management
# ---------------------------------------------------------------------------

async def get_custom_pay_items(
    company_id: int,
    user_id: int,
    db: AsyncConnection,
    *,
    include_retired: bool = False,
) -> list[CustomPayItem]:
    """
    List all company-owned custom pay items (IsSystemStandard=FALSE, CompanyID=set).
    Requires AllCompanyBranches scope.
    By default excludes Retired items; pass include_retired=True for full list.
    """
    await _ensure_company_admin(company_id, user_id, db)

    status_filter = "" if include_retired else "AND pi.status != 'Retired'"
    result = await db.execute(
        text(f"""
            SELECT {_CUSTOM_ITEM_COLS}
            FROM   payroll.payitems pi
            WHERE  pi.companyid       = :cid
              AND  pi.issystemstandard = FALSE
              {status_filter}
            ORDER BY pi.sortorder, pi.payitemname, pi.payitemcode
        """),
        {"cid": company_id},
    )
    rows = result.mappings().all()
    item_ids = [r["payitemid"] for r in rows]
    rn_map = await _fetch_rate_names_for_items(item_ids, company_id, db)
    return [_row_to_custom_pay_item(r, rate_names=rn_map.get(r["payitemid"])) for r in rows]


async def get_custom_pay_item_by_id(
    item_id: int,
    company_id: int,
    user_id: int,
    db: AsyncConnection,
) -> CustomPayItem:
    """Fetch a single custom pay item. Requires AllCompanyBranches scope."""
    await _ensure_company_admin(company_id, user_id, db)
    row = await _get_custom_item_or_404(item_id, company_id, db)
    if bool(row["issystemstandard"]):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Custom pay item {item_id} not found.",
        )
    rn_map = await _fetch_rate_names_for_items([item_id], company_id, db)
    return _row_to_custom_pay_item(row, rate_names=rn_map.get(item_id))


async def create_custom_pay_item(
    company_id: int,
    user_id: int,
    data: CustomPayItemCreate,
    db: AsyncConnection,
) -> CustomPayItem:
    """
    Admin-direct create: insert a new company-level custom pay item.

    Does NOT create any BranchPayItemConfig — the item starts inactive on all
    branches. Branches activate it via PATCH /settings/branches/{id}/pay-items/{id}.

    Requires AllCompanyBranches scope + setup.manage.
    """
    await _ensure_company_admin(company_id, user_id, db)

    # LLR-A: New Custom Daily PayItems must be created through the CDPI workflow.
    if data.item_scope == "Daily":
        raise HTTPException(
            status_code=422,
            detail=(
                "Custom Daily PayItems must be created through the CDPI workflow. "
                "Use POST /settings/cdpi/direct-company-items instead."
            ),
        )

    # Resolve pay_item_code: use supplied value or auto-generate.
    if data.pay_item_code is None:
        # Auto-generation with collision retry.
        # _block_if_code_taken raises HTTPException on conflict; we catch it
        # and try a fresh code rather than surfacing the error to the caller.
        pay_item_code: str | None = None
        for attempt in range(_CPI_MAX_RETRIES):
            candidate = _generate_pay_item_code()
            try:
                await _block_if_code_taken(company_id, candidate, db)
                pay_item_code = candidate
                break
            except HTTPException:
                if attempt == _CPI_MAX_RETRIES - 1:
                    raise HTTPException(
                        status_code=500,
                        detail=(
                            "Could not generate a unique pay item code. "
                            "Please try again."
                        ),
                    )
        assert pay_item_code is not None  # loop always breaks or raises above
    else:
        await _block_if_code_taken(company_id, data.pay_item_code, db)
        pay_item_code = data.pay_item_code

    # Resolve datatype and unit from value_type (wizard path) or legacy fields.
    from app.settings.schemas import _VALUE_TYPE_DATATYPE_MAP  # local avoids circular
    _RATE_USING_BEHAVIORS = {
        "PerUnit", "OrdinalTier", "RangeBracket", "RangeProgressive", "Block"
    }
    if data.value_type is not None:
        _dt, _default_unit = _VALUE_TYPE_DATATYPE_MAP.get(data.value_type, ("Decimal", None))
        item_datatype: str      = _dt
        item_unit:     str | None = data.unit or _default_unit
    else:
        item_datatype = "Decimal"
        item_unit     = data.unit

    # Derive display flags from item_scope / rate_behavior
    appears_in_entry = data.item_scope == "Daily"
    requires_rate    = data.rate_behavior in _RATE_USING_BEHAVIORS

    # Auto-assign sort_order when not supplied: append after the highest existing
    # company item, using gap-of-10 spacing so future items can be inserted between.
    if data.sort_order is None:
        max_r = await db.execute(
            text("""
                SELECT COALESCE(MAX(sortorder), 0) AS max_order
                FROM   payroll.payitems
                WHERE  companyid = :cid
            """),
            {"cid": company_id},
        )
        next_order: int = int(max_r.scalar_one() or 0) + 10
    else:
        next_order = data.sort_order

    result = await db.execute(
        text("""
            INSERT INTO payroll.payitems (
                companyid, branchid, payitemcode, displaylabel, payitemname,
                category, datatype, unit, status, sortorder,
                appearsinpayrollentry, appearsinledger, appearsinreports,
                requiresrate, issystemstandard,
                itemscope, ratebehavior, isdefaultbranchactive,
                requestingbranchid, createdbyuserid, notes
            ) VALUES (
                :cid, NULL, :code, :display_label, :name,
                :category, :datatype, :unit, 'Active', :sort_order,
                :in_entry, TRUE, TRUE,
                :requires_rate, FALSE,
                :item_scope, :rate_behavior, FALSE,
                NULL, :uid, :notes
            )
            RETURNING payitemid
        """),
        {
            "cid":           company_id,
            "code":          pay_item_code,
            "display_label": data.display_label,
            "name":          data.pay_item_name,
            "category":      data.category,
            "unit":          item_unit,
            "datatype":      item_datatype,
            "sort_order":    next_order,
            "in_entry":      appears_in_entry,
            "requires_rate": requires_rate,
            "item_scope":    data.item_scope,
            "rate_behavior": data.rate_behavior,
            "uid":           user_id,
            "notes":         data.notes,
        },
    )
    new_id = result.scalar_one()

    # Build the list of rate field names for this item.
    # For PerUnit: one field (use rate_names[0] or fall back to pay_item_name + " Rate").
    # For multi-bracket behaviors: use all supplied rate_names or default to "Rate 1", "Rate 2", …3.
    _MULTI_RATE_BEHAVIORS = {"OrdinalTier", "RangeBracket", "RangeProgressive", "Block"}
    effective_rate_names: list[str] = []
    if requires_rate:
        if data.rate_names:
            effective_rate_names = [n.strip() for n in data.rate_names if n.strip()]
        if not effective_rate_names:
            if data.rate_behavior in _MULTI_RATE_BEHAVIORS:
                effective_rate_names = ["Rate 1", "Rate 2", "Rate 3"]
            elif requires_rate:
                effective_rate_names = [data.pay_item_name.strip() + " Rate"]

    # Persist rate_names to payitemsettings (rate_name_1, rate_name_2, …) AND
    # create the corresponding RateTypes + PayItemRateTypeMap rows so the item
    # appears in the Pay Rates matrix.
    # Fix 3E-A: previously only payitemsettings rows were written; RateTypes and
    # PayItemRateTypeMap rows were missing, causing custom items to be invisible
    # in Pay Rates (INNER JOIN on PayItemRateTypeMap filtered them out).
    for idx, rname in enumerate(effective_rate_names, start=1):
        # 1. Persist display name to PayItemSettings
        await db.execute(
            text("""
                INSERT INTO payroll.payitemsettings
                    (payitemid, companyid, settingkey,
                     settingdatatype, settingvaluetext,
                     status, createdbyuserid)
                VALUES
                    (:piid, :cid, :key,
                     'Text', :val,
                     'Active', :uid)
                ON CONFLICT DO NOTHING
            """),
            {
                "piid": new_id,
                "cid":  company_id,
                "key":  f"rate_name_{idx}",
                "val":  rname,
                "uid":  user_id,
            },
        )
        # 2. Create a RateTypes row for this rate field.
        # Rate code: CPI_<payitemid>_<idx> -- unique, company-scoped in practice.
        # Phase 4C: set CompanyID so the RateType is structurally owned by this company.
        rate_code = f"CPI_{new_id}_{idx}"
        rt_result = await db.execute(
            text("""
                INSERT INTO payroll.ratetypes (ratecode, ratename, unitname, isactive, companyid)
                VALUES (:code, :name, :unit, TRUE, :cid)
                ON CONFLICT (ratecode) DO UPDATE
                    SET ratename  = EXCLUDED.ratename,
                        companyid = EXCLUDED.companyid
                RETURNING ratetypeid
            """),
            {
                "code": rate_code,
                "name": rname,
                "unit": item_unit or "Unit",
                "cid":  company_id,
            },
        )
        rt_id = rt_result.scalar_one()
        # 3. Create the PayItemRateTypeMap row.
        await db.execute(
            text("""
                INSERT INTO payroll.payitemratetypemap
                    (payitemid, ratetypeid, isprimary, status)
                VALUES
                    (:piid, :rtid, :primary, 'Active')
                ON CONFLICT (payitemid, ratetypeid) DO NOTHING
            """),
            {
                "piid":    new_id,
                "rtid":    rt_id,
                "primary": (idx == 1),
            },
        )

    await _write_settings_audit(
        db,
        company_id=company_id,
        branch_id=None,
        user_id=user_id,
        action_code="CUSTOM_PAY_ITEM_CREATED",
        entity_name="PayItems",
        entity_id=str(new_id),
        new_value={
            "pay_item_code":  pay_item_code,
            "item_scope":     data.item_scope,
            "rate_behavior":  data.rate_behavior,
            "pay_item_name":  data.pay_item_name,
        },
    )

    row = await _get_custom_item_or_404(new_id, company_id, db)
    rn_map = await _fetch_rate_names_for_items([new_id], company_id, db)
    return _row_to_custom_pay_item(row, rate_names=rn_map.get(new_id))


async def update_custom_pay_item(
    item_id: int,
    company_id: int,
    user_id: int,
    data: CustomPayItemUpdate,
    db: AsyncConnection,
) -> CustomPayItem:
    """
    Partially update mutable metadata on a custom pay item.

    Immutable fields (PayItemCode, ItemScope, RateBehavior) are never touched.
    System items (IsSystemStandard=TRUE) are rejected with 422.
    Retired items cannot be updated.

    Requires AllCompanyBranches scope + setup.manage.
    """
    await _ensure_company_admin(company_id, user_id, db)

    cur = await db.execute(
        text(f"""
            SELECT {_CUSTOM_ITEM_COLS}
            FROM   payroll.payitems pi
            WHERE  pi.payitemid = :iid AND pi.companyid = :cid
            FOR UPDATE
        """),
        {"iid": item_id, "cid": company_id},
    )
    row = cur.mappings().first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail=f"Custom pay item {item_id} not found.")
    if bool(row["issystemstandard"]):
        raise HTTPException(status_code=422,
                            detail="System pay items cannot be modified through this endpoint.")
    if row["status"] == "Retired":
        raise HTTPException(status_code=422,
                            detail="Retired pay items cannot be updated.")

    # Merge: apply patch where non-None, otherwise keep current DB value.
    new_label   = data.display_label if data.display_label  is not None else row.get("displaylabel")
    new_name    = data.pay_item_name  if data.pay_item_name  is not None else row["payitemname"]
    new_cat     = data.category       if data.category       is not None else row["category"]
    new_unit    = data.unit           if data.unit           is not None else row.get("unit")
    new_order   = data.sort_order     if data.sort_order     is not None else row["sortorder"]
    new_notes   = data.notes          if data.notes          is not None else row.get("notes")

    # Enforce Daily/Period invariants (ItemScope is immutable — check against DB value).
    item_scope = row["itemscope"]
    if item_scope == "Daily":
        # Daily items must always have a unit.
        if not new_unit or not str(new_unit).strip():
            raise HTTPException(
                status_code=422,
                detail="Daily custom pay items require a unit (e.g. 'Stop', 'km').",
            )
    elif item_scope == "Period":
        # Period items must not have a unit; reject if caller is trying to set one.
        if data.unit is not None:
            raise HTTPException(
                status_code=422,
                detail="Period custom pay items do not use a unit. Remove 'unit' from the request.",
            )
        new_unit = None  # Always keep null for Period items regardless.

    await db.execute(
        text("""
            UPDATE payroll.payitems
            SET    displaylabel  = :label,
                   payitemname   = :name,
                   category      = :cat,
                   unit          = :unit,
                   sortorder     = :order,
                   notes         = :notes,
                   updatedbyuserid = :uid,
                   updatedatutc  = NOW()
            WHERE  payitemid = :iid
        """),
        {
            "label": new_label, "name": new_name, "cat": new_cat,
            "unit": new_unit, "order": new_order, "notes": new_notes,
            "uid": user_id, "iid": item_id,
        },
    )

    await _write_settings_audit(
        db,
        company_id=company_id,
        branch_id=None,
        user_id=user_id,
        action_code="CUSTOM_PAY_ITEM_UPDATED",
        entity_name="PayItems",
        entity_id=str(item_id),
        old_value={
            "pay_item_name": row["payitemname"],
            "category":      row["category"],
            "display_label": row.get("displaylabel"),
        },
        new_value={
            "pay_item_name": new_name,
            "category":      new_cat,
            "display_label": new_label,
        },
    )

    updated = await _get_custom_item_or_404(item_id, company_id, db)
    return _row_to_custom_pay_item(updated)


async def get_custom_pay_item_usage(
    item_id: int,
    company_id: int,
    user_id: int,
    db: AsyncConnection,
) -> CustomPayItemUsage:
    """
    Return usage counts for a custom pay item (draft lines + final lines).
    Used to decide between physical delete and retire.
    Requires AllCompanyBranches scope.
    """
    await _ensure_company_admin(company_id, user_id, db)
    row = await _get_custom_item_or_404(item_id, company_id, db)
    if bool(row["issystemstandard"]):
        raise HTTPException(status_code=422,
                            detail="System pay items are not managed through this endpoint.")
    return await _compute_usage(item_id, company_id, row["payitemcode"], db)


async def delete_custom_pay_item(
    item_id: int,
    company_id: int,
    user_id: int,
    db: AsyncConnection,
) -> CustomPayItemDeleteResult:
    """
    Smart delete for a custom pay item.

    Decision tree:
      - System item              → 422, blocked.
      - Already Retired          → idempotent 200, returns current state.
      - Meaningful or final use  → retire (Status = 'Retired').
      - Only non-meaningful use  → clean empty/voided draft lines, physical delete.
      - Never used               → physical delete.

    Physical delete sequence: PayItemSettings → PayItemLineTypeMap →
        PayItemRateTypeMap → BranchPayItemConfig → PayrollDraftLines (empty only)
        → NULL out CustomPayItemRequests.ApprovedPayItemID → PayItems.

    Retired item codes are permanently blocked (ux_PayItems_Company_PayItemCode
    index keeps the code row, so the unique constraint prevents reuse).

    Requires AllCompanyBranches scope + setup.manage.
    """
    await _ensure_company_admin(company_id, user_id, db)

    # Pre-check: look up item by ID only so we can distinguish "not found"
    # from "found but belongs to a different company / is a system item".
    # System items have CompanyID IS NULL — the company-scoped FOR UPDATE query
    # below would silently return no row for them, yielding a misleading 404.
    pre = await db.execute(
        text("""
            SELECT payitemid, companyid, issystemstandard
            FROM   payroll.payitems
            WHERE  payitemid = :iid
        """),
        {"iid": item_id},
    )
    pre_row = pre.mappings().first()
    if pre_row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail=f"Custom pay item {item_id} not found.")
    if bool(pre_row["issystemstandard"]):
        raise HTTPException(status_code=422,
                            detail="System pay items cannot be deleted.")
    if pre_row["companyid"] != company_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail=f"Custom pay item {item_id} not found.")

    cur = await db.execute(
        text(f"""
            SELECT {_CUSTOM_ITEM_COLS}
            FROM   payroll.payitems pi
            WHERE  pi.payitemid = :iid AND pi.companyid = :cid
            FOR UPDATE
        """),
        {"iid": item_id, "cid": company_id},
    )
    row = cur.mappings().first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail=f"Custom pay item {item_id} not found.")
    if bool(row["issystemstandard"]):
        raise HTTPException(status_code=422,
                            detail="System pay items cannot be deleted.")

    pay_item_code = row["payitemcode"]

    # Idempotent: already retired
    if row["status"] == "Retired":
        return CustomPayItemDeleteResult(
            pay_item_id=item_id,
            pay_item_code=pay_item_code,
            deletion_type="retired",
            cleaned_draft_lines=0,
        )

    usage = await _compute_usage(item_id, company_id, pay_item_code, db)

    # CP-2C: if the item is referenced by any period pay-item snapshot, retire
    # instead of physically deleting — snapshot rows must outlive the catalog row.
    if not usage.deletion_would_retire:
        snap_count_row = await db.execute(
            text("""
                SELECT COUNT(*) AS cnt
                FROM payroll.payrollperiodpayitems
                WHERE payitemid = :iid
            """),
            {"iid": item_id},
        )
        snap_count = int((snap_count_row.scalar_one() or 0))
        if snap_count > 0:
            # Force retire path — physical delete would violate the FK.
            usage = usage.model_copy(update={"deletion_would_retire": True})

    if usage.deletion_would_retire:
        # --- RETIRE ---
        await db.execute(
            text("""
                UPDATE payroll.payitems
                SET    status = 'Retired', updatedatutc = NOW(), updatedbyuserid = :uid
                WHERE  payitemid = :iid
            """),
            {"uid": user_id, "iid": item_id},
        )
        await _write_settings_audit(
            db,
            company_id=company_id,
            branch_id=None,
            user_id=user_id,
            action_code="CUSTOM_PAY_ITEM_RETIRED",
            entity_name="PayItems",
            entity_id=str(item_id),
            old_value={"status": row["status"]},
            new_value={"status": "Retired"},
        )
        return CustomPayItemDeleteResult(
            pay_item_id=item_id,
            pay_item_code=pay_item_code,
            deletion_type="retired",
            cleaned_draft_lines=0,
        )

    else:
        # --- PHYSICAL DELETE ---
        cleaned = usage.non_meaningful_draft_line_count

        # CP-0A: Lock all periods that have DraftLines referencing this pay item, then
        # verify none are non-Open.  Using SELECT FOR UPDATE means any concurrent
        # period-status transition must wait for this transaction to commit, so the
        # status we read is guaranteed to be the committed final state at decision time.
        # This prevents the race where a period transitions from Open → InReview between
        # our safety check and the physical deletion.
        locked_periods = await db.execute(
            text("""
                SELECT pp.payrollperiodid, pp.status
                FROM   payroll.payrollperiods pp
                WHERE  pp.companyid = :cid
                  AND  pp.payrollperiodid IN (
                           SELECT DISTINCT dl.payrollperiodid
                           FROM   payroll.payrolldraftlines dl
                           WHERE  dl.companyid = :cid
                             AND  dl.linetype  = :code
                       )
                FOR UPDATE
            """),
            {"cid": company_id, "code": pay_item_code},
        )
        locked_rows = locked_periods.mappings().all()
        non_open_count = sum(1 for r in locked_rows if r["status"] != "Open")
        if non_open_count > 0:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Cannot physically delete pay item '{pay_item_code}': "
                    f"{non_open_count} payroll source row(s) reference it in non-Open "
                    "periods (InReview, Approved, Locked, Archived, or Cancelled). "
                    "Deactivate or retire the item instead to preserve historical records."
                ),
            )

        # CP-0A: Recompute usage after holding both the PayItem lock and all
        # referenced Period locks.  Any concurrent zero→meaningful update or new
        # DraftLine insert would have had to acquire the PayItem lock first; since
        # we hold it, no such change can commit between our initial usage check and
        # here.  The recompute is defence-in-depth: if usage changed while we were
        # waiting to acquire the period locks (which also serialise via FOR UPDATE),
        # we re-evaluate and route to retire instead of physical delete.
        usage = await _compute_usage(item_id, company_id, pay_item_code, db)
        if usage.deletion_would_retire:
            await db.execute(
                text("""
                    UPDATE payroll.payitems
                    SET    status = 'Retired', updatedatutc = NOW(), updatedbyuserid = :uid
                    WHERE  payitemid = :iid
                """),
                {"uid": user_id, "iid": item_id},
            )
            await _write_settings_audit(
                db,
                company_id=company_id,
                branch_id=None,
                user_id=user_id,
                action_code="CUSTOM_PAY_ITEM_RETIRED",
                entity_name="PayItems",
                entity_id=str(item_id),
                old_value={"status": row["status"]},
                new_value={"status": "Retired"},
            )
            return CustomPayItemDeleteResult(
                pay_item_id=item_id,
                pay_item_code=pay_item_code,
                deletion_type="retired",
                cleaned_draft_lines=0,
            )

        # 1. Clean empty/voided draft lines in Open periods only.
        # After all locks and the recomputed usage check we know all remaining
        # references are non-meaningful rows in Open periods.
        cleaned = usage.non_meaningful_draft_line_count
        if cleaned > 0:
            await db.execute(
                text("""
                    DELETE FROM payroll.payrolldraftlines
                    WHERE  companyid = :cid
                      AND  linetype  = :code
                      AND  (status = 'Void'
                            OR (quantity = 0
                                AND rateamount IS NULL
                                AND (calculatedamount IS NULL OR calculatedamount = 0)))
                      AND  payrollperiodid IN (
                               SELECT payrollperiodid
                               FROM   payroll.payrollperiods
                               WHERE  companyid = :cid AND status = 'Open'
                           )
                """),
                {"cid": company_id, "code": pay_item_code},
            )

        # 2. Remove supporting catalog rows (FK safety — no ON DELETE CASCADE)
        #    Phase 4B.3: capture the mapped CPI_ RateType IDs BEFORE deleting
        #    PayItemRateTypeMap, so we can deactivate any that become orphaned
        #    (no remaining mappings, no DriverRates) after the delete.  This
        #    prevents the orphaned-CPI_ exploit where an active+unmapped RateType
        #    generated by a now-deleted PayItem could be claimed by another company.
        mapped_rt_result = await db.execute(
            text("""
                SELECT pirm.ratetypeid, rt.ratecode
                FROM   payroll.payitemratetypemap pirm
                JOIN   payroll.ratetypes rt ON rt.ratetypeid = pirm.ratetypeid
                WHERE  pirm.payitemid = :iid
                  AND  rt.ratecode LIKE 'CPI_%'
            """),
            {"iid": item_id},
        )
        cpi_rt_ids = [r["ratetypeid"] for r in mapped_rt_result.mappings().all()]

        for tbl in (
            "payroll.payitemsettings",
            "payroll.payitemlinetypemap",
            "payroll.payitemratetypemap",
            "payroll.branchpayitemconfig",
        ):
            await db.execute(
                text(f"DELETE FROM {tbl} WHERE payitemid = :iid"),
                {"iid": item_id},
            )

        # Deactivate generated CPI_ RateTypes that are now orphaned:
        # no remaining PayItemRateTypeMap rows AND no DriverRates using them.
        # RateTypes with any remaining usage are left untouched.
        for rtid in cpi_rt_ids:
            await db.execute(
                text("""
                    UPDATE payroll.ratetypes
                    SET    isactive = FALSE
                    WHERE  ratetypeid = :rtid
                      AND  NOT EXISTS (
                              SELECT 1 FROM payroll.payitemratetypemap
                              WHERE  ratetypeid = :rtid
                           )
                      AND  NOT EXISTS (
                              SELECT 1 FROM payroll.driverrates
                              WHERE  ratetypeid = :rtid
                           )
                """),
                {"rtid": rtid},
            )

        # 3. NULL out ApprovedPayItemID in any matching request (preserve request history)
        await db.execute(
            text("""
                UPDATE payroll.custompayitemrequests
                SET    approvedpayitemid = NULL
                WHERE  approvedpayitemid = :iid
            """),
            {"iid": item_id},
        )

        # 4. Delete the item itself
        await db.execute(
            text("DELETE FROM payroll.payitems WHERE payitemid = :iid"),
            {"iid": item_id},
        )

        await _write_settings_audit(
            db,
            company_id=company_id,
            branch_id=None,
            user_id=user_id,
            action_code="CUSTOM_PAY_ITEM_DELETED",
            entity_name="PayItems",
            entity_id=str(item_id),
            old_value={
                "pay_item_code": pay_item_code,
                "status":        row["status"],
                "cleaned_lines": cleaned,
            },
            new_value=None,
        )
        return CustomPayItemDeleteResult(
            pay_item_id=None,
            pay_item_code=pay_item_code,
            deletion_type="physical",
            cleaned_draft_lines=cleaned,
        )


# ---------------------------------------------------------------------------
# Fix 3E-B: Backfill broken custom pay items (no PayItemRateTypeMap rows)
# ---------------------------------------------------------------------------

async def backfill_custom_pay_item_rate_structure(
    company_id: int,
    db: AsyncConnection,
) -> list[dict]:
    """
    Scan all custom Daily pay items for this company that have RequiresRate=TRUE
    but NO active PayItemRateTypeMap rows, and create minimal RateTypes +
    PayItemRateTypeMap rows for each.

    Safe to run multiple times (idempotent: ON CONFLICT DO NOTHING).
    Does NOT delete or modify any existing DriverRates, PayrollDraftLines, or
    PayrollFinalLines rows.

    Returns a list of repaired items: [{"pay_item_id": ..., "pay_item_name": ..., "rate_types_created": N}]
    """
    # Find broken items: custom + requires_rate + no active mapping
    broken_result = await db.execute(
        text("""
            SELECT pi.payitemid, pi.payitemname, pi.unit, pi.ratebehavior
            FROM   payroll.payitems pi
            WHERE  pi.companyid        = :cid
              AND  pi.issystemstandard = FALSE
              AND  pi.requiresrate     = TRUE
              AND  pi.status          != 'Retired'
              AND  NOT EXISTS (
                       SELECT 1
                       FROM   payroll.payitemratetypemap pirm
                       WHERE  pirm.payitemid = pi.payitemid
                         AND  pirm.status    = 'Active'
                   )
        """),
        {"cid": company_id},
    )
    broken_rows = broken_result.mappings().all()
    repaired: list[dict] = []
    _MULTI_BEHAVIORS = {"OrdinalTier", "RangeBracket", "RangeProgressive", "Block"}

    for row in broken_rows:
        pid = row["payitemid"]
        pname = row["payitemname"]
        punit = row.get("unit")
        pbehav = row["ratebehavior"]

        rate_names = (
            ["Rate 1", "Rate 2", "Rate 3"]
            if pbehav in _MULTI_BEHAVIORS
            else [pname.strip() + " Rate"]
        )
        created = 0
        for idx, rname in enumerate(rate_names, start=1):
            rate_code = f"CPI_{pid}_{idx}"
            # Phase 4C: set CompanyID on the RateType for structural ownership.
            rt_result = await db.execute(
                text("""
                    INSERT INTO payroll.ratetypes (ratecode, ratename, unitname, isactive, companyid)
                    VALUES (:code, :name, :unit, TRUE, :cid)
                    ON CONFLICT (ratecode) DO UPDATE
                        SET ratename  = EXCLUDED.ratename,
                            companyid = EXCLUDED.companyid
                    RETURNING ratetypeid
                """),
                {"code": rate_code, "name": rname, "unit": punit or "Unit", "cid": company_id},
            )
            rt_id = rt_result.scalar_one()
            map_result = await db.execute(
                text("""
                    INSERT INTO payroll.payitemratetypemap
                        (payitemid, ratetypeid, isprimary, status)
                    VALUES (:piid, :rtid, :primary, 'Active')
                    ON CONFLICT (payitemid, ratetypeid) DO NOTHING
                    RETURNING payitemratetypemapid
                """),
                {"piid": pid, "rtid": rt_id, "primary": (idx == 1)},
            )
            if map_result.first() is not None:
                created += 1
        repaired.append({
            "pay_item_id":        pid,
            "pay_item_name":      pname,
            "rate_types_created": created,
        })

    return repaired


# ---------------------------------------------------------------------------
# Public service functions — branch request / admin approval flow
# ---------------------------------------------------------------------------

async def create_pay_item_request(
    company_id: int,
    user_id: int,
    data: CustomPayItemRequestCreate,
    db: AsyncConnection,
) -> CustomPayItemRequest:
    """
    Branch user submits a request for a new custom pay item.

    Requires branch access + payroll.entry permission on the requesting branch.
    Blocks duplicate requests: if a PendingApproval or Approved request already
    exists for this code in this company, returns 422.
    System item codes are explicitly blocked.
    """
    # Branch access check
    can_see_all, branch_ids = await _check_branch_access(company_id, user_id, db)
    if not can_see_all and data.branch_id not in branch_ids:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have access to this branch.",
        )

    # Branch must belong to this company
    await _check_branch_belongs_to_company(data.branch_id, company_id, db)

    # Permission gate
    await _check_permission(company_id, user_id, data.branch_id, "payroll.entry", db)

    # LLR-A: New Custom Daily requests must use the CDPI workflow.
    if data.item_scope == "Daily":
        raise HTTPException(
            status_code=422,
            detail=(
                "Custom Daily PayItems must be requested through the CDPI workflow. "
                "Use POST /settings/cdpi/requests instead."
            ),
        )

    # Duplicate / conflict checks (service-level; DB index is the race backstop)
    await _block_if_code_taken(company_id, data.pay_item_code, db)

    try:
        result = await db.execute(
            text("""
                INSERT INTO payroll.custompayitemrequests (
                    companyid, requestingbranchid, requestedbyuserid,
                    payitemcode, displaylabel, payitemname,
                    itemscope, ratebehavior, category, unit, notes, sortorder
                ) VALUES (
                    :cid, :bid, :uid,
                    :code, :display_label, :name,
                    :item_scope, :rate_behavior, :category, :unit, :notes, :sort_order
                )
                RETURNING requestid
            """),
            {
                "cid":           company_id,
                "bid":           data.branch_id,
                "uid":           user_id,
                "code":          data.pay_item_code,
                "display_label": data.display_label,
                "name":          data.pay_item_name,
                "item_scope":    data.item_scope,
                "rate_behavior": data.rate_behavior,
                "category":      data.category,
                "unit":          data.unit,
                "notes":         data.notes,
                "sort_order":    data.sort_order,
            },
        )
    except SAIntegrityError:
        raise HTTPException(
            status_code=422,
            detail=(
                f"A pending or approved request for code '{data.pay_item_code}' already "
                "exists for this company."
            ),
        )
    new_request_id = result.scalar_one()

    await _write_settings_audit(
        db,
        company_id=company_id,
        branch_id=data.branch_id,
        user_id=user_id,
        action_code="CUSTOM_PAY_ITEM_REQUESTED",
        entity_name="CustomPayItemRequests",
        entity_id=str(new_request_id),
        new_value={
            "pay_item_code": data.pay_item_code,
            "item_scope":    data.item_scope,
            "rate_behavior": data.rate_behavior,
            "pay_item_name": data.pay_item_name,
        },
    )

    return await _load_request_by_id(new_request_id, db)


async def _load_request_by_id(request_id: int, db: AsyncConnection) -> CustomPayItemRequest:
    """Internal: load a request row with joined display names."""
    result = await db.execute(
        text(f"""
            SELECT {_REQUEST_COLS}
            FROM   payroll.custompayitemrequests r
            JOIN   core.branches b  ON b.branchid = r.requestingbranchid
            JOIN   sec.users u      ON u.userid   = r.requestedbyuserid
            LEFT JOIN sec.users du  ON du.userid  = r.decidedbyuserid
            WHERE  r.requestid = :rid
        """),
        {"rid": request_id},
    )
    row = result.mappings().first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail=f"Request {request_id} not found.")
    return _row_to_request(row)


async def get_pay_item_requests(
    company_id: int,
    user_id: int,
    db: AsyncConnection,
    *,
    request_status: str | None = None,
) -> list[CustomPayItemRequest]:
    """
    List pay item requests.
    Admin (AllCompanyBranches) sees all company requests.
    Branch-scoped users see only requests from their own branch(es).
    """
    can_see_all, branch_ids = await _check_branch_access(company_id, user_id, db)

    filters = ["r.companyid = :cid"]
    params: dict = {"cid": company_id}

    if not can_see_all:
        if not branch_ids:
            return []
        in_clause, in_params = _build_in_clause(branch_ids, "bid")
        filters.append(f"r.requestingbranchid IN ({in_clause})")
        params.update(in_params)

    if request_status is not None:
        filters.append("r.status = :req_status")
        params["req_status"] = request_status

    where = " AND ".join(filters)
    result = await db.execute(
        text(f"""
            SELECT {_REQUEST_COLS}
            FROM   payroll.custompayitemrequests r
            JOIN   core.branches b  ON b.branchid = r.requestingbranchid
            JOIN   sec.users u      ON u.userid   = r.requestedbyuserid
            LEFT JOIN sec.users du  ON du.userid  = r.decidedbyuserid
            WHERE  {where}
            ORDER BY r.requestedatutc DESC
        """),
        params,
    )
    return [_row_to_request(r) for r in result.mappings().all()]


async def get_pay_item_request_by_id(
    request_id: int,
    company_id: int,
    user_id: int,
    db: AsyncConnection,
) -> CustomPayItemRequest:
    """
    Fetch a single request. Branch-scoped users can only see requests from
    their own branch(es); admin can see all.
    """
    can_see_all, branch_ids = await _check_branch_access(company_id, user_id, db)

    result = await db.execute(
        text(f"""
            SELECT {_REQUEST_COLS}
            FROM   payroll.custompayitemrequests r
            JOIN   core.branches b  ON b.branchid = r.requestingbranchid
            JOIN   sec.users u      ON u.userid   = r.requestedbyuserid
            LEFT JOIN sec.users du  ON du.userid  = r.decidedbyuserid
            WHERE  r.requestid  = :rid
              AND  r.companyid  = :cid
        """),
        {"rid": request_id, "cid": company_id},
    )
    row = result.mappings().first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail=f"Request {request_id} not found.")

    # Scope check for branch-limited users
    if not can_see_all and row["requestingbranchid"] not in branch_ids:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="You do not have access to this request.")
    return _row_to_request(row)


async def decide_pay_item_request(
    request_id: int,
    company_id: int,
    user_id: int,
    data: CustomPayItemRequestDecide,
    db: AsyncConnection,
) -> CustomPayItemRequest:
    """
    Admin approves or rejects a custom pay item request.

    Approval (atomic transaction):
      1. Re-run duplicate code check (race-condition guard).
      2. INSERT payroll.PayItems (company-level, Active, IsDefaultBranchActive=FALSE).
      3. INSERT payroll.BranchPayItemConfig for the requesting branch (IsActive=TRUE).
      4. UPDATE CustomPayItemRequests (Status=Approved, ApprovedPayItemID=new id).
      5. Audit CUSTOM_PAY_ITEM_APPROVED.

    Rejection:
      1. UPDATE CustomPayItemRequests (Status=Rejected).
      2. Audit CUSTOM_PAY_ITEM_REJECTED.
      No PayItem row is created.

    Requires AllCompanyBranches scope + setup.manage.
    """
    await _ensure_company_admin(company_id, user_id, db)

    # Lock the request row
    lock_result = await db.execute(
        text("""
            SELECT requestid, companyid, requestingbranchid, payitemcode,
                   displaylabel, payitemname, itemscope, ratebehavior,
                   category, unit, notes, sortorder, status
            FROM   payroll.custompayitemrequests
            WHERE  requestid = :rid AND companyid = :cid
            FOR UPDATE
        """),
        {"rid": request_id, "cid": company_id},
    )
    req = lock_result.mappings().first()
    if req is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail=f"Request {request_id} not found.")

    # Terminal status check
    if req["status"] != "PendingApproval":
        raise HTTPException(
            status_code=422,
            detail=(
                f"This request is already in '{req['status']}' status "
                "and cannot be decided again."
            ),
        )

    if data.decision == "Approved":
        # LLR-A: Approving legacy Daily requests is disabled; CDPI must be used.
        if req["itemscope"] == "Daily":
            raise HTTPException(
                status_code=422,
                detail=(
                    "Approving legacy Daily custom pay item requests is disabled. "
                    "Create Custom Daily PayItems through the CDPI workflow instead."
                ),
            )

        # Race-condition guard: re-check code availability
        await _block_if_code_taken(
            company_id, req["payitemcode"], db,
            exclude_request_id=request_id,
        )

        _rate_using = {"PerUnit", "OrdinalTier", "RangeBracket", "RangeProgressive", "Block"}
        appears_in_entry = req["itemscope"] == "Daily"
        requires_rate    = req["ratebehavior"] in _rate_using
        today            = _date.today()

        # Determine safe effective_from for the BranchPayItemConfig:
        # If the requesting branch has an open payroll period containing today,
        # the config must not start inside it — schedule it for the day after
        # that period ends (same rule as M11 update_pay_item_config).
        requesting_branch_id = req["requestingbranchid"]
        period_max_end = await _get_current_open_period_max_end(
            requesting_branch_id, company_id, db
        )
        if period_max_end is not None:
            config_effective_from: _date = period_max_end + _timedelta(days=1)
        else:
            config_effective_from = today

        # 1. Create the PayItem
        ins = await db.execute(
            text("""
                INSERT INTO payroll.payitems (
                    companyid, branchid, payitemcode, displaylabel, payitemname,
                    category, datatype, unit, status, sortorder,
                    appearsinpayrollentry, appearsinledger, appearsinreports,
                    requiresrate, issystemstandard,
                    itemscope, ratebehavior, isdefaultbranchactive,
                    requestingbranchid, createdbyuserid, notes
                ) VALUES (
                    :cid, NULL, :code, :display_label, :name,
                    :category, 'Decimal', :unit, 'Active', :sort_order,
                    :in_entry, TRUE, TRUE,
                    :requires_rate, FALSE,
                    :item_scope, :rate_behavior, FALSE,
                    :req_branch_id, :uid, :notes
                )
                RETURNING payitemid
            """),
            {
                "cid":           company_id,
                "code":          req["payitemcode"],
                "display_label": req.get("displaylabel"),
                "name":          req["payitemname"],
                "category":      req["category"],
                "unit":          req.get("unit"),
                "sort_order":    req["sortorder"],
                "in_entry":      appears_in_entry,
                "requires_rate": requires_rate,
                "item_scope":    req["itemscope"],
                "rate_behavior": req["ratebehavior"],
                "req_branch_id": req["requestingbranchid"],
                "uid":           user_id,
                "notes":         req.get("notes"),
            },
        )
        new_item_id = ins.scalar_one()

        # 1b. Create RateTypes + PayItemRateTypeMap for rate-using items (Fix 3E-A).
        _req_item_unit = req.get("unit")
        _req_rate_behavior = req["ratebehavior"]
        _MULTI_RATE_BEHAVIORS_REQ = {"OrdinalTier", "RangeBracket", "RangeProgressive", "Block"}
        if requires_rate:
            _req_rate_names: list[str] = (
                ["Rate 1", "Rate 2", "Rate 3"]
                if _req_rate_behavior in _MULTI_RATE_BEHAVIORS_REQ
                else [req["payitemname"].strip() + " Rate"]
            )
            for _idx, _rname in enumerate(_req_rate_names, start=1):
                _rate_code = f"CPI_{new_item_id}_{_idx}"
                _rt_result = await db.execute(
                    text("""
                        INSERT INTO payroll.ratetypes (ratecode, ratename, unitname, isactive, companyid)
                        VALUES (:code, :name, :unit, TRUE, :cid)
                        ON CONFLICT (ratecode) DO UPDATE
                            SET ratename  = EXCLUDED.ratename,
                                companyid = EXCLUDED.companyid
                        RETURNING ratetypeid
                    """),
                    {"code": _rate_code, "name": _rname, "unit": _req_item_unit or "Unit", "cid": company_id},
                )
                _rt_id = _rt_result.scalar_one()
                await db.execute(
                    text("""
                        INSERT INTO payroll.payitemratetypemap
                            (payitemid, ratetypeid, isprimary, status)
                        VALUES (:piid, :rtid, :primary, 'Active')
                        ON CONFLICT (payitemid, ratetypeid) DO NOTHING
                    """),
                    {"piid": new_item_id, "rtid": _rt_id, "primary": (_idx == 1)},
                )

        # 2. Create BranchPayItemConfig for requesting branch.
        # Use safe effective_from: if an open period is running for that branch today,
        # schedule activation for the day after it ends (same rule as M11).
        await db.execute(
            text("""
                INSERT INTO payroll.branchpayitemconfig (
                    companyid, branchid, payitemid,
                    isactive, effectivefrom, createdbyuserid
                ) VALUES (
                    :cid, :bid, :item_id, TRUE, :eff_from, :uid
                )
            """),
            {
                "cid":      company_id,
                "bid":      requesting_branch_id,
                "item_id":  new_item_id,
                "eff_from": config_effective_from,
                "uid":      user_id,
            },
        )

        # 3. Mark request as Approved
        await db.execute(
            text("""
                UPDATE payroll.custompayitemrequests
                SET    status            = 'Approved',
                       decidedbyuserid   = :uid,
                       decidedatutc      = NOW(),
                       decisionreason    = :reason,
                       approvedpayitemid = :item_id
                WHERE  requestid = :rid
            """),
            {
                "uid":     user_id,
                "reason":  data.decision_reason,
                "item_id": new_item_id,
                "rid":     request_id,
            },
        )

        # 4. Audit
        await _write_settings_audit(
            db,
            company_id=company_id,
            branch_id=req["requestingbranchid"],
            user_id=user_id,
            action_code="CUSTOM_PAY_ITEM_APPROVED",
            entity_name="CustomPayItemRequests",
            entity_id=str(request_id),
            new_value={
                "pay_item_code":         req["payitemcode"],
                "approved_pay_item_id":  new_item_id,
                "activated_for_branch":  requesting_branch_id,
                "config_effective_from": str(config_effective_from),
            },
        )

    else:  # Rejected
        await db.execute(
            text("""
                UPDATE payroll.custompayitemrequests
                SET    status          = 'Rejected',
                       decidedbyuserid = :uid,
                       decidedatutc    = NOW(),
                       decisionreason  = :reason
                WHERE  requestid = :rid
            """),
            {"uid": user_id, "reason": data.decision_reason, "rid": request_id},
        )

        await _write_settings_audit(
            db,
            company_id=company_id,
            branch_id=req["requestingbranchid"],
            user_id=user_id,
            action_code="CUSTOM_PAY_ITEM_REJECTED",
            entity_name="CustomPayItemRequests",
            entity_id=str(request_id),
            old_value={"pay_item_code": req["payitemcode"]},
            new_value={"decision_reason": data.decision_reason},
        )

    return await _load_request_by_id(request_id, db)


# ===========================================================================
# M13: PayItemRateTypeMap — assign a rate type to a custom PerUnit item
# ===========================================================================

async def assign_rate_type_to_pay_item(
    item_id: int,
    data: PayItemRateTypeMapCreate,
    company_id: int,
    user_id: int,
    db: AsyncConnection,
) -> PayItemRateTypeMapSummary:
    """
    Create or update the PayItemRateTypeMap entry for a custom PerUnit pay item.

    This mapping is required for the calculation engine to look up the driver's
    approved DriverRate when inserting draft lines for the item.

    Idempotent on (PayItemID, RateTypeID): an ON CONFLICT DO UPDATE is used so
    calling this endpoint twice for the same pair just refreshes the row.

    Guards:
      - Caller must have AllCompanyBranches scope + setup.manage permission.
      - Pay item must belong to this company (not a system item).
      - Pay item must use a rate-based behavior (PerUnit, OrdinalTier, RangeBracket,
        RangeProgressive, or Block).  EnteredAmount / Fixed / None items do not
        use DriverRates and therefore do not need a RateType mapping.
      - rate_type_id must exist and be active.
    """
    # Issue 5 fix: this write requires AllCompanyBranches scope + setup.manage,
    # the same gate as all other pay-item and company-setup writes.
    await _ensure_company_admin(company_id, user_id, db)

    # Behaviors that use DriverRates (and therefore need a RateType mapping).
    _RATE_USING_BEHAVIORS = {
        "PerUnit", "OrdinalTier", "RangeBracket", "RangeProgressive", "Block"
    }

    # Verify the item belongs to this company and uses a rate-based behavior.
    pi_result = await db.execute(
        text("""
            SELECT payitemid, companyid, ratebehavior, payitemcode
            FROM   payroll.payitems
            WHERE  payitemid = :piid
              AND  companyid = :cid
        """),
        {"piid": item_id, "cid": company_id},
    )
    pi_row = pi_result.mappings().first()
    if pi_row is None:
        raise HTTPException(
            status_code=404,
            detail="Pay item not found for this company.",
        )
    if pi_row["ratebehavior"] not in _RATE_USING_BEHAVIORS:
        raise HTTPException(
            status_code=422,
            detail=(
                "Rate type mapping requires a rate-based pay item behavior. "
                f"This item has RateBehavior '{pi_row['ratebehavior']}' "
                "which does not use driver rates (EnteredAmount / Fixed / None)."
            ),
        )

    # Verify rate type exists and is active.
    rt_result = await db.execute(
        text("""
            SELECT ratetypeid, ratecode, ratename
            FROM   payroll.ratetypes
            WHERE  ratetypeid = :rtid AND isactive = TRUE
        """),
        {"rtid": data.rate_type_id},
    )
    rt_row = rt_result.mappings().first()
    if rt_row is None:
        raise HTTPException(
            status_code=422,
            detail="rate_type_id does not exist or is inactive.",
        )

    # Company scope guard (Phase 4C structural check):
    # Prevent mapping a company PayItem to a foreign or unmapped RateType.
    #
    # Valid targets (RateType.CompanyID):
    #   IS NULL          -> system type: any company may add its own PayItem
    #   = company_id     -> own custom type: safe
    #
    # Invalid targets:
    #   IS NOT NULL AND != company_id -> foreign company type: reject
    #
    # The DB trigger (trg_guard_payitemratetypemap_ownership) enforces the same
    # rule at insert time, so this service check is defence-in-depth.
    scope_result = await db.execute(
        text("""
            SELECT companyid
            FROM   payroll.ratetypes
            WHERE  ratetypeid = :rtid
        """),
        {"rtid": data.rate_type_id},
    )
    scope_row = scope_result.mappings().first()
    rt_company = scope_row["companyid"] if scope_row else None

    if rt_company is not None and rt_company != company_id:
        # Foreign-owned custom RateType
        raise HTTPException(
            status_code=422,
            detail="Rate type does not belong to this company.",
        )

    # If this mapping is primary, demote any existing primary mapping for the item.
    if data.is_primary:
        await db.execute(
            text("""
                UPDATE payroll.payitemratetypemap
                SET    isprimary = FALSE
                WHERE  payitemid  = :piid
                  AND  isprimary  = TRUE
                  AND  ratetypeid != :rtid
            """),
            {"piid": item_id, "rtid": data.rate_type_id},
        )

    # Insert or update (idempotent on the unique key PayItemID+RateTypeID).
    map_result = await db.execute(
        text("""
            INSERT INTO payroll.payitemratetypemap
                (payitemid, ratetypeid, isprimary, status)
            VALUES (:piid, :rtid, :is_primary, 'Active')
            ON CONFLICT (payitemid, ratetypeid) DO UPDATE
                SET isprimary = EXCLUDED.isprimary,
                    status    = 'Active'
            RETURNING payitemratetypemapid
        """),
        {
            "piid":       item_id,
            "rtid":       data.rate_type_id,
            "is_primary": data.is_primary,
        },
    )
    map_id: int = map_result.scalar_one()

    await _write_settings_audit(
        db,
        company_id=company_id,
        branch_id=None,
        user_id=user_id,
        action_code="PAY_ITEM_RATE_TYPE_ASSIGNED",
        entity_name="PayItemRateTypeMap",
        entity_id=str(map_id),
        new_value={
            "pay_item_id":  item_id,
            "rate_type_id": data.rate_type_id,
            "rate_code":    rt_row["ratecode"],
            "is_primary":   data.is_primary,
        },
    )

    return PayItemRateTypeMapSummary(
        pay_item_rate_type_map_id=map_id,
        pay_item_id=item_id,
        rate_type_id=int(rt_row["ratetypeid"]),
        rate_code=rt_row["ratecode"],
        rate_name=rt_row["ratename"],
        is_primary=data.is_primary,
        status="Active",
    )


# ===========================================================================
# Pay item ordering
# ===========================================================================

async def update_pay_item_order(
    company_id: int,
    user_id: int,
    data: "PayItemOrderUpdate",
    db: AsyncConnection,
) -> None:
    """
    Replace the sort_order of each listed pay item.

    All pay_item_ids must be valid and visible to this company
    (system items: companyid IS NULL; custom items: companyid = :cid).
    Invalid or inaccessible IDs are rejected with HTTP 422 before any writes.

    Runs all UPDATE statements in the same SQLAlchemy transaction (opened by
    engine.begin() in get_db).  A failure in any UPDATE rolls back all changes.

    Note on system items: system items (IsSystemStandard=TRUE) have CompanyID=NULL
    and their sort_order is shared across all companies in a multi-tenant deployment.
    In a single-company setup this is acceptable.  If you need per-company ordering
    of system items in a multi-tenant context, introduce a separate override table.

    Requires AllCompanyBranches scope + setup.manage.
    """
    from app.settings.schemas import PayItemOrderUpdate  # local import avoids circular

    await _ensure_company_admin(company_id, user_id, db)

    ids = [entry.pay_item_id for entry in data.items]
    in_clause, in_params = _build_in_clause(ids, "pid")

    # Validate: all IDs must be visible to this company (system or company-owned, not Retired).
    found_r = await db.execute(
        text(f"""
            SELECT payitemid
            FROM   payroll.payitems
            WHERE  payitemid IN ({in_clause})
              AND  status    != 'Retired'
              AND  (companyid IS NULL OR companyid = :cid)
        """),
        {"cid": company_id, **in_params},
    )
    found_ids = {row["payitemid"] for row in found_r.mappings().all()}
    invalid = [i for i in ids if i not in found_ids]
    if invalid:
        raise HTTPException(
            status_code=422,
            detail=f"Pay item ID(s) not found or not accessible for this company: {invalid}",
        )

    # Apply all updates within the same transaction.
    for entry in data.items:
        await db.execute(
            text("""
                UPDATE payroll.payitems
                SET    sortorder    = :order,
                       updatedatutc = NOW()
                WHERE  payitemid = :pid
            """),
            {"order": entry.sort_order, "pid": entry.pay_item_id},
        )

    await _write_settings_audit(
        db,
        company_id=company_id,
        branch_id=None,
        user_id=user_id,
        action_code="PAY_ITEM_ORDER_UPDATED",
        entity_name="PayItems",
        entity_id=f"order:{company_id}",
        new_value={
            "item_count": len(data.items),
            "items": [
                {"pay_item_id": e.pay_item_id, "sort_order": e.sort_order}
                for e in data.items
            ],
        },
    )
