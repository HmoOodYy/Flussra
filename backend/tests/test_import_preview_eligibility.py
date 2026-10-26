"""Import uses permanent identities and canonical date-specific eligibility."""
from datetime import date, timedelta
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import text

from app.imports.payroll_context import load_context
from app.payroll.eligibility import (
    _assert_driver_eligible_for_workdate_via_snapshot,
    _lifecycle_eligible_assignment_ids,
    lifecycle_eligible_assignment_days,
)
from app.payroll.period_read import get_period_by_id
from tests.builders.workforce import create_workforce_driver
from tests.compensation_api_fixtures import (  # noqa: F401
    p3c_application,
    p3c_database_engine,
    p3c_http_client,
)
from tests.compensation_database_fixtures import p3b_database  # noqa: F401
from tests.db_state import DRIVER_ASSIGNMENT_HISTORY_TRIGGER, suspended_test_triggers
from tests.payroll_runtime_fixtures import (
    assign_weekly_setup,
    build_payroll_tenant,
    create_period,
    make_user,
    rated_definition,
    set_employment,
    setup_anchor,
)
from tests.test_import_preview_api import (  # noqa: F401
    _definition_target,
    _map,
    _payroll_inputs,
    _payroll_snapshot,
    _period,
    _upload,
    payroll_tenant,
)
from tests.test_p4b_entry_contract import _two_definition_period

pytestmark = pytest.mark.asyncio


async def page(client, tenant, iid, mapped):
    result = await client.get(f"/payroll/imports/{iid}/rows", headers=tenant.admin,
        params={"expected_revision": mapped["revision"], "plan_fingerprint": mapped["fingerprints"]["plan"]})
    assert result.status_code == 200, result.text
    return result.json()["items"]


async def test_existing_source_rescue_allows_only_exact_date_existing_quantity_changes(
    p3c_client, p3c_engine, tenant,
):
    period, ids = await _two_definition_period(p3c_client, p3c_engine, tenant)
    source = await _payroll_inputs(p3c_engine, tenant, period)
    day, other = source["dates"][:2]
    line = await p3c_client.post(f"/payroll/periods/{period['payroll_period_id']}/lines", headers=tenant.admin,
        json={"driver_assignment_id": tenant.driver_a, "payroll_period_definition_id": ids["ALPHA"],
              "work_date": day.isoformat(), "quantity": "2"})
    assert line.status_code == 201, line.text
    # The frozen roster still contains both dates; canonical live lifecycle no
    # longer allows new input after this retroactive employment change.
    set_employment(tenant, tenant.driver_a, termination_date=day - timedelta(days=1), employment_status="Terminated")
    async with p3c_engine.connect() as db:
        args = (tenant.company_id, tenant.branch_a, period["payroll_period_id"], tenant.driver_a)
        await _assert_driver_eligible_for_workdate_via_snapshot(*args, day, db)
        for check_day, rescue in ((day, False), (other, True)):
            with pytest.raises(HTTPException) as rejected:
                await _assert_driver_eligible_for_workdate_via_snapshot(*args, check_day, db,
                    allow_existing_source_rescue=rescue)
            assert rejected.value.status_code == 422
    before = await _payroll_snapshot(p3c_engine, period["payroll_period_id"])
    data = (f"DriverNumber,WorkDate,Alpha,Beta\n{source['drivernumber']},{day},4,5\n"
            f"{source['drivernumber']},{other},4,\n").encode()
    created = await _upload(p3c_client, tenant, period, data)
    assert created.status_code == 200, created.text
    iid = created.json()["import_id"]
    response = await p3c_client.put(f"/payroll/imports/{iid}/mapping", headers=tenant.admin,
        json={"expected_revision": 1, "column_mapping": {"1": "DriverNumber", "2": "WorkDate",
              "3": f"Definition:{ids['ALPHA']}", "4": f"Definition:{ids['BETA']}"}})
    assert response.status_code == 200, response.text
    rows = await page(p3c_client, tenant, iid, response.json())
    assert [cell["outcome"] for cell in rows[0]["cells"]] == ["Conflict", "Invalid"]
    assert rows[0]["cells"][0]["current_value"]["quantity"] == "2"
    assert rows[1]["cells"][0]["outcome"] == "Invalid"
    issues = await p3c_client.get(f"/payroll/imports/{iid}/issues", headers=tenant.admin,
        params={"expected_revision": 2, "plan_fingerprint": response.json()["fingerprints"]["plan"]})
    assert issues.status_code == 200, issues.text
    codes = {item["code"] for item in issues.json()["items"]}
    assert {"IMPORT_NEW_SOURCE_NOT_ELIGIBLE", "IMPORT_ASSIGNMENT_UNRESOLVED"} <= codes
    assert await _payroll_snapshot(p3c_engine, period["payroll_period_id"]) == before


async def test_company_identity_precedes_period_roster_and_never_discloses_another_company(
    p3c_client, p3c_engine, tenant, p3b_dsn,
):
    period = await _period(p3c_client, p3c_engine, tenant)
    source = await _payroll_inputs(p3c_engine, tenant, period)
    manager = make_user(tenant, ["employees.manage"])["Authorization"].split()[1]
    other = build_payroll_tenant(p3b_dsn)
    foreign_manager = make_user(other, ["employees.manage"])["Authorization"].split()[1]
    # Workforce legitimately appends eligible new assignments to editable
    # periods. Use a future window to ensure this Driver is outside this roster;
    # the other local Driver has no assignment in the target Branch at all.
    after_period = date.fromisoformat(period["end_date"]) + timedelta(days=3)
    identities = [await create_workforce_driver(p3c_client, token, branch, label, f"IMP-{uuid4().hex[:10]}",
                  effective_from_at=f"{after_period}T00:00:00Z" if label == "Late Driver" else "2020-01-01T00:00:00Z")
                  for token, branch, label in ((manager, tenant.branch_b, "Other branch"),
                                               (manager, tenant.branch_a, "Late Driver"),
                                               (foreign_manager, other.branch_a, "Foreign Driver"))]
    async with p3c_engine.connect() as db:
        keys = [(await db.execute(text("SELECT employeekey FROM core.employees WHERE employeeid = :eid"),
                                  {"eid": item["employee_id"]})).scalar_one() for item in identities]
        assert await db.scalar(text("""SELECT count(*) FROM payroll.payrollperioddrivereligibility
            WHERE payrollperiodid = :pid AND driverassignmentid = ANY(CAST(:ids AS integer[]))"""),
            {"pid": period["payroll_period_id"], "ids": [item["driver_assignment_id"] for item in identities[:2]]}) == 0
    day = source["dates"][0]
    data = ("DriverNumber,EmployeeKey,WorkDate,Quantity\n" + "".join(
        f"{item['driver_number']},{key},{day},1\n" for item, key in zip(identities, keys, strict=True)) +
        f"{identities[0]['driver_number']},{keys[1]},{day},1\n").encode()
    uploaded = await _upload(p3c_client, tenant, period, data)
    assert uploaded.status_code == 200, uploaded.text
    iid = uploaded.json()["import_id"]
    target = await _definition_target(p3c_client, tenant, iid)
    before = await _payroll_snapshot(p3c_engine, period["payroll_period_id"])
    revision = 1
    for kinds in (("DriverNumber", "Ignore"), ("Ignore", "EmployeeKey"), ("DriverNumber", "EmployeeKey")):
        mapped = await p3c_client.put(f"/payroll/imports/{iid}/mapping", headers=tenant.admin,
            json={"expected_revision": revision, "column_mapping": {
                "1": kinds[0], "2": kinds[1], "3": "WorkDate", "4": target}})
        assert mapped.status_code == 200, mapped.text
        revision += 1
        rows = await page(p3c_client, tenant, iid, mapped.json())
        for index in (0, 1):
            assert {(item["resolution"], item["driver_id"]) for item in rows[index]["identifiers"]} == {
                ("Exact", identities[index]["driver_id"])}
            assert rows[index]["cells"][0]["driver_assignment_id"] is None
        assert {(item["resolution"], item["driver_id"]) for item in rows[2]["identifiers"]} == {("Unresolved", None)}
        issues = await p3c_client.get(f"/payroll/imports/{iid}/issues", headers=tenant.admin,
            params={"expected_revision": revision, "plan_fingerprint": mapped.json()["fingerprints"]["plan"]})
        assert issues.status_code == 200, issues.text
        by_row = {index: {item["code"] for item in issues.json()["items"] if item["source_row"] == index}
                  for index in range(2, 6)}
        assert by_row[2] == by_row[3] == {"IMPORT_ASSIGNMENT_UNRESOLVED"}
        assert by_row[4] == {"IMPORT_DRIVER_UNRESOLVED"}
        if "Ignore" not in kinds:
            assert by_row[5] == {"IMPORT_DRIVER_DISAGREEMENT"}
        refreshed = await p3c_client.post(f"/payroll/imports/{iid}/refresh", headers=tenant.admin,
                                         json={"expected_revision": revision})
        assert refreshed.status_code == 200, refreshed.text
        revision += 1
        assert refreshed.json()["fingerprints"] == mapped.json()["fingerprints"]
        assert await page(p3c_client, tenant, iid, refreshed.json()) == rows
    assert await _payroll_snapshot(p3c_engine, period["payroll_period_id"]) == before


async def test_missing_period_eligibility_marker_refuses_mapping_without_detail(
    p3c_client, p3c_engine, tenant,
):
    period = await _period(p3c_client, p3c_engine, tenant)
    source = await _payroll_inputs(p3c_engine, tenant, period)
    created = await _upload(p3c_client, tenant, period,
        f"DriverNumber,WorkDate,Quantity\n{source['drivernumber']},{source['dates'][0]},1\n".encode())
    assert created.status_code == 200, created.text
    iid = created.json()["import_id"]
    target = await _definition_target(p3c_client, tenant, iid)
    async with p3c_engine.begin() as db:
        await db.execute(text("DELETE FROM payroll.payrollperiodeligibilitysnapshots WHERE payrollperiodid = :pid"),
                         {"pid": period["payroll_period_id"]})
    result = await _map(p3c_client, tenant, iid, target)
    assert result.status_code == 409, result.text
    assert result.json()["detail"]["code"] == "ELIGIBILITY_SNAPSHOT_MISSING"
    async with p3c_engine.connect() as db:
        assert await db.scalar(text("SELECT revision FROM import.imports WHERE importid = :iid"), {"iid": iid}) == 1
        assert await db.scalar(text("SELECT count(*) FROM import.payrollworkrecords WHERE importid = :iid"), {"iid": iid}) == 0


async def test_one_driver_resolves_to_each_date_effective_home_assignment_without_gap_rescue(
    p3c_client, p3c_engine, tenant,
):
    await rated_definition(p3c_client, tenant, rate="25")
    switch, returned = setup_anchor() + timedelta(days=2), setup_anchor() + timedelta(days=3)
    params = {"cid": tenant.company_id, "aid": tenant.driver_a, "bid": tenant.branch_a,
              "other": tenant.branch_b, "switch": switch, "returned": returned}
    # Legal materialized A -> B -> A history, before creating this period.
    # The approved helper restores the exact history guard; no production
    # trigger is weakened, and no existing payroll fact is moved.
    async with p3c_engine.begin() as db:
        async with suspended_test_triggers(db, [DRIVER_ASSIGNMENT_HISTORY_TRIGGER]):
            await db.execute(text("""
                UPDATE core.driverassignments SET effectivetoat = core.fn_LocalDateBoundary(
                    (SELECT timezoneid FROM core.branches WHERE branchid = :bid), :switch), endreason = 'Transferred'
                WHERE companyid = :cid AND driverassignmentid = :aid
            """), params)
            middle = await db.scalar(text("""
                INSERT INTO core.driverassignments(companyid, branchid, employeeid, driverid,
                    effectivefromat, effectivetoat, endreason, transferredfromassignmentid)
                SELECT a.companyid, :other, employeeid, driverid,
                    core.fn_LocalDateBoundary(b.timezoneid, :switch), core.fn_LocalDateBoundary(b.timezoneid, :returned),
                    'Transferred', :aid FROM core.driverassignments a
                JOIN core.branches b ON b.branchid = :bid AND b.companyid = a.companyid
                WHERE a.driverassignmentid = :aid RETURNING driverassignmentid
            """), params)
            params["middle"] = middle
            destination = await db.scalar(text("""
                INSERT INTO core.driverassignments(companyid, branchid, employeeid, driverid,
                    effectivefromat, transferredfromassignmentid)
                SELECT a.companyid, :bid, employeeid, driverid,
                    core.fn_LocalDateBoundary(b.timezoneid, :returned), :middle FROM core.driverassignments a
                JOIN core.branches b ON b.branchid = :bid AND b.companyid = a.companyid
                WHERE a.driverassignmentid = :aid RETURNING driverassignmentid
            """), params)
            await db.execute(text("UPDATE core.driverassignments SET transferredtoassignmentid = :middle WHERE driverassignmentid = :aid"), params)
            await db.execute(text("UPDATE core.driverassignments SET transferredtoassignmentid = :destination WHERE driverassignmentid = :middle"),
                             {**params, "destination": destination})
    await assign_weekly_setup(p3c_engine, tenant, tenant.branch_a)
    period = await create_period(p3c_client, tenant, tenant.branch_a)
    source = await _payroll_inputs(p3c_engine, tenant, period)
    start = date.fromisoformat(period["start_date"])
    data = (f"DriverNumber,WorkDate,Quantity\n{source['drivernumber']},{start},1\n"
            f"{source['drivernumber']},{returned},2\n{source['drivernumber']},{switch},3\n").encode()
    created = await _upload(p3c_client, tenant, period, data)
    assert created.status_code == 200, created.text
    iid = created.json()["import_id"]
    target = await _definition_target(p3c_client, tenant, iid)
    mapped = await _map(p3c_client, tenant, iid, target)
    assert mapped.status_code == 200, mapped.text
    rows = await page(p3c_client, tenant, iid, mapped.json())
    assert [row["cells"][0]["driver_assignment_id"] for row in rows] == [tenant.driver_a, destination, None]
    assert [row["cells"][0]["outcome"] for row in rows] == ["New", "New", "Invalid"]
    async with p3c_engine.connect() as db:
        summary = await get_period_by_id(tenant.company_id, tenant.owner, period["payroll_period_id"], db)
        context = await load_context(db, tenant.company_id, summary)
        ids = [entry["driverassignmentid"] for entry in context["roster"]]
        matrix = await lifecycle_eligible_assignment_days(period["payroll_period_id"], tenant.company_id,
                                                         tenant.branch_a, None, ids, db)
        expanded_matrix = await lifecycle_eligible_assignment_days(
            period["payroll_period_id"], tenant.company_id, tenant.branch_a, None,
            ids + list(range(1_000_000_000, 1_000_040_000)), db,
        )
        assert expanded_matrix == matrix
        for day in (start, switch, returned):
            canonical_ids = await _lifecycle_eligible_assignment_ids(period["payroll_period_id"], tenant.company_id,
                                                                    tenant.branch_a, day, ids, db)
            assert canonical_ids == {aid for aid, eligible_day in matrix if day == eligible_day}
        assert middle not in ids
