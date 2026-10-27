from __future__ import annotations

from io import BytesIO
from uuid import uuid4

import pytest
from openpyxl import Workbook
from sqlalchemy import text

from tests.calculation_snapshot_helpers import finalize, standard_scenario, submit_and_approve
from tests.compensation_api_fixtures import (  # noqa: F401 - register isolated API fixtures
    p3c_application,
    p3c_database_engine,
    p3c_http_client,
)
from tests.compensation_database_fixtures import p3b_database  # noqa: F401
from tests.payroll_runtime_fixtures import (
    add_driver,
    build_payroll_tenant,
    open_period_with_grid,
    rated_definition,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture(name="tenant")
def payroll_tenant(p3b_dsn):
    return build_payroll_tenant(p3b_dsn)


def _multipart(files: list[tuple[str, bytes]], boundary: str = "import-batch") -> bytes:
    chunks = []
    for filename, data in files:
        chunks.extend((
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{filename}\"\r\n"
            "Content-Type: application/octet-stream\r\n\r\n".encode(),
            data,
            b"\r\n",
        ))
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks)


async def _upload(client, tenant, period, files):
    return await client.post(
        f"/payroll/periods/{period['payroll_period_id']}/imports",
        content=_multipart(files),
        headers={**tenant.admin, "Content-Type": "multipart/form-data; boundary=import-batch",
                 "Idempotency-Key": str(uuid4())},
    )


async def _eligible_driver_date(engine, tenant, period):
    async with engine.connect() as db:
        row = (await db.execute(text("""
            SELECT d.driverid, d.drivernumber, e.employeekey, e.fullname,
                p.drivernamesnapshot AS period_driver_name, x.workdate
            FROM payroll.payrollperioddrivereligibility p
            JOIN core.driverassignments a ON a.driverassignmentid=p.driverassignmentid
                AND a.companyid=p.companyid AND a.branchid=p.branchid
            JOIN core.drivers d ON d.driverid=a.driverid AND d.companyid=a.companyid
            JOIN core.employees e ON e.employeeid=a.employeeid AND e.companyid=a.companyid
            CROSS JOIN LATERAL unnest(p.eligibleworkdates) AS x(workdate)
            WHERE p.companyid=:cid AND p.payrollperiodid=:pid AND p.branchid=:bid
                AND p.iseligibleforperiod
            ORDER BY d.driverid, x.workdate LIMIT 1
        """), {"cid": tenant.company_id, "pid": period["payroll_period_id"],
               "bid": period["branch_id"]})).mappings().one()
        return dict(row)


async def _definition(engine, tenant, period):
    from app.imports.mapping import targets as mapping_targets
    from app.payroll.period_definitions import list_period_definitions

    async with engine.connect() as db:
        definitions = await list_period_definitions(period["payroll_period_id"], tenant.company_id, db, active_only=True)
    candidate = next(item for item in mapping_targets(definitions) if item["target"].startswith("Definition:"))
    return int(candidate["target"].split(":", 1)[1]), candidate["code"]


async def _map_and_preview(client, tenant, created, definition_id):
    import_id = created["import_id"]
    layout = created["layouts"][0]
    response = await client.put(
        f"/payroll/imports/{import_id}/layouts/{layout['layout_id']}/mapping",
        json={"expected_revision": created["revision"], "column_mapping": {
            "1": "DriverNumber", "2": "WorkDate", "3": f"Definition:{definition_id}"}},
        headers=tenant.admin,
    )
    assert response.status_code == 200, response.text
    preview = await client.post(
        f"/payroll/imports/{import_id}/preview",
        json={"expected_revision": response.json()["revision"]}, headers=tenant.admin,
    )
    assert preview.status_code == 200, preview.text
    return preview.json()


async def _map_custom_and_preview(client, tenant, created, mapping):
    layout_id = created["layouts"][0]["layout_id"]
    mapped = await client.put(f"/payroll/imports/{created['import_id']}/layouts/{layout_id}/mapping",
        json={"expected_revision": created["revision"], "column_mapping": mapping}, headers=tenant.admin)
    assert mapped.status_code == 200, mapped.text
    preview = await client.post(f"/payroll/imports/{created['import_id']}/preview",
        json={"expected_revision": mapped.json()["revision"]}, headers=tenant.admin)
    assert preview.status_code == 200, preview.text
    return preview.json()


async def test_ordered_csv_batch_shares_layout_and_preserves_physical_row_ownership(
    p3c_client, p3c_app, p3c_engine, tenant, monkeypatch,
):
    period, _grid = await open_period_with_grid(p3c_client, p3c_engine, tenant)
    driver = await _eligible_driver_date(p3c_engine, tenant, period)
    definition_id, definition_code = await _definition(p3c_engine, tenant, period)
    first_date = driver["workdate"]
    async with p3c_engine.connect() as db:
        second_date = (await db.execute(text("""
            SELECT workdate FROM payroll.payrollperioddays
            WHERE companyid=:cid AND payrollperiodid=:pid AND workdate>:first
            ORDER BY workdate LIMIT 1
        """), {"cid": tenant.company_id, "pid": period["payroll_period_id"],
               "first": first_date})).scalar_one_or_none()
    assert second_date is not None
    long_header = "Driver number " + "x" * 100
    header = f"{long_header},WorkDate,{definition_code}\n"
    first = f"{driver['drivernumber']},{first_date.isoformat()},4\n".encode()
    second = f"{driver['drivernumber']},{second_date.isoformat()},5\n".encode()
    async with p3c_engine.connect() as db:
        canonical_before = (await db.execute(text("""
            SELECT p.status::text,
                (SELECT count(*) FROM payroll.payrolldraftlines WHERE payrollperiodid=p.payrollperiodid),
                (SELECT count(*) FROM audit.auditlog WHERE companyid=p.companyid AND branchid=p.branchid),
                (SELECT count(*) FROM payroll.payrollperiodauditevidenceevents WHERE payrollperiodid=p.payrollperiodid)
            FROM payroll.payrollperiods p WHERE p.payrollperiodid=:pid
        """), {"pid": period["payroll_period_id"]})).one()
    uploaded = await _upload(p3c_client, tenant, period, [("daily.csv", header.encode() + first),
                                                          ("daily.csv", header.encode() + second)])
    assert uploaded.status_code == 200, uploaded.text
    created = uploaded.json()
    assert [item["ordinal"] for item in created["files"]] == [1, 2]
    assert [item["name"] for item in created["files"]] == ["daily.csv", "daily.csv"]
    assert len(created["layouts"]) == 1
    units = [item["units"][0] for item in created["files"]]
    assert len({unit["source_unit_id"] for unit in units}) == 2
    assert all(unit["selected"] and unit["layout_id"] == created["layouts"][0]["layout_id"] for unit in units)
    layout_summary = created["layouts"][0]["inspection"]
    assert layout_summary["header_cells"][0] == long_header[:80]
    assert layout_summary["header_count"] == 3
    assert layout_summary["header_cells_truncated"] is True

    header_page = await p3c_client.get(
        f"/payroll/imports/{created['import_id']}?header_layout_id={created['layouts'][0]['layout_id']}"
        "&header_offset=0&header_limit=1", headers=tenant.admin,
    )
    assert header_page.status_code == 200, header_page.text
    assert header_page.json()["header_page"]["items"] == [{"source_column": 1, "label": long_header}]
    assert header_page.json()["header_page"]["next_offset"] == 1

    first_targets = await p3c_client.get(
        f"/payroll/imports/{created['import_id']}?target_limit=1", headers=tenant.admin,
    )
    assert first_targets.status_code == 200, first_targets.text
    assert len(first_targets.json()["mapping_targets"]) == 1
    page = first_targets.json()["mapping_target_page"]
    assert page["offset"] == 0 and page["limit"] == 1
    assert page["total"] >= 1
    if page["total"] > 1:
        assert page["next_offset"] == 1
        second_targets = await p3c_client.get(
            f"/payroll/imports/{created['import_id']}?target_offset=1&target_limit=1", headers=tenant.admin,
        )
        assert second_targets.status_code == 200, second_targets.text
        assert second_targets.json()["mapping_targets"] != first_targets.json()["mapping_targets"]
    invalid_target_page = await p3c_client.get(
        f"/payroll/imports/{created['import_id']}?target_limit=101", headers=tenant.admin,
    )
    assert invalid_target_page.status_code == 422

    refreshed = await _map_and_preview(p3c_client, tenant, created, definition_id)
    assert refreshed["status"] == "NeedsReview"
    assert refreshed["preview"]["counts"]["rows"] == 2
    assert refreshed["preview"]["counts"]["new"] == 2
    assert refreshed["preview"]["plan_fingerprint"]
    plan = refreshed["preview"]["plan_fingerprint"]
    query = f"expected_revision={refreshed['revision']}&plan_fingerprint={plan}"
    settings = p3c_app.state.settings
    monkeypatch.setattr(settings, "IMPORT_ROW_PAGE_DEFAULT", 1)
    monkeypatch.setattr(settings, "IMPORT_ROW_PAGE_MAX", 2)
    monkeypatch.setattr(settings, "IMPORT_ISSUE_PAGE_DEFAULT", 1)
    monkeypatch.setattr(settings, "IMPORT_ISSUE_PAGE_MAX", 2)
    monkeypatch.setattr(settings, "IMPORT_SOURCE_PAGE_DEFAULT", 1)
    monkeypatch.setattr(settings, "IMPORT_SOURCE_PAGE_MAX", 2)
    monkeypatch.setattr(settings, "IMPORT_SOURCE_CELL_PAGE_DEFAULT", 2)
    monkeypatch.setattr(settings, "IMPORT_SOURCE_CELL_PAGE_MAX", 2)
    rows = await p3c_client.get(f"/payroll/imports/{created['import_id']}/rows?{query}", headers=tenant.admin)
    issues = await p3c_client.get(f"/payroll/imports/{created['import_id']}/issues?{query}", headers=tenant.admin)
    source_rows = await p3c_client.get(
        f"/payroll/imports/{created['import_id']}/source-records?{query}", headers=tenant.admin,
    )
    assert rows.status_code == 200, rows.text
    assert rows.json()["filtered_count"] == 2
    assert rows.json()["limit"] == 1
    assert len(rows.json()["items"]) == 1
    assert rows.json()["next_offset"] == 1
    raised_rows = await p3c_client.get(
        f"/payroll/imports/{created['import_id']}/rows?{query}&limit=2", headers=tenant.admin,
    )
    over_limit_rows = await p3c_client.get(
        f"/payroll/imports/{created['import_id']}/rows?{query}&limit=3", headers=tenant.admin,
    )
    assert raised_rows.status_code == 200 and raised_rows.json()["limit"] == 2
    assert len(raised_rows.json()["items"]) == 2
    assert over_limit_rows.status_code == 422
    assert over_limit_rows.json()["detail"]["code"] == "IMPORT_PAGE_INVALID"
    invalid_severity = await p3c_client.get(
        f"/payroll/imports/{created['import_id']}/rows?{query}&severity=Unexpected", headers=tenant.admin,
    )
    assert invalid_severity.status_code == 422
    first_value = next(iter(rows.json()["items"][0]["values"].values()))
    assert first_value["candidate_count"] == 1
    assert len(first_value["candidates"]) == 1
    assert "raw_value" not in first_value["candidates"][0]
    assert rows.json()["items"][0]["source_record_count"] == 1
    assert rows.json()["items"][0]["source_records_sampled"] is False
    assert issues.status_code == 200, issues.text
    assert issues.json()["filtered_count"] == 0
    assert issues.json()["limit"] == 1
    raised_issue_page = await p3c_client.get(
        f"/payroll/imports/{created['import_id']}/issues?{query}&limit=2", headers=tenant.admin,
    )
    over_limit_issue_page = await p3c_client.get(
        f"/payroll/imports/{created['import_id']}/issues?{query}&limit=3", headers=tenant.admin,
    )
    assert raised_issue_page.status_code == 200 and raised_issue_page.json()["limit"] == 2
    assert over_limit_issue_page.status_code == 422
    assert source_rows.status_code == 200, source_rows.text
    assert source_rows.json()["filtered_count"] == 2
    assert source_rows.json()["limit"] == 1
    assert len(source_rows.json()["items"]) == 1
    assert source_rows.json()["next_offset"] == 1
    assert source_rows.json()["items"][0]["cell_count"] == 3
    assert source_rows.json()["items"][0]["cell_limit"] == 2
    assert len(source_rows.json()["items"][0]["cells"]) == 2
    assert source_rows.json()["items"][0]["values_count"] == 1
    assert source_rows.json()["items"][0]["values_json"] == {}
    assert source_rows.json()["items"][0]["next_cell_offset"] == 2
    raised_source_page = await p3c_client.get(
        f"/payroll/imports/{created['import_id']}/source-records?{query}&limit=2&cell_limit=2",
        headers=tenant.admin,
    )
    over_limit_source_page = await p3c_client.get(
        f"/payroll/imports/{created['import_id']}/source-records?{query}&limit=3", headers=tenant.admin,
    )
    over_limit_cell_page = await p3c_client.get(
        f"/payroll/imports/{created['import_id']}/source-records?{query}&cell_limit=3",
        headers=tenant.admin,
    )
    assert raised_source_page.status_code == 200 and raised_source_page.json()["limit"] == 2
    assert len(raised_source_page.json()["items"]) == 2
    assert over_limit_source_page.status_code == 422
    assert over_limit_cell_page.status_code == 422
    second_cell_page = await p3c_client.get(
        f"/payroll/imports/{created['import_id']}/source-records?{query}&cell_offset=1&cell_limit=1",
        headers=tenant.admin,
    )
    assert second_cell_page.status_code == 200, second_cell_page.text
    assert len(second_cell_page.json()["items"][0]["cells"]) == 1
    assert second_cell_page.json()["items"][0]["next_cell_offset"] == 2
    third_cell_page = await p3c_client.get(
        f"/payroll/imports/{created['import_id']}/source-records?{query}&cell_offset=2&cell_limit=1",
        headers=tenant.admin,
    )
    assert third_cell_page.status_code == 200, third_cell_page.text
    assert third_cell_page.json()["items"][0]["next_cell_offset"] is None
    assert len(third_cell_page.json()["items"][0]["values_json"]) == 1
    assert next(iter(third_cell_page.json()["items"][0]["values_json"].values()))["source_column"] == 3
    async with p3c_engine.connect() as db:
        physical = (await db.execute(text("""
            SELECT f.fileordinal, fr.sourcerownumber, fr.sourceunitid
            FROM import.filesourcerecords fr
            JOIN import.filesourceunits u ON u.sourceunitid=fr.sourceunitid
            JOIN import.filesources f ON f.filesourceid=u.filesourceid
            WHERE fr.importid=:iid ORDER BY f.fileordinal
        """), {"iid": created["import_id"]})).all()
        normalized = await db.scalar(text("""
            SELECT count(*) FROM import.payrollworkrecords WHERE importid=:iid AND companyid=:cid
        """), {"iid": created["import_id"], "cid": tenant.company_id})
        legacy_cells = await db.scalar(text("""
            SELECT to_regclass('import.payrollworkcells') IS NOT NULL
        """))
    assert [(row.fileordinal, row.sourcerownumber) for row in physical] == [(1, 2), (2, 2)]
    assert physical[0].sourceunitid != physical[1].sourceunitid
    assert normalized == 2
    assert legacy_cells is False
    async with p3c_engine.connect() as db:
        canonical_after = (await db.execute(text("""
            SELECT p.status::text,
                (SELECT count(*) FROM payroll.payrolldraftlines WHERE payrollperiodid=p.payrollperiodid),
                (SELECT count(*) FROM audit.auditlog WHERE companyid=p.companyid AND branchid=p.branchid),
                (SELECT count(*) FROM payroll.payrollperiodauditevidenceevents WHERE payrollperiodid=p.payrollperiodid)
            FROM payroll.payrollperiods p WHERE p.payrollperiodid=:pid
        """), {"pid": period["payroll_period_id"]})).one()
    assert canonical_after == canonical_before


async def test_record_issues_are_read_from_bounded_per_record_json_not_duplicated(
    p3c_client, p3c_engine, tenant,
):
    period, _grid = await open_period_with_grid(p3c_client, p3c_engine, tenant)
    driver = await _eligible_driver_date(p3c_engine, tenant, period)
    definition_id, definition_code = await _definition(p3c_engine, tenant, period)
    data = f"DriverNumber,WorkDate,{definition_code}\nunknown-driver,{driver['workdate'].isoformat()},not-a-quantity\n".encode()
    uploaded = await _upload(p3c_client, tenant, period, [("invalid.csv", data)])
    assert uploaded.status_code == 200, uploaded.text
    created = uploaded.json()
    preview = await _map_and_preview(p3c_client, tenant, created, definition_id)
    assert preview["status"] == "NeedsReview"
    query = f"expected_revision={preview['revision']}&plan_fingerprint={preview['preview']['plan_fingerprint']}"
    issues = await p3c_client.get(f"/payroll/imports/{created['import_id']}/issues?{query}&severity=Error",
                                  headers=tenant.admin)
    assert issues.status_code == 200, issues.text
    assert issues.json()["filtered_count"] >= 1
    assert all(item["severity"] == "Error" for item in issues.json()["items"])
    quantity_issue = next(item for item in issues.json()["items"] if item["code"] == "IMPORT_QUANTITY_INVALID")
    assert quantity_issue["value"] == "not-a-quantity"
    source_rows = await p3c_client.get(
        f"/payroll/imports/{created['import_id']}/source-records?{query}&cell_offset=2&cell_limit=1",
        headers=tenant.admin,
    )
    assert source_rows.status_code == 200, source_rows.text
    source_row = source_rows.json()["items"][0]
    assert source_row["issue_count"] > len(source_row["issues"])
    assert any(issue["code"] == "IMPORT_QUANTITY_INVALID" for issue in source_row["issues"])
    first_source_page = await p3c_client.get(
        f"/payroll/imports/{created['import_id']}/source-records?{query}&cell_offset=0&cell_limit=1",
        headers=tenant.admin,
    )
    assert first_source_page.status_code == 200, first_source_page.text
    first_source_row = first_source_page.json()["items"][0]
    all_source_issues = {(issue["code"], issue.get("column")) for issue in source_row["issues"]}
    all_source_issues.update((issue["code"], issue.get("column")) for issue in first_source_row["issues"])
    assert len(all_source_issues) == source_row["issue_count"]
    async with p3c_engine.connect() as db:
        relational_issue_count = await db.scalar(text(
            "SELECT count(*) FROM import.importissues WHERE importid=:iid"), {"iid": created["import_id"]})
        record_issues = await db.scalar(text(
            "SELECT count(*) FROM import.payrollworkrecords r WHERE r.importid=:iid AND jsonb_array_length(r.issuesjson)>0"),
            {"iid": created["import_id"]})
    assert relational_issue_count == 0
    assert record_issues == 1


async def test_grid_keeps_all_definitions_when_more_than_five_layouts_share_one_driver_day(
    p3c_client, p3c_engine, tenant,
):
    for index in range(5):
        await rated_definition(p3c_client, tenant, definition_code=f"BATCH_ITEM_{index + 1}")
    period, _grid = await open_period_with_grid(p3c_client, p3c_engine, tenant)
    driver = await _eligible_driver_date(p3c_engine, tenant, period)
    from app.imports.mapping import targets as mapping_targets
    from app.payroll.period_definitions import list_period_definitions

    async with p3c_engine.connect() as db:
        definitions = await list_period_definitions(period["payroll_period_id"], tenant.company_id, db, active_only=True)
    targets = [item for item in mapping_targets(definitions) if item["target"].startswith("Definition:")]
    assert len(targets) >= 6
    data_files = []
    for index, definition in enumerate(targets[:6], start=1):
        data = (f"DriverNumber,WorkDate,{definition['code']}\n"
                f"{driver['drivernumber']},{driver['workdate'].isoformat()},{index}\n").encode()
        data_files.append((f"part-{index}.csv", data))
    uploaded = await _upload(p3c_client, tenant, period, data_files)
    assert uploaded.status_code == 200, uploaded.text
    created = uploaded.json()
    assert len(created["layouts"]) == 6
    revision = created["revision"]
    for layout in created["layouts"]:
        # Each layout's immutable header sample identifies its one quantity target.
        header = layout["inspection"]["header_cells"][2]
        target = next(item for item in targets if item["code"] == header)
        mapped = await p3c_client.put(
            f"/payroll/imports/{created['import_id']}/layouts/{layout['layout_id']}/mapping",
            json={"expected_revision": revision, "column_mapping": {
                "1": "DriverNumber", "2": "WorkDate", "3": target["target"]}},
            headers=tenant.admin,
        )
        assert mapped.status_code == 200, mapped.text
        revision = mapped.json()["revision"]
    preview = await p3c_client.post(f"/payroll/imports/{created['import_id']}/preview",
        json={"expected_revision": revision}, headers=tenant.admin)
    assert preview.status_code == 200, preview.text
    query = f"expected_revision={preview.json()['revision']}&plan_fingerprint={preview.json()['preview']['plan_fingerprint']}"
    rows = await p3c_client.get(f"/payroll/imports/{created['import_id']}/rows?{query}", headers=tenant.admin)
    assert rows.status_code == 200, rows.text
    assert rows.json()["filtered_count"] == 1
    grid = rows.json()["items"][0]
    assert grid["source_record_count"] == 6
    assert grid["source_records_sampled"] is True
    assert len(grid["values"]) == 6
    assert {value["candidate_count"] for value in grid["values"].values()} == {1}
    assert all(len(value["candidates"]) == 1 for value in grid["values"].values())


async def test_company_identity_resolves_before_period_roster_and_rejects_foreign_identity(
    p3c_client, p3c_engine, tenant, p3b_dsn,
):
    period, _grid = await open_period_with_grid(p3c_client, p3c_engine, tenant)
    late_assignment_id = add_driver(tenant)
    foreign_tenant = build_payroll_tenant(p3b_dsn)
    async with p3c_engine.connect() as db:
        late = (await db.execute(text("""
            SELECT d.driverid, d.drivernumber, e.employeekey
            FROM core.driverassignments a JOIN core.drivers d ON d.driverid=a.driverid
                AND d.companyid=a.companyid
            JOIN core.employees e ON e.employeeid=a.employeeid AND e.companyid=a.companyid
            WHERE a.companyid=:cid AND a.driverassignmentid=:aid
        """), {"cid": tenant.company_id, "aid": late_assignment_id})).one()
        foreign_number = await db.scalar(text(
            "SELECT drivernumber FROM core.drivers WHERE companyid=:cid AND driverid=:did"),
            {"cid": foreign_tenant.company_id, "did": foreign_tenant.driver_a_id})
    known = await _eligible_driver_date(p3c_engine, tenant, period)
    definition_id, definition_code = await _definition(p3c_engine, tenant, period)
    data = (f"DriverNumber,WorkDate,{definition_code}\n"
            f"{late.drivernumber},{known['workdate'].isoformat()},1\n"
            f"{foreign_number},{known['workdate'].isoformat()},2\n").encode()
    uploaded = await _upload(p3c_client, tenant, period, [("identity.csv", data)])
    assert uploaded.status_code == 200, uploaded.text
    created = uploaded.json()
    preview = await _map_and_preview(p3c_client, tenant, created, definition_id)
    query = f"expected_revision={preview['revision']}&plan_fingerprint={preview['preview']['plan_fingerprint']}"
    rows = await p3c_client.get(f"/payroll/imports/{created['import_id']}/rows?{query}", headers=tenant.admin)
    issues = await p3c_client.get(f"/payroll/imports/{created['import_id']}/issues?{query}", headers=tenant.admin)
    assert rows.status_code == issues.status_code == 200
    late_group = next(item for item in rows.json()["items"] if item["driver_id"] == late.driverid)
    unknown_group = next(item for item in rows.json()["items"] if item["driver_id"] is None)
    assert late_group["error_count"] >= 1
    assert unknown_group["error_count"] >= 1
    assert any(issue["code"] == "IMPORT_ASSIGNMENT_UNRESOLVED" for issue in issues.json()["items"])
    assert any(issue["code"] == "IMPORT_DRIVER_UNRESOLVED" for issue in issues.json()["items"])
    async with p3c_engine.connect() as db:
        resolutions = (await db.execute(text("""
            SELECT m.normalizedvalue, m.driverid, m.resolution
            FROM import.payrollworkdrivermatches m WHERE m.importid=:iid AND m.companyid=:cid
            ORDER BY m.normalizedvalue
        """), {"iid": created["import_id"], "cid": tenant.company_id})).all()
    assert (late.drivernumber, late.driverid, "Exact") in resolutions
    assert (foreign_number, None, "Unresolved") in resolutions


async def test_matching_stable_ids_disagreeing_ids_and_name_proposals_are_distinct(
    p3c_client, p3c_engine, tenant,
):
    proposal_assignment_id = add_driver(tenant)
    async with p3c_engine.begin() as db:
        await db.execute(text("""
            UPDATE core.employees e SET fullname='Unique Proposal Driver'
            FROM core.driverassignments a
            WHERE a.driverassignmentid=:aid AND a.companyid=:cid AND a.employeeid=e.employeeid
        """), {"aid": proposal_assignment_id, "cid": tenant.company_id})
    period, _grid = await open_period_with_grid(p3c_client, p3c_engine, tenant)
    driver = await _eligible_driver_date(p3c_engine, tenant, period)
    async with p3c_engine.connect() as db:
        dates = (await db.execute(text("""
            SELECT x.workdate FROM payroll.payrollperioddrivereligibility p
            CROSS JOIN LATERAL unnest(p.eligibleworkdates) AS x(workdate)
            JOIN core.driverassignments a ON a.driverassignmentid=p.driverassignmentid
                AND a.companyid=p.companyid AND a.branchid=p.branchid
            JOIN core.drivers d ON d.driverid=a.driverid AND d.companyid=a.companyid
            WHERE p.companyid=:cid AND p.payrollperiodid=:pid AND d.drivernumber=:number
                AND p.iseligibleforperiod ORDER BY x.workdate LIMIT 2
        """), {"cid": tenant.company_id, "pid": period["payroll_period_id"],
               "number": driver["drivernumber"]})).scalars().all()
    assert len(dates) == 2
    async with p3c_engine.connect() as db:
        other = (await db.execute(text("""
            SELECT d.driverid, d.drivernumber, p.drivernamesnapshot AS drivername FROM core.driverassignments a
            JOIN payroll.payrollperioddrivereligibility p ON p.driverassignmentid=a.driverassignmentid
                AND p.companyid=a.companyid AND p.branchid=a.branchid
            JOIN core.drivers d ON d.driverid=a.driverid AND d.companyid=a.companyid
            WHERE a.companyid=:cid AND a.driverassignmentid=:aid AND p.payrollperiodid=:pid
        """), {"cid": tenant.company_id, "aid": proposal_assignment_id,
                "pid": period["payroll_period_id"]})).one()
    assert other.drivername
    definition_id, definition_code = await _definition(p3c_engine, tenant, period)
    data = (f"DriverNumber,EmployeeKey,DriverName,WorkDate,{definition_code}\n"
            f"{driver['drivernumber']},,,{dates[0].isoformat()},1\n"
            f"{driver['drivernumber']},,{other.drivername},{dates[0].isoformat()},2\n"
            f",,{other.drivername},{dates[1].isoformat()},3\n").encode()
    uploaded = await _upload(p3c_client, tenant, period, [("identities.csv", data)])
    assert uploaded.status_code == 200, uploaded.text
    created = uploaded.json()
    preview = await _map_custom_and_preview(p3c_client, tenant, created, {
        "1": "DriverNumber", "2": "EmployeeKey", "3": "DriverName",
        "4": "WorkDate", "5": f"Definition:{definition_id}",
    })
    query = f"expected_revision={preview['revision']}&plan_fingerprint={preview['preview']['plan_fingerprint']}"
    rows = await p3c_client.get(f"/payroll/imports/{created['import_id']}/rows?{query}&limit=100", headers=tenant.admin)
    issues = await p3c_client.get(f"/payroll/imports/{created['import_id']}/issues?{query}&limit=200", headers=tenant.admin)
    assert rows.status_code == issues.status_code == 200
    exact = next(item for item in rows.json()["items"] if item["work_date"] == dates[0].isoformat()
                 and item["driver_id"] == driver["driverid"])
    proposal = next(item for item in rows.json()["items"] if item["work_date"] == dates[1].isoformat())
    assert exact["error_count"] == 0
    assert next(iter(exact["values"].values()))["candidates"][0]["outcome"] == "New"
    assert proposal["driver_id"] == other.driverid
    assert proposal["conflict_count"] >= 1
    assert any(issue["code"] == "IMPORT_DRIVER_DISAGREEMENT" for issue in issues.json()["items"])
    assert any(issue["code"] == "IMPORT_DRIVER_CONFIRMATION_REQUIRED" for issue in issues.json()["items"])


async def test_import_default_work_date_refreshes_every_tagged_unit_and_clearing_blocks_preview(
    p3c_client, p3c_engine, tenant,
):
    period, _grid = await open_period_with_grid(p3c_client, p3c_engine, tenant)
    driver = await _eligible_driver_date(p3c_engine, tenant, period)
    definition_id, definition_code = await _definition(p3c_engine, tenant, period)
    async with p3c_engine.connect() as db:
        next_date = await db.scalar(text("""
            SELECT workdate FROM payroll.payrollperioddays
            WHERE companyid=:cid AND payrollperiodid=:pid AND workdate<>:current
            ORDER BY workdate LIMIT 1
        """), {"cid": tenant.company_id, "pid": period["payroll_period_id"],
               "current": driver["workdate"]})
    assert next_date is not None

    header = f"DriverNumber,{definition_code}\n"
    uploaded = await _upload(p3c_client, tenant, period, [
        ("left.csv", (header + f"{driver['drivernumber']},1\n").encode()),
        ("right.csv", (header + f"{driver['drivernumber']},2\n").encode()),
    ])
    assert uploaded.status_code == 200, uploaded.text
    created = uploaded.json()
    source_units = [unit for file in created["files"] for unit in file["units"]]
    assert len(source_units) == 2

    interpreted = await p3c_client.put(
        f"/payroll/imports/{created['import_id']}/interpretation",
        json={"expected_revision": created["revision"], "default_work_date": driver["workdate"].isoformat(),
              "units": [{"source_unit_id": unit["source_unit_id"], "selected": True,
                         "date_source": {"kind": "import_fixed"}} for unit in source_units]},
        headers=tenant.admin,
    )
    assert interpreted.status_code == 200, interpreted.text
    interpreted_json = interpreted.json()
    assert interpreted_json["default_work_date"] == driver["workdate"].isoformat()
    assert all(unit["date_source"] == {"kind": "import_fixed"}
               for file in interpreted_json["files"] for unit in file["units"])

    layout = interpreted_json["layouts"][0]
    mapped = await p3c_client.put(
        f"/payroll/imports/{created['import_id']}/layouts/{layout['layout_id']}/mapping",
        json={"expected_revision": interpreted_json["revision"], "column_mapping": {
            "1": "DriverNumber", "2": f"Definition:{definition_id}"}},
        headers=tenant.admin,
    )
    assert mapped.status_code == 200, mapped.text
    preview = await p3c_client.post(
        f"/payroll/imports/{created['import_id']}/preview",
        json={"expected_revision": mapped.json()["revision"]}, headers=tenant.admin,
    )
    assert preview.status_code == 200, preview.text
    first_preview = preview.json()
    query = f"expected_revision={first_preview['revision']}&plan_fingerprint={first_preview['preview']['plan_fingerprint']}"
    first_rows = await p3c_client.get(f"/payroll/imports/{created['import_id']}/rows?{query}", headers=tenant.admin)
    assert first_rows.status_code == 200, first_rows.text
    assert first_rows.json()["items"][0]["work_date"] == driver["workdate"].isoformat()
    assert first_rows.json()["items"][0]["source_record_count"] == 2

    changed_default = await p3c_client.put(
        f"/payroll/imports/{created['import_id']}/interpretation",
        json={"expected_revision": first_preview["revision"], "default_work_date": next_date.isoformat()},
        headers=tenant.admin,
    )
    assert changed_default.status_code == 200, changed_default.text
    changed_json = changed_default.json()
    assert all(unit["date_source"] == {"kind": "import_fixed"}
               for file in changed_json["files"] for unit in file["units"])
    refreshed = await p3c_client.post(
        f"/payroll/imports/{created['import_id']}/preview",
        json={"expected_revision": changed_json["revision"]}, headers=tenant.admin,
    )
    assert refreshed.status_code == 200, refreshed.text
    refreshed_json = refreshed.json()
    query = f"expected_revision={refreshed_json['revision']}&plan_fingerprint={refreshed_json['preview']['plan_fingerprint']}"
    refreshed_rows = await p3c_client.get(f"/payroll/imports/{created['import_id']}/rows?{query}",
                                          headers=tenant.admin)
    assert refreshed_rows.status_code == 200, refreshed_rows.text
    assert refreshed_rows.json()["items"][0]["work_date"] == next_date.isoformat()

    cleared = await p3c_client.put(
        f"/payroll/imports/{created['import_id']}/interpretation",
        json={"expected_revision": refreshed_json["revision"], "default_work_date": None},
        headers=tenant.admin,
    )
    assert cleared.status_code == 200, cleared.text
    blocked = await p3c_client.post(
        f"/payroll/imports/{created['import_id']}/preview",
        json={"expected_revision": cleared.json()["revision"]}, headers=tenant.admin,
    )
    assert blocked.status_code == 422
    assert blocked.json()["detail"]["code"] == "IMPORT_DATE_SOURCE_REQUIRED"
    async with p3c_engine.connect() as db:
        published_rows = await db.scalar(text(
            "SELECT count(*) FROM import.payrollworkrecords WHERE importid=:iid"),
            {"iid": created["import_id"]})
    assert published_rows == 0


async def _assert_date_provider_survives_preview_refresh(client, engine, tenant, period, file_name, file_bytes,
                                                         unit_name, provider):
    driver = await _eligible_driver_date(engine, tenant, period)
    definition_id, definition_code = await _definition(engine, tenant, period)
    uploaded = await _upload(client, tenant, period, [(file_name, file_bytes)])
    assert uploaded.status_code == 200, uploaded.text
    created = uploaded.json()
    unit = created["files"][0]["units"][0]
    assert unit["name"] == unit_name

    interpreted = await client.put(
        f"/payroll/imports/{created['import_id']}/interpretation",
        json={"expected_revision": created["revision"], "units": [{
            "source_unit_id": unit["source_unit_id"], "selected": True, "date_source": provider,
        }]},
        headers=tenant.admin,
    )
    assert interpreted.status_code == 200, interpreted.text
    layout = interpreted.json()["layouts"][0]
    mapped = await client.put(
        f"/payroll/imports/{created['import_id']}/layouts/{layout['layout_id']}/mapping",
        json={"expected_revision": interpreted.json()["revision"], "column_mapping": {
            "1": "DriverNumber", "2": f"Definition:{definition_id}",
        }},
        headers=tenant.admin,
    )
    assert mapped.status_code == 200, mapped.text

    preview = await client.post(
        f"/payroll/imports/{created['import_id']}/preview",
        json={"expected_revision": mapped.json()["revision"]},
        headers=tenant.admin,
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["preview"]["counts"]["errors"] == 0
    expected_date = driver["workdate"].isoformat()
    query = (f"expected_revision={preview.json()['revision']}&"
             f"plan_fingerprint={preview.json()['preview']['plan_fingerprint']}")
    rows = await client.get(f"/payroll/imports/{created['import_id']}/rows?{query}", headers=tenant.admin)
    assert rows.status_code == 200, rows.text
    assert rows.json()["items"][0]["work_date"] == expected_date

    refreshed = await client.post(
        f"/payroll/imports/{created['import_id']}/preview",
        json={"expected_revision": preview.json()["revision"]},
        headers=tenant.admin,
    )
    assert refreshed.status_code == 200, refreshed.text
    refreshed_query = (f"expected_revision={refreshed.json()['revision']}&"
                       f"plan_fingerprint={refreshed.json()['preview']['plan_fingerprint']}")
    refreshed_rows = await client.get(
        f"/payroll/imports/{created['import_id']}/rows?{refreshed_query}", headers=tenant.admin,
    )
    assert refreshed_rows.status_code == 200, refreshed_rows.text
    assert refreshed_rows.json()["items"][0]["work_date"] == expected_date


async def test_filename_date_provider_survives_preview_refresh(p3c_client, p3c_engine, tenant):
    period, _grid = await open_period_with_grid(p3c_client, p3c_engine, tenant)
    driver = await _eligible_driver_date(p3c_engine, tenant, period)
    date_text = driver["workdate"].isoformat()
    payload = f"DriverNumber,Definition\n{driver['drivernumber']},4\n".encode()
    await _assert_date_provider_survives_preview_refresh(
        p3c_client, p3c_engine, tenant, period, f"payroll-{date_text}.csv", payload, "CSV",
        {"kind": "filename", "value": date_text},
    )


async def test_sheet_date_provider_survives_preview_refresh(p3c_client, p3c_engine, tenant):
    period, _grid = await open_period_with_grid(p3c_client, p3c_engine, tenant)
    driver = await _eligible_driver_date(p3c_engine, tenant, period)
    date_text = driver["workdate"].isoformat()
    unit_name = f"Payroll {date_text}"
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = unit_name
    sheet.append(["DriverNumber", "Definition"])
    sheet.append([driver["drivernumber"], 4])
    output = BytesIO()
    workbook.save(output)
    await _assert_date_provider_survives_preview_refresh(
        p3c_client, p3c_engine, tenant, period, "payroll.xlsx", output.getvalue(), unit_name,
        {"kind": "sheet", "value": date_text},
    )


async def test_inferred_date_order_survives_refresh_and_finalization_purge(p3c_client, p3c_engine, tenant):
    scenario = await standard_scenario(p3c_client, p3c_engine, tenant)
    period = scenario.period
    async with p3c_engine.connect() as db:
        driver = (await db.execute(text("""
            SELECT d.drivernumber, x.workdate
            FROM payroll.payrollperioddrivereligibility p
            JOIN core.driverassignments a ON a.driverassignmentid=p.driverassignmentid
                AND a.companyid=p.companyid AND a.branchid=p.branchid
            JOIN core.drivers d ON d.driverid=a.driverid AND d.companyid=a.companyid
            CROSS JOIN LATERAL unnest(p.eligibleworkdates) AS x(workdate)
            WHERE p.companyid=:cid AND p.payrollperiodid=:pid AND p.iseligibleforperiod
                AND extract(day FROM x.workdate)<>extract(month FROM x.workdate)
            ORDER BY x.workdate, d.driverid LIMIT 1
        """), {"cid": tenant.company_id, "pid": scenario.period_id})).one()
    definition_id, definition_code = await _definition(p3c_engine, tenant, period)
    date_text = driver.workdate.strftime("%d/%m/%Y")
    payload = (f"DriverNumber,WorkDate,{definition_code}\n"
               f"{driver.drivernumber},{date_text},4\n").encode()
    uploaded = await _upload(p3c_client, tenant, period, [("inferred-date.csv", payload)])
    assert uploaded.status_code == 200, uploaded.text
    created = uploaded.json()
    preview = await _map_and_preview(p3c_client, tenant, created, definition_id)
    unit = preview["files"][0]["units"][0]
    assert unit["date_order"] == "DMY"
    assert unit["inspection"]["accepted_date_order"] == "DMY"
    assert unit["inspection"]["date_order_state"] == "detected"

    refreshed = await p3c_client.post(
        f"/payroll/imports/{created['import_id']}/preview",
        json={"expected_revision": preview["revision"]},
        headers=tenant.admin,
    )
    assert refreshed.status_code == 200, refreshed.text
    refreshed_json = refreshed.json()
    refreshed_unit = refreshed_json["files"][0]["units"][0]
    assert refreshed_unit["date_order"] == "DMY"
    assert refreshed_unit["inspection"]["accepted_date_order"] == "DMY"
    assert refreshed_json["preview"]["mapping_fingerprint"] == preview["preview"]["mapping_fingerprint"]
    assert refreshed_json["preview"]["content_fingerprint"] == preview["preview"]["content_fingerprint"]
    assert refreshed_json["preview"]["plan_fingerprint"] == preview["preview"]["plan_fingerprint"]

    await submit_and_approve(p3c_client, tenant, period)
    finalized = await finalize(p3c_client, tenant, period)
    assert finalized.status_code == 200, finalized.text
    retained = await p3c_client.get(f"/payroll/imports/{created['import_id']}", headers=tenant.admin)
    assert retained.status_code == 200, retained.text
    retained_unit = retained.json()["files"][0]["units"][0]
    assert retained.json()["files"][0]["content_purged_at"] is not None
    assert retained_unit["inspection"] == {}
    assert retained_unit["date_order"] == "DMY"
    async with p3c_engine.connect() as db:
        stored_order = await db.scalar(text("""
            SELECT dateorder FROM import.filesourceunits
            WHERE sourceunitid=:unit AND importid=:iid AND companyid=:cid
        """), {"unit": retained_unit["source_unit_id"], "iid": created["import_id"],
               "cid": tenant.company_id})
    assert stored_order == "DMY"


async def test_grid_sample_surfaces_nonblank_candidate_after_earlier_blank_sources(
    p3c_client, p3c_engine, tenant,
):
    period, _grid = await open_period_with_grid(p3c_client, p3c_engine, tenant)
    driver = await _eligible_driver_date(p3c_engine, tenant, period)
    definition_id, definition_code = await _definition(p3c_engine, tenant, period)
    data = (f"DriverNumber,WorkDate,{definition_code}\n"
            + "".join(f"{driver['drivernumber']},{driver['workdate'].isoformat()},\n" for _ in range(5))
            + f"{driver['drivernumber']},{driver['workdate'].isoformat()},7\n").encode()
    uploaded = await _upload(p3c_client, tenant, period, [("blank-prefix.csv", data)])
    assert uploaded.status_code == 200, uploaded.text
    created = uploaded.json()
    preview = await _map_and_preview(p3c_client, tenant, created, definition_id)
    query = f"expected_revision={preview['revision']}&plan_fingerprint={preview['preview']['plan_fingerprint']}"
    rows = await p3c_client.get(f"/payroll/imports/{created['import_id']}/rows?{query}", headers=tenant.admin)
    assert rows.status_code == 200, rows.text
    assert rows.json()["summary"]["skipped"] == 5
    group = rows.json()["items"][0]
    assert group["source_record_count"] == 6
    value = next(iter(group["values"].values()))
    assert value["candidate_count"] == 6
    assert value["nonblank_candidate_count"] == 1
    assert value["candidates"][0]["quantity"] == "7"
    assert value["candidates"][0]["is_blank"] is False
