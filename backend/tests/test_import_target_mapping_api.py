from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import text

from tests.compensation_api_fixtures import (  # noqa: F401 - register isolated API fixtures
    p3c_application,
    p3c_database_engine,
    p3c_http_client,
)
from tests.compensation_database_fixtures import p3b_database  # noqa: F401
from tests.payroll_runtime_fixtures import (
    build_payroll_tenant,
    map_import_columns,
    open_period_with_grid,
    rated_definition,
)
from tests.test_import_batch_preview_api import _eligible_driver_date, _upload

pytestmark = pytest.mark.asyncio


@pytest.fixture(name="tenant")
def payroll_tenant(p3b_dsn):
    return build_payroll_tenant(p3b_dsn)


async def test_duplicate_quantity_sum_keeps_all_operands_and_explicitly_ignores_extra_duplicate(
    p3c_client, p3c_engine, tenant,
):
    whole_number = await rated_definition(
        p3c_client, tenant, definition_code=f"SUM_{uuid4().hex[:8]}", input_type="WholeNumber")
    period, _grid = await open_period_with_grid(p3c_client, p3c_engine, tenant)
    driver = await _eligible_driver_date(p3c_engine, tenant, period)
    from app.imports.mapping import targets as mapping_targets
    from app.payroll.period_definitions import list_period_definitions

    async with p3c_engine.connect() as db:
        definitions = await list_period_definitions(period["payroll_period_id"], tenant.company_id, db,
                                                    active_only=True)
    target = next(item for item in mapping_targets(definitions)
                  if item.get("code") == whole_number["definition_code"])
    definition_id, definition_code = int(target["target"].split(":", 1)[1]), target["code"]
    csv = (f"DriverNumber,WorkDate,{definition_code},{definition_code},{definition_code}\n"
           f"{driver['drivernumber']},{driver['workdate'].isoformat()},0.5,0.5,99\n").encode()
    uploaded = await _upload(p3c_client, tenant, period, [("sum.csv", csv)])
    assert uploaded.status_code == 200, uploaded.text
    created = uploaded.json()

    mapped = await map_import_columns(p3c_client, tenant, created, {
        "1": "DriverNumber", "2": "WorkDate", "3": f"Definition:{definition_id}",
        "4": f"Definition:{definition_id}", "5": "Ignore",
    })
    assert mapped.status_code == 200, mapped.text
    preview = await p3c_client.post(
        f"/payroll/imports/{created['import_id']}/preview",
        json={"expected_revision": mapped.json()["revision"]}, headers=tenant.admin)
    assert preview.status_code == 200, preview.text
    query = (f"expected_revision={preview.json()['revision']}&"
             f"plan_fingerprint={preview.json()['preview']['plan_fingerprint']}")
    rows = await p3c_client.get(f"/payroll/imports/{created['import_id']}/rows?{query}",
                                headers=tenant.admin)
    assert rows.status_code == 200, rows.text
    value = next(iter(rows.json()["items"][0]["values"].values()))
    candidate = value["candidates"][0]
    assert candidate["quantity"] == "1"
    assert candidate["source_columns"] == [3, 4]
    assert candidate["transform"] == "Sum"
    async with p3c_engine.connect() as db:
        stored = await db.scalar(text("""
            SELECT ColumnMapping FROM import.SourceLayouts
            WHERE ImportID=:iid AND CompanyID=:cid
        """), {"iid": created["import_id"], "cid": tenant.company_id})
    assert stored == {"5": "Ignore"}


async def test_source_cell_pages_bound_sum_operand_details_and_preserve_later_operands(
    p3c_client, p3c_app, p3c_engine, tenant, monkeypatch,
):
    definition = await rated_definition(
        p3c_client, tenant, definition_code=f"SUM_{uuid4().hex[:8]}")
    period, _grid = await open_period_with_grid(p3c_client, p3c_engine, tenant)
    driver = await _eligible_driver_date(p3c_engine, tenant, period)
    from app.imports.mapping import targets as mapping_targets
    from app.payroll.period_definitions import list_period_definitions

    async with p3c_engine.connect() as db:
        definitions = await list_period_definitions(
            period["payroll_period_id"], tenant.company_id, db, active_only=True)
    target = next(item for item in mapping_targets(definitions)
                  if item.get("code") == definition["definition_code"])
    target_id = int(target["target"].split(":", 1)[1])
    code = definition["definition_code"]
    operand_count = 12
    raw = "7" * 600
    headings = ["DriverNumber", "WorkDate", *([code] * operand_count)]
    row = [driver["drivernumber"], driver["workdate"].isoformat(), *([raw] * operand_count)]
    uploaded = await _upload(
        p3c_client, tenant, period,
        [("wide-sum.csv", (",".join(headings) + "\n" + ",".join(row) + "\n").encode())])
    assert uploaded.status_code == 200, uploaded.text
    created = uploaded.json()
    signature = created["layouts"][0]["inspection"]["duplicate_header_signatures"][0]["signature"]
    inputs = [{"header": code, "occurrences": [
        {"duplicate_signature": signature, "occurrence_ordinal": ordinal}]} for ordinal in range(1, operand_count + 1)]
    mapped = await p3c_client.put(
        f"/payroll/imports/{created['import_id']}/mapping",
        json={"expected_revision": created["revision"],
              "shared_columns": {"DriverNumber": {"header": "DriverNumber"},
                                 "WorkDate": {"header": "WorkDate"}},
              "targets": [{"target": f"Definition:{target_id}", "transform": "Sum",
                           "inputs": inputs, "expected_head_revision": 0}]},
        headers=tenant.admin,
    )
    assert mapped.status_code == 200, mapped.text
    preview = await p3c_client.post(
        f"/payroll/imports/{created['import_id']}/preview",
        json={"expected_revision": mapped.json()["revision"]}, headers=tenant.admin)
    assert preview.status_code == 200, preview.text
    query = (f"expected_revision={preview.json()['revision']}&"
             f"plan_fingerprint={preview.json()['preview']['plan_fingerprint']}")
    monkeypatch.setattr(p3c_app.state.settings, "IMPORT_MAX_RESPONSE_BYTES", 6_000)

    first = await p3c_client.get(
        f"/payroll/imports/{created['import_id']}/source-records?{query}&cell_offset=2&cell_limit=1",
        headers=tenant.admin)
    assert first.status_code == 200, first.text
    first_record = first.json()["items"][0]
    first_value = first_record["values_json"][str(target_id)]
    assert sorted(first_value["source_columns"]) == list(range(3, operand_count + 3))
    assert first_value["raw_value_operand_count"] == operand_count
    assert [operand["column"] for operand in first_value["raw_value"]] == [3]
    assert len(first.content) <= 6_000

    second = await p3c_client.get(
        f"/payroll/imports/{created['import_id']}/source-records?{query}&cell_offset=3&cell_limit=1",
        headers=tenant.admin)
    assert second.status_code == 200, second.text
    second_record = second.json()["items"][0]
    second_value = second_record["values_json"][str(target_id)]
    assert [operand["column"] for operand in second_value["raw_value"]] == [4]
    assert second_value["raw_value_operand_count"] == operand_count
    assert len(second.content) <= 6_000


async def test_sum_invalid_operand_issue_identifies_and_pages_the_invalid_cell(
    p3c_client, p3c_engine, tenant,
):
    definition = await rated_definition(
        p3c_client, tenant, definition_code=f"SUM_{uuid4().hex[:8]}")
    period, _grid = await open_period_with_grid(p3c_client, p3c_engine, tenant)
    driver = await _eligible_driver_date(p3c_engine, tenant, period)
    from app.imports.mapping import targets as mapping_targets
    from app.payroll.period_definitions import list_period_definitions

    async with p3c_engine.connect() as db:
        definitions = await list_period_definitions(
            period["payroll_period_id"], tenant.company_id, db, active_only=True)
    target = next(item for item in mapping_targets(definitions)
                  if item.get("code") == definition["definition_code"])
    target_id, code = int(target["target"].split(":", 1)[1]), target["code"]
    csv = (f"DriverNumber,WorkDate,{code},{code}\n"
           f"{driver['drivernumber']},{driver['workdate'].isoformat()},1,invalid\n").encode()
    uploaded = await _upload(p3c_client, tenant, period, [("invalid-sum.csv", csv)])
    assert uploaded.status_code == 200, uploaded.text
    created = uploaded.json()
    signature = created["layouts"][0]["inspection"]["duplicate_header_signatures"][0]["signature"]
    mapped = await p3c_client.put(
        f"/payroll/imports/{created['import_id']}/mapping",
        json={"expected_revision": created["revision"],
              "shared_columns": {"DriverNumber": {"header": "DriverNumber"},
                                 "WorkDate": {"header": "WorkDate"}},
              "targets": [{"target": f"Definition:{target_id}", "transform": "Sum",
                           "inputs": [{"header": code, "occurrences": [
                               {"duplicate_signature": signature, "occurrence_ordinal": 1}]},
                                      {"header": code, "occurrences": [
                               {"duplicate_signature": signature, "occurrence_ordinal": 2}]}],
                           "expected_head_revision": 0}]},
        headers=tenant.admin,
    )
    assert mapped.status_code == 200, mapped.text
    preview = await p3c_client.post(
        f"/payroll/imports/{created['import_id']}/preview",
        json={"expected_revision": mapped.json()["revision"]}, headers=tenant.admin)
    assert preview.status_code == 200, preview.text
    query = (f"expected_revision={preview.json()['revision']}&"
             f"plan_fingerprint={preview.json()['preview']['plan_fingerprint']}")
    source = await p3c_client.get(
        f"/payroll/imports/{created['import_id']}/source-records?{query}&cell_offset=3&cell_limit=1",
        headers=tenant.admin)
    assert source.status_code == 200, source.text
    record = source.json()["items"][0]
    invalid_issue = next(issue for issue in record["issues"]
                         if issue["code"] == "IMPORT_QUANTITY_INVALID")
    assert invalid_issue["column"] == 4
    assert invalid_issue["value_operand_count"] == 2
    assert [operand["column"] for operand in invalid_issue["value"]] == [4]


async def test_sum_missing_one_physical_input_warns_without_publishing_a_partial_quantity(
    p3c_client, p3c_engine, tenant,
):
    definition = await rated_definition(
        p3c_client, tenant, definition_code=f"SUM_{uuid4().hex[:8]}")
    period, _grid = await open_period_with_grid(p3c_client, p3c_engine, tenant)
    async with p3c_engine.connect() as db:
        eligible = (await db.execute(text("""
            SELECT d.DriverNumber, x.WorkDate
            FROM payroll.PayrollPeriodDriverEligibility p
            JOIN core.DriverAssignments a ON a.DriverAssignmentID=p.DriverAssignmentID
                AND a.CompanyID=p.CompanyID AND a.BranchID=p.BranchID
            JOIN core.Drivers d ON d.DriverID=a.DriverID AND d.CompanyID=a.CompanyID
            CROSS JOIN LATERAL unnest(p.EligibleWorkDates) AS x(WorkDate)
            WHERE p.CompanyID=:cid AND p.PayrollPeriodID=:pid AND p.BranchID=:bid
                AND p.IsEligibleForPeriod
            ORDER BY d.DriverID, x.WorkDate LIMIT 2
        """), {"cid": tenant.company_id, "pid": period["payroll_period_id"],
               "bid": period["branch_id"]})).all()
        from app.payroll.period_definitions import list_period_definitions

        definitions = await list_period_definitions(
            period["payroll_period_id"], tenant.company_id, db, active_only=True)
        before_lines = await db.scalar(text("""
            SELECT count(*) FROM payroll.PayrollDraftLines
            WHERE CompanyID=:cid AND PayrollPeriodID=:pid
        """), {"cid": tenant.company_id, "pid": period["payroll_period_id"]})
    assert len(eligible) == 2
    driver_number, first_date = eligible[0]
    assert eligible[1][0] == driver_number
    second_date = eligible[1][1]
    from app.imports.mapping import targets as mapping_targets

    target = next(item for item in mapping_targets(definitions)
                  if item.get("code") == definition["definition_code"])
    target_id, code = int(target["target"].split(":", 1)[1]), target["code"]
    uploaded = await _upload(p3c_client, tenant, period, [
        ("one-input.csv", (f"DriverNumber,WorkDate,{code}\n"
                            f"{driver_number},{first_date.isoformat()},4\n").encode()),
        ("two-inputs.csv", (f"DriverNumber,WorkDate,{code},{code}\n"
                             f"{driver_number},{second_date.isoformat()},0.5,0.25\n").encode()),
    ])
    assert uploaded.status_code == 200, uploaded.text
    created = uploaded.json()
    one_arrangement = next(layout["inspection"]["duplicate_header_signatures"][0]["signature"]
                           for layout in created["layouts"]
                           if layout["inspection"]["duplicate_header_signatures"])
    absent_unique = {"duplicate_signature": None, "occurrence_ordinal": None}
    first_input = {"header": code, "occurrences": [
        {"duplicate_signature": None, "occurrence_ordinal": 1},
        {"duplicate_signature": one_arrangement, "occurrence_ordinal": 1},
    ]}
    second_input = {"header": code, "occurrences": [
        absent_unique,
        {"duplicate_signature": one_arrangement, "occurrence_ordinal": 2},
    ]}
    mapped = await p3c_client.put(
        f"/payroll/imports/{created['import_id']}/mapping",
        json={"expected_revision": created["revision"],
              "shared_columns": {"DriverNumber": {"header": "DriverNumber"},
                                 "WorkDate": {"header": "WorkDate"}},
              "targets": [{"target": f"Definition:{target_id}", "transform": "Sum",
                           "inputs": [first_input, second_input], "expected_head_revision": 0}]},
        headers=tenant.admin,
    )
    assert mapped.status_code == 200, mapped.text
    preview = await p3c_client.post(
        f"/payroll/imports/{created['import_id']}/preview",
        json={"expected_revision": mapped.json()["revision"]}, headers=tenant.admin)
    assert preview.status_code == 200, preview.text
    query = (f"expected_revision={preview.json()['revision']}&"
             f"plan_fingerprint={preview.json()['preview']['plan_fingerprint']}")
    rows = await p3c_client.get(f"/payroll/imports/{created['import_id']}/rows?{query}",
                                headers=tenant.admin)
    assert rows.status_code == 200, rows.text
    by_date = {item["work_date"]: item for item in rows.json()["items"]}
    partial = by_date[first_date.isoformat()]
    complete = by_date[second_date.isoformat()]
    partial_candidate = partial["values"][str(target_id)]["candidates"][0]
    complete_candidate = complete["values"][str(target_id)]["candidates"][0]
    assert partial_candidate["quantity"] is None
    assert partial_candidate["source_columns"] == [3]
    assert partial_candidate["is_blank"] is True
    assert partial["warning_count"] == 1
    assert complete_candidate["quantity"] == "0.75"
    assert complete_candidate["source_columns"] == [3, 4]
    async with p3c_engine.connect() as db:
        after_lines = await db.scalar(text("""
            SELECT count(*) FROM payroll.PayrollDraftLines
            WHERE CompanyID=:cid AND PayrollPeriodID=:pid
        """), {"cid": tenant.company_id, "pid": period["payroll_period_id"]})
    assert after_lines == before_lines


async def test_reused_quantity_selector_rejects_whole_mapping_snapshot_without_revision_change(
    p3c_client, p3c_engine, tenant,
):
    first = await rated_definition(p3c_client, tenant, definition_code=f"MAP_{uuid4().hex[:8]}")
    second = await rated_definition(p3c_client, tenant, definition_code=f"MAP_{uuid4().hex[:8]}")
    period, _grid = await open_period_with_grid(p3c_client, p3c_engine, tenant)
    driver = await _eligible_driver_date(p3c_engine, tenant, period)
    first_code = first["definition_code"]
    second_code = second["definition_code"]
    csv = (f"DriverNumber,WorkDate,{first_code}\n"
           f"{driver['drivernumber']},{driver['workdate'].isoformat()},1\n").encode()
    uploaded = await _upload(p3c_client, tenant, period, [("reuse.csv", csv)])
    assert uploaded.status_code == 200, uploaded.text
    created = uploaded.json()
    invalid = await p3c_client.put(
        f"/payroll/imports/{created['import_id']}/mapping",
        json={"expected_revision": created["revision"], "unexpected": "do not echo this value"},
        headers=tenant.admin,
    )
    assert invalid.status_code == 422, invalid.text
    assert invalid.json()["detail"]["code"] == "IMPORT_MAPPING_REQUEST_INVALID"
    assert "do not echo this value" not in invalid.text
    from app.imports.mapping import targets as mapping_targets
    from app.payroll.period_definitions import list_period_definitions

    async with p3c_engine.connect() as db:
        definitions = await list_period_definitions(period["payroll_period_id"], tenant.company_id, db,
                                                    active_only=True)
    targets = {item["code"]: item["target"] for item in mapping_targets(definitions)
               if item.get("code") and item["target"].startswith("Definition:")}
    same_input = {"header": first_code}
    rejected = await p3c_client.put(
        f"/payroll/imports/{created['import_id']}/mapping",
        json={"expected_revision": created["revision"],
              "shared_columns": {"DriverNumber": {"header": "DriverNumber"},
                                 "WorkDate": {"header": "WorkDate"}},
              "targets": [
                  {"target": targets[first_code], "transform": "Direct",
                   "inputs": [same_input], "expected_head_revision": 0},
                  {"target": targets[second_code], "transform": "Direct",
                   "inputs": [same_input], "expected_head_revision": 0},
              ]},
        headers=tenant.admin,
    )
    assert rejected.status_code == 422, rejected.text
    assert rejected.json()["detail"]["code"] == "IMPORT_QUANTITY_COLUMN_REUSED"
    current = await p3c_client.get(f"/payroll/imports/{created['import_id']}", headers=tenant.admin)
    assert current.status_code == 200, current.text
    assert current.json()["revision"] == created["revision"]
    async with p3c_engine.connect() as db:
        pending_count = await db.scalar(text("""
            SELECT count(*) FROM import.PayrollWorkPendingRules
            WHERE ImportID=:iid AND CompanyID=:cid
        """), {"iid": created["import_id"], "cid": tenant.company_id})
    assert pending_count == 0


async def test_mapping_body_is_not_read_before_mutation_authorization(
    p3c_client, p3c_engine, tenant, monkeypatch,
):
    period, _grid = await open_period_with_grid(p3c_client, p3c_engine, tenant)
    driver = await _eligible_driver_date(p3c_engine, tenant, period)
    uploaded = await _upload(p3c_client, tenant, period, [(
        "unauthorized.csv",
        f"DriverNumber,WorkDate,Hours\n{driver['drivernumber']},{driver['workdate'].isoformat()},1\n".encode(),
    )])
    assert uploaded.status_code == 200, uploaded.text
    from app.imports import router as import_router

    receiver = import_router.receive_mapping_json
    called = False

    async def observed_receiver(*args, **kwargs):
        nonlocal called
        called = True
        return await receiver(*args, **kwargs)

    monkeypatch.setattr(import_router, "receive_mapping_json", observed_receiver)
    response = await p3c_client.put(
        f"/payroll/imports/{uploaded.json()['import_id']}/mapping",
        content=b'{"expected_revision":1,"unexpected":"not read"}',
        headers={**tenant.headers(tenant.viewer), "Content-Type": "application/json"},
    )
    assert response.status_code == 403, response.text
    assert called is False
