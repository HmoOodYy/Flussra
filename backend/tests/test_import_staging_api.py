from __future__ import annotations

import io
from uuid import uuid4

import pytest
from openpyxl import Workbook
from sqlalchemy import text

from tests.compensation_api_fixtures import (  # noqa: F401 - register isolated API fixtures
    p3c_application,
    p3c_database_engine,
    p3c_http_client,
)
from tests.compensation_database_fixtures import p3b_database  # noqa: F401
from tests.payroll_runtime_fixtures import (
    build_payroll_tenant,
    force_status,
    make_driver_self_user,
    make_user,
    open_period_with_grid,
)
from tests.test_import_file_inspection import _add_nested_xml

pytestmark = pytest.mark.asyncio


@pytest.fixture(name="tenant")
def payroll_tenant(p3b_dsn):
    return build_payroll_tenant(p3b_dsn)


def _file_body(data: bytes, *, filename: str = "payroll.csv", boundary: str = "import-a1") -> bytes:
    return (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
        f"filename=\"{filename}\"\r\nContent-Type: text/csv\r\n\r\n".encode()
        + data
        + f"\r\n--{boundary}--\r\n".encode()
    )


def _upload_headers(tenant, key=None, headers=None):
    return {
        **(headers or tenant.admin),
        "Content-Type": "multipart/form-data; boundary=import-a1",
        "Idempotency-Key": str(key or uuid4()),
    }


async def _upload(
    client, tenant, period,
    data=b"Driver Number,Work Date,Regular\n0012,2026-10-20,4\n",
    *, filename="payroll.csv", key=None, headers=None,
):
    return await client.post(
        f"/payroll/periods/{period['payroll_period_id']}/imports",
        content=_file_body(data, filename=filename),
        headers=_upload_headers(tenant, key, headers),
    )


def _xlsx_bytes() -> bytes:
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.append(["Driver Number", "Work Date", "Regular"])
    worksheet.append(["0012", "2026-10-20", 4])
    output = io.BytesIO()
    workbook.save(output)
    return output.getvalue()


async def _period(client, engine, tenant):
    period, _grid = await open_period_with_grid(client, engine, tenant)
    return period


async def _canonical_counts(engine, period_id: int) -> tuple[str, ...]:
    async with engine.connect() as conn:
        row = (await conn.execute(text("""
            SELECT p.status::text,
                   (SELECT count(*)::text FROM payroll.payrolldraftlines d WHERE d.payrollperiodid = p.payrollperiodid),
                   (SELECT count(*)::text FROM payroll.payrollperioddriverdayentrystate s WHERE s.payrollperiodid = p.payrollperiodid),
                   (SELECT count(*)::text FROM payroll.payrollbonusevents b WHERE b.payrollperiodid = p.payrollperiodid)
            FROM payroll.payrollperiods p WHERE p.payrollperiodid = :pid
        """), {"pid": period_id})).one()
    return tuple(row)


async def test_upload_idempotency_retries_same_creation_and_separates_new_or_changed_identity(
    p3c_client, p3c_engine, tenant,
):
    period = await _period(p3c_client, p3c_engine, tenant)
    data = b"Driver Number,Work Date,Regular\n0012,2026-10-20,4\n"
    key = uuid4()

    first = await _upload(p3c_client, tenant, period, data, key=key)
    retry = await _upload(p3c_client, tenant, period, data, key=key)
    changed_identity = await _upload(p3c_client, tenant, period, data + b"\n", key=key)
    new_key = await _upload(p3c_client, tenant, period, data, key=uuid4())

    assert first.status_code == 200, first.text
    assert retry.status_code == 200, retry.text
    assert retry.json()["import_id"] == first.json()["import_id"]
    assert retry.json()["revision"] == first.json()["revision"] == 1
    assert changed_identity.status_code == 409, changed_identity.text
    assert changed_identity.json()["detail"]["code"] == "IMPORT_IDEMPOTENCY_CONFLICT"
    assert new_key.status_code == 200, new_key.text
    assert new_key.json()["import_id"] != first.json()["import_id"]


async def test_interpretation_revision_conflicts_are_atomic_and_cancel_is_revisioned(
    p3c_client, p3c_engine, tenant,
):
    period = await _period(p3c_client, p3c_engine, tenant)
    created = await _upload(p3c_client, tenant, period)
    assert created.status_code == 200, created.text
    import_id = created.json()["import_id"]
    path = f"/payroll/imports/{import_id}"
    source_unit_id = created.json()["files"][0]["units"][0]["source_unit_id"]

    invalid_non_first = await p3c_client.put(
        f"{path}/interpretation", json={"expected_revision": 1, "units": [
            {"source_unit_id": source_unit_id, "selected": True, "header_row_number": 2}]},
        headers=tenant.admin,
    )
    interpreted = await p3c_client.put(
        f"{path}/interpretation", json={"expected_revision": 1, "units": [
            {"source_unit_id": source_unit_id, "selected": True, "header_row_number": 1}]},
        headers=tenant.admin,
    )
    assert interpreted.status_code == 200, interpreted.text
    assert invalid_non_first.status_code == 422
    assert invalid_non_first.json()["detail"]["code"] == "IMPORT_HEADER_ROW_INVALID"
    assert interpreted.json()["revision"] == 2
    interpreted_unit = interpreted.json()["files"][0]["units"][0]
    assert interpreted_unit["header_row_number"] == 1
    assert interpreted_unit["layout_id"] == created.json()["files"][0]["units"][0]["layout_id"]

    stale = await p3c_client.put(
        f"{path}/interpretation", json={"expected_revision": 1, "units": [
            {"source_unit_id": source_unit_id, "selected": True, "header_row_number": 1}]},
        headers=tenant.admin,
    )
    invalid = await p3c_client.put(
        f"{path}/interpretation", json={"expected_revision": 2, "units": [
            {"source_unit_id": source_unit_id, "selected": True, "header_row_number": 99}]},
        headers=tenant.admin,
    )
    after_failures = await p3c_client.get(path, headers=tenant.admin)
    assert stale.status_code == 409, stale.text
    assert stale.json()["detail"]["code"] == "IMPORT_REVISION_CONFLICT"
    assert invalid.status_code == 422, invalid.text
    assert invalid.json()["detail"]["code"] == "IMPORT_HEADER_ROW_INVALID"
    assert after_failures.json()["revision"] == 2
    assert after_failures.json()["files"][0]["units"][0]["header_row_number"] == 1

    stale_cancel = await p3c_client.post(
        f"{path}/cancel", json={"expected_revision": 1}, headers=tenant.admin,
    )
    cancelled = await p3c_client.post(
        f"{path}/cancel", json={"expected_revision": 2}, headers=tenant.admin,
    )
    repeated_cancel = await p3c_client.post(
        f"{path}/cancel", json={"expected_revision": 3}, headers=tenant.admin,
    )
    assert stale_cancel.status_code == 409
    assert stale_cancel.json()["detail"]["code"] == "IMPORT_REVISION_CONFLICT"
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "Cancelled"
    assert cancelled.json()["revision"] == 3
    assert repeated_cancel.status_code == 200, repeated_cancel.text
    assert repeated_cancel.json()["status"] == "Cancelled"
    assert repeated_cancel.json()["revision"] == 3


async def test_interpretation_can_clear_explicit_date_order_and_date_context(
    p3c_client, p3c_engine, tenant,
):
    period = await _period(p3c_client, p3c_engine, tenant)
    created = await _upload(p3c_client, tenant, period)
    assert created.status_code == 200, created.text
    iid = created.json()["import_id"]
    unit_id = created.json()["files"][0]["units"][0]["source_unit_id"]
    fixed_date = period["start_date"]
    malformed = await p3c_client.put(f"/payroll/imports/{iid}/interpretation", json={
        "expected_revision": 1,
        "units": [{"source_unit_id": unit_id, "selected": True,
                   "date_source": {"kind": "fixed_unit", "value": None}}],
    }, headers=tenant.admin)
    assert malformed.status_code == 422, malformed.text
    assert malformed.json()["detail"]["code"] == "IMPORT_DATE_SOURCE_INVALID"
    accepted = await p3c_client.put(f"/payroll/imports/{iid}/interpretation", json={
        "expected_revision": 1, "default_work_date": fixed_date,
        "units": [{"source_unit_id": unit_id, "selected": True, "date_order": "DMY",
                   "date_source": {"kind": "fixed_unit", "value": fixed_date}}],
    }, headers=tenant.admin)
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["default_work_date"] == fixed_date
    assert accepted.json()["files"][0]["units"][0]["date_order"] == "DMY"
    assert accepted.json()["files"][0]["units"][0]["date_source"] == {
        "kind": "fixed_unit", "value": fixed_date}

    cleared = await p3c_client.put(f"/payroll/imports/{iid}/interpretation", json={
        "expected_revision": accepted.json()["revision"], "default_work_date": None,
        "units": [{"source_unit_id": unit_id, "selected": True, "date_order": None,
                   "date_source": None}],
    }, headers=tenant.admin)
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["default_work_date"] is None
    assert cleared.json()["files"][0]["units"][0]["date_order"] is None
    assert cleared.json()["files"][0]["units"][0]["date_source"] == {}


async def test_import_read_and_mutation_enforce_company_branch_driver_and_permission_boundaries(
    p3c_client, p3c_engine, tenant, p3b_dsn,
):
    period = await _period(p3c_client, p3c_engine, tenant)
    created = await _upload(p3c_client, tenant, period)
    assert created.status_code == 200, created.text
    import_id = created.json()["import_id"]
    read_path = f"/payroll/imports/{import_id}"
    reader = make_user(tenant, ["payroll.view"])
    branch_b_user = make_user(
        tenant, ["payroll.entry"], scope="SpecificBranch", branch_id=tenant.branch_b,
    )
    driver_user = make_driver_self_user(tenant, ["payroll.entry", "payroll.view"])
    foreign_tenant = build_payroll_tenant(p3b_dsn)

    read_only_read = await p3c_client.get(read_path, headers=reader)
    read_only_mutation = await p3c_client.post(
        f"/payroll/imports/{import_id}/cancel", json={"expected_revision": 1}, headers=reader,
    )
    read_only_upload = await _upload(p3c_client, tenant, period, headers=reader)
    branch_denial = await p3c_client.get(read_path, headers=branch_b_user)
    driver_denial = await p3c_client.get(read_path, headers=driver_user)
    company_isolation = await p3c_client.get(read_path, headers=foreign_tenant.admin)

    assert read_only_read.status_code == 200, read_only_read.text
    assert read_only_mutation.status_code == 403, read_only_mutation.text
    assert read_only_upload.status_code == 403, read_only_upload.text
    assert branch_denial.status_code == 403, branch_denial.text
    assert driver_denial.status_code == 403, driver_denial.text
    assert company_isolation.status_code == 404, company_isolation.text


async def test_effective_import_policy_is_read_only_and_uses_payroll_read_authorization(
    p3c_client, p3c_engine, tenant, p3b_dsn,
):
    period = await _period(p3c_client, p3c_engine, tenant)
    before = await _canonical_counts(p3c_engine, period["payroll_period_id"])
    path = f"/payroll/periods/{period['payroll_period_id']}/imports/policy"
    reader = make_user(tenant, ["payroll.view"])
    entry_user = make_user(tenant, ["payroll.entry"])
    wrong_branch = make_user(tenant, ["payroll.entry"], scope="SpecificBranch", branch_id=tenant.branch_b)
    driver_user = make_driver_self_user(tenant, ["payroll.entry", "payroll.view"])
    no_permission = make_user(tenant, [])
    foreign_tenant = build_payroll_tenant(p3b_dsn)

    anonymous = await p3c_client.get(path)
    viewed = await p3c_client.get(path, headers=reader)
    entered = await p3c_client.get(path, headers=entry_user)
    wrong_branch_response = await p3c_client.get(path, headers=wrong_branch)
    driver_response = await p3c_client.get(path, headers=driver_user)
    no_permission_response = await p3c_client.get(path, headers=no_permission)
    foreign_response = await p3c_client.get(path, headers=foreign_tenant.admin)
    after = await _canonical_counts(p3c_engine, period["payroll_period_id"])

    assert after == before
    assert anonymous.status_code == 401
    assert viewed.status_code == entered.status_code == 200
    assert wrong_branch_response.status_code == driver_response.status_code == no_permission_response.status_code == 403
    assert foreign_response.status_code == 404
    body = viewed.json()
    assert body["policy_version"] == 1
    assert body["policy_fingerprint"] == entered.json()["policy_fingerprint"]
    assert body["company_id"] == tenant.company_id
    assert body["branch_id"] == period["branch_id"]
    assert body["period_id"] == period["payroll_period_id"]
    assert body["product_policy"] == {
        "max_company_pay_items": 50,
        "max_branch_input_concepts": 15,
        "max_import_combined_columns": 40,
        "max_import_canonical_file_bytes": 1_000_000,
    }
    assert body["import_resource_policy"]["max_file_bytes"] == 10_000_000
    assert body["import_resource_policy"]["max_response_bytes"] == 2_000_000
    assert body["import_resource_policy"]["max_request_body_bytes"] == 50_256_000
    assert "DATABASE_URL" not in body
    assert "SECRET_KEY" not in body


async def test_upload_enforces_runtime_configured_file_size(p3c_client, p3c_app, p3c_engine,
                                                             tenant, monkeypatch):
    period = await _period(p3c_client, p3c_engine, tenant)
    monkeypatch.setattr(p3c_app.state.settings, "IMPORT_MAX_FILE_BYTES", 8)

    response = await _upload(p3c_client, tenant, period)

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "IMPORT_FILE_TOO_LARGE"


async def test_import_metadata_uses_effective_target_header_and_response_bounds(
    p3c_client, p3c_app, p3c_engine, tenant, monkeypatch,
):
    period = await _period(p3c_client, p3c_engine, tenant)
    created = await _upload(p3c_client, tenant, period)
    assert created.status_code == 200, created.text
    import_id = created.json()["import_id"]
    layout_id = created.json()["layouts"][0]["layout_id"]
    settings = p3c_app.state.settings
    monkeypatch.setattr(settings, "IMPORT_TARGET_PAGE_DEFAULT", 1)
    monkeypatch.setattr(settings, "IMPORT_TARGET_PAGE_MAX", 2)
    monkeypatch.setattr(settings, "IMPORT_HEADER_PAGE_DEFAULT", 1)
    monkeypatch.setattr(settings, "IMPORT_HEADER_PAGE_MAX", 2)
    monkeypatch.setattr(settings, "IMPORT_LAYOUT_HEADER_SAMPLE", 1)
    monkeypatch.setattr(settings, "IMPORT_MAX_HEADER_LABEL_CHARS", 5)

    default_page = await p3c_client.get(
        f"/payroll/imports/{import_id}?header_layout_id={layout_id}", headers=tenant.admin,
    )
    raised_page = await p3c_client.get(
        f"/payroll/imports/{import_id}?target_limit=2&header_layout_id={layout_id}&header_limit=2",
        headers=tenant.admin,
    )
    rejected_target_page = await p3c_client.get(
        f"/payroll/imports/{import_id}?target_limit=3", headers=tenant.admin,
    )
    rejected_header_page = await p3c_client.get(
        f"/payroll/imports/{import_id}?header_layout_id={layout_id}&header_limit=3", headers=tenant.admin,
    )

    assert default_page.status_code == 200, default_page.text
    assert default_page.json()["mapping_target_page"]["limit"] == 1
    assert default_page.json()["header_page"]["limit"] == 1
    assert default_page.json()["header_page"]["items"][0] == {"source_column": 1, "label": "Driver Number"}
    assert default_page.json()["layouts"][0]["inspection"]["header_cells"] == ["Drive"]
    assert raised_page.status_code == 200, raised_page.text
    assert raised_page.json()["mapping_target_page"]["limit"] == 2
    assert raised_page.json()["header_page"]["limit"] == 2
    assert rejected_target_page.status_code == 422
    assert rejected_target_page.json()["detail"]["code"] == "IMPORT_TARGET_PAGE_INVALID"
    assert rejected_header_page.status_code == 422
    assert rejected_header_page.json()["detail"]["code"] == "IMPORT_HEADER_PAGE_INVALID"

    monkeypatch.setattr(settings, "IMPORT_MAX_RESPONSE_BYTES", 256)
    bounded_response = await p3c_client.get(f"/payroll/imports/{import_id}", headers=tenant.admin)
    assert bounded_response.status_code == 422
    assert bounded_response.json()["detail"]["code"] == "IMPORT_METADATA_TOO_LARGE"


async def test_upload_parser_runs_after_auth_transaction_and_before_period_lock_without_payroll_writes(
    p3c_client, p3c_engine, tenant, monkeypatch,
):
    from app.imports import batch_service

    period = await _period(p3c_client, p3c_engine, tenant)
    period_id = period["payroll_period_id"]
    before = await _canonical_counts(p3c_engine, period_id)
    original_extract = batch_service.extract_canonical_file
    original_header_inspect = batch_service.inspect_canonical_headers
    original_period_lock = batch_service._lock_period_for_mutation
    lock_called = False
    observations: list[tuple[str, int, bool]] = []

    def inspect_without_db_transaction(*args, **kwargs):
        observations.append(("headers", p3c_engine.sync_engine.pool.checkedout(), lock_called))
        return original_header_inspect(*args, **kwargs)

    def extract_without_db_transaction(*args, **kwargs):
        observations.append(("canonical", p3c_engine.sync_engine.pool.checkedout(), lock_called))
        return original_extract(*args, **kwargs)

    async def observed_period_lock(*args, **kwargs):
        nonlocal lock_called
        lock_called = True
        return await original_period_lock(*args, **kwargs)

    monkeypatch.setattr(batch_service, "inspect_canonical_headers", inspect_without_db_transaction)
    monkeypatch.setattr(batch_service, "extract_canonical_file", extract_without_db_transaction)
    monkeypatch.setattr(batch_service, "_lock_period_for_mutation", observed_period_lock)
    created = await _upload(p3c_client, tenant, period)
    after = await _canonical_counts(p3c_engine, period_id)

    assert created.status_code == 200, created.text
    assert observations == [("headers", 0, False), ("canonical", 0, False)]
    assert lock_called is True
    assert after == before


async def test_cancelled_period_rejects_new_upload_and_reinterpretation(
    p3c_client, p3c_engine, tenant,
):
    period = await _period(p3c_client, p3c_engine, tenant)
    created = await _upload(p3c_client, tenant, period)
    assert created.status_code == 200, created.text
    import_id = created.json()["import_id"]
    force_status(tenant, period["payroll_period_id"], "Cancelled")

    new_upload = await _upload(p3c_client, tenant, period, key=uuid4())
    source_unit_id = created.json()["files"][0]["units"][0]["source_unit_id"]
    reinterpretation = await p3c_client.put(
        f"/payroll/imports/{import_id}/interpretation",
        json={"expected_revision": 1, "units": [
            {"source_unit_id": source_unit_id, "selected": True, "header_row_number": 2}]},
        headers=tenant.admin,
    )

    assert new_upload.status_code == 409, new_upload.text
    assert new_upload.json()["detail"]["code"] == "IMPORT_PERIOD_NOT_EDITABLE"
    assert reinterpretation.status_code == 409, reinterpretation.text
    assert reinterpretation.json()["detail"]["code"] == "IMPORT_NOT_EDITABLE"


@pytest.mark.parametrize("filename", ["payroll.csv", "payroll.xlsx"])
async def test_ymd_interpretation_is_normalized_and_persisted_for_each_file_type(
    p3c_client, p3c_engine, tenant, filename,
):
    period = await _period(p3c_client, p3c_engine, tenant)
    data = (
        b"Driver Number,Work Date,Regular\n0012,2026-10-20,4\n"
        if filename.endswith(".csv")
        else _xlsx_bytes()
    )
    created = await _upload(p3c_client, tenant, period, data, filename=filename)
    assert created.status_code == 200, created.text
    import_id = created.json()["import_id"]
    path = f"/payroll/imports/{import_id}"
    source_unit_id = created.json()["files"][0]["units"][0]["source_unit_id"]

    interpreted = await p3c_client.put(
        f"{path}/interpretation",
        json={"expected_revision": 1, "units": [
            {"source_unit_id": source_unit_id, "selected": True, "date_order": "YMD"}]},
        headers=tenant.admin,
    )
    read_back = await p3c_client.get(path, headers=tenant.admin)

    assert interpreted.status_code == 200, interpreted.text
    assert interpreted.json()["revision"] == 2
    assert read_back.status_code == 200, read_back.text
    unit = read_back.json()["files"][0]["units"][0]
    assert unit["date_order"] == "YMD"
    assert unit["inspection"]["date_order"] == "YMD"


async def test_depth_rejection_does_not_stage_or_partially_reinterpret_import(
    p3c_client, p3c_engine, tenant,
):
    from app.imports.files import MAX_XLSX_XML_DEPTH

    period = await _period(p3c_client, p3c_engine, tenant)
    valid_data = _xlsx_bytes()
    over_depth_data = _add_nested_xml(
        valid_data, "xl/workbook.xml", MAX_XLSX_XML_DEPTH + 1
    )
    rejected_upload = await _upload(
        p3c_client, tenant, period, over_depth_data, filename="payroll.xlsx"
    )

    assert rejected_upload.status_code == 422, rejected_upload.text
    assert rejected_upload.json()["detail"]["code"] == "IMPORT_XLSX_XML_DEPTH_LIMIT_EXCEEDED"
    async with p3c_engine.connect() as conn:
        count_after_rejection = await conn.scalar(text("""
            SELECT count(*) FROM import.imports i
            JOIN import.payrollworkimports w
              ON w.importid = i.importid AND w.companyid = i.companyid
            WHERE i.companyid = :cid AND w.payrollperiodid = :pid
        """), {"cid": tenant.company_id, "pid": period["payroll_period_id"]})
    assert count_after_rejection == 0

    created = await _upload(
        p3c_client, tenant, period, valid_data, filename="payroll.xlsx"
    )
    assert created.status_code == 200, created.text
    import_id = created.json()["import_id"]
    path = f"/payroll/imports/{import_id}"
    before = await p3c_client.get(path, headers=tenant.admin)
    assert before.status_code == 200, before.text
    source_unit_id = before.json()["files"][0]["units"][0]["source_unit_id"]
    rejected_reinterpretation = await p3c_client.put(
        f"{path}/interpretation",
        json={"expected_revision": 1, "units": [
            {"source_unit_id": source_unit_id, "selected": True, "header_row_number": 99}]},
        headers=tenant.admin,
    )
    after = await p3c_client.get(path, headers=tenant.admin)

    assert rejected_reinterpretation.status_code == 422, rejected_reinterpretation.text
    assert rejected_reinterpretation.json()["detail"]["code"] == "IMPORT_HEADER_ROW_INVALID"
    assert after.status_code == 200, after.text
    assert after.json()["revision"] == before.json()["revision"] == 1
    assert after.json()["status"] == before.json()["status"] == "NeedsMapping"
    assert after.json()["files"][0]["units"] == before.json()["files"][0]["units"]
