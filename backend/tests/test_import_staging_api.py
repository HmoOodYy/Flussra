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
    force_status,
    make_driver_self_user,
    make_user,
    open_period_with_grid,
)

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
    *, key=None, headers=None,
):
    return await client.post(
        f"/payroll/periods/{period['payroll_period_id']}/imports",
        content=_file_body(data),
        headers=_upload_headers(tenant, key, headers),
    )


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

    interpreted = await p3c_client.put(
        f"{path}/mapping", json={"expected_revision": 1, "header_row_number": 2},
        headers=tenant.admin,
    )
    assert interpreted.status_code == 200, interpreted.text
    assert interpreted.json()["revision"] == 2
    assert interpreted.json()["interpretation"]["header_row_number"] == 2

    stale = await p3c_client.put(
        f"{path}/mapping", json={"expected_revision": 1, "header_row_number": 1},
        headers=tenant.admin,
    )
    invalid = await p3c_client.put(
        f"{path}/mapping", json={"expected_revision": 2, "header_row_number": 99},
        headers=tenant.admin,
    )
    after_failures = await p3c_client.get(path, headers=tenant.admin)
    assert stale.status_code == 409, stale.text
    assert stale.json()["detail"]["code"] == "IMPORT_REVISION_CONFLICT"
    assert invalid.status_code == 422, invalid.text
    assert invalid.json()["detail"]["code"] == "IMPORT_HEADER_ROW_INVALID"
    assert after_failures.json()["revision"] == 2
    assert after_failures.json()["interpretation"]["header_row_number"] == 2

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


async def test_upload_parser_runs_after_auth_transaction_and_before_period_lock_without_payroll_writes(
    p3c_client, p3c_engine, tenant, monkeypatch,
):
    from app.imports import service

    period = await _period(p3c_client, p3c_engine, tenant)
    period_id = period["payroll_period_id"]
    before = await _canonical_counts(p3c_engine, period_id)
    original_inspect = service.inspect_file
    original_period_lock = service._lock_period_for_mutation
    lock_called = False
    observations: list[tuple[int, bool]] = []

    def inspect_without_db_transaction(*args, **kwargs):
        observations.append((p3c_engine.sync_engine.pool.checkedout(), lock_called))
        return original_inspect(*args, **kwargs)

    async def observed_period_lock(*args, **kwargs):
        nonlocal lock_called
        lock_called = True
        return await original_period_lock(*args, **kwargs)

    monkeypatch.setattr(service, "inspect_file", inspect_without_db_transaction)
    monkeypatch.setattr(service, "_lock_period_for_mutation", observed_period_lock)
    created = await _upload(p3c_client, tenant, period)
    after = await _canonical_counts(p3c_engine, period_id)

    assert created.status_code == 200, created.text
    assert observations == [(0, False)]
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
    reinterpretation = await p3c_client.put(
        f"/payroll/imports/{import_id}/mapping",
        json={"expected_revision": 1, "header_row_number": 2},
        headers=tenant.admin,
    )

    assert new_upload.status_code == 409, new_upload.text
    assert new_upload.json()["detail"]["code"] == "IMPORT_PERIOD_NOT_EDITABLE"
    assert reinterpretation.status_code == 409, reinterpretation.text
    assert reinterpretation.json()["detail"]["code"] == "IMPORT_NOT_EDITABLE"
