"""Company-owned Status rate columns.

A Status rate column is what makes a Status monetary: a Status bound to a column is paid
``HoursValue x`` the Driver's rate for that column. The column owns one scalar RateDefinition
with its one scalar component (created with the column, atomically), against which each
permanent Driver's Status rate values are authored as ordinary rate schedules.

A Company has exactly one default active column. Normalized names are unique among a
Company's active columns. Every writer takes the Company's Status catalog lock first, so the
default switch and the name uniqueness cannot race.
"""
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError as SAIntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from app.access.branch_access import check_branch_access
from app.settings import audit, security
from app.settings.schemas import (
    StatusRateColumn,
    StatusRateColumnCreate,
    StatusRateColumnUpdate,
)
from app.settings.status_names import STATUS_NAME_MAX_LENGTH, normalize_status_name

_COLUMN_SELECT = """
    SELECT src.statusratecolumnid, src.companyid, src.columnname, src.isdefault,
           src.isactive, src.createdatutc, src.updatedatutc,
           rd.ratedefinitionid, c.ratecomponentdefinitionid
    FROM   payroll.statusratecolumns src
    JOIN   payroll.ratedefinitions rd
           ON rd.statusratecolumnid = src.statusratecolumnid AND rd.companyid = src.companyid
    JOIN   payroll.ratecomponentdefinitions c
           ON c.ratedefinitionid = rd.ratedefinitionid AND c.sequenceno = 1
"""


def _coded(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})


def _row_to_status_rate_column(row) -> StatusRateColumn:
    return StatusRateColumn(
        status_rate_column_id=row["statusratecolumnid"],
        company_id=row["companyid"],
        column_name=row["columnname"],
        is_default=bool(row["isdefault"]),
        is_active=bool(row["isactive"]),
        rate_definition_id=row["ratedefinitionid"],
        rate_component_definition_id=row["ratecomponentdefinitionid"],
        created_at_utc=row["createdatutc"],
        updated_at_utc=row["updatedatutc"],
    )


def _normalized_column_name(name: str) -> str:
    normalized = normalize_status_name(name)
    if not normalized:
        raise _coded(422, "STATUS_RATE_COLUMN_NAME_REQUIRED", "column_name must not be blank.")
    if len(normalized) > STATUS_NAME_MAX_LENGTH:
        raise _coded(422, "STATUS_RATE_COLUMN_NAME_TOO_LONG", "column_name is too long.")
    return normalized


async def lock_status_catalog(company_id: int, db: AsyncConnection) -> None:
    """Serialize every Status catalog and rate column writer of one Company."""
    await db.execute(
        text("SELECT payroll.fn_LockStatusCatalog(:cid)"), {"cid": company_id},
    )


async def list_status_rate_columns(
    company_id: int,
    user_id: int,
    db: AsyncConnection,
    *,
    active_only: bool = True,
) -> list[StatusRateColumn]:
    """Every Status rate column of the Company (any user with access to the Company)."""
    await check_branch_access(company_id, user_id, db)
    # A Company always has its default column: one inserted without it heals here.
    await ensure_default_status_rate_column(company_id, db)
    where = "WHERE src.companyid = :cid" + (" AND src.isactive" if active_only else "")
    result = await db.execute(
        text(f"{_COLUMN_SELECT} {where} ORDER BY src.isdefault DESC, src.statusratecolumnid"),
        {"cid": company_id},
    )
    return [_row_to_status_rate_column(r) for r in result.mappings().all()]


async def _load_column(
    company_id: int, column_id: int, db: AsyncConnection, *, for_update: bool = False,
) -> dict:
    row = (await db.execute(
        text(f"{_COLUMN_SELECT} WHERE src.statusratecolumnid = :sid AND src.companyid = :cid"
             + (" FOR UPDATE OF src" if for_update else "")),
        {"sid": column_id, "cid": company_id},
    )).mappings().first()
    if row is None:
        raise _coded(404, "STATUS_RATE_COLUMN_NOT_FOUND", f"Status rate column {column_id} not found.")
    return dict(row)


async def _insert_column(
    company_id: int, user_id: int | None, name: str, normalized: str, is_default: bool,
    db: AsyncConnection,
) -> int:
    """The column, its scalar RateDefinition and its one scalar component, atomically (one
    database function creates all three and locks the fixed structure)."""
    return int((await db.execute(
        text("SELECT payroll.fn_CreateStatusRateColumn(:cid, :name, :norm, :def, :uid)"),
        {"cid": company_id, "name": name, "norm": normalized, "def": is_default, "uid": user_id},
    )).scalar_one())


async def _name_in_use(
    company_id: int, normalized: str, db: AsyncConnection, *, exclude_id: int | None = None,
) -> bool:
    return (await db.execute(
        text("""
            SELECT 1 FROM payroll.statusratecolumns
            WHERE  companyid = :cid AND isactive AND normalizedcolumnname = :norm
              AND  statusratecolumnid IS DISTINCT FROM CAST(:excl AS integer)
        """),
        {"cid": company_id, "norm": normalized, "excl": exclude_id},
    )).first() is not None


async def create_status_rate_column(
    company_id: int,
    user_id: int,
    data: StatusRateColumnCreate,
    db: AsyncConnection,
) -> StatusRateColumn:
    """Create a Company Status rate column with its RateDefinition. Requires
    AllCompanyBranches scope. ``is_default`` makes it the Company default (the previous
    default is demoted atomically)."""
    await security.ensure_company_admin(company_id, user_id, db)
    await ensure_default_status_rate_column(company_id, db)

    normalized = _normalized_column_name(data.column_name)
    if await _name_in_use(company_id, normalized, db):
        raise _coded(
            422, "STATUS_RATE_COLUMN_NAME_NOT_UNIQUE",
            f"An active status rate column named {data.column_name!r} already exists.")
    if data.is_default:
        await db.execute(
            text("UPDATE payroll.statusratecolumns SET isdefault = FALSE, "
                 "updatedbyuserid = :uid, updatedatutc = NOW() "
                 "WHERE companyid = :cid AND isdefault"),
            {"cid": company_id, "uid": user_id},
        )
    try:
        column_id = await _insert_column(
            company_id, user_id, data.column_name, normalized, data.is_default, db)
    except SAIntegrityError as exc:
        raise _coded(422, "STATUS_RATE_COLUMN_NOT_SAVED",
                     "The status rate column could not be created.") from exc

    await audit.write_settings_audit(
        db, company_id=company_id, branch_id=None, user_id=user_id,
        action_code="STATUS_RATE_COLUMN_CREATED", entity_name="StatusRateColumns",
        entity_id=str(column_id),
        new_value={"column_name": data.column_name, "is_default": data.is_default},
    )
    return _row_to_status_rate_column(await _load_column(company_id, column_id, db))


async def update_status_rate_column(
    company_id: int,
    user_id: int,
    column_id: int,
    data: StatusRateColumnUpdate,
    db: AsyncConnection,
) -> StatusRateColumn:
    """Rename, activate/deactivate or make a column the Company default. Requires
    AllCompanyBranches scope.

    The default column is always active, and a Company always has exactly one: a default is
    replaced by making another column the default, never by clearing or deactivating it. A
    column that active Statuses are bound to cannot be deactivated.
    """
    await security.ensure_company_admin(company_id, user_id, db)
    await lock_status_catalog(company_id, db)
    row = await _load_column(company_id, column_id, db, for_update=True)

    name = data.column_name if data.column_name is not None else row["columnname"]
    is_active = data.is_active if data.is_active is not None else bool(row["isactive"])
    is_default = data.is_default if data.is_default is not None else bool(row["isdefault"])
    normalized = _normalized_column_name(name)

    if row["isdefault"] and (not is_default or not is_active):
        raise _coded(
            409, "STATUS_RATE_COLUMN_IS_DEFAULT",
            "The default status rate column cannot be cleared or deactivated. "
            "Make another column the default first.")
    if is_default and not is_active:
        raise _coded(422, "STATUS_RATE_COLUMN_DEFAULT_INACTIVE",
                     "A default status rate column must be active.")
    if bool(row["isactive"]) and not is_active:
        bound = (await db.execute(
            text("SELECT 1 FROM payroll.payrollstatuskeys "
                 "WHERE statusratecolumnid = :sid AND companyid = :cid AND isactive LIMIT 1"),
            {"sid": column_id, "cid": company_id},
        )).first()
        if bound is not None:
            raise _coded(
                409, "STATUS_RATE_COLUMN_IN_USE",
                "Active Statuses are bound to this status rate column. "
                "Deactivate them or bind them to another column first.")
    if is_active and await _name_in_use(company_id, normalized, db, exclude_id=column_id):
        raise _coded(
            422, "STATUS_RATE_COLUMN_NAME_NOT_UNIQUE",
            f"An active status rate column named {name!r} already exists.")

    if is_default and not row["isdefault"]:
        await db.execute(
            text("UPDATE payroll.statusratecolumns SET isdefault = FALSE, "
                 "updatedbyuserid = :uid, updatedatutc = NOW() "
                 "WHERE companyid = :cid AND isdefault"),
            {"cid": company_id, "uid": user_id},
        )
    try:
        await db.execute(
            text("""
                UPDATE payroll.statusratecolumns
                SET    columnname = :name, normalizedcolumnname = :norm,
                       isactive = :active, isdefault = :def,
                       updatedbyuserid = :uid, updatedatutc = NOW()
                WHERE  statusratecolumnid = :sid AND companyid = :cid
            """),
            {"name": name, "norm": normalized, "active": is_active, "def": is_default,
             "uid": user_id, "sid": column_id, "cid": company_id},
        )
    except SAIntegrityError as exc:
        raise _coded(422, "STATUS_RATE_COLUMN_NOT_SAVED",
                     "The status rate column could not be saved.") from exc

    await audit.write_settings_audit(
        db, company_id=company_id, branch_id=None, user_id=user_id,
        action_code="STATUS_RATE_COLUMN_UPDATED", entity_name="StatusRateColumns",
        entity_id=str(column_id),
        old_value={"column_name": row["columnname"], "is_active": bool(row["isactive"]),
                   "is_default": bool(row["isdefault"])},
        new_value={"column_name": name, "is_active": is_active, "is_default": is_default},
    )
    return _row_to_status_rate_column(await _load_column(company_id, column_id, db))


async def deactivate_status_rate_column(
    company_id: int,
    user_id: int,
    column_id: int,
    db: AsyncConnection,
) -> StatusRateColumn:
    """Deactivate a column (idempotent). The column and its RateDefinition stay for the
    history of the Driver rate schedules and frozen evidence that name them."""
    await security.ensure_company_admin(company_id, user_id, db)
    row = await _load_column(company_id, column_id, db)
    if not row["isactive"]:
        return _row_to_status_rate_column(row)
    return await update_status_rate_column(
        company_id, user_id, column_id, StatusRateColumnUpdate(is_active=False), db)


async def validate_status_rate_column(
    status_rate_column_id: int, company_id: int, db: AsyncConnection,
) -> None:
    """Raise 422 unless the column is an active column of the Company."""
    found = (await db.execute(
        text("""
            SELECT 1 FROM payroll.statusratecolumns
            WHERE  statusratecolumnid = :sid AND companyid = :cid AND isactive
        """),
        {"sid": status_rate_column_id, "cid": company_id},
    )).first()
    if found is None:
        raise _coded(
            422, "STATUS_RATE_COLUMN_NOT_FOUND",
            f"Status rate column {status_rate_column_id} was not found or is not active.")


async def ensure_default_status_rate_column(company_id: int, db: AsyncConnection) -> int:
    """Idempotently ensure the Company has its one default active Status rate column, with
    its RateDefinition and scalar component. Returns the column id.

    Called when a Company is created, so every Company starts with the default. Takes the
    Status catalog lock, so two concurrent callers create one column.
    """
    return int((await db.execute(
        text("SELECT payroll.fn_EnsureDefaultStatusRateColumn(:cid)"), {"cid": company_id},
    )).scalar_one())
