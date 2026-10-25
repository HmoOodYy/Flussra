"""The canonical development and demo dataset of the target model.

One small, payroll-ready Company: DEMO / Demo Logistics in USD with two Branches, a Company
Owner who is not an Employee, a custom Payroll Manager scoped to HQ, a non-driver staff
Employee, a current Driver with a DRIVER/Self account, a future Driver whose linked account
stays staged, one PerUnit PayDefinition applicable to HQ, the current Driver's Approved rate
schedule and a published default weekly Payroll Setup assigned to HQ. There is deliberately no
payroll history: no period, entry, calculation, finalization or review exists.

Only the Company, HQ, the two protected default roles and the first Owner are inserted
directly, because no product path creates a Company or its first Owner. Every other record
is written through the application's own HTTP routes, so the same validation, governance,
locks and audit apply as for any user.

``seed_canonical_demo`` writes into a freshly migrated, empty database and refuses anything
else; ``verify_canonical_demo`` reads only. Use ``scripts/reset_dev_database.py`` to reset a
development database and seed it.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402
from httpx import ASGITransport  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine  # noqa: E402

from app.auth.security import create_access_token, hash_password  # noqa: E402

PASSWORD = "TestPass123!"
COMPANY_CODE = "DEMO"
COMPANY_NAME = "Demo Logistics"
CURRENCY = "USD"

HQ = {"code": "HQ", "name": "Headquarters", "time_zone": "America/New_York"}
NORTH = {"code": "NORTH", "name": "North Depot", "time_zone": "America/Chicago"}

OWNER_USERNAME = "admin"
PAYROLL_MANAGER_USERNAME = "payroll.manager"
DRIVER_USERNAME = "jordan.rivera"
STAGED_USERNAME = "casey.brooks"

PAYROLL_MANAGER_ROLE = "Payroll Manager"
# The enforced payroll operation permissions plus the reads that operation needs: the Driver
# roster and rates behind the Day Grid, period reports and the Finalized Payroll Library.
PAYROLL_MANAGER_PERMISSIONS = (
    "drivers.view", "ledger.view", "payrates.view", "payroll.entry", "payroll.finalize",
    "payroll.period.create", "payroll.view", "reports.view", "review.decide",
)
DRIVER_ROLE_PERMISSIONS = ("drivers.view",)

STAFF = {"employee_key": "E-1001", "full_name": "Morgan Lee", "hire_date": "2025-06-02"}
CURRENT_DRIVER = {"employee_key": "E-1002", "full_name": "Jordan Rivera",
                  "driver_number": "D-1001", "start": date(2026, 1, 5)}
FUTURE_DRIVER = {"employee_key": "E-1003", "full_name": "Casey Brooks",
                 "driver_number": "D-1002", "starts_in_days": 14}

PAY_DEFINITION = {"definition_code": "DELIVERY", "definition_name": "Delivery stops",
                  "input_type": "WholeNumber", "unit": "stop", "calculation_method": "PerUnit"}
RATE_AMOUNT = Decimal("12.50")
PAYROLL_SETUP_NAME = "Weekly payroll"
WEEKEND_OFF_MASK = 65  # Sunday (bit 0) and Saturday (bit 6)

# Execution history and Import staging are empty after a canonical reset.
PAYROLL_HISTORY_TABLES = (
    "payroll.payrollperiods", "payroll.payrolldraftlines",
    "payroll.payrollperioddriverdayentrystate", "payroll.payrollbonusevents",
    "payroll.driverpayrules", "payroll.payrollcalculationsnapshots",
    "payroll.payrollfinallines", "payroll.payrollperiodauditevidenceevents",
    "payroll.payrollperiodworkflowactionevidence", "review.managerreviewitems",
    "import.imports",
)


class SeedError(RuntimeError):
    """The canonical seed could not be written; nothing after the failing step was written."""


@dataclass(frozen=True)
class SeedSummary:
    company_id: int
    hq_branch_id: int
    north_branch_id: int
    owner_user_id: int
    payroll_manager_user_id: int
    driver_user_id: int
    staged_user_id: int
    staff_employee_id: int
    current_driver_id: int
    current_driver_assignment_id: int
    future_driver_id: int
    future_driver_assignment_id: int
    pay_definition_id: int
    rate_definition_id: int
    driver_rate_schedule_id: int
    payroll_setup_id: int
    payroll_setup_anchor: date


def _local_midnight(day: date, time_zone: str) -> str:
    return datetime.combine(day, time.min, ZoneInfo(time_zone)).isoformat()


def _monday_of_previous_week(day: date) -> date:
    return day - timedelta(days=day.weekday() + 7)


async def _bootstrap(engine: AsyncEngine) -> tuple[int, int, int]:
    """The Company, HQ, protected default roles and first Owner: no product path creates them."""
    async with engine.begin() as db:
        existing = (await db.execute(text(
            "SELECT (SELECT count(*) FROM core.companies) + (SELECT count(*) FROM sec.users)"
        ))).scalar_one()
        if existing:
            raise SeedError(
                "The canonical seed writes only into a freshly migrated, empty database; "
                "reset it with scripts/reset_dev_database.py.")
        company_id = (await db.execute(text("""
            INSERT INTO core.companies (companycode, companyname, status, issuspended, currencycode)
            VALUES (:code, :name, 'Active', FALSE, :currency) RETURNING companyid
        """), {"code": COMPANY_CODE, "name": COMPANY_NAME, "currency": CURRENCY})).scalar_one()
        await db.execute(text("SELECT payroll.fn_EnsureDefaultStatusRateColumn(:cid)"),
                         {"cid": company_id})
        hq_id = (await db.execute(text("""
            INSERT INTO core.branches (companyid, branchcode, branchname, status, isdefault, timezoneid)
            VALUES (:cid, :code, :name, 'Active', TRUE, :tz) RETURNING branchid
        """), {"cid": company_id, "code": HQ["code"], "name": HQ["name"],
               "tz": HQ["time_zone"]})).scalar_one()
        owner_role = (await db.execute(text("""
            INSERT INTO sec.companyroles
                (companyid, rolecode, rolename, rolelevel, isdefault, isprotected, iscustom, isactive)
            VALUES (:cid, 'COMPANY_OWNER', 'Company Owner', 100, TRUE, TRUE, FALSE, TRUE)
            RETURNING companyroleid
        """), {"cid": company_id})).scalar_one()
        await db.execute(text("""
            INSERT INTO sec.companyrolepermissions (companyroleid, permissioncode)
            SELECT :rid, permissioncode FROM sec.permissions
        """), {"rid": owner_role})
        driver_role = (await db.execute(text("""
            INSERT INTO sec.companyroles
                (companyid, rolecode, rolename, rolelevel, isdefault, isprotected, iscustom, isactive)
            VALUES (:cid, 'DRIVER', 'Driver', 10, TRUE, TRUE, FALSE, TRUE)
            RETURNING companyroleid
        """), {"cid": company_id})).scalar_one()
        for code in DRIVER_ROLE_PERMISSIONS:
            await db.execute(text("""
                INSERT INTO sec.companyrolepermissions (companyroleid, permissioncode)
                VALUES (:rid, :code)
            """), {"rid": driver_role, "code": code})
        owner_id = (await db.execute(text("""
            INSERT INTO sec.users
                (companyid, username, displayname, passwordhash, isactive, canlogin,
                 isstaged, mustchangepassword)
            VALUES (:cid, :username, 'Company Owner', :hash, TRUE, TRUE, FALSE, FALSE)
            RETURNING userid
        """), {"cid": company_id, "username": OWNER_USERNAME,
               "hash": hash_password(PASSWORD)})).scalar_one()
        await db.execute(text("""
            INSERT INTO sec.userbranchroles
                (userid, companyid, branchid, companyroleid, scopetype, isactive)
            VALUES (:uid, :cid, NULL, :rid, 'AllCompanyBranches', TRUE)
        """), {"uid": owner_id, "cid": company_id, "rid": owner_role})
    return company_id, hq_id, owner_id


class _Api:
    """The application's own routes, in process, as one authenticated user."""

    def __init__(self, client: httpx.AsyncClient, token: str):
        self._client = client
        self._headers = {"Authorization": f"Bearer {token}"}

    async def call(self, method: str, path: str, expected: int, **kwargs):
        response = await self._client.request(method, path, headers=self._headers, **kwargs)
        if response.status_code != expected:
            raise SeedError(f"{method} {path} returned {response.status_code}: {response.text}")
        return response.json() if response.content else None


def application_client(engine: AsyncEngine) -> httpx.AsyncClient:
    """An HTTP client bound to an in-process application that uses ``engine``."""
    from app.main import create_app

    app = create_app()
    app.state.engine = engine
    return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://canonical-demo")


async def seed_canonical_demo(database_url: str) -> SeedSummary:
    """Write the canonical demo into a freshly migrated, empty database."""
    engine = create_async_engine(database_url)
    try:
        company_id, hq_id, owner_id = await _bootstrap(engine)
        async with application_client(engine) as client:
            login = await client.post("/auth/login", json={
                "username": OWNER_USERNAME, "password": PASSWORD, "company_code": COMPANY_CODE})
            if login.status_code != 200:
                raise SeedError(f"The Owner could not log in: {login.text}")
            return await _seed_through_routes(
                _Api(client, login.json()["access_token"]), company_id, hq_id, owner_id)
    finally:
        await engine.dispose()


async def _seed_through_routes(api: _Api, company_id: int, hq_id: int, owner_id: int) -> SeedSummary:
    north = await api.call("POST", "/settings/branches", 201, json={
        "branch_name": NORTH["name"], "branch_code": NORTH["code"],
        "time_zone_id": NORTH["time_zone"]})
    north_id = north["branch_id"]
    business_dates = {b["branch_id"]: date.fromisoformat(b["business_date"])
                      for b in await api.call("GET", "/core/branches", 200)}

    # Access: the custom Payroll Manager, scoped to HQ.
    role = await api.call("POST", "/admin/company-roles", 201, json={
        "role_name": PAYROLL_MANAGER_ROLE,
        "notes": "Runs HQ payroll from period creation through finalization."})
    await api.call("PUT", f"/admin/company-roles/{role['company_role_id']}/permissions", 200,
                   json={"permission_codes": list(PAYROLL_MANAGER_PERMISSIONS)})
    manager = await api.call("POST", "/admin/users", 201, json={
        "username": PAYROLL_MANAGER_USERNAME, "display_name": "Taylor Quinn", "password": PASSWORD,
        "can_login": True, "must_change_password": False,
        "role_assignment": {"company_role_id": role["company_role_id"],
                            "scope_type": "SpecificBranch", "branch_id": hq_id}})

    # Workforce: staff, the current Driver and the future Driver.
    staff = await api.call("POST", "/workforce/employees", 201, json={
        "branch_id": hq_id, **STAFF})
    current = await api.call("POST", "/workforce/employees", 201, json={
        "branch_id": hq_id, "employee_key": CURRENT_DRIVER["employee_key"],
        "full_name": CURRENT_DRIVER["full_name"], "hire_date": CURRENT_DRIVER["start"].isoformat(),
        "driver": {"driver_number": CURRENT_DRIVER["driver_number"],
                   "effective_from_at": _local_midnight(CURRENT_DRIVER["start"], HQ["time_zone"])}})
    future_start = business_dates[north_id] + timedelta(days=FUTURE_DRIVER["starts_in_days"])
    future = await api.call("POST", "/workforce/employees", 201, json={
        "branch_id": north_id, "employee_key": FUTURE_DRIVER["employee_key"],
        "full_name": FUTURE_DRIVER["full_name"], "hire_date": future_start.isoformat(),
        "driver": {"driver_number": FUTURE_DRIVER["driver_number"],
                   "effective_from_at": _local_midnight(future_start, NORTH["time_zone"])}})
    if current["driver_state"] != "current" or future["driver_state"] != "pending":
        raise SeedError("The Drivers did not reach their current and pending states.")

    # Access for the Drivers: Self for the current one; the future one stays staged until
    # provisioning, because DRIVER access requires a current assignment.
    roles = {r["role_code"]: r["company_role_id"]
             for r in await api.call("GET", "/admin/company-roles", 200)}
    driver_user = await api.call("POST", "/admin/users", 201, json={
        "username": DRIVER_USERNAME, "display_name": CURRENT_DRIVER["full_name"],
        "password": PASSWORD, "can_login": True, "must_change_password": False,
        "employee_id": current["employee_id"],
        "role_assignment": {"company_role_id": roles["DRIVER"], "scope_type": "Self"}})
    staged_user = await api.call("POST", "/admin/users", 201, json={
        "username": STAGED_USERNAME, "display_name": FUTURE_DRIVER["full_name"],
        "password": PASSWORD, "is_staged": True, "can_login": False,
        "employee_id": future["employee_id"]})

    # Compensation: one PerUnit definition applicable to HQ and the Approved rate that makes the
    # current Driver payroll-ready under it.
    definition = await api.call("POST", "/compensation/pay-definitions", 201, json=PAY_DEFINITION)
    await api.call(
        "PATCH", f"/compensation/branches/{hq_id}/pay-definitions/{definition['pay_definition_id']}",
        200, json={"is_active": True, "effective_from": CURRENT_DRIVER["start"].isoformat()})
    schedule = await api.call("POST", "/compensation/driver-rate-schedules", 201, json={
        "driver_id": current["driver"]["driver_id"],
        "rate_definition_id": definition["rate_definition_id"],
        "effective_from": CURRENT_DRIVER["start"].isoformat(), "effective_to": None})
    schedule_path = f"/compensation/driver-rate-schedules/{schedule['driver_rate_schedule_id']}"
    await api.call("PUT", f"{schedule_path}/values", 200, json={"values": [{
        "rate_component_definition_id": definition["components"][0]["rate_component_definition_id"],
        "amount": str(RATE_AMOUNT)}]})
    await api.call("POST", f"{schedule_path}/approve", 200)

    # Payroll Setup: a published weekly default assigned to HQ from last week's period.
    anchor = _monday_of_previous_week(business_dates[hq_id])
    setup = await api.call("POST", "/payroll-setup/setups", 201, json={
        "setup_name": PAYROLL_SETUP_NAME})
    draft = await api.call("POST", f"/payroll-setup/setups/{setup['setup_id']}/drafts", 201, json={
        "payroll_frequency": "Week", "anchor_start_date": anchor.isoformat(),
        "normal_days_off_mask": WEEKEND_OFF_MASK})
    await api.call(
        "POST", f"/payroll-setup/setups/{setup['setup_id']}/drafts/{draft['version_id']}/publish",
        201, json={"effective_from_date": anchor.isoformat()})
    await api.call("PUT", "/payroll-setup/default", 204, json={"setup_id": setup["setup_id"]})
    await api.call("POST", f"/payroll-setup/branches/{hq_id}/assignments", 201, json={
        "setup_id": setup["setup_id"], "effective_from_date": anchor.isoformat(),
        "reason": "Canonical demo payroll schedule"})

    return SeedSummary(
        company_id=company_id, hq_branch_id=hq_id, north_branch_id=north_id,
        owner_user_id=owner_id, payroll_manager_user_id=manager["user_id"],
        driver_user_id=driver_user["user_id"], staged_user_id=staged_user["user_id"],
        staff_employee_id=staff["employee_id"],
        current_driver_id=current["driver"]["driver_id"],
        current_driver_assignment_id=current["current_or_pending_assignment"]["driver_assignment_id"],
        future_driver_id=future["driver"]["driver_id"],
        future_driver_assignment_id=future["current_or_pending_assignment"]["driver_assignment_id"],
        pay_definition_id=definition["pay_definition_id"],
        rate_definition_id=definition["rate_definition_id"],
        driver_rate_schedule_id=schedule["driver_rate_schedule_id"],
        payroll_setup_id=setup["setup_id"], payroll_setup_anchor=anchor,
    )


# ---------------------------------------------------------------------------
# Verification (read-only)
# ---------------------------------------------------------------------------

async def verify_canonical_demo(database_url: str) -> list[str]:
    """Every way the database differs from the canonical demo; empty when it matches.

    Reads only: catalog and table reads, then the product's own read routes with directly
    issued tokens (no login, which would record the sign-in).
    """
    engine = create_async_engine(database_url)
    problems: list[str] = []
    try:
        async with engine.connect() as db:
            facts = await _read_facts(db)
        problems.extend(_compare_facts(facts))
        if not problems:
            async with application_client(engine) as client:
                problems.extend(await _verify_product_reads(client, facts))
    finally:
        await engine.dispose()
    return problems


async def _read_facts(db) -> dict:
    async def rows(statement: str, **params) -> list[dict]:
        return [dict(r) for r in (await db.execute(text(statement), params)).mappings().all()]

    facts: dict = {
        "companies": await rows("""
            SELECT companyid, companycode, companyname, currencycode, status, issuspended
            FROM core.companies"""),
        "branches": await rows("""
            SELECT branchid, branchcode, branchname, status, isdefault, timezoneid
            FROM core.branches ORDER BY branchid"""),
        "roles": await rows("""
            SELECT cr.companyroleid, cr.rolecode, cr.rolename, cr.iscustom, cr.isprotected,
                   cr.isactive,
                   COALESCE(array_agg(p.permissioncode ORDER BY p.permissioncode)
                            FILTER (WHERE p.permissioncode IS NOT NULL), '{}') AS permissions
            FROM sec.companyroles cr
            LEFT JOIN sec.companyrolepermissions p ON p.companyroleid = cr.companyroleid
            GROUP BY cr.companyroleid ORDER BY cr.companyroleid"""),
        "catalog": [r["permissioncode"] for r in await rows(
            "SELECT permissioncode FROM sec.permissions ORDER BY permissioncode")],
        "users": await rows("""
            SELECT userid, username, employeeid, isactive, canlogin, isstaged
            FROM sec.users ORDER BY userid"""),
        "assignments": await rows("""
            SELECT ubr.userid, cr.rolecode, cr.rolename, ubr.scopetype, ubr.branchid, ubr.isactive
            FROM sec.userbranchroles ubr
            JOIN sec.companyroles cr ON cr.companyroleid = ubr.companyroleid"""),
        "overrides": (await db.execute(text(
            "SELECT count(*) FROM sec.userpermissionoverrides"))).scalar_one(),
        "employees": await rows("""
            SELECT e.employeeid, e.employeekey, e.fullname, e.branchid, e.employmentstatus,
                   d.driverid, d.drivernumber, d.driverstatus
            FROM core.employees e LEFT JOIN core.drivers d ON d.employeeid = e.employeeid
            ORDER BY e.employeeid"""),
        "driver_assignments": await rows("""
            SELECT a.driverassignmentid, a.driverid, a.branchid, a.effectivetoat,
                   a.effectivefromat > now() AS starts_later,
                   a.driverassignmentid = core.fn_EffectiveDriverAssignment(a.companyid, a.driverid)
                       AS is_current
            FROM core.driverassignments a ORDER BY a.driverassignmentid"""),
        "pay_definitions": await rows("""
            SELECT pd.paydefinitionid, pd.definitioncode, pd.calculationmethod, pd.inputtype,
                   pd.status, rd.ratedefinitionid
            FROM payroll.paydefinitions pd
            JOIN payroll.ratedefinitions rd ON rd.paydefinitionid = pd.paydefinitionid"""),
        "applicability": await rows("""
            SELECT branchid, paydefinitionid, isactive, effectivefrom, effectiveto
            FROM payroll.branchpaydefinitionconfig"""),
        "schedules": await rows("""
            SELECT s.driverratescheduleid, s.driverid, s.ratedefinitionid, s.status,
                   s.effectivefrom, s.effectiveto,
                   array_agg(v.amount ORDER BY v.ratecomponentdefinitionid) AS amounts
            FROM payroll.driverrateschedules s
            LEFT JOIN payroll.driverrateschedulevalues v
                   ON v.driverratescheduleid = s.driverratescheduleid
            GROUP BY s.driverratescheduleid"""),
        "setups": await rows("""
            SELECT s.payrollsetupid, s.setupname, s.status,
                   c.defaultpayrollsetupid = s.payrollsetupid AS is_default
            FROM payroll.payrollsetups s JOIN core.companies c ON c.companyid = s.companyid"""),
        "setup_assignments": await rows("""
            SELECT branchid, payrollsetupid, effectivetodate, withdrawnatutc
            FROM payroll.branchpayrollsetupassignments"""),
        "history": {table: (await db.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one()
                    for table in PAYROLL_HISTORY_TABLES},
    }
    return facts


def _compare_facts(facts: dict) -> list[str]:
    problems: list[str] = []

    def expect(condition: bool, message: str) -> None:
        if not condition:
            problems.append(message)

    expect([(c["companycode"], c["companyname"], c["currencycode"], c["status"], c["issuspended"])
            for c in facts["companies"]] == [(COMPANY_CODE, COMPANY_NAME, CURRENCY, "Active", False)],
           f"Expected one Active, unsuspended {COMPANY_CODE} Company in {CURRENCY}.")
    expect([(b["branchcode"], b["status"], b["isdefault"], b["timezoneid"])
            for b in facts["branches"]] == [
               (HQ["code"], "Active", True, HQ["time_zone"]),
               (NORTH["code"], "Active", False, NORTH["time_zone"])],
           "Expected Active Branches HQ (default) and NORTH.")
    if problems:
        return problems
    branch = {b["branchcode"]: b["branchid"] for b in facts["branches"]}

    roles = {r["rolecode"] if not r["iscustom"] else r["rolename"]: r for r in facts["roles"]}
    expect(set(roles) == {"COMPANY_OWNER", "DRIVER", PAYROLL_MANAGER_ROLE},
           "Expected exactly the COMPANY_OWNER, DRIVER and Payroll Manager roles.")
    expect(list(roles.get("COMPANY_OWNER", {}).get("permissions", [])) == facts["catalog"],
           "The Company Owner must hold the whole permission catalog.")
    expect(tuple(roles.get("DRIVER", {}).get("permissions", [])) == DRIVER_ROLE_PERMISSIONS,
           "The DRIVER role must hold only its Self permissions.")
    expect(tuple(roles.get(PAYROLL_MANAGER_ROLE, {}).get("permissions", []))
           == PAYROLL_MANAGER_PERMISSIONS,
           "The Payroll Manager role must hold exactly the payroll permissions.")
    expect(facts["overrides"] == 0, "No user permission override belongs to the canonical demo.")

    employees = {e["employeekey"]: e for e in facts["employees"]}
    expect(set(employees) == {STAFF["employee_key"], CURRENT_DRIVER["employee_key"],
                              FUTURE_DRIVER["employee_key"]},
           "Expected exactly the staff, current Driver and future Driver Employees.")
    if problems:
        return problems
    staff = employees[STAFF["employee_key"]]
    current = employees[CURRENT_DRIVER["employee_key"]]
    future = employees[FUTURE_DRIVER["employee_key"]]
    expect(staff["driverid"] is None and staff["branchid"] == branch["HQ"],
           "The staff Employee must be an HQ non-driver.")
    expect(current["drivernumber"] == CURRENT_DRIVER["driver_number"]
           and current["driverstatus"] == "Active",
           "The current Driver must be Active with its driver number.")
    expect(future["drivernumber"] == FUTURE_DRIVER["driver_number"],
           "The future Driver must carry its driver number.")
    by_driver: dict[int, list[dict]] = {}
    for assignment in facts["driver_assignments"]:
        by_driver.setdefault(assignment["driverid"], []).append(assignment)
    expect(sorted(by_driver) == sorted([current["driverid"], future["driverid"]]),
           "Only the two Drivers may have DriverAssignments.")
    current_tenures = by_driver.get(current["driverid"], [])
    expect(len(current_tenures) == 1 and current_tenures[0]["is_current"]
           and current_tenures[0]["branchid"] == branch["HQ"]
           and current_tenures[0]["effectivetoat"] is None,
           "The current Driver must have exactly one open, current HQ assignment.")
    future_tenures = by_driver.get(future["driverid"], [])
    expect(len(future_tenures) == 1 and future_tenures[0]["starts_later"]
           and not future_tenures[0]["is_current"]
           and future_tenures[0]["branchid"] == branch["NORTH"],
           "The future Driver must have exactly one pending NORTH assignment.")

    users = {u["username"]: u for u in facts["users"]}
    expect(set(users) == {OWNER_USERNAME, PAYROLL_MANAGER_USERNAME, DRIVER_USERNAME, STAGED_USERNAME},
           "Expected exactly the Owner, Payroll Manager, Driver and staged users.")
    if problems:
        return problems
    assignments: dict[int, list[tuple]] = {}
    for a in facts["assignments"]:
        if a["isactive"]:
            assignments.setdefault(a["userid"], []).append(
                (a["rolecode"] if a["rolecode"] in ("COMPANY_OWNER", "DRIVER") else a["rolename"],
                 a["scopetype"], a["branchid"]))
    owner, manager = users[OWNER_USERNAME], users[PAYROLL_MANAGER_USERNAME]
    driver, staged = users[DRIVER_USERNAME], users[STAGED_USERNAME]
    expect(owner["employeeid"] is None and owner["canlogin"] and not owner["isstaged"]
           and assignments.get(owner["userid"]) == [("COMPANY_OWNER", "AllCompanyBranches", None)],
           "The Owner must be an unlinked, enabled Company Owner over all Branches.")
    expect(manager["employeeid"] is None and manager["canlogin"] and not manager["isstaged"]
           and assignments.get(manager["userid"])
           == [(PAYROLL_MANAGER_ROLE, "SpecificBranch", branch["HQ"])],
           "The Payroll Manager must be an enabled, unlinked user scoped to HQ.")
    expect(driver["employeeid"] == current["employeeid"] and driver["canlogin"]
           and assignments.get(driver["userid"]) == [("DRIVER", "Self", None)],
           "The Driver user must be linked to the current Driver with DRIVER/Self access.")
    expect(staged["employeeid"] == future["employeeid"] and staged["isstaged"]
           and not staged["canlogin"] and staged["userid"] not in assignments,
           "The future Driver's user must stay staged: no login and no active role assignment.")

    definitions = facts["pay_definitions"]
    expect([(d["definitioncode"], d["calculationmethod"], d["inputtype"]) for d in definitions]
           == [(PAY_DEFINITION["definition_code"], "PerUnit", PAY_DEFINITION["input_type"])],
           "Expected exactly one PerUnit DELIVERY PayDefinition.")
    if problems:
        return problems
    definition = definitions[0]
    expect([(a["branchid"], a["paydefinitionid"], a["isactive"], a["effectiveto"])
            for a in facts["applicability"]]
           == [(branch["HQ"], definition["paydefinitionid"], True, None)],
           "DELIVERY must be applicable to HQ only, open-ended.")
    expect([(s["driverid"], s["ratedefinitionid"], s["status"], s["effectiveto"], s["amounts"])
            for s in facts["schedules"]]
           == [(current["driverid"], definition["ratedefinitionid"], "Approved", None, [RATE_AMOUNT])],
           f"The current Driver must have exactly one open Approved DELIVERY rate of {RATE_AMOUNT}.")
    expect([(s["setupname"], s["status"], s["is_default"]) for s in facts["setups"]]
           == [(PAYROLL_SETUP_NAME, "Active", True)],
           "Expected exactly one Active default Payroll Setup.")
    expect([(a["branchid"], a["effectivetodate"], a["withdrawnatutc"])
            for a in facts["setup_assignments"]] == [(branch["HQ"], None, None)]
           and {a["payrollsetupid"] for a in facts["setup_assignments"]}
           == {s["payrollsetupid"] for s in facts["setups"]},
           "Only HQ is assigned the default Payroll Setup, open-ended.")
    for table, count in facts["history"].items():
        expect(count == 0, f"The canonical demo has no payroll history, but {table} has {count} rows.")
    return problems


async def _verify_product_reads(client: httpx.AsyncClient, facts: dict) -> list[str]:
    """Payroll readiness and access scope as the product itself reports them."""
    problems: list[str] = []
    company_id = facts["companies"][0]["companyid"]
    users = {u["username"]: u["userid"] for u in facts["users"]}
    branch = {b["branchcode"]: b["branchid"] for b in facts["branches"]}
    current = next(e for e in facts["employees"] if e["employeekey"] == CURRENT_DRIVER["employee_key"])
    definition = facts["pay_definitions"][0]

    def headers(username: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {create_access_token(users[username], company_id)}"}

    async def read(username: str, path: str, **params):
        response = await client.get(path, params=params, headers=headers(username))
        return response.status_code, (response.json() if response.content else None)

    status, body = await read(OWNER_USERNAME, f"/payroll/branches/{branch['HQ']}/period-candidates",
                              mode="OPEN_CREATION")
    if status != 200 or not (body or {}).get("selected"):
        problems.append(f"HQ offers no payroll period to create ({status}: {body}).")
    status, body = await read(OWNER_USERNAME, "/core/branches")
    today = {b["branch_id"]: b["business_date"] for b in body or []} if status == 200 else {}
    status, body = await read(
        OWNER_USERNAME,
        f"/compensation/drivers/{current['driverid']}/rate-definitions/"
        f"{definition['ratedefinitionid']}/resolution", work_date=today.get(branch["HQ"]))
    if status != 200 or body["schedule_status"] != "Approved" or [
            Decimal(str(c["amount"])) for c in body["components"]] != [RATE_AMOUNT]:
        problems.append(f"The current Driver's DELIVERY rate does not resolve ({status}: {body}).")
    status, body = await read(OWNER_USERNAME, "/core/driver-assignments")
    if status != 200 or sorted(r["state"] for r in body) != ["current", "pending"]:
        problems.append(f"The Owner should see one current and one pending assignment ({status}).")
    status, body = await read(PAYROLL_MANAGER_USERNAME, "/core/driver-assignments")
    if status != 200 or [(r["branch_id"], r["state"]) for r in body] != [(branch["HQ"], "current")]:
        problems.append(f"The HQ Payroll Manager should see only the current HQ Driver ({status}).")
    status, body = await read(DRIVER_USERNAME, "/auth/me")
    if status != 200 or [(a["role_code"], a["scope"]) for a in body["self_assignments"]] != [
            ("DRIVER", "Self")]:
        problems.append(f"The Driver account does not resolve to DRIVER/Self ({status}).")
    status, _ = await read(DRIVER_USERNAME, "/core/driver-assignments")
    if status != 403:
        problems.append(f"A DRIVER/Self account must not read the Driver roster ({status}).")
    return problems
