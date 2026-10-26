"""Lean tenant-scoped reads of canonical payroll authority for Import previews."""
from sqlalchemy import text

from app.imports.errors import ImportFailure
from app.imports.mapping import canonical, operational
from app.payroll.eligibility import (
    lifecycle_eligible_assignment_days,
    require_period_eligibility_snapshot,
)
from app.payroll.period_definitions import list_period_definitions


async def load_context(db, cid: int, period, *, dates=None, identifiers=None) -> dict:
    pid, bid = period.payroll_period_id, period.branch_id
    params = {"cid": cid, "pid": pid, "bid": bid, "dates": dates}
    await require_period_eligibility_snapshot(pid, cid, bid, db)
    business_scope = dict((await db.execute(text("""
        SELECT c.companycode AS company_code, b.branchcode AS branch_code,
            p.periodcode AS period_code, p.periodtype AS period_type
        FROM payroll.payrollperiods p
        JOIN core.companies c ON c.companyid = p.companyid
        JOIN core.branches b ON b.companyid = p.companyid AND b.branchid = p.branchid
        WHERE p.companyid = :cid AND p.branchid = :bid AND p.payrollperiodid = :pid
    """), params)).mappings().one())
    definitions = operational(await list_period_definitions(pid, cid, db, active_only=True))
    days = [dict(row) for row in (await db.execute(text("""
        SELECT workdate, isconfiguredoffday FROM payroll.payrollperioddays
        WHERE companyid = :cid AND payrollperiodid = :pid ORDER BY workdate
    """), params)).mappings()]
    if not days:
        raise ImportFailure("IMPORT_PERIOD_DAYS_MISSING", "The period day snapshot is missing; rebuild this invalid period.", 409)
    roster = [dict(row) for row in (await db.execute(text("""
        SELECT p.driverassignmentid, a.driverid, dr.drivernumber, e.employeekey,
            p.drivernamesnapshot AS drivername, p.iseligibleforperiod, p.eligibilityreasoncode,
            p.eligibleworkdates, a.effectivefromat, a.effectivetoat, a.endreason,
            p.branchtimezoneidsnapshot, e.fullname AS currentdrivername
        FROM payroll.payrollperioddrivereligibility p
        JOIN core.driverassignments a ON a.driverassignmentid = p.driverassignmentid
            AND a.companyid = p.companyid AND a.branchid = p.branchid
        JOIN core.drivers dr ON dr.driverid = a.driverid AND dr.companyid = a.companyid
        JOIN core.employees e ON e.employeeid = a.employeeid AND e.companyid = a.companyid
        WHERE p.companyid = :cid AND p.payrollperiodid = :pid AND p.branchid = :bid
        ORDER BY p.driverassignmentid
    """), params)).mappings()]
    # Stable identity is Company authority, not membership of a target roster.
    # A single set-based lookup only requests supplied keys; roster identities
    # additionally supply semantic references for name-only proposals.
    requested = identifiers or {"DriverNumber": [], "EmployeeKey": []}
    identity_params = {**params, "numbers": requested["DriverNumber"], "keys": requested["EmployeeKey"]}
    found = [dict(row) for row in (await db.execute(text("""
        SELECT d.driverid, d.drivernumber, e.employeekey, e.fullname AS drivername
        FROM core.drivers d JOIN core.employees e
          ON e.companyid = d.companyid AND e.employeeid = d.employeeid
        WHERE d.companyid = :cid AND (
            btrim(d.drivernumber) = ANY(CAST(:numbers AS text[]))
            OR btrim(e.employeekey) = ANY(CAST(:keys AS text[])))
        ORDER BY d.driverid
    """), identity_params)).mappings()]
    drivers = {row["driverid"]: {"driverid": row["driverid"], "drivernumber": row["drivernumber"],
               "employeekey": row["employeekey"], "drivername": row["currentdrivername"]} for row in roster}
    drivers.update({row["driverid"]: row for row in found})
    live = await lifecycle_eligible_assignment_days(pid, cid, bid, None,
        [row["driverassignmentid"] for row in roster], db)
    date_filter = "AND workdate = ANY(CAST(:dates AS date[]))" if dates is not None else ""
    lines = [dict(row) for row in (await db.execute(text(f"""
        SELECT driverassignmentid, workdate, payrollperioddefinitionid, quantity
        FROM payroll.payrolldraftlines
        WHERE companyid = :cid AND branchid = :bid AND payrollperiodid = :pid
            AND status <> 'Void' {date_filter}
        ORDER BY driverassignmentid, workdate, payrollperioddefinitionid, draftlineid
    """), params)).mappings()]
    entries = [dict(row) for row in (await db.execute(text(f"""
        SELECT e.driverassignmentid, e.workdate, e.statuskeyid, k.statuscode,
            COALESCE(k.isoffreason, FALSE) AS isoffreason
        FROM payroll.payrollperioddriverdayentrystate e
        LEFT JOIN payroll.payrollstatuskeys k ON k.companyid = e.companyid AND k.statuskeyid = e.statuskeyid
        WHERE e.companyid = :cid AND e.branchid = :bid AND e.payrollperiodid = :pid
            AND NOT e.isvoided {date_filter.replace('workdate', 'e.workdate')}
        ORDER BY e.driverassignmentid, e.workdate
    """), params)).mappings()]
    return {"definitions": definitions, "days": days, "roster": roster, "drivers": list(drivers.values()),
            "live_days": live, "lines": lines, "entries": entries,
            "period": {"id": pid, "company_id": cid, "branch_id": bid, **business_scope,
                       "start": period.start_date, "end": period.end_date}}


def context_identity(context) -> dict:
    return canonical({key: ([vars(d) for d in value] if key == "definitions" else value)
                      for key, value in context.items()})
