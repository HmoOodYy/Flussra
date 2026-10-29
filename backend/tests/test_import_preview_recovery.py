"""Explicit recovery after lost responses and interrupted preview publication."""
from __future__ import annotations

import asyncio
import os
import queue
import subprocess
import sys
import threading
import time

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.dependencies import get_db
from app.imports import batch_service
from tests.compensation_api_fixtures import (  # noqa: F401
    p3c_application,
    p3c_database_engine,
    p3c_http_client,
)
from tests.compensation_database_fixtures import p3b_database  # noqa: F401
from tests.payroll_runtime_fixtures import build_payroll_tenant, open_period_with_grid
from tests.test_import_batch_preview_api import (
    _definition,
    _eligible_driver_date,
    _map_and_preview,
    _upload,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture(name="tenant")
def payroll_tenant(p3b_dsn):
    return build_payroll_tenant(p3b_dsn)


def _database_url(dsn: dict) -> str:
    from sqlalchemy.engine import URL

    return URL.create(
        "postgresql+asyncpg",
        username=dsn["user"],
        password=dsn.get("password"),
        host=dsn["host"],
        port=dsn["port"],
        database=dsn["database"],
    ).render_as_string(hide_password=False)


async def _mapped_preview(client, engine, tenant, period):
    driver = await _eligible_driver_date(engine, tenant, period)
    definition_id, definition_code = await _definition(engine, tenant, period)
    async with engine.connect() as db:
        assignment_id = await db.scalar(text("""
            SELECT DriverAssignmentID FROM core.DriverAssignments
            WHERE CompanyID=:cid AND BranchID=:bid AND DriverID=:did
            ORDER BY DriverAssignmentID LIMIT 1
        """), {"cid": tenant.company_id, "bid": period["branch_id"], "did": driver["driverid"]})
    manual = await client.post(
        f"/payroll/periods/{period['payroll_period_id']}/lines", headers=tenant.admin,
        json={"driver_assignment_id": assignment_id, "work_date": driver["workdate"].isoformat(),
              "payroll_period_definition_id": definition_id, "quantity": "99", "source_type": "Manual"})
    assert manual.status_code == 201, manual.text
    data = (f"DriverNumber,WorkDate,{definition_code}\n"
            f"{driver['drivernumber']},{driver['workdate'].isoformat()},4\n").encode()
    uploaded = await _upload(client, tenant, period, [("recovery.csv", data)])
    assert uploaded.status_code == 200, uploaded.text
    created = uploaded.json()
    preview = await _map_and_preview(client, tenant, created, definition_id)
    return created, preview, driver, definition_id


async def _recovery_snapshot(engine, tenant, import_id, period):
    import_tables = (
        "import.imports",
        "import.payrollworkimports",
        "import.filecanonicaldata",
        "import.filesources",
        "import.filesourceunits",
        "import.sourcelayouts",
        "import.filesourcerecords",
        "import.payrollworkpendingrules",
        "import.payrollworkpreviewgroups",
        "import.payrollworkrecords",
        "import.payrollworkdrivermatches",
        "import.payrollworkrecordidentifiers",
        "import.importsourcerecords",
        "import.importissues",
        "import.payrollworkpreviewrulecontexts",
    )
    async with engine.connect() as db:
        snapshot = {}
        for table in import_tables:
            result = await db.execute(text(
                f"SELECT * FROM {table} WHERE companyid=:cid AND importid=:iid ORDER BY 1"
            ), {"cid": tenant.company_id, "iid": import_id})
            snapshot[table] = [dict(row) for row in result.mappings().all()]
        for table, order_column in (
            ("payroll.payrolldraftlines", "draftlineid"),
            ("payroll.payrollperiodauditevidenceevents", "payrollperiodauditevidenceeventid"),
        ):
            result = await db.execute(text(
                f"SELECT * FROM {table} WHERE companyid=:cid AND payrollperiodid=:pid ORDER BY {order_column}"
            ), {"cid": tenant.company_id, "pid": period["payroll_period_id"]})
            snapshot[table] = [dict(row) for row in result.mappings().all()]
        result = await db.execute(text("""
            SELECT * FROM import.payrollworkruleheads
            WHERE companyid=:cid AND branchid=:bid ORDER BY paydefinitionid
        """), {"cid": tenant.company_id, "bid": period["branch_id"]})
        snapshot["import.payrollworkruleheads"] = [dict(row) for row in result.mappings().all()]
        result = await db.execute(text("""
            SELECT r.* FROM import.payrollworkruleversions r
            WHERE r.companyid=:cid AND (
                r.originimportid=:iid OR EXISTS (
                    SELECT 1 FROM import.payrollworkpendingrules p
                    WHERE p.companyid=r.companyid AND p.ruleversionid=r.ruleversionid
                      AND p.branchid=r.branchid AND p.paydefinitionid=r.paydefinitionid
                      AND p.importid=:iid))
            ORDER BY r.ruleversionid
        """), {"cid": tenant.company_id, "iid": import_id})
        snapshot["import.payrollworkruleversions"] = [dict(row) for row in result.mappings().all()]
    return snapshot


def _assert_manual_rows_preserved(snapshot):
    from decimal import Decimal

    draft_lines = snapshot["payroll.payrolldraftlines"]
    evidence = snapshot["payroll.payrollperiodauditevidenceevents"]
    assert draft_lines
    assert any(row["sourcetype"] == "Manual" and row["quantity"] == Decimal("99")
               for row in draft_lines)
    assert evidence
    assert snapshot["import.filecanonicaldata"]
    assert snapshot["import.filesourcerecords"]
    assert snapshot["import.payrollworkpendingrules"]
    assert snapshot["import.payrollworkruleversions"]
    assert snapshot["import.payrollworkimports"][0]["reviewsummary"]["semantic_input_receipt"]["version"] == 1


class _FailAfterResponseDelivery:
    """Model a connection failure only after the ASGI response has been sent."""

    def __init__(self, app):
        self.app = app
        self.failed = False

    async def __call__(self, scope, receive, send):
        async def send_then_fail(message):
            await send(message)
            if (not self.failed and message["type"] == "http.response.body"
                    and not message.get("more_body", False)):
                self.failed = True
                raise ConnectionResetError("simulated response transport loss after delivery")

        await self.app(scope, receive, send_then_fail)


async def test_lost_preview_response_is_recovered_from_metadata_before_retry(
    p3c_client, p3c_app, p3c_engine, tenant,
):
    period, _ = await open_period_with_grid(p3c_client, p3c_engine, tenant)
    created, initial_preview, _driver, _definition_id = await _mapped_preview(
        p3c_client, p3c_engine, tenant, period)
    import_id = created["import_id"]
    before = await _recovery_snapshot(p3c_engine, tenant, import_id, period)
    _assert_manual_rows_preserved(before)
    original_revision = initial_preview["revision"]

    transport = httpx.ASGITransport(app=_FailAfterResponseDelivery(p3c_app))
    with pytest.raises(ConnectionResetError, match="after delivery"):
        async with httpx.AsyncClient(transport=transport, base_url="http://recovery.test") as client:
            await client.post(f"/payroll/imports/{import_id}/preview",
                              json={"expected_revision": original_revision}, headers=tenant.admin)

    recovered = await p3c_client.get(f"/payroll/imports/{import_id}", headers=tenant.admin)
    assert recovered.status_code == 200, recovered.text
    current = recovered.json()
    assert current["revision"] == original_revision + 1
    assert current["preview"]["plan_fingerprint"]
    assert current["status"] == "NeedsReview"
    after_commit = await _recovery_snapshot(p3c_engine, tenant, import_id, period)
    assert after_commit["import.payrollworkrecords"]

    stale_retry = await p3c_client.post(
        f"/payroll/imports/{import_id}/preview",
        json={"expected_revision": original_revision}, headers=tenant.admin)
    assert stale_retry.status_code == 409, stale_retry.text
    assert stale_retry.json()["detail"]["code"] == "IMPORT_REVISION_CONFLICT"
    page = await p3c_client.get(
        f"/payroll/imports/{import_id}/rows?expected_revision={current['revision']}&"
        f"plan_fingerprint={current['preview']['plan_fingerprint']}", headers=tenant.admin)
    assert page.status_code == 200, page.text
    assert page.json()["filtered_count"] == 1
    assert await _recovery_snapshot(p3c_engine, tenant, import_id, period) == after_commit
    assert before["payroll.payrolldraftlines"] == after_commit["payroll.payrolldraftlines"]
    assert before["payroll.payrollperiodauditevidenceevents"] == after_commit["payroll.payrollperiodauditevidenceevents"]
    assert before["import.payrollworkruleheads"] == after_commit["import.payrollworkruleheads"]


async def test_simultaneous_preview_requests_at_one_revision_publish_once(
    p3c_client, p3c_engine, tenant, monkeypatch,
):
    period, _ = await open_period_with_grid(p3c_client, p3c_engine, tenant)
    created, baseline_preview, _driver, _definition_id = await _mapped_preview(
        p3c_client, p3c_engine, tenant, period)
    import_id = created["import_id"]
    revision = baseline_preview["revision"]
    before = await _recovery_snapshot(p3c_engine, tenant, import_id, period)
    _assert_manual_rows_preserved(before)

    barrier = threading.Barrier(3)
    release = threading.Event()
    build = batch_service.build_batch_preview

    def hold_both_builds(*args, **kwargs):
        result = build(*args, **kwargs)
        barrier.wait(timeout=15)
        if not release.wait(timeout=15):
            raise RuntimeError("test did not release preview builders")
        return result

    monkeypatch.setattr(batch_service, "build_batch_preview", hold_both_builds)
    requests = [asyncio.create_task(p3c_client.post(
        f"/payroll/imports/{import_id}/preview", json={"expected_revision": revision}, headers=tenant.admin
    )) for _ in range(2)]
    try:
        await asyncio.wait_for(asyncio.to_thread(barrier.wait, 15), timeout=17)
    finally:
        release.set()
    responses = await asyncio.gather(*requests)
    assert sorted(response.status_code for response in responses) == [200, 409]
    refused = next(response for response in responses if response.status_code == 409)
    assert refused.json()["detail"]["code"] == "IMPORT_REVISION_CONFLICT"

    current = await p3c_client.get(f"/payroll/imports/{import_id}", headers=tenant.admin)
    assert current.status_code == 200, current.text
    published = current.json()
    assert published["revision"] == revision + 1
    assert published["preview"]["plan_fingerprint"]
    after = await _recovery_snapshot(p3c_engine, tenant, import_id, period)
    assert len(after["import.payrollworkrecords"]) == 1
    assert len(after["import.importsourcerecords"]) == 1
    assert len(after["import.payrollworkpreviewgroups"]) == 1
    assert after["payroll.payrolldraftlines"] == before["payroll.payrolldraftlines"]
    assert after["payroll.payrollperiodauditevidenceevents"] == before["payroll.payrollperiodauditevidenceevents"]
    assert after["import.payrollworkruleheads"] == before["import.payrollworkruleheads"]
    plan = published["preview"]["plan_fingerprint"]
    page = await p3c_client.get(
        f"/payroll/imports/{import_id}/rows?expected_revision={published['revision']}&plan_fingerprint={plan}",
        headers=tenant.admin)
    assert page.status_code == 200, page.text
    assert page.json()["filtered_count"] == 1


async def test_cancelled_preview_before_publication_keeps_old_state_and_can_be_retried(
    p3c_client, p3c_engine, tenant, monkeypatch,
):
    period, _ = await open_period_with_grid(p3c_client, p3c_engine, tenant)
    created, old_preview, _driver, _definition_id = await _mapped_preview(
        p3c_client, p3c_engine, tenant, period)
    import_id = created["import_id"]
    revision = old_preview["revision"]
    before = await _recovery_snapshot(p3c_engine, tenant, import_id, period)
    _assert_manual_rows_preserved(before)

    entered, release = threading.Event(), threading.Event()
    prepare = batch_service.prepare_preview

    def held_prepare(result):
        prepared = prepare(result)
        entered.set()
        if not release.wait(15):
            raise RuntimeError("test did not release the prepared preview")
        return prepared

    monkeypatch.setattr(batch_service, "prepare_preview", held_prepare)
    request = asyncio.create_task(p3c_client.post(
        f"/payroll/imports/{import_id}/preview", json={"expected_revision": revision}, headers=tenant.admin))
    try:
        assert await asyncio.to_thread(entered.wait, 10)
        request.cancel()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(request, timeout=15)

    current = await p3c_client.get(f"/payroll/imports/{import_id}", headers=tenant.admin)
    assert current.status_code == 200, current.text
    assert current.json()["revision"] == revision
    assert current.json()["preview"]["plan_fingerprint"] == old_preview["preview"]["plan_fingerprint"]
    assert await _recovery_snapshot(p3c_engine, tenant, import_id, period) == before

    retried = await p3c_client.post(
        f"/payroll/imports/{import_id}/preview", json={"expected_revision": revision}, headers=tenant.admin)
    assert retried.status_code == 200, retried.text
    assert retried.json()["revision"] == revision + 1
    after_retry = await _recovery_snapshot(p3c_engine, tenant, import_id, period)
    assert len(after_retry["import.payrollworkrecords"]) == 1
    assert after_retry["payroll.payrolldraftlines"] == before["payroll.payrolldraftlines"]
    assert after_retry["payroll.payrollperiodauditevidenceevents"] == before["payroll.payrollperiodauditevidenceevents"]
    assert after_retry["import.payrollworkruleheads"] == before["import.payrollworkruleheads"]
    _assert_manual_rows_preserved(after_retry)


def _read_child_pipe(pipe, destination, ready_lines=None):
    for line in iter(pipe.readline, ""):
        line = line.rstrip("\r\n")
        destination.append(line)
        if ready_lines is not None:
            ready_lines.put(line)


def _child_reached_barrier(process, ready_lines, expected: str, timeout: float = 30):
    deadline = time.monotonic() + timeout
    while process.poll() is None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            line = ready_lines.get(timeout=min(0.1, remaining))
        except queue.Empty:
            continue
        if line == expected:
            return True
    return False


def _stop_child(process, stdout_reader, stderr_reader):
    if process.poll() is None:
        try:
            process.kill()
        except OSError:
            if process.poll() is None:
                raise
    process.wait(timeout=10)
    if stdout_reader.ident is not None:
        stdout_reader.join(timeout=5)
    if stderr_reader.ident is not None:
        stderr_reader.join(timeout=5)
    for pipe in (process.stdin, process.stdout, process.stderr):
        if pipe is not None and not pipe.closed:
            pipe.close()


def _run_preview_child() -> None:
    """Child entry point used only by process-interruption recovery tests."""
    async def run():
        from httpx import ASGITransport, AsyncClient
        from sqlalchemy.ext.asyncio import create_async_engine

        from app.dependencies import get_db as dependency_get_db
        from app.imports import batch_service as service
        from app.main import create_app

        engine = create_async_engine(os.environ["DATABASE_URL"], echo=False)
        app = create_app()
        app.state.engine = engine

        async def child_db():
            async with engine.begin() as db:
                yield db

        app.dependency_overrides[dependency_get_db] = child_db
        phase = os.environ["IMPORT_RECOVERY_PHASE"]
        if phase == "prepared":
            original = service.prepare_preview

            def pause_after_prepare(result):
                prepared = original(result)
                print("IMPORT_RECOVERY_READY:prepared", flush=True)
                sys.stdin.readline()
                return prepared

            service.prepare_preview = pause_after_prepare
        elif phase == "persisted":
            original = service.persist_preview

            async def pause_after_persist(*args, **kwargs):
                await original(*args, **kwargs)
                print("IMPORT_RECOVERY_READY:persisted", flush=True)
                sys.stdin.readline()

            service.persist_preview = pause_after_persist
        else:
            raise RuntimeError("unsupported child pause phase")

        headers = {"Authorization": os.environ["IMPORT_RECOVERY_AUTHORIZATION"]}
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://child.test") as client:
            response = await client.post(
                f"/payroll/imports/{os.environ['IMPORT_RECOVERY_IMPORT_ID']}/preview",
                json={"expected_revision": int(os.environ["IMPORT_RECOVERY_REVISION"])},
                headers=headers,
            )
            print(f"CHILD_RESPONSE:{response.status_code}", flush=True)
        await engine.dispose()

    asyncio.run(run())


async def _fresh_client(dsn):
    from app.main import create_app

    engine = create_async_engine(_database_url(dsn), echo=False)
    app = create_app()
    app.state.engine = engine

    async def child_db():
        async with engine.begin() as db:
            yield db

    app.dependency_overrides[get_db] = child_db
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fresh.test")
    return engine, app, client


@pytest.mark.parametrize("phase", ["prepared", "persisted"])
async def test_process_interruption_before_commit_preserves_state_and_allows_explicit_retry(
    p3c_client, p3c_engine, tenant, p3b_dsn, phase,
):
    period, _ = await open_period_with_grid(p3c_client, p3c_engine, tenant)
    created, old_preview, _driver, _definition_id = await _mapped_preview(
        p3c_client, p3c_engine, tenant, period)
    import_id = created["import_id"]
    revision = old_preview["revision"]
    before = await _recovery_snapshot(p3c_engine, tenant, import_id, period)
    _assert_manual_rows_preserved(before)

    environment = os.environ.copy()
    environment["DATABASE_URL"] = _database_url(p3b_dsn)
    environment["SECRET_KEY"] = os.environ.get(
        "SECRET_KEY", "test-only-secret-key-never-use-outside-tests-000000000000000000000000")
    environment["ENVIRONMENT"] = "test"
    environment["IMPORT_RECOVERY_AUTHORIZATION"] = tenant.admin["Authorization"]
    environment["IMPORT_RECOVERY_IMPORT_ID"] = str(import_id)
    environment["IMPORT_RECOVERY_REVISION"] = str(revision)
    environment["IMPORT_RECOVERY_PHASE"] = phase
    backend = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    process = subprocess.Popen(
        [sys.executable, "-c", "from tests.test_import_preview_recovery import _run_preview_child; _run_preview_child()"],
        cwd=backend, env=environment, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1,
    )
    stdout_lines, stderr_lines, ready_lines = [], [], queue.Queue()
    stdout_reader = threading.Thread(
        target=_read_child_pipe, args=(process.stdout, stdout_lines, ready_lines), daemon=True)
    stderr_reader = threading.Thread(target=_read_child_pipe, args=(process.stderr, stderr_lines), daemon=True)
    reached_barrier = False
    child_alive_at_barrier = False
    try:
        stdout_reader.start()
        stderr_reader.start()
        reached_barrier = _child_reached_barrier(process, ready_lines, f"IMPORT_RECOVERY_READY:{phase}")
        child_alive_at_barrier = process.poll() is None
    finally:
        _stop_child(process, stdout_reader, stderr_reader)
    if not reached_barrier:
        safe_stderr = "\n".join(stderr_lines[-12:])
        for secret in (environment["DATABASE_URL"], environment["IMPORT_RECOVERY_AUTHORIZATION"]):
            safe_stderr = safe_stderr.replace(secret, "<redacted>")
        pytest.fail(f"child exited before the barrier (exit={process.returncode}); stderr tail: {safe_stderr}")
    assert child_alive_at_barrier, "child exited instead of remaining paused at the barrier"

    fresh_engine, _fresh_app_instance, fresh_client = await _fresh_client(p3b_dsn)
    try:
        recovered = await fresh_client.get(f"/payroll/imports/{import_id}", headers=tenant.admin)
        assert recovered.status_code == 200, recovered.text
        assert recovered.json()["revision"] == revision
        assert recovered.json()["preview"]["plan_fingerprint"] == old_preview["preview"]["plan_fingerprint"]
        after_interruption = await _recovery_snapshot(fresh_engine, tenant, import_id, period)
        assert after_interruption == before

        retried = await fresh_client.post(
            f"/payroll/imports/{import_id}/preview", json={"expected_revision": revision}, headers=tenant.admin)
        assert retried.status_code == 200, retried.text
        assert retried.json()["revision"] == revision + 1
        assert retried.json()["preview"]["plan_fingerprint"]
        after_retry = await _recovery_snapshot(fresh_engine, tenant, import_id, period)
        assert len(after_retry["import.payrollworkrecords"]) == 1
        assert after_retry["payroll.payrolldraftlines"] == before["payroll.payrolldraftlines"]
        assert after_retry["payroll.payrollperiodauditevidenceevents"] == before["payroll.payrollperiodauditevidenceevents"]
        assert after_retry["import.payrollworkruleheads"] == before["import.payrollworkruleheads"]
        _assert_manual_rows_preserved(after_retry)
    finally:
        await fresh_client.aclose()
        await fresh_engine.dispose()
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)
