"""StatusRateColumn generation, reads, creation, and branch defaults."""
import secrets
import string as _string

from fastapi import HTTPException, status
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError as SAIntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from app.access.branch_access import check_branch_access
from app.settings import security
from app.settings.schemas import (
    StatusRateColumn,
    StatusRateColumnCreate,
)

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
    can_see_all, branch_ids = await check_branch_access(company_id, user_id, db)
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
    await security.ensure_company_admin(company_id, user_id, db)

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


async def validate_status_rate_column_for_branch(
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


async def ensure_default_status_rate_column_for_branch(
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
