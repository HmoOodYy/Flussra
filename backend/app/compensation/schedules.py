"""Target DriverRateSchedule authoring.

One DriverRateSchedule is one complete effective-dated schedule owned by the permanent
Driver. Its values never carry dates or lifecycle of their own. A schedule has no Branch
and no DriverAssignment: who may author it follows the Driver's home tenure on the change's
start date (``app.compensation.driver_context``), and a permanent transfer carries it over
without any step.

Lock order for every writer: authorize from an unlocked read; Company currency FOR SHARE
(monetary writers); the Driver's DriverAssignments FOR SHARE; Branch workflow advisory
locks for every Branch whose tenure overlaps the change (writers with payroll effect:
approve and void); the RateDefinition structure lock; the schedule rows FOR UPDATE; one
decision instant; then the finalized-payroll guard, the context check at that instant and
the DML. The order and its deadlock analysis live in ``driver_context``.

Start date of a change = the first date whose resolved compensation changes: approve and
void use the schedule's ``effective_from`` (a supersession tail starts there too), and a
Pending create/update uses the Pending ``effective_from`` (validated early; approval is
authoritative). Discarding a Pending schedule has no payroll effect and needs only
authority over the Driver now.

The database enforces ownership, non-overlap, complete-set approval, the structural lock
and, at the DML statement's own instant, the context check; this layer adds permissions,
the currency gate, successor supersession, the finalized guard and stable errors.
"""

from datetime import date, timedelta

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection

from app.business_clock import branch_today
from app.compensation.driver_context import (
    ReadScope,
    driver_compensation_read_scope,
    lock_driver_compensation_write,
    require_compensation_authority,
    resolve_compensation_context,
)
from app.compensation.errors import compensation_error, translate_database_error
from app.compensation.schemas import (
    DriverPayRateRow,
    DriverRateSummaryItem,
    ScheduleBrief,
    ScheduleCreate,
    ScheduleSummary,
    ScheduleUpdate,
    ScheduleValue,
    ScheduleValuesReplace,
    ScheduleVoid,
)
from app.payroll.guards import require_no_finalized_payroll
from app.workforce.ops import decision_instant

_SCHEDULE_COLUMNS = """
    s.driverratescheduleid   AS driver_rate_schedule_id,
    s.companyid              AS company_id,
    s.driverid               AS driver_id,
    s.paydefinitionid        AS pay_definition_id,
    s.statusratecolumnid     AS status_rate_column_id,
    s.ratedefinitionid       AS rate_definition_id,
    s.effectivefrom          AS effective_from,
    s.effectiveto            AS effective_to,
    s.status                 AS status,
    s.createdbyuserid        AS created_by_user_id,
    s.createdatutc           AS created_at_utc,
    s.updatedbyuserid        AS updated_by_user_id,
    s.updatedatutc           AS updated_at_utc,
    s.approvedbyuserid       AS approved_by_user_id,
    s.approvedatutc          AS approved_at_utc,
    s.voidedbyuserid         AS voided_by_user_id,
    s.voidedatutc            AS voided_at_utc,
    s.voidreason             AS void_reason,
    s.notes                  AS notes
"""

_VOID_STATUSES = ("Approved", "Superseded")


def _schedule_not_found() -> HTTPException:
    return compensation_error("SCHEDULE_NOT_FOUND", "Rate schedule not found.", 404)


def _database_error(exc: DBAPIError) -> Exception:
    translated = translate_database_error(exc)
    return translated if translated is not None else exc


async def _load_schedule_row(
    company_id: int, schedule_id: int, db: AsyncConnection, *, for_update: bool = False,
) -> dict:
    row = (await db.execute(
        text(f"""
            SELECT {_SCHEDULE_COLUMNS}
            FROM   payroll.driverrateschedules s
            WHERE  s.driverratescheduleid = :sid AND s.companyid = :cid
            {"FOR UPDATE OF s" if for_update else ""}
        """),
        {"sid": schedule_id, "cid": company_id},
    )).mappings().first()
    if row is None:
        raise _schedule_not_found()
    return dict(row)


def _require_same_schedule(locked: dict, pre: dict) -> None:
    """The row read under its lock is the one the request was authorized and locked for."""
    if any(locked[key] != pre[key] for key in ("driver_id", "rate_definition_id", "effective_from")):
        raise compensation_error(
            "SCHEDULE_CHANGED", "The schedule changed during the request.", 409)


def _require_pending(row: dict, action: str) -> None:
    if row["status"] != "Pending":
        raise compensation_error(
            "SCHEDULE_NOT_PENDING",
            f"Only Pending schedules can be {action}. Current status: {row['status']}.", 409)


def _require_voidable(row: dict) -> None:
    if row["status"] == "Pending":
        raise compensation_error(
            "PENDING_CANNOT_BE_VOIDED",
            "A Pending schedule is discarded, not voided.", 409)
    if row["status"] not in _VOID_STATUSES:
        raise compensation_error(
            "SCHEDULE_INVALID_TRANSITION",
            f"A {row['status']} schedule cannot be voided.", 409)


async def _load_values(
    schedule_id: int | None, rate_definition_id: int, db: AsyncConnection,
) -> list[ScheduleValue]:
    rows = (await db.execute(
        text("""
            SELECT c.ratecomponentdefinitionid AS rate_component_definition_id,
                   c.sequenceno AS sequence_no, c.ordinalfrom AS ordinal_from,
                   c.ordinalto AS ordinal_to, v.amount AS amount
            FROM   payroll.ratecomponentdefinitions c
            LEFT JOIN payroll.driverrateschedulevalues v
                   ON v.ratecomponentdefinitionid = c.ratecomponentdefinitionid
                  AND v.driverratescheduleid = :sid
            WHERE  c.ratedefinitionid = :rid
            ORDER  BY c.sequenceno
        """),
        {"sid": schedule_id, "rid": rate_definition_id},
    )).mappings().all()
    return [ScheduleValue.model_validate(dict(row)) for row in rows]


async def _summary(company_id: int, schedule_id: int, db: AsyncConnection) -> ScheduleSummary:
    row = await _load_schedule_row(company_id, schedule_id, db)
    row["values"] = await _load_values(schedule_id, row["rate_definition_id"], db)
    return ScheduleSummary.model_validate(row)


async def _authorable_rate_definition(
    company_id: int, rate_definition_id: int, db: AsyncConnection,
) -> dict:
    """Any scalar RateDefinition of the Company whose owner is Active: a PayDefinition that is
    Active, or a Status rate column that is active. Both are authored by the same lifecycle.

    Branch applicability is deliberately not checked: it is decided at calculation, where
    a missing value still blocks. PayDefinition retirement takes the RateDefinition
    structural lock before touching the PayDefinition, so for a caller that holds that lock
    the status read here cannot change underneath it.
    """
    row = (await db.execute(
        text("""
            SELECT rd.ratedefinitionid, rd.shape, rd.paydefinitionid, rd.statusratecolumnid,
                   pd.status AS definition_status, src.isactive AS column_active
            FROM   payroll.ratedefinitions rd
            LEFT JOIN payroll.paydefinitions pd ON pd.paydefinitionid = rd.paydefinitionid
            LEFT JOIN payroll.statusratecolumns src ON src.statusratecolumnid = rd.statusratecolumnid
            WHERE  rd.ratedefinitionid = :rid AND rd.companyid = :cid
        """),
        {"rid": rate_definition_id, "cid": company_id},
    )).mappings().first()
    if row is None:
        raise compensation_error("RATE_DEFINITION_NOT_FOUND", "Rate definition not found.", 404)
    if row["shape"] != "Scalar":
        raise compensation_error(
            "RATE_DEFINITION_NOT_AUTHORABLE",
            "Only scalar (PerUnit) rate definitions can be authored at this time.", 422)
    if row["statusratecolumnid"] is not None:
        if not row["column_active"]:
            raise compensation_error(
                "STATUS_RATE_COLUMN_NOT_ACTIVE", "The Status rate column is not active.", 422)
    elif row["definition_status"] != "Active":
        raise compensation_error(
            "PAY_DEFINITION_NOT_ACTIVE", "The PayDefinition is not Active.", 422)
    return dict(row)


def _validate_window(effective_from: date, effective_to: date | None) -> None:
    if effective_to is not None and effective_to < effective_from:
        raise compensation_error(
            "INVALID_EFFECTIVE_WINDOW", "effective_to must not precede effective_from.", 422)


async def _recheck_authority(
    company_id: int, user_id: int, driver_id: int, start_date: date | None, db: AsyncConnection,
    *, mode: str = "write",
) -> None:
    """Decide authority again at one wall-clock instant read after every lock is held."""
    instant = await decision_instant(db)
    await require_compensation_authority(
        company_id, user_id, driver_id, start_date, db, mode=mode, at=instant)


async def create_pending(
    company_id: int, user_id: int, data: ScheduleCreate, db: AsyncConnection,
) -> ScheduleSummary:
    await require_compensation_authority(
        company_id, user_id, data.driver_id, data.effective_from, db)
    await _authorable_rate_definition(company_id, data.rate_definition_id, db)
    _validate_window(data.effective_from, data.effective_to)

    await lock_driver_compensation_write(
        company_id, data.driver_id, data.rate_definition_id, db, monetary=False, lock_from=None)
    definition = await _authorable_rate_definition(company_id, data.rate_definition_id, db)
    await _recheck_authority(company_id, user_id, data.driver_id, data.effective_from, db)
    try:
        schedule_id = (await db.execute(
            text("""
                INSERT INTO payroll.driverrateschedules
                    (companyid, driverid, paydefinitionid, statusratecolumnid, ratedefinitionid,
                     effectivefrom, effectiveto, notes, createdbyuserid)
                VALUES (:cid, :did, :pdid, :srcid, :rid, :efrom, :eto, :notes, :uid)
                RETURNING driverratescheduleid
            """),
            {"cid": company_id, "did": data.driver_id, "pdid": definition["paydefinitionid"],
             "srcid": definition["statusratecolumnid"], "rid": data.rate_definition_id, "efrom": data.effective_from,
             "eto": data.effective_to, "notes": data.notes, "uid": user_id},
        )).scalar_one()
    except DBAPIError as exc:
        raise _database_error(exc) from exc
    return await _summary(company_id, int(schedule_id), db)


async def get_schedule(
    company_id: int, user_id: int, schedule_id: int, db: AsyncConnection,
) -> ScheduleSummary:
    row = await _load_schedule_row(company_id, schedule_id, db)
    scope = await driver_compensation_read_scope(company_id, user_id, row["driver_id"], db)
    if not scope.sees(row):
        raise _schedule_not_found()
    return await _summary(company_id, schedule_id, db)


async def update_pending(
    company_id: int, user_id: int, schedule_id: int, data: ScheduleUpdate,
    db: AsyncConnection,
) -> ScheduleSummary:
    pre = await _load_schedule_row(company_id, schedule_id, db)
    new_from = data.effective_from if "effective_from" in data.model_fields_set \
        and data.effective_from is not None else pre["effective_from"]
    await require_compensation_authority(company_id, user_id, pre["driver_id"], new_from, db)

    await lock_driver_compensation_write(
        company_id, pre["driver_id"], pre["rate_definition_id"], db,
        monetary=False, lock_from=None)
    row = await _load_schedule_row(company_id, schedule_id, db, for_update=True)
    _require_pending(row, "edited")

    effective_from = data.effective_from if "effective_from" in data.model_fields_set \
        and data.effective_from is not None else row["effective_from"]
    effective_to = data.effective_to if "effective_to" in data.model_fields_set \
        else row["effective_to"]
    notes = data.notes if "notes" in data.model_fields_set else row["notes"]
    _validate_window(effective_from, effective_to)
    await _authorable_rate_definition(company_id, row["rate_definition_id"], db)
    await _recheck_authority(company_id, user_id, row["driver_id"], effective_from, db)
    try:
        await db.execute(
            text("""
                UPDATE payroll.driverrateschedules
                SET    effectivefrom = :efrom, effectiveto = :eto, notes = :notes,
                       updatedbyuserid = :uid, updatedatutc = now()
                WHERE  driverratescheduleid = :sid AND status = 'Pending'
            """),
            {"efrom": effective_from, "eto": effective_to, "notes": notes,
             "uid": user_id, "sid": schedule_id},
        )
    except DBAPIError as exc:
        raise _database_error(exc) from exc
    return await _summary(company_id, schedule_id, db)


async def replace_values(
    company_id: int, user_id: int, schedule_id: int, data: ScheduleValuesReplace,
    db: AsyncConnection,
) -> ScheduleSummary:
    pre = await _load_schedule_row(company_id, schedule_id, db)
    await require_compensation_authority(
        company_id, user_id, pre["driver_id"], pre["effective_from"], db)
    _require_pending(pre, "edited")

    await lock_driver_compensation_write(
        company_id, pre["driver_id"], pre["rate_definition_id"], db,
        monetary=True, lock_from=None)
    row = await _load_schedule_row(company_id, schedule_id, db, for_update=True)
    _require_same_schedule(row, pre)
    _require_pending(row, "edited")
    await _recheck_authority(company_id, user_id, row["driver_id"], row["effective_from"], db)

    components = {
        value.rate_component_definition_id
        for value in await _load_values(schedule_id, row["rate_definition_id"], db)
    }
    submitted_ids = [item.rate_component_definition_id for item in data.values]
    if len(set(submitted_ids)) != len(submitted_ids):
        raise compensation_error(
            "DUPLICATE_COMPONENT_VALUE", "Each component may appear only once.", 422)
    unknown = set(submitted_ids) - components
    if unknown:
        raise compensation_error(
            "UNKNOWN_COMPONENT",
            "A value references a component outside this rate definition.", 422)

    try:
        await db.execute(
            text("""
                DELETE FROM payroll.driverrateschedulevalues
                WHERE  driverratescheduleid = :sid
                  AND  NOT (ratecomponentdefinitionid = ANY(:keep))
            """),
            {"sid": schedule_id, "keep": submitted_ids},
        )
        for item in data.values:
            await db.execute(
                text("""
                    INSERT INTO payroll.driverrateschedulevalues
                        (driverratescheduleid, ratedefinitionid,
                         ratecomponentdefinitionid, amount)
                    VALUES (:sid, :rid, :cid, :amount)
                    ON CONFLICT (driverratescheduleid, ratecomponentdefinitionid)
                    DO UPDATE SET amount = EXCLUDED.amount
                """),
                {"sid": schedule_id, "rid": row["rate_definition_id"],
                 "cid": item.rate_component_definition_id, "amount": item.amount},
            )
        await db.execute(
            text("""
                UPDATE payroll.driverrateschedules
                SET    updatedbyuserid = :uid, updatedatutc = now()
                WHERE  driverratescheduleid = :sid
            """),
            {"uid": user_id, "sid": schedule_id},
        )
    except DBAPIError as exc:
        raise _database_error(exc) from exc
    return await _summary(company_id, schedule_id, db)


def _later_end(first: date | None, second: date | None) -> date | None:
    """The later of two inclusive ends, where None is unbounded."""
    if first is None or second is None:
        return None
    return max(first, second)


async def approve(
    company_id: int, user_id: int, schedule_id: int, db: AsyncConnection,
) -> ScheduleSummary:
    pre = await _load_schedule_row(company_id, schedule_id, db)
    await require_compensation_authority(
        company_id, user_id, pre["driver_id"], pre["effective_from"], db)
    _require_pending(pre, "approved")

    await lock_driver_compensation_write(
        company_id, pre["driver_id"], pre["rate_definition_id"], db,
        monetary=True, lock_from=pre["effective_from"])
    row = await _load_schedule_row(company_id, schedule_id, db, for_update=True)
    _require_same_schedule(row, pre)
    _require_pending(row, "approved")
    await _authorable_rate_definition(company_id, row["rate_definition_id"], db)

    values = await _load_values(schedule_id, row["rate_definition_id"], db)
    if not values or any(value.amount is None for value in values):
        raise compensation_error(
            "SCHEDULE_INCOMPLETE",
            "Every required component needs a value before approval.", 422)

    prior = (await db.execute(
        text("""
            SELECT driverratescheduleid, effectivefrom, effectiveto
            FROM   payroll.driverrateschedules
            WHERE  driverid = :did AND ratedefinitionid = :rid AND status = 'Approved'
            FOR UPDATE
        """),
        {"did": row["driver_id"], "rid": row["rate_definition_id"]},
    )).mappings().first()
    if prior is not None and row["effective_from"] <= prior["effectivefrom"]:
        raise compensation_error(
            "SUCCESSOR_NOT_AFTER_CURRENT",
            "A successor must take effect after the current Approved schedule.", 422)

    # The footprint is every date whose resolved compensation changes: the new window, and
    # the superseded tail of the prior schedule when it runs past the new window's end.
    footprint_to = (_later_end(row["effective_to"], prior["effectiveto"])
                    if prior is not None else row["effective_to"])
    await _recheck_authority(company_id, user_id, row["driver_id"], row["effective_from"], db)
    await require_no_finalized_payroll(
        company_id, row["driver_id"], row["effective_from"], footprint_to, "WorkDate", db)

    try:
        if prior is not None:
            close_on = row["effective_from"] - timedelta(days=1)
            if prior["effectiveto"] is not None and prior["effectiveto"] < close_on:
                close_on = prior["effectiveto"]
            await db.execute(
                text("""
                    UPDATE payroll.driverrateschedules
                    SET    status = 'Superseded', effectiveto = :close_on,
                           updatedbyuserid = :uid, updatedatutc = now()
                    WHERE  driverratescheduleid = :pid
                """),
                {"close_on": close_on, "uid": user_id, "pid": prior["driverratescheduleid"]},
            )
        await db.execute(
            text("""
                UPDATE payroll.driverrateschedules
                SET    status = 'Approved', approvedbyuserid = :uid, approvedatutc = now(),
                       updatedbyuserid = :uid, updatedatutc = now()
                WHERE  driverratescheduleid = :sid AND status = 'Pending'
            """),
            {"uid": user_id, "sid": schedule_id},
        )
    except DBAPIError as exc:
        raise _database_error(exc) from exc
    return await _summary(company_id, schedule_id, db)


async def discard(
    company_id: int, user_id: int, schedule_id: int, db: AsyncConnection,
) -> None:
    pre = await _load_schedule_row(company_id, schedule_id, db)
    await require_compensation_authority(
        company_id, user_id, pre["driver_id"], None, db, mode="discard")

    await lock_driver_compensation_write(
        company_id, pre["driver_id"], pre["rate_definition_id"], db,
        monetary=False, lock_from=None)
    row = await _load_schedule_row(company_id, schedule_id, db, for_update=True)
    _require_pending(row, "discarded")
    await _recheck_authority(company_id, user_id, row["driver_id"], None, db, mode="discard")
    await db.execute(
        text("SELECT set_config('flussra.actor_user_id', :uid, true)"),
        {"uid": str(user_id)},
    )
    try:
        await db.execute(
            text("DELETE FROM payroll.driverrateschedules "
                 "WHERE driverratescheduleid = :sid AND status = 'Pending'"),
            {"sid": schedule_id},
        )
    except DBAPIError as exc:
        raise _database_error(exc) from exc


async def void(
    company_id: int, user_id: int, schedule_id: int, data: ScheduleVoid,
    db: AsyncConnection,
) -> ScheduleSummary:
    pre = await _load_schedule_row(company_id, schedule_id, db)
    await require_compensation_authority(
        company_id, user_id, pre["driver_id"], pre["effective_from"], db)
    _require_voidable(pre)

    await lock_driver_compensation_write(
        company_id, pre["driver_id"], pre["rate_definition_id"], db,
        monetary=True, lock_from=pre["effective_from"])
    row = await _load_schedule_row(company_id, schedule_id, db, for_update=True)
    _require_same_schedule(row, pre)
    _require_voidable(row)
    await _recheck_authority(company_id, user_id, row["driver_id"], row["effective_from"], db)
    await require_no_finalized_payroll(
        company_id, row["driver_id"], row["effective_from"], row["effective_to"], "WorkDate", db)
    try:
        await db.execute(
            text("""
                UPDATE payroll.driverrateschedules
                SET    status = 'Voided', voidedbyuserid = :uid, voidedatutc = now(),
                       voidreason = :reason, updatedbyuserid = :uid, updatedatutc = now()
                WHERE  driverratescheduleid = :sid AND status IN ('Approved', 'Superseded')
            """),
            {"uid": user_id, "reason": data.reason, "sid": schedule_id},
        )
    except DBAPIError as exc:
        raise _database_error(exc) from exc
    return await _summary(company_id, schedule_id, db)


async def history(
    company_id: int, user_id: int, driver_id: int, rate_definition_id: int,
    db: AsyncConnection,
) -> list[ScheduleSummary]:
    scope = await driver_compensation_read_scope(company_id, user_id, driver_id, db)
    owned = (await db.execute(
        text("SELECT 1 FROM payroll.ratedefinitions WHERE ratedefinitionid = :rid AND companyid = :cid"),
        {"rid": rate_definition_id, "cid": company_id},
    )).scalar_one_or_none()
    if owned is None:
        raise compensation_error("RATE_DEFINITION_NOT_FOUND", "Rate definition not found.", 404)
    params: dict = {"cid": company_id, "did": driver_id, "rid": rate_definition_id}
    ids = (await db.execute(
        text(f"""
            SELECT s.driverratescheduleid FROM payroll.driverrateschedules s
            WHERE  s.companyid = :cid AND s.driverid = :did AND s.ratedefinitionid = :rid
              {scope.row_filter("s", params)}
            ORDER  BY s.effectivefrom DESC, s.driverratescheduleid DESC
        """),
        params,
    )).scalars().all()
    return [await _summary(company_id, int(i), db) for i in ids]


async def driver_summary(
    company_id: int, user_id: int, driver_id: int, db: AsyncConnection,
) -> list[DriverRateSummaryItem]:
    """Every rate definition the Driver has schedules for, grouped per definition regardless
    of its owner status (the history source), limited to what the caller may read:
    past-Branch readers see only overlapping rows and never a Pending one. A definition is
    owned by a PayDefinition or by a Company Status rate column (``kind``)."""
    scope = await driver_compensation_read_scope(company_id, user_id, driver_id, db)
    params: dict = {"cid": company_id, "did": driver_id}
    rows = (await db.execute(
        text(f"""
            SELECT s.ratedefinitionid AS rate_definition_id,
                   CASE WHEN rd.statusratecolumnid IS NULL THEN 'PayDefinition'
                        ELSE 'StatusRateColumn' END AS kind,
                   rd.paydefinitionid AS pay_definition_id,
                   rd.statusratecolumnid AS status_rate_column_id,
                   src.columnname AS status_rate_column_name,
                   pd.definitioncode AS definition_code,
                   COALESCE(pd.definitionname, src.columnname) AS definition_name,
                   COALESCE(pd.unit, CASE WHEN src.statusratecolumnid IS NOT NULL THEN 'hour' END)
                       AS unit,
                   pd.status AS pay_definition_status,
                   max(s.driverratescheduleid) FILTER (WHERE s.status = 'Approved')
                       AS approved_schedule_id,
                   max(s.effectivefrom) FILTER (WHERE s.status = 'Approved')
                       AS approved_effective_from,
                   max(s.driverratescheduleid) FILTER (WHERE s.status = 'Pending')
                       AS pending_schedule_id
            FROM   payroll.driverrateschedules s
            JOIN   payroll.ratedefinitions rd ON rd.ratedefinitionid = s.ratedefinitionid
            LEFT JOIN payroll.paydefinitions pd ON pd.paydefinitionid = rd.paydefinitionid
            LEFT JOIN payroll.statusratecolumns src
                   ON src.statusratecolumnid = rd.statusratecolumnid
            WHERE  s.companyid = :cid AND s.driverid = :did
              {scope.row_filter("s", params)}
            GROUP  BY s.ratedefinitionid, rd.paydefinitionid, rd.statusratecolumnid,
                      src.statusratecolumnid, src.columnname, pd.definitioncode,
                      pd.definitionname, pd.unit, pd.status
            ORDER  BY lower(COALESCE(pd.definitionname, src.columnname)), s.ratedefinitionid
        """),
        params,
    )).mappings().all()
    return [DriverRateSummaryItem.model_validate(dict(row)) for row in rows]


async def _reference_branch_id(company_id: int, driver_id: int, db: AsyncConnection) -> int | None:
    """The Branch whose business date is "today" for the Driver: the current tenure, else
    the next pending one, else the latest historical one."""
    return (await db.execute(
        text("""
            SELECT a.branchid FROM core.driverassignments a
            WHERE  a.companyid = :cid AND a.driverid = :did
            ORDER  BY (a.driverassignmentid
                       = core.fn_EffectiveDriverAssignment(a.companyid, a.driverid)) DESC,
                      (a.effectivefromat > NOW()
                       AND (a.effectivetoat IS NULL OR a.effectivetoat > a.effectivefromat)) DESC,
                      a.effectivefromat DESC, a.driverassignmentid DESC
            LIMIT  1
        """),
        {"cid": company_id, "did": driver_id},
    )).scalar_one_or_none()


async def driver_pay_rates(
    company_id: int, user_id: int, driver_id: int, as_of: date | None, db: AsyncConnection,
) -> list[DriverPayRateRow]:
    """Every authorable PayDefinition (Company-Active, scalar) and Status rate column (active)
    with the Driver's rate state.

    The reference date defaults to today in the Branch of the Driver's current (else next,
    else latest) tenure. ``applicable_in_context_branch`` says whether the definition has an
    active Branch configuration, on the reference date, in the Branch of the tenure that is
    the authority context for that date; authoring is allowed either way. Rates are
    configuration only; nothing is calculated here. Rows are limited to what the caller may
    read (see ``ReadScope``).
    """
    scope: ReadScope = await driver_compensation_read_scope(company_id, user_id, driver_id, db)
    if as_of is not None:
        reference = as_of
    else:
        reference_branch = await _reference_branch_id(company_id, driver_id, db)
        reference = (
            await branch_today(company_id, reference_branch, db) if reference_branch is not None
            else (await db.execute(text("SELECT core.fn_BusinessDate('UTC', now())"))).scalar_one()
        )
    context = await resolve_compensation_context(company_id, driver_id, reference, db)
    context_branch_id = context.branch_id if context is not None else None

    definitions = (await db.execute(
        text("""
            SELECT pd.paydefinitionid AS pay_definition_id, pd.definitioncode AS definition_code,
                   pd.definitionname AS definition_name, pd.inputtype AS input_type,
                   pd.unit AS unit, rd.ratedefinitionid AS rate_definition_id,
                   c.ratecomponentdefinitionid AS rate_component_definition_id,
                   CASE WHEN CAST(:bid AS integer) IS NULL THEN NULL ELSE EXISTS (
                       SELECT 1 FROM payroll.branchpayitemconfig bpic
                       WHERE  bpic.companyid = :cid AND bpic.branchid = CAST(:bid AS integer)
                         AND  bpic.paydefinitionid = pd.paydefinitionid
                         AND  bpic.isactive
                         AND  bpic.effectivefrom <= :asof
                         AND  (bpic.effectiveto IS NULL OR bpic.effectiveto >= :asof)
                   ) END AS applicable_in_context_branch
            FROM   payroll.paydefinitions pd
            JOIN   payroll.ratedefinitions rd
                   ON rd.paydefinitionid = pd.paydefinitionid AND rd.shape = 'Scalar'
            JOIN   payroll.ratecomponentdefinitions c
                   ON c.ratedefinitionid = rd.ratedefinitionid AND c.sequenceno = 1
            WHERE  pd.companyid = :cid AND pd.status = 'Active'
            ORDER  BY lower(pd.definitionname), pd.definitioncode, pd.paydefinitionid
        """),
        {"cid": company_id, "bid": context_branch_id, "asof": reference},
    )).mappings().all()
    definitions = [{**dict(d), "kind": "PayDefinition", "status_rate_column_id": None,
                    "status_rate_column_name": None} for d in definitions]
    # The Company active Status rate columns are authored beside the PayDefinitions: each is
    # labelled by its column name and has no Branch applicability.
    definitions += [
        {"kind": "StatusRateColumn", "pay_definition_id": None, "definition_code": None,
         "definition_name": row["columnname"], "input_type": "Decimal", "unit": "hour",
         "rate_definition_id": row["ratedefinitionid"],
         "rate_component_definition_id": row["ratecomponentdefinitionid"],
         "applicable_in_context_branch": None,
         "status_rate_column_id": row["statusratecolumnid"],
         "status_rate_column_name": row["columnname"]}
        for row in (await db.execute(
            text("""
                SELECT src.statusratecolumnid, src.columnname, rd.ratedefinitionid,
                       c.ratecomponentdefinitionid
                FROM   payroll.statusratecolumns src
                JOIN   payroll.ratedefinitions rd
                       ON rd.statusratecolumnid = src.statusratecolumnid AND rd.shape = 'Scalar'
                JOIN   payroll.ratecomponentdefinitions c
                       ON c.ratedefinitionid = rd.ratedefinitionid AND c.sequenceno = 1
                WHERE  src.companyid = :cid AND src.isactive
                ORDER  BY src.isdefault DESC, lower(src.columnname), src.statusratecolumnid
            """),
            {"cid": company_id},
        )).mappings().all()
    ]
    if not definitions:
        return []

    params: dict = {"cid": company_id, "did": driver_id,
                    "rids": [d["rate_definition_id"] for d in definitions]}
    schedule_rows = (await db.execute(
        text(f"""
            SELECT s.ratedefinitionid AS rate_definition_id,
                   s.driverratescheduleid AS driver_rate_schedule_id, s.status,
                   s.effectivefrom AS effective_from, s.effectiveto AS effective_to,
                   v.amount AS amount
            FROM   payroll.driverrateschedules s
            LEFT JOIN payroll.driverrateschedulevalues v
                   ON v.driverratescheduleid = s.driverratescheduleid
            WHERE  s.companyid = :cid AND s.driverid = :did
              AND  s.ratedefinitionid = ANY(:rids) AND s.status <> 'Voided'
              {scope.row_filter("s", params)}
            ORDER  BY s.effectivefrom, s.driverratescheduleid
        """),
        params,
    )).mappings().all()
    by_definition: dict[int, list[ScheduleBrief]] = {}
    for row in schedule_rows:
        by_definition.setdefault(row["rate_definition_id"], []).append(
            ScheduleBrief.model_validate(
                {key: row[key] for key in ScheduleBrief.model_fields}))

    result = []
    for definition in definitions:
        briefs = by_definition.get(definition["rate_definition_id"], [])
        current = next(
            (b for b in briefs
             if b.status in ("Approved", "Superseded") and b.effective_from <= reference
             and (b.effective_to is None or b.effective_to >= reference)), None)
        future = next(
            (b for b in briefs if b.status == "Approved" and b.effective_from > reference), None)
        pending = next((b for b in briefs if b.status == "Pending"), None)
        result.append(DriverPayRateRow(
            **definition, current=current, future=future, pending=pending))
    return result
