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
import random as _random
import secrets
import string as _string
from datetime import date as _date
from typing import TYPE_CHECKING

from fastapi import HTTPException, status
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError as SAIntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from app.company_concurrency import lock_company_for_mutation
from app.company_currency import (
    company_has_durable_monetary_state,
    currency_error,
)
from app.core.service import (
    _build_in_clause,
    _check_branch_access,
    _check_permission,
    _require_not_driver_role,
)
from app.payroll_setup import boundaries
from app.payroll_setup.errors import PolicyError
from app.payroll_setup.locks import lock_company, lock_setups
from app.payroll_setup.payroll_policy import assign_setup
from app.payroll_setup.readiness import (
    branch_schedule_readiness,
    branch_schedule_readiness_detail,
)
from app.payroll_setup.schemas import SetupResponse
from app.settings.schemas import (
    BranchAdmin,
    BranchCreate,
    BranchUpdate,
    CompanyProfile,
    CompanyUpdate,
    OnboardingOptionsResponse,
    StatusKey,
    StatusKeyCreate,
    StatusKeyUpdate,
    StatusRateColumn,
    StatusRateColumnCreate,
    SupportedCurrency,
)

if TYPE_CHECKING:
    pass

# ---------------------------------------------------------------------------
# Status rate-column code generation
# ---------------------------------------------------------------------------

# Characters used in generated SRC_ rate codes.
# Ambiguous characters (O / 0 / I / 1 / L) are excluded so printed codes
# are easy to read and transcribe without error.
_RATE_CODE_CHARSET: str = "".join(
    c for c in (_string.ascii_uppercase + _string.digits)
    if c not in "O0I1L"
)
_SRC_CODE_MAX_RETRIES: int = 10  # collision is astronomically unlikely; 10 gives a clean ceiling

_SRC_SUFFIX_LEN: int = 8  # SRC_{company_id}_{8 chars}


def _generate_src_rate_code(company_id: int) -> str:
    """Return a new candidate StatusRateColumn RateCode: SRC_{company_id}_{8 chars}."""
    suffix = "".join(secrets.choice(_RATE_CODE_CHARSET) for _ in range(_SRC_SUFFIX_LEN))
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


async def _ensure_branch_creator(
    company_id: int, user_id: int, db: AsyncConnection,
    *, with_payroll_start: bool, with_default: bool,
) -> None:
    """branches.create (company-wide, non-driver); payroll_setup.assign when a first payroll start date is sent; settings-admin (setup.manage via _ensure_company_admin) when the new branch is to become the Company default."""
    can_see_all, _ = await _check_branch_access(company_id, user_id, db)
    if not can_see_all:
        raise HTTPException(status_code=403, detail="This action requires company-level (all-branches) access.")
    await _require_not_driver_role(company_id, user_id, db)
    await _check_permission(company_id, user_id, None, "branches.create", db)
    if with_payroll_start:
        await _check_permission(company_id, user_id, None, "payroll_setup.assign", db)
    if with_default:
        await _ensure_company_admin(company_id, user_id, db)


async def _attach_readiness_reasons(
    branches: list[BranchAdmin], company_id: int, user_id: int,
    db: AsyncConnection,
) -> None:
    """Expose reason codes and evaluated dates only to non-drivers with
    payroll.view on each branch."""
    if not branches:
        return
    try:
        await _require_not_driver_role(company_id, user_id, db)
    except HTTPException as exc:
        if exc.status_code == 403:
            return
        raise
    for branch in branches:
        try:
            await _check_permission(company_id, user_id, branch.branch_id, "payroll.view", db)
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

    in_clause, in_params = _build_in_clause(branch_ids, "bid")
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
                c.currencycode,
                sc.currencyname,
                sc.minorunitdigits AS currency_minor_unit_digits,
                core.fn_company_has_durable_monetary_state(c.companyid) AS has_monetary_state,
                c.createdatutc,
                c.updatedatutc,
                b.branchid   AS default_branch_id,
                b.branchname AS default_branch_name
            FROM  core.companies c
            LEFT  JOIN core.supportedcurrencies sc
                   ON sc.currencycode = c.currencycode
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

    if row["currencycode"] is None and row["has_monetary_state"]:
        raise currency_error(
            "COMPANY_CURRENCY_INVARIANT_VIOLATION",
            "Unconfigured Company has durable monetary state; reset/reseed is required.",
            409,
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
        currency_code=row["currencycode"],
        currency_name=row["currencyname"],
        currency_minor_unit_digits=row["currency_minor_unit_digits"],
        currency_change_locked=bool(row["currencycode"] and row["has_monetary_state"]),
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

    # Company mutation takes the conflicting row lock first. It must never take
    # the shared monetary guard and then upgrade: two such transactions would
    # each hold SHARE and deadlock on the UPDATE.
    await lock_company_for_mutation(company_id, db)
    current = await get_company_profile(company_id, user_id, db)
    currency_changed = "currency_code" in data.model_fields_set
    new_currency = current.currency_code
    if currency_changed:
        if data.currency_code is None:
            raise currency_error(
                "COMPANY_CURRENCY_REQUIRED", "Company currency cannot be cleared."
            )
        new_currency = data.currency_code
        supported = (await db.execute(
            text("SELECT 1 FROM core.supportedcurrencies WHERE currencycode = :code"),
            {"code": new_currency},
        )).scalar_one_or_none()
        if supported is None:
            raise currency_error(
                "UNSUPPORTED_CURRENCY_CODE",
                f"Currency code '{new_currency}' is not supported.",
            )
        if new_currency != current.currency_code:
            if await company_has_durable_monetary_state(company_id, db):
                raise currency_error(
                    "COMPANY_CURRENCY_CHANGE_BLOCKED",
                    "Company currency is locked after the first monetary write.",
                    409,
                )
            await db.execute(
                text("UPDATE core.companies SET currencycode = :code WHERE companyid = :cid"),
                {"cid": company_id, "code": new_currency},
            )

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
            "currency_code": current.currency_code,
        },
        new_value={
            "company_name":       data.company_name,
            "legal_name":         data.legal_name,
            "timezone_name":      new_tz,
            "notes":              data.notes,
            "allow_self_approval": new_self_approval,
            "currency_code": new_currency,
        },
    )

    return await get_company_profile(company_id, user_id, db)


async def list_supported_currencies(
    company_id: int, user_id: int, db: AsyncConnection
) -> list[SupportedCurrency]:
    await _ensure_company_admin(company_id, user_id, db)
    rows = (await db.execute(text("""
        SELECT currencycode, currencyname, numericcode, minorunitdigits
        FROM core.supportedcurrencies ORDER BY currencycode
    """))).mappings().all()
    return [
        SupportedCurrency(
            currency_code=row["currencycode"],
            currency_name=row["currencyname"],
            numeric_code=row["numericcode"].strip(),
            minor_unit_digits=row["minorunitdigits"],
        )
        for row in rows
    ]


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
    await _ensure_branch_creator(
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
    await _ensure_branch_creator(
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


# ===========================================================================
# Payroll status keys
# ===========================================================================

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
    for attempt in range(_SRC_CODE_MAX_RETRIES):
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
            if "ratecode" in msg and attempt < _SRC_CODE_MAX_RETRIES - 1:
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
