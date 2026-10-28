"""Lean tenant-scoped reads of canonical payroll authority for Import previews."""
from collections import defaultdict
from functools import partial

import anyio
from sqlalchemy import text

from app.imports.errors import ImportFailure
from app.imports.identity import (
    finish_name_match_state,
    fold_company_name_rows,
    new_name_match_state,
)
from app.imports.mapping import canonical, encoded, normalized_identifier, operational
from app.imports.policy import DEFAULT_IMPORT_RESOURCE_POLICY, ImportResourcePolicy
from app.payroll.eligibility import (
    lifecycle_eligible_assignment_days,
    require_period_eligibility_snapshot,
)
from app.payroll.period_definitions import list_period_definitions


async def _lookup_company_names(db, cid: int, names: list[str], policy: ImportResourcePolicy,
                               retained_drivers: dict | None = None):
    """Resolve requested names from canonical Company Drivers using bounded streaming."""
    states = {name: new_name_match_state() for name in names}
    result = await db.stream(text("""
        WITH bounded_driver_ids AS MATERIALIZED (
            SELECT d.driverid FROM core.drivers d
            WHERE d.companyid = :cid
            ORDER BY d.driverid
            LIMIT :limit_plus_one
        ), bounded_count AS MATERIALIZED (
            SELECT count(*)::INTEGER AS row_count FROM bounded_driver_ids
        )
        SELECT c.row_count, d.driverid, d.drivernumber, e.employeekey, e.fullname
        FROM bounded_count c
        LEFT JOIN bounded_driver_ids b ON c.row_count <= :max_rows
        LEFT JOIN core.drivers d ON d.driverid = b.driverid AND d.companyid = :cid
        LEFT JOIN core.employees e ON e.employeeid = d.employeeid AND e.companyid = d.companyid
        ORDER BY d.driverid NULLS FIRST
    """), {"cid": cid, "max_rows": policy.max_identity_lookup_rows,
          "limit_plus_one": policy.max_identity_lookup_rows + 1})
    first_batch = True
    try:
        batch_size = min(policy.identity_lookup_batch_rows, policy.max_identity_lookup_rows)
        async for partition in result.mappings().partitions(batch_size):
            if first_batch:
                first_batch = False
                row_count = partition[0]["row_count"]
                if row_count > policy.max_identity_lookup_rows:
                    raise ImportFailure("IMPORT_IDENTITY_LOOKUP_LIMIT_EXCEEDED",
                        f"Company Driver identity lookup exceeds the configured limit of "
                        f"{policy.max_identity_lookup_rows} rows. No partial preview was published.", 422)
                if row_count == 0:
                    break
            await anyio.to_thread.run_sync(partial(fold_company_name_rows, states, partition,
                                                   retained_drivers))
    finally:
        await result.close()
    return finish_name_match_state(states)


async def load_context(db, cid: int, period, *, dates=None, identifiers=None,
                       resource_policy: ImportResourcePolicy = DEFAULT_IMPORT_RESOURCE_POLICY) -> dict:
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
    requested = identifiers if identifiers is not None else {
        "DriverNumber": [], "EmployeeKey": [], "DriverName": []}
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
    requested_names = sorted(set(requested.get("DriverName", [])))
    name_matches = (await _lookup_company_names(db, cid, requested_names, resource_policy, drivers)
                    if requested_names else {})
    for match in name_matches.values():
        if match["driver"] is not None:
            driver = match["driver"]
            drivers[driver["driverid"]] = driver
    live = await lifecycle_eligible_assignment_days(pid, cid, bid, None,
        [row["driverassignmentid"] for row in roster], db)
    date_filter = "AND workdate = ANY(CAST(:dates AS date[]))" if dates is not None else ""
    lines = [dict(row) for row in (await db.execute(text(f"""
        SELECT draftlineid, driverassignmentid, workdate, payrollperioddefinitionid, quantity,
            sourcetype, addedbyuserid
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
    drivers_by_id = drivers
    identity_indexes = {"DriverNumber": defaultdict(set), "EmployeeKey": defaultdict(set),
                        "DriverName": defaultdict(set)}
    for driver in drivers.values():
        for kind, key in (("DriverNumber", "drivernumber"), ("EmployeeKey", "employeekey")):
            value = normalized_identifier(kind, driver.get(key))
            if value:
                identity_indexes[kind][value].add(driver["driverid"])
    assignments_by_driver = defaultdict(list)
    for name, match in name_matches.items():
        if match["driver_id"] is not None:
            identity_indexes["DriverName"][name].add(match["driver_id"])
    for entry in roster:
        assignments_by_driver[entry["driverid"]].append(entry)
    day_map = {row["workdate"]: row["isconfiguredoffday"] for row in days}
    current_lines = {}
    current_line_provenance = {}
    existing_source_dates = set()
    for line in lines:
        key = (line["driverassignmentid"], line["workdate"], line["payrollperioddefinitionid"])
        current_lines[key] = line["quantity"]
        current_line_provenance[key] = {"source_type": line["sourcetype"],
                                        "draft_line_id": line["draftlineid"],
                                        "added_by_user_id": line["addedbyuserid"]}
        existing_source_dates.add(key[:2])
    entry_map = {}
    for entry in entries:
        entry_map[(entry["driverassignmentid"], entry["workdate"])] = entry
        existing_source_dates.add((entry["driverassignmentid"], entry["workdate"]))
    return {"definitions": definitions, "definition_by_id": {d.payroll_period_definition_id: d for d in definitions},
            "days": day_map, "roster": roster, "drivers": list(drivers.values()),
            "drivers_by_id": drivers_by_id, "identity_indexes": identity_indexes,
            "name_match_summaries": name_matches,
            "assignments_by_driver": assignments_by_driver, "live_days": live,
            "current_lines": current_lines, "existing_source_dates": existing_source_dates,
            "current_line_provenance": current_line_provenance,
            "entries": entry_map,
            "period": {"id": pid, "company_id": cid, "branch_id": bid, **business_scope,
                       "start": period.start_date, "end": period.end_date}}


def context_identity(context) -> dict:
    derived = {"definition_by_id", "drivers_by_id", "identity_indexes", "assignments_by_driver",
               "current_lines", "current_line_provenance", "existing_source_dates"}
    identity = {key: ([vars(d) for d in value] if key == "definitions" else value)
                for key, value in context.items() if key not in derived}
    identity["current_lines"] = sorted((
        {"driver_assignment_id": assignment_id, "work_date": work_date.isoformat(),
         "definition_id": definition_id, "quantity": canonical(quantity)}
        for (assignment_id, work_date, definition_id), quantity in context["current_lines"].items()
    ), key=encoded)
    identity["current_line_provenance"] = sorted((
        {"driver_assignment_id": assignment_id, "work_date": work_date.isoformat(),
         "definition_id": definition_id, **provenance}
        for (assignment_id, work_date, definition_id), provenance in context["current_line_provenance"].items()
    ), key=encoded)
    identity["existing_source_dates"] = sorted((
        {"driver_assignment_id": assignment_id, "work_date": work_date.isoformat()}
        for assignment_id, work_date in context["existing_source_dates"]
    ), key=encoded)
    return canonical(identity)
