"""Target DriverRateAssignment authoring.

One DriverRateAssignment is one complete effective-dated schedule. Its values
never carry dates or lifecycle of their own.

Lock order for every writer (mirrors the database triggers):

1. RateDefinitions row, FOR NO KEY UPDATE (the single structural lock);
2. Company monetary guard, before any monetary value write or approval;
3. the assignment row and its dependent value rows.

Monetary mutations (value replacement and approval) authorize the caller from
an unlocked pre-read first, so an unauthorized caller never reaches the Company
currency state, and re-validate the assignment after taking its row lock.
Non-monetary mutations lock the RateDefinition and then the assignment row.

The database enforces ownership, non-overlap, complete-set approval and the
structural lock; this layer adds permissions, the currency gate, successor
supersession and stable errors without duplicating those invariants.
"""

from datetime import date, timedelta

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection

from app.company_currency import lock_and_get_company_currency_for_monetary_write
from app.compensation.branch_config import require_definition_applicable
from app.compensation.errors import compensation_error, translate_database_error
from app.compensation.guards import (
    load_company_driver_branch,
    require_rate_edit,
    require_rate_read,
)
from app.compensation.schemas import (
    AssignmentBrief,
    AssignmentCreate,
    AssignmentSummary,
    AssignmentUpdate,
    AssignmentValue,
    AssignmentValuesReplace,
    AssignmentVoid,
    DriverPayRateRow,
    DriverRateSummaryItem,
)
from app.payroll.guards import _check_not_in_finalized_period
from app.rate_definition_concurrency import lock_rate_definition_structure

_ASSIGNMENT_COLUMNS = """
    a.driverrateassignmentid AS driver_rate_assignment_id,
    a.companyid              AS company_id,
    a.branchid               AS branch_id,
    a.driverid               AS driver_id,
    a.ratedefinitionid       AS rate_definition_id,
    a.effectivefrom          AS effective_from,
    a.effectiveto            AS effective_to,
    a.status                 AS status,
    a.createdbyuserid        AS created_by_user_id,
    a.createdatutc           AS created_at_utc,
    a.updatedbyuserid        AS updated_by_user_id,
    a.updatedatutc           AS updated_at_utc,
    a.approvedbyuserid       AS approved_by_user_id,
    a.approvedatutc          AS approved_at_utc,
    a.voidedbyuserid         AS voided_by_user_id,
    a.voidedatutc            AS voided_at_utc,
    a.voidreason             AS void_reason,
    a.notes                  AS notes
"""


def _assignment_not_found() -> HTTPException:
    return compensation_error("ASSIGNMENT_NOT_FOUND", "Rate assignment not found.", 404)


def _database_error(exc: DBAPIError) -> Exception:
    translated = translate_database_error(exc)
    return translated if translated is not None else exc


async def _load_assignment_row(
    company_id: int, assignment_id: int, db: AsyncConnection, *, for_update: bool = False,
) -> dict:
    row = (await db.execute(
        text(f"""
            SELECT {_ASSIGNMENT_COLUMNS}
            FROM   payroll.driverrateassignments a
            WHERE  a.driverrateassignmentid = :aid AND a.companyid = :cid
            {"FOR UPDATE OF a" if for_update else ""}
        """),
        {"aid": assignment_id, "cid": company_id},
    )).mappings().first()
    if row is None:
        raise _assignment_not_found()
    return dict(row)


async def _lock_and_load(
    company_id: int, assignment_id: int, db: AsyncConnection,
) -> dict:
    """Lock the RateDefinition first, then the assignment row."""
    pre = await _load_assignment_row(company_id, assignment_id, db)
    await lock_rate_definition_structure(pre["rate_definition_id"], db)
    return await _load_assignment_row(company_id, assignment_id, db, for_update=True)


async def _lock_monetary_pending_assignment(
    company_id: int, user_id: int, assignment_id: int, action: str, db: AsyncConnection,
) -> dict:
    """Authorize, then lock RateDefinition -> Company monetary guard -> assignment row.

    Returns the locked Pending assignment row for a monetary mutation.
    """
    pre = await _load_assignment_row(company_id, assignment_id, db)
    await require_rate_edit(company_id, user_id, pre["branch_id"], db)
    _require_pending(pre, action)

    await lock_rate_definition_structure(pre["rate_definition_id"], db)
    await lock_and_get_company_currency_for_monetary_write(company_id, db)

    row = await _load_assignment_row(company_id, assignment_id, db, for_update=True)
    if row["rate_definition_id"] != pre["rate_definition_id"]:
        raise compensation_error(
            "ASSIGNMENT_CHANGED", "The assignment changed during the request.", 409)
    _require_pending(row, action)
    return row


async def _load_values(
    assignment_id: int | None, rate_definition_id: int, db: AsyncConnection,
) -> list[AssignmentValue]:
    rows = (await db.execute(
        text("""
            SELECT c.ratecomponentdefinitionid AS rate_component_definition_id,
                   c.sequenceno AS sequence_no, c.ordinalfrom AS ordinal_from,
                   c.ordinalto AS ordinal_to, v.amount AS amount
            FROM   payroll.ratecomponentdefinitions c
            LEFT JOIN payroll.driverratevalues v
                   ON v.ratecomponentdefinitionid = c.ratecomponentdefinitionid
                  AND v.driverrateassignmentid = :aid
            WHERE  c.ratedefinitionid = :rid
            ORDER  BY c.sequenceno
        """),
        {"aid": assignment_id, "rid": rate_definition_id},
    )).mappings().all()
    return [AssignmentValue.model_validate(dict(row)) for row in rows]


async def _summary(company_id: int, assignment_id: int, db: AsyncConnection) -> AssignmentSummary:
    row = await _load_assignment_row(company_id, assignment_id, db)
    row["values"] = await _load_values(assignment_id, row["rate_definition_id"], db)
    return AssignmentSummary.model_validate(row)


async def _authorable_rate_definition(
    company_id: int, rate_definition_id: int, db: AsyncConnection,
) -> dict:
    row = (await db.execute(
        text("""
            SELECT rd.ratedefinitionid, rd.shape, rd.paydefinitionid, pd.status AS definition_status
            FROM   payroll.ratedefinitions rd
            LEFT JOIN payroll.paydefinitions pd ON pd.paydefinitionid = rd.paydefinitionid
            WHERE  rd.ratedefinitionid = :rid AND rd.companyid = :cid
        """),
        {"rid": rate_definition_id, "cid": company_id},
    )).mappings().first()
    if row is None:
        raise compensation_error("RATE_DEFINITION_NOT_FOUND", "Rate definition not found.", 404)
    if row["paydefinitionid"] is None:
        raise compensation_error(
            "RATE_DEFINITION_NOT_AUTHORABLE",
            "Only PayDefinition-owned rate definitions can be authored here.", 422)
    if row["shape"] != "Scalar":
        raise compensation_error(
            "RATE_DEFINITION_NOT_AUTHORABLE",
            "Only scalar (PerUnit) rate definitions can be authored at this time.", 422)
    if row["definition_status"] != "Active":
        raise compensation_error(
            "PAY_DEFINITION_NOT_ACTIVE", "The PayDefinition is not Active.", 422)
    return dict(row)


async def _require_authoring_authority(
    company_id: int, rate_definition_id: int, branch_id: int, effective_from: date,
    db: AsyncConnection,
) -> None:
    """Revalidate PayDefinition status and Branch applicability for a target write.

    The caller holds the RateDefinition structural lock. PayDefinition retirement
    takes that same lock before touching the PayDefinition, so the status read here
    cannot change underneath the caller until it ends.
    """
    definition = await _authorable_rate_definition(company_id, rate_definition_id, db)
    await require_definition_applicable(
        company_id, branch_id, definition["paydefinitionid"], effective_from, db)


def _validate_window(effective_from: date, effective_to: date | None) -> None:
    if effective_to is not None and effective_to < effective_from:
        raise compensation_error(
            "INVALID_EFFECTIVE_WINDOW", "effective_to must not precede effective_from.", 422)


async def create_pending(
    company_id: int, user_id: int, data: AssignmentCreate, db: AsyncConnection,
) -> AssignmentSummary:
    branch_id = await load_company_driver_branch(company_id, user_id, data.driver_id, db)
    await require_rate_edit(company_id, user_id, branch_id, db)
    await _authorable_rate_definition(company_id, data.rate_definition_id, db)
    _validate_window(data.effective_from, data.effective_to)

    await lock_rate_definition_structure(data.rate_definition_id, db)
    await _require_authoring_authority(
        company_id, data.rate_definition_id, branch_id, data.effective_from, db)
    try:
        assignment_id = (await db.execute(
            text("""
                INSERT INTO payroll.driverrateassignments
                    (companyid, branchid, driverid, ratedefinitionid, effectivefrom,
                     effectiveto, notes, createdbyuserid)
                VALUES (:cid, :bid, :did, :rid, :efrom, :eto, :notes, :uid)
                RETURNING driverrateassignmentid
            """),
            {"cid": company_id, "bid": branch_id, "did": data.driver_id,
             "rid": data.rate_definition_id, "efrom": data.effective_from,
             "eto": data.effective_to, "notes": data.notes, "uid": user_id},
        )).scalar_one()
    except DBAPIError as exc:
        raise _database_error(exc) from exc
    return await _summary(company_id, int(assignment_id), db)


async def get_assignment(
    company_id: int, user_id: int, assignment_id: int, db: AsyncConnection,
) -> AssignmentSummary:
    row = await _load_assignment_row(company_id, assignment_id, db)
    await load_company_driver_branch(company_id, user_id, row["driver_id"], db)
    await require_rate_read(company_id, user_id, row["branch_id"], db)
    return await _summary(company_id, assignment_id, db)


def _require_pending(row: dict, action: str) -> None:
    if row["status"] != "Pending":
        raise compensation_error(
            "ASSIGNMENT_NOT_PENDING",
            f"Only Pending assignments can be {action}. Current status: {row['status']}.", 409)


async def update_pending(
    company_id: int, user_id: int, assignment_id: int, data: AssignmentUpdate,
    db: AsyncConnection,
) -> AssignmentSummary:
    row = await _lock_and_load(company_id, assignment_id, db)
    await require_rate_edit(company_id, user_id, row["branch_id"], db)
    _require_pending(row, "edited")

    effective_from = data.effective_from if "effective_from" in data.model_fields_set \
        and data.effective_from is not None else row["effective_from"]
    effective_to = data.effective_to if "effective_to" in data.model_fields_set \
        else row["effective_to"]
    notes = data.notes if "notes" in data.model_fields_set else row["notes"]
    _validate_window(effective_from, effective_to)
    if effective_from != row["effective_from"]:
        await _require_authoring_authority(
            company_id, row["rate_definition_id"], row["branch_id"], effective_from, db)
    try:
        await db.execute(
            text("""
                UPDATE payroll.driverrateassignments
                SET    effectivefrom = :efrom, effectiveto = :eto, notes = :notes,
                       updatedbyuserid = :uid, updatedatutc = now()
                WHERE  driverrateassignmentid = :aid AND status = 'Pending'
            """),
            {"efrom": effective_from, "eto": effective_to, "notes": notes,
             "uid": user_id, "aid": assignment_id},
        )
    except DBAPIError as exc:
        raise _database_error(exc) from exc
    return await _summary(company_id, assignment_id, db)


async def replace_values(
    company_id: int, user_id: int, assignment_id: int, data: AssignmentValuesReplace,
    db: AsyncConnection,
) -> AssignmentSummary:
    row = await _lock_monetary_pending_assignment(
        company_id, user_id, assignment_id, "edited", db)

    components = {
        value.rate_component_definition_id
        for value in await _load_values(assignment_id, row["rate_definition_id"], db)
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
                DELETE FROM payroll.driverratevalues
                WHERE  driverrateassignmentid = :aid
                  AND  NOT (ratecomponentdefinitionid = ANY(:keep))
            """),
            {"aid": assignment_id, "keep": submitted_ids},
        )
        for item in data.values:
            await db.execute(
                text("""
                    INSERT INTO payroll.driverratevalues
                        (driverrateassignmentid, ratedefinitionid,
                         ratecomponentdefinitionid, amount)
                    VALUES (:aid, :rid, :cid, :amount)
                    ON CONFLICT (driverrateassignmentid, ratecomponentdefinitionid)
                    DO UPDATE SET amount = EXCLUDED.amount
                """),
                {"aid": assignment_id, "rid": row["rate_definition_id"],
                 "cid": item.rate_component_definition_id, "amount": item.amount},
            )
        await db.execute(
            text("""
                UPDATE payroll.driverrateassignments
                SET    updatedbyuserid = :uid, updatedatutc = now()
                WHERE  driverrateassignmentid = :aid
            """),
            {"uid": user_id, "aid": assignment_id},
        )
    except DBAPIError as exc:
        raise _database_error(exc) from exc
    return await _summary(company_id, assignment_id, db)


async def approve(
    company_id: int, user_id: int, assignment_id: int, db: AsyncConnection,
) -> AssignmentSummary:
    row = await _lock_monetary_pending_assignment(
        company_id, user_id, assignment_id, "approved", db)
    await _require_authoring_authority(
        company_id, row["rate_definition_id"], row["branch_id"], row["effective_from"], db)
    await _check_not_in_finalized_period(
        company_id, row["branch_id"], row["effective_from"], db, label="rate")

    values = await _load_values(assignment_id, row["rate_definition_id"], db)
    if not values or any(value.amount is None for value in values):
        raise compensation_error(
            "ASSIGNMENT_INCOMPLETE",
            "Every required component needs a value before approval.", 422)

    prior = (await db.execute(
        text("""
            SELECT driverrateassignmentid, effectivefrom, effectiveto
            FROM   payroll.driverrateassignments
            WHERE  driverid = :did AND ratedefinitionid = :rid AND status = 'Approved'
            FOR UPDATE
        """),
        {"did": row["driver_id"], "rid": row["rate_definition_id"]},
    )).mappings().first()

    try:
        if prior is not None:
            if row["effective_from"] <= prior["effectivefrom"]:
                raise compensation_error(
                    "SUCCESSOR_NOT_AFTER_CURRENT",
                    "A successor must take effect after the current Approved assignment.", 422)
            close_on = row["effective_from"] - timedelta(days=1)
            if prior["effectiveto"] is not None and prior["effectiveto"] < close_on:
                close_on = prior["effectiveto"]
            await db.execute(
                text("""
                    UPDATE payroll.driverrateassignments
                    SET    status = 'Superseded', effectiveto = :close_on,
                           updatedbyuserid = :uid, updatedatutc = now()
                    WHERE  driverrateassignmentid = :pid
                """),
                {"close_on": close_on, "uid": user_id, "pid": prior["driverrateassignmentid"]},
            )
        await db.execute(
            text("""
                UPDATE payroll.driverrateassignments
                SET    status = 'Approved', approvedbyuserid = :uid, approvedatutc = now(),
                       updatedbyuserid = :uid, updatedatutc = now()
                WHERE  driverrateassignmentid = :aid AND status = 'Pending'
            """),
            {"uid": user_id, "aid": assignment_id},
        )
    except DBAPIError as exc:
        raise _database_error(exc) from exc
    return await _summary(company_id, assignment_id, db)


async def discard(
    company_id: int, user_id: int, assignment_id: int, db: AsyncConnection,
) -> None:
    row = await _lock_and_load(company_id, assignment_id, db)
    await require_rate_edit(company_id, user_id, row["branch_id"], db)
    _require_pending(row, "discarded")
    await db.execute(
        text("SELECT set_config('flussra.actor_user_id', :uid, true)"),
        {"uid": str(user_id)},
    )
    try:
        await db.execute(
            text("DELETE FROM payroll.driverrateassignments "
                 "WHERE driverrateassignmentid = :aid AND status = 'Pending'"),
            {"aid": assignment_id},
        )
    except DBAPIError as exc:
        raise _database_error(exc) from exc


async def _check_window_outside_finalized_periods(
    row: dict, db: AsyncConnection,
) -> None:
    overlapping = (await db.execute(
        text("""
            SELECT startdate, enddate, status
            FROM   payroll.payrollperiods
            WHERE  companyid = :cid AND branchid = :bid AND status IN ('Locked', 'Archived')
              AND  daterange(startdate, enddate, '[]')
                   && daterange(CAST(:efrom AS date), CAST(:eto AS date), '[]')
            LIMIT  1
        """),
        {"cid": row["company_id"], "bid": row["branch_id"],
         "efrom": row["effective_from"], "eto": row["effective_to"]},
    )).mappings().first()
    if overlapping is not None:
        raise compensation_error(
            "ASSIGNMENT_IN_FINALIZED_PERIOD",
            "Cannot void an assignment whose window overlaps a finalized payroll period "
            f"({overlapping['status']} period {overlapping['startdate']} to "
            f"{overlapping['enddate']}).", 422)


async def void(
    company_id: int, user_id: int, assignment_id: int, data: AssignmentVoid,
    db: AsyncConnection,
) -> AssignmentSummary:
    row = await _lock_and_load(company_id, assignment_id, db)
    await require_rate_edit(company_id, user_id, row["branch_id"], db)
    if row["status"] == "Pending":
        raise compensation_error(
            "PENDING_CANNOT_BE_VOIDED",
            "A Pending assignment is discarded, not voided.", 409)
    if row["status"] not in ("Approved", "Superseded"):
        raise compensation_error(
            "ASSIGNMENT_INVALID_TRANSITION",
            f"A {row['status']} assignment cannot be voided.", 409)
    await _check_window_outside_finalized_periods(row, db)
    try:
        await db.execute(
            text("""
                UPDATE payroll.driverrateassignments
                SET    status = 'Voided', voidedbyuserid = :uid, voidedatutc = now(),
                       voidreason = :reason, updatedbyuserid = :uid, updatedatutc = now()
                WHERE  driverrateassignmentid = :aid AND status IN ('Approved', 'Superseded')
            """),
            {"uid": user_id, "reason": data.reason, "aid": assignment_id},
        )
    except DBAPIError as exc:
        raise _database_error(exc) from exc
    return await _summary(company_id, assignment_id, db)


async def history(
    company_id: int, user_id: int, driver_id: int, rate_definition_id: int,
    db: AsyncConnection,
) -> list[AssignmentSummary]:
    branch_id = await load_company_driver_branch(company_id, user_id, driver_id, db)
    await require_rate_read(company_id, user_id, branch_id, db)
    owned = (await db.execute(
        text("SELECT 1 FROM payroll.ratedefinitions WHERE ratedefinitionid = :rid AND companyid = :cid"),
        {"rid": rate_definition_id, "cid": company_id},
    )).scalar_one_or_none()
    if owned is None:
        raise compensation_error("RATE_DEFINITION_NOT_FOUND", "Rate definition not found.", 404)
    ids = (await db.execute(
        text("""
            SELECT driverrateassignmentid FROM payroll.driverrateassignments
            WHERE  companyid = :cid AND driverid = :did AND ratedefinitionid = :rid
            ORDER  BY effectivefrom DESC, driverrateassignmentid DESC
        """),
        {"cid": company_id, "did": driver_id, "rid": rate_definition_id},
    )).scalars().all()
    return [await _summary(company_id, int(i), db) for i in ids]


async def driver_summary(
    company_id: int, user_id: int, driver_id: int, db: AsyncConnection,
) -> list[DriverRateSummaryItem]:
    branch_id = await load_company_driver_branch(company_id, user_id, driver_id, db)
    await require_rate_read(company_id, user_id, branch_id, db)
    rows = (await db.execute(
        text("""
            SELECT a.ratedefinitionid AS rate_definition_id,
                   rd.paydefinitionid AS pay_definition_id,
                   pd.definitioncode AS definition_code,
                   max(a.driverrateassignmentid) FILTER (WHERE a.status = 'Approved')
                       AS approved_assignment_id,
                   max(a.effectivefrom) FILTER (WHERE a.status = 'Approved')
                       AS approved_effective_from,
                   max(a.driverrateassignmentid) FILTER (WHERE a.status = 'Pending')
                       AS pending_assignment_id
            FROM   payroll.driverrateassignments a
            JOIN   payroll.ratedefinitions rd ON rd.ratedefinitionid = a.ratedefinitionid
            LEFT JOIN payroll.paydefinitions pd ON pd.paydefinitionid = rd.paydefinitionid
            WHERE  a.companyid = :cid AND a.driverid = :did
            GROUP  BY a.ratedefinitionid, rd.paydefinitionid, pd.definitioncode
            ORDER  BY a.ratedefinitionid
        """),
        {"cid": company_id, "did": driver_id},
    )).mappings().all()
    return [DriverRateSummaryItem.model_validate(dict(row)) for row in rows]



async def driver_pay_rates(
    company_id: int, user_id: int, driver_id: int, as_of: date | None, db: AsyncConnection,
) -> list[DriverPayRateRow]:
    """Branch-applicable scalar PayDefinitions and the Driver's rate state for each.

    A definition is listed when it is Active and its Branch configuration is
    active on the reference date. Rates are configuration only; nothing is
    calculated here.
    """
    branch_id = await load_company_driver_branch(company_id, user_id, driver_id, db)
    await require_rate_read(company_id, user_id, branch_id, db)
    reference = as_of or (await db.execute(
        text("SELECT core.fn_CompanyToday(:cid)"), {"cid": company_id})).scalar_one()

    definitions = (await db.execute(
        text("""
            SELECT pd.paydefinitionid AS pay_definition_id, pd.definitioncode AS definition_code,
                   pd.definitionname AS definition_name, pd.inputtype AS input_type,
                   pd.unit AS unit, rd.ratedefinitionid AS rate_definition_id,
                   c.ratecomponentdefinitionid AS rate_component_definition_id
            FROM   payroll.paydefinitions pd
            JOIN   payroll.ratedefinitions rd
                   ON rd.paydefinitionid = pd.paydefinitionid AND rd.shape = 'Scalar'
            JOIN   payroll.ratecomponentdefinitions c
                   ON c.ratedefinitionid = rd.ratedefinitionid AND c.sequenceno = 1
            WHERE  pd.companyid = :cid AND pd.status = 'Active'
              AND  EXISTS (
                       SELECT 1 FROM payroll.branchpayitemconfig bpic
                       WHERE  bpic.companyid = :cid AND bpic.branchid = :bid
                         AND  bpic.paydefinitionid = pd.paydefinitionid
                         AND  bpic.isactive
                         AND  bpic.effectivefrom <= :asof
                         AND  (bpic.effectiveto IS NULL OR bpic.effectiveto >= :asof))
            ORDER  BY lower(pd.definitionname), pd.definitioncode, pd.paydefinitionid
        """),
        {"cid": company_id, "bid": branch_id, "asof": reference},
    )).mappings().all()
    if not definitions:
        return []

    assignment_rows = (await db.execute(
        text("""
            SELECT a.ratedefinitionid AS rate_definition_id,
                   a.driverrateassignmentid AS driver_rate_assignment_id, a.status,
                   a.effectivefrom AS effective_from, a.effectiveto AS effective_to,
                   v.amount AS amount
            FROM   payroll.driverrateassignments a
            LEFT JOIN payroll.driverratevalues v
                   ON v.driverrateassignmentid = a.driverrateassignmentid
            WHERE  a.companyid = :cid AND a.driverid = :did
              AND  a.ratedefinitionid = ANY(:rids) AND a.status <> 'Voided'
            ORDER  BY a.effectivefrom, a.driverrateassignmentid
        """),
        {"cid": company_id, "did": driver_id,
         "rids": [d["rate_definition_id"] for d in definitions]},
    )).mappings().all()
    by_definition: dict[int, list[AssignmentBrief]] = {}
    for row in assignment_rows:
        by_definition.setdefault(row["rate_definition_id"], []).append(
            AssignmentBrief.model_validate(
                {key: row[key] for key in AssignmentBrief.model_fields}))

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
            **dict(definition), current=current, future=future, pending=pending))
    return result
