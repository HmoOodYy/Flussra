"""P6D immutable finalized-audit route and persistence contracts."""
from __future__ import annotations

import importlib.util
import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import httpx
import psycopg2
import pytest
import pytest_asyncio
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from app.payroll.service import _create_period_pay_item_rows, finalize_period
from tests.test_p6a_finalized_library import (
    _auth,
    _scoped_permission_token,
    _seed_finalized_period,
)
from tests.test_p6c_finalized_rates_used import _seed_finalized_rates_period

_MIGRATION_0065_PATH = (
    Path(__file__).resolve().parents[2]
    / "migrations"
    / "versions"
    / "0065_p6d_immutable_period_audit_evidence.py"
)
_MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"
_MIGRATION_0065_SPEC = importlib.util.spec_from_file_location(
    "p6d_migration_0065", _MIGRATION_0065_PATH,
)


@pytest_asyncio.fixture(scope="session")
async def paytest_branch_id(session_db_conn) -> int:
    row = (await session_db_conn.execute(
        text("""
            INSERT INTO core.branches
                (companyid, branchcode, branchname, status, isdefault)
            VALUES (1, :code, :name, 'Active', FALSE)
            RETURNING branchid
        """), {"code": f"P6D_{uuid4().hex[:10]}", "name": "P6D isolated"},
    )).mappings().one()
    await session_db_conn.commit()
    return int(row["branchid"])


async def _release_draft_slot(direct_db, branch_id: int) -> None:
    """Release only this module's mutable Draft slot; immutable history stays."""
    await direct_db.execute(
        text("""
            UPDATE payroll.payrollperiods
            SET status = 'Cancelled', currentreturnreviewitemid = NULL
            WHERE branchid = :branch_id AND status = 'Draft'
        """), {"branch_id": branch_id},
    )
    await direct_db.commit()
assert _MIGRATION_0065_SPEC and _MIGRATION_0065_SPEC.loader
_MIGRATION_0065 = importlib.util.module_from_spec(_MIGRATION_0065_SPEC)
_MIGRATION_0065_SPEC.loader.exec_module(_MIGRATION_0065)


@pytest_asyncio.fixture
async def isolated_0065_database(pg_instance):
    """Provide a disposable fully migrated database with no P6D evidence."""
    db_name = f"p6d_clean_{uuid4().hex[:12]}"
    admin_dsn = dict(pg_instance.dsn())
    admin = psycopg2.connect(**admin_dsn)
    admin.autocommit = True
    try:
        with admin.cursor() as cur:
            cur.execute(f'CREATE DATABASE "{db_name}"')
    finally:
        admin.close()

    isolated_dsn = dict(pg_instance.dsn())
    isolated_dsn["database"] = db_name
    isolated = psycopg2.connect(**isolated_dsn)
    isolated.autocommit = True
    try:
        with isolated.cursor() as cur:
            for migration_file in sorted(_MIGRATIONS_DIR.joinpath("sql").glob("*.sql")):
                cur.execute(migration_file.read_text(encoding="utf-8"))
    finally:
        isolated.close()

    url = (
        f"postgresql+asyncpg://{isolated_dsn['user']}@{isolated_dsn['host']}"
        f":{isolated_dsn['port']}/{db_name}"
    )
    try:
        yield url
    finally:
        cleanup = psycopg2.connect(**admin_dsn)
        cleanup.autocommit = True
        try:
            with cleanup.cursor() as cur:
                cur.execute(f'DROP DATABASE IF EXISTS "{db_name}"')
        finally:
            cleanup.close()


def _run_0065_migration(connection, direction: str) -> None:
    context = MigrationContext.configure(connection)
    with Operations.context(Operations(context)):
        getattr(_MIGRATION_0065, direction)()


async def _seed_open_period(
    direct_db, branch_id: int, *, status: str = "Open",
) -> tuple[int, int, date]:
    """Create a normal Open period and driver for real P6D writer-path tests."""
    marker = uuid4().hex
    work_date = date(2098, 4, 1)
    await direct_db.execute(text("""
        UPDATE payroll.payrollperiods
        SET status = 'Cancelled'
        WHERE branchid = :branch_id AND periodcode LIKE 'P6D-WRITER-%'
          AND status = 'Open'
          AND NOT EXISTS (
              SELECT 1 FROM payroll.payrollcalculationsnapshots s
              WHERE s.payrollperiodid = payrollperiods.payrollperiodid
          )
    """), {"branch_id": branch_id})
    employee_id = int((await direct_db.execute(text("""
        INSERT INTO core.employees
            (companyid, branchid, fullname, employeetype, employmentstatus, createdbyuserid)
        VALUES (1, :branch_id, :name, 'Driver', 'Active', 1)
        RETURNING employeeid
    """), {"branch_id": branch_id, "name": f"P6D writer {marker}"})).scalar_one())
    driver_id = int((await direct_db.execute(text("""
        INSERT INTO core.drivers (companyid, branchid, employeeid, drivercode, driverstatus)
        VALUES (1, :branch_id, :employee_id, :code, 'Active')
        RETURNING driverid
    """), {
        "branch_id": branch_id, "employee_id": employee_id, "code": f"P6D-{marker[:20]}",
    })).scalar_one())
    period_id = int((await direct_db.execute(text("""
        INSERT INTO payroll.payrollperiods
            (companyid, branchid, status, periodcode, periodname, periodtype, startdate, enddate)
        VALUES (1, :branch_id, :status, :code, :name, 'Week', '2098-04-01', '2098-04-07')
        RETURNING payrollperiodid
    """), {
        "branch_id": branch_id, "status": status,
        "code": f"P6D-WRITER-{marker}", "name": f"P6D writer {marker}",
    })).scalar_one())
    await _create_period_pay_item_rows(period_id, 1, branch_id, work_date, direct_db)
    await direct_db.commit()
    return period_id, driver_id, work_date


async def _remove_p6d_writer_periods(direct_db, branch_id: int) -> None:
    """Retire active writer fixtures without deleting immutable evidence."""
    await direct_db.execute(text("""
        UPDATE payroll.payrollperiods
        SET status = 'Cancelled'
        WHERE branchid = :branch_id AND periodcode LIKE 'P6D-WRITER-%'
          AND status = 'Open'
          AND NOT EXISTS (
              SELECT 1 FROM payroll.payrollcalculationsnapshots s
              WHERE s.payrollperiodid = payrollperiods.payrollperiodid
          )
    """), {"branch_id": branch_id})
    await direct_db.commit()


async def _events(direct_db, period_id: int) -> list[dict]:
    return (await direct_db.execute(text("""
        SELECT payrollperiodauditevidenceeventid, evidencedomain, actioncode,
               sourceentitytype, sourceentityid, driverid, workdate, payitemid,
               beforestatejson, afterstatejson, actordisplaynamesnapshot,
               responsibilitycontextsnapshot, correlationid, sourcerevision
        FROM payroll.payrollperiodauditevidenceevents
        WHERE payrollperiodid = :period_id
        ORDER BY occurredatutc, payrollperiodauditevidenceeventid
    """), {"period_id": period_id})).mappings().all()


async def _create_legacy_period_pay_item(
    direct_db,
    branch_id: int,
) -> str:
    """Seed an existing legacy custom Period item for the supported route."""
    code = f"P6D_LEGACY_{uuid4().hex[:10]}".upper()
    await direct_db.execute(text("""
        INSERT INTO payroll.payitems
            (companyid, branchid, payitemcode, payitemname, category, datatype,
             itemscope, ratebehavior, status, sortorder,
             appearsinpayrollentry, appearsinledger, appearsinreports,
             requiresrate, issystemstandard, isdefaultbranchactive)
        VALUES
            (1, :branch_id, :code, 'P6D Legacy Period Pay', 'Custom', 'Number',
             'Period', 'EnteredAmount', 'Active', 901,
             FALSE, TRUE, TRUE, FALSE, FALSE, TRUE)
    """), {"branch_id": branch_id, "code": code})
    await direct_db.commit()
    return code


async def _seed_driver(direct_db, branch_id: int) -> int:
    marker = uuid4().hex
    employee_id = int((await direct_db.execute(text("""
        INSERT INTO core.employees
            (companyid, branchid, fullname, employeetype, employmentstatus, createdbyuserid)
        VALUES (1, :branch_id, :name, 'Driver', 'Active', 1)
        RETURNING employeeid
    """), {"branch_id": branch_id, "name": f"P6D bonus {marker}"})).scalar_one())
    return int((await direct_db.execute(text("""
        INSERT INTO core.drivers (companyid, branchid, employeeid, drivercode, driverstatus)
        VALUES (1, :branch_id, :employee_id, :code, 'Active')
        RETURNING driverid
    """), {
        "branch_id": branch_id, "employee_id": employee_id, "code": f"P6D-BONUS-{marker[:16]}",
    })).scalar_one())


async def _insert_event(
    direct_db, *, period_id: int, branch_id: int, snapshot_id: int,
    domain: str = "SOURCE", action: str = "SOURCE_CREATED", actor_name: str = "Frozen Audit User",
    review_item_id: int | None = None, driver_id: int | None = None,
    after_state: dict | None = None, responsibility_context: dict | None = None,
    link_membership: bool = True,
) -> int:
    # Review-comment membership is a deferred transaction constraint. Keep all
    # fixture inserts in one real transaction rather than the autocommit seed connection.
    transactional_engine = create_async_engine(direct_db.engine.url, echo=False)
    try:
        async with transactional_engine.begin() as evidence_db:
            await evidence_db.execute(text("""
            INSERT INTO payroll.payrollperiodauditevidencecoverage
                (companyid, branchid, payrollperiodid, evidencedomain, coveragestate)
            VALUES (1, :branch_id, :period_id, :domain, 'COMPLETE')
            ON CONFLICT (payrollperiodid, evidencedomain) DO NOTHING
            """), {"branch_id": branch_id, "period_id": period_id, "domain": domain})
            event_id = int((await evidence_db.execute(text("""
            INSERT INTO payroll.payrollperiodauditevidenceevents
                (companyid, branchid, payrollperiodid, evidencedomain, actioncode,
                 sourceentitytype, sourceentityid, reviewitemid, actoruserid, actordisplaynamesnapshot,
                 responsibilitycontextsnapshot, afterstatejson, driverid)
            VALUES (1, :branch_id, :period_id, :domain, :action,
                    'TestSource', :entity_id, :review_item_id, 1, :actor_name,
                    CAST(:responsibility_context AS jsonb), CAST(:after_state AS jsonb), :driver_id)
            RETURNING payrollperiodauditevidenceeventid
            """), {
            "branch_id": branch_id, "period_id": period_id, "domain": domain,
            "action": action, "entity_id": str(uuid4()), "actor_name": actor_name,
            "review_item_id": review_item_id, "driver_id": driver_id,
            "after_state": json.dumps(after_state or {}),
            "responsibility_context": json.dumps(responsibility_context or {"value": "frozen"}),
            })).scalar_one())
            if snapshot_id and link_membership:
                await evidence_db.execute(text("""
                INSERT INTO payroll.payrollperiodauditevidencesnapshotevents
                    (payrollperiodauditevidenceeventid, payrollcalculationsnapshotid,
                     companyid, branchid, payrollperiodid)
                VALUES (:event_id, :snapshot_id, 1, :branch_id, :period_id)
                """), {"event_id": event_id, "snapshot_id": snapshot_id,
                       "branch_id": branch_id, "period_id": period_id})
    finally:
        await transactional_engine.dispose()
    return event_id


@pytest.mark.asyncio
async def test_finalized_audit_uses_immutable_evidence_and_exact_snapshot(
    session_client: httpx.AsyncClient, auth_token: str, paytest_branch_id: int,
    direct_db, test_database_url: str,
):
    period_id, snapshot_id, _, review_item_id = await _seed_finalized_period(
        direct_db, test_database_url, paytest_branch_id,
    )
    event_id = await _insert_event(
        direct_db, period_id=period_id, branch_id=paytest_branch_id, snapshot_id=snapshot_id,
    )
    response = await session_client.get(
        f"/payroll/finalized/{period_id}/audit", headers=_auth(auth_token),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["metadata"]["snapshot_id"] == snapshot_id
    assert body["metadata"]["revision_number"] == 1
    assert body["metadata"]["section_availability"]["source"]["state"] == "AVAILABLE"
    assert [event["event_id"] for event in body["source_events"]] == [event_id]
    assert body["source_events"][0]["actor_display_name"] == "Frozen Audit User"
    assert body["revision_groups"][0]["event_ids"] == [event_id]

    review_event_id = await _insert_event(
        direct_db, period_id=period_id, branch_id=paytest_branch_id, snapshot_id=snapshot_id,
        domain="REVIEW_COMMENT", action="REVIEW_COMMENT_ADDED", review_item_id=review_item_id,
    )
    with_comment = await session_client.get(
        f"/payroll/finalized/{period_id}/audit", headers=_auth(auth_token),
    )
    assert [event["event_id"] for event in with_comment.json()["review_events"]] == [review_event_id]
    assert with_comment.json()["review_events"][0]["review_item_id"] == review_item_id

    await direct_db.execute(text("UPDATE sec.users SET displayname = 'Mutable User' WHERE userid = 1"))
    await direct_db.commit()
    frozen = await session_client.get(
        f"/payroll/finalized/{period_id}/audit", headers=_auth(auth_token),
    )
    assert frozen.status_code == 200
    assert frozen.json()["source_events"][0]["actor_display_name"] == "Frozen Audit User"
    # Restore the shared seed user's mutable catalog value for later tests.
    await direct_db.execute(text(
        "UPDATE sec.users SET displayname = 'Admin User' WHERE userid = 1"
    ))
    await direct_db.commit()


@pytest.mark.asyncio
async def test_finalized_audit_requires_both_ledger_permissions(
    session_client: httpx.AsyncClient, auth_token: str, paytest_branch_id: int,
    direct_db, test_database_url: str,
):
    period_id, _, _, _ = await _seed_finalized_period(direct_db, test_database_url, paytest_branch_id)
    both = await _scoped_permission_token(
        session_client, auth_token, paytest_branch_id, ["ledger.view", "ledger.audit.view"],
    )
    ledger_only = await _scoped_permission_token(
        session_client, auth_token, paytest_branch_id, ["ledger.view"],
    )
    audit_only = await _scoped_permission_token(
        session_client, auth_token, paytest_branch_id, ["ledger.audit.view"],
    )
    payroll_only = await _scoped_permission_token(
        session_client, auth_token, paytest_branch_id, ["payroll.view"],
    )
    reports_only = await _scoped_permission_token(
        session_client, auth_token, paytest_branch_id, ["reports.view"],
    )
    assert (await session_client.get(
        f"/payroll/finalized/{period_id}/audit", headers=_auth(both),
    )).status_code == 200
    for token in (ledger_only, audit_only, payroll_only, reports_only):
        assert (await session_client.get(
            f"/payroll/finalized/{period_id}/audit", headers=_auth(token),
        )).status_code == 403


@pytest.mark.asyncio
async def test_finalized_audit_denies_driver_oda_and_foreign_branch_without_leaking_period(
    session_client: httpx.AsyncClient, auth_token: str, paytest_branch_id: int,
    hq_branch_id: int, direct_db, test_database_url: str,
):
    period_id, _, _, _ = await _seed_finalized_period(
        direct_db, test_database_url, paytest_branch_id,
    )
    driver_token, driver_user_id = await _scoped_permission_token(
        session_client, auth_token, paytest_branch_id,
        ["ledger.view", "ledger.audit.view"], return_user_id=True,
    )
    roles = await session_client.get("/admin/company-roles", headers=_auth(auth_token))
    assert roles.status_code == 200, roles.text
    driver_role_id = next(
        role["company_role_id"] for role in roles.json() if role["role_code"] == "DRIVER"
    )
    for scope_type in ("SpecificBranch", "OwnDriverDataOnly"):
        assignment = await session_client.post(
            f"/admin/users/{driver_user_id}/company-role-assignments",
            json={"company_role_id": driver_role_id, "scope_type": scope_type, "branch_id": paytest_branch_id},
            headers=_auth(auth_token),
        )
        assert assignment.status_code == 201, assignment.text
        denied = await session_client.get(
            f"/payroll/finalized/{period_id}/audit", headers=_auth(driver_token),
        )
        assert denied.status_code == 403, denied.text
        assert str(period_id) not in denied.text

    other_branch_token = await _scoped_permission_token(
        session_client, auth_token, hq_branch_id, ["ledger.view", "ledger.audit.view"],
    )
    denied_branch = await session_client.get(
        f"/payroll/finalized/{period_id}/audit", headers=_auth(other_branch_token),
    )
    assert denied_branch.status_code == 403, denied_branch.text
    assert str(period_id) not in denied_branch.text

    marker = uuid4().hex[:12]
    company_id = int((await direct_db.execute(text("""
        INSERT INTO core.companies
            (companycode, companyname, legalname, status, issuspended, timezonename)
        VALUES (:code, :name, :name, 'Active', FALSE, 'UTC')
        RETURNING companyid
    """), {"code": f"P6D-{marker}", "name": f"P6D foreign {marker}"})).scalar_one())
    branch_id = int((await direct_db.execute(text("""
        INSERT INTO core.branches (companyid, branchcode, branchname, status, isdefault)
        VALUES (:company_id, :code, :name, 'Active', TRUE)
        RETURNING branchid
    """), {
        "company_id": company_id, "code": f"P6D-{marker}", "name": f"P6D foreign {marker}",
    })).scalar_one())
    foreign_period_id = int((await direct_db.execute(text("""
        INSERT INTO payroll.payrollperiods
            (companyid, branchid, status, periodcode, periodname, periodtype, startdate, enddate)
        VALUES (:company_id, :branch_id, 'Locked', :code, :name, 'Week', '2098-06-01', '2098-06-07')
        RETURNING payrollperiodid
    """), {
        "company_id": company_id, "branch_id": branch_id,
        "code": f"P6D-{marker}", "name": f"P6D foreign {marker}",
    })).scalar_one())
    await direct_db.commit()
    try:
        foreign_company = await session_client.get(
            f"/payroll/finalized/{foreign_period_id}/audit", headers=_auth(auth_token),
        )
        assert foreign_company.status_code == 404, foreign_company.text
        assert str(foreign_period_id) not in foreign_company.text
    finally:
        await direct_db.execute(text("DELETE FROM payroll.payrollperiods WHERE payrollperiodid = :period_id"), {
            "period_id": foreign_period_id,
        })
        await direct_db.execute(text("DELETE FROM core.branches WHERE branchid = :branch_id"), {
            "branch_id": branch_id,
        })
        await direct_db.execute(text("DELETE FROM core.companies WHERE companyid = :company_id"), {
            "company_id": company_id,
        })
        await direct_db.commit()


@pytest.mark.asyncio
async def test_finalized_audit_rejects_non_finalized_period_before_loading_evidence(
    session_client: httpx.AsyncClient, auth_token: str, paytest_branch_id: int, direct_db,
):
    period_id = int((await direct_db.execute(text("""
        INSERT INTO payroll.payrollperiods
            (companyid, branchid, status, periodcode, periodname, periodtype, startdate, enddate)
        VALUES (1, :branch_id, 'Approved', :code, 'P6D non-finalized', 'Week', '2098-05-01', '2098-05-07')
        RETURNING payrollperiodid
    """), {"branch_id": paytest_branch_id, "code": f"P6D-NONFINAL-{uuid4().hex}"})).scalar_one())
    await direct_db.commit()
    response = await session_client.get(
        f"/payroll/finalized/{period_id}/audit", headers=_auth(auth_token),
    )
    assert response.status_code == 422, response.text
    assert "available only for Locked or Archived periods" in response.json()["detail"]


@pytest.mark.asyncio
async def test_finalized_audit_rejects_non_finalized_terminal_states(
    session_client: httpx.AsyncClient, auth_token: str, paytest_branch_id: int, direct_db,
):
    await _release_draft_slot(direct_db, paytest_branch_id)
    period_ids: dict[str, int] = {}
    for status in ("Draft", "Approved", "Cancelled"):
        period_ids[status] = int((await direct_db.execute(text("""
            INSERT INTO payroll.payrollperiods
                (companyid, branchid, status, periodcode, periodname, periodtype, startdate, enddate)
            VALUES (1, :branch_id, :status, :code, :name, 'Week', '2098-07-01', '2098-07-07')
            RETURNING payrollperiodid
        """), {
            "branch_id": paytest_branch_id, "status": status,
            "code": f"P6D-{status}-{uuid4().hex}", "name": f"P6D {status}",
        })).scalar_one())
    await direct_db.commit()
    for status, period_id in period_ids.items():
        response = await session_client.get(
            f"/payroll/finalized/{period_id}/audit", headers=_auth(auth_token),
        )
        assert response.status_code == 422, f"{status}: {response.text}"
        assert "available only for Locked or Archived periods" in response.json()["detail"]
    await direct_db.execute(text("""
        DELETE FROM payroll.payrollperiods
        WHERE payrollperiodid = ANY(:period_ids)
    """), {"period_ids": list(period_ids.values())})
    await direct_db.commit()


@pytest.mark.asyncio
async def test_finalized_audit_archived_period_keeps_the_same_immutable_history(
    session_client: httpx.AsyncClient, auth_token: str, paytest_branch_id: int,
    direct_db, test_database_url: str,
):
    period_id, snapshot_id, _, review_item_id = await _seed_finalized_period(
        direct_db, test_database_url, paytest_branch_id,
    )
    event_id = await _insert_event(
        direct_db, period_id=period_id, branch_id=paytest_branch_id, snapshot_id=snapshot_id,
    )
    locked = await session_client.get(
        f"/payroll/finalized/{period_id}/audit", headers=_auth(auth_token),
    )
    assert locked.status_code == 200, locked.text
    archived = await session_client.patch(
        f"/payroll/periods/{period_id}/status", json={"status": "Archived"}, headers=_auth(auth_token),
    )
    assert archived.status_code == 200, archived.text
    archived_audit = await session_client.get(
        f"/payroll/finalized/{period_id}/audit", headers=_auth(auth_token),
    )
    assert archived_audit.status_code == 200, archived_audit.text
    assert archived_audit.json()["metadata"]["snapshot_id"] == locked.json()["metadata"]["snapshot_id"]
    assert [event["event_id"] for event in archived_audit.json()["chronology"]] == [event_id]


@pytest.mark.asyncio
async def test_p6d_coverage_distinguishes_legacy_partial_and_empty(
    session_client: httpx.AsyncClient, auth_token: str, paytest_branch_id: int,
    direct_db, test_database_url: str,
):
    period_id, snapshot_id, _, _ = await _seed_finalized_period(direct_db, test_database_url, paytest_branch_id)
    legacy = await session_client.get(f"/payroll/finalized/{period_id}/audit", headers=_auth(auth_token))
    assert legacy.status_code == 200
    assert legacy.json()["metadata"]["section_availability"]["source"]["state"] == "UNAVAILABLE"

    await direct_db.execute(text("""
        INSERT INTO payroll.payrollperiodauditevidencecoverage
            (companyid, branchid, payrollperiodid, evidencedomain, coveragestate)
        VALUES (1, :branch_id, :period_id, 'BONUS', 'COMPLETE')
    """), {"branch_id": paytest_branch_id, "period_id": period_id})
    await direct_db.commit()
    zero = await session_client.get(f"/payroll/finalized/{period_id}/audit", headers=_auth(auth_token))
    assert zero.json()["metadata"]["section_availability"]["bonus"]["state"] == "EMPTY"

    await _insert_event(
        direct_db, period_id=period_id, branch_id=paytest_branch_id, snapshot_id=snapshot_id,
        domain="STATUS_NOTE", action="STATUS_SET",
    )
    # The event was captured after a pre-0065 period existed. Its immutable
    # event is useful, but lifetime completeness remains unknown.
    await direct_db.execute(text("""
        INSERT INTO payroll.payrollperiodauditevidencecoverage
            (companyid, branchid, payrollperiodid, evidencedomain, coveragestate)
        VALUES (1, :branch_id, :period_id, 'REVIEW_COMMENT', 'PARTIAL')
        ON CONFLICT (payrollperiodid, evidencedomain) DO NOTHING
    """), {"branch_id": paytest_branch_id, "period_id": period_id})
    await direct_db.commit()
    partial = await session_client.get(f"/payroll/finalized/{period_id}/audit", headers=_auth(auth_token))
    assert partial.json()["metadata"]["section_availability"]["review_comment"]["state"] == "PARTIAL"


@pytest.mark.asyncio
async def test_p6d_evidence_scope_and_immutability_guards(
    direct_db, paytest_branch_id: int, test_database_url: str,
):
    period_id, snapshot_id, _, _ = await _seed_finalized_period(direct_db, test_database_url, paytest_branch_id)
    event_id = await _insert_event(
        direct_db, period_id=period_id, branch_id=paytest_branch_id, snapshot_id=snapshot_id,
    )
    engine = create_async_engine(test_database_url, echo=False)
    try:
        async with engine.begin() as check_db:
            with pytest.raises(IntegrityError):
                async with check_db.begin_nested():
                    await check_db.execute(text("""
                    UPDATE payroll.payrollperiodauditevidenceevents
                    SET actordisplaynamesnapshot = 'Mutated'
                    WHERE payrollperiodauditevidenceeventid = :event_id
                """), {"event_id": event_id})
            with pytest.raises(IntegrityError):
                async with check_db.begin_nested():
                    await check_db.execute(text("""
                    DELETE FROM payroll.payrollperiodauditevidenceevents
                    WHERE payrollperiodauditevidenceeventid = :event_id
                """), {"event_id": event_id})
            with pytest.raises(IntegrityError):
                async with check_db.begin_nested():
                    await check_db.execute(text("""
                    DELETE FROM payroll.payrollperiodauditevidencesnapshotevents
                    WHERE payrollperiodauditevidenceeventid = :event_id
                """), {"event_id": event_id})
            with pytest.raises(IntegrityError):
                async with check_db.begin_nested():
                    await check_db.execute(text("""
                    UPDATE payroll.payrollperiodauditevidencesnapshotevents
                    SET linkedatutc = NOW()
                    WHERE payrollperiodauditevidenceeventid = :event_id
                """), {"event_id": event_id})
            with pytest.raises(IntegrityError):
                async with check_db.begin_nested():
                    await check_db.execute(text("""
                    UPDATE payroll.payrollperiodauditevidencecoverage
                    SET coveragestate = 'PARTIAL'
                    WHERE payrollperiodid = :period_id AND evidencedomain = 'SOURCE'
                """), {"period_id": period_id})
            with pytest.raises(IntegrityError):
                async with check_db.begin_nested():
                    await check_db.execute(text("""
                    DELETE FROM payroll.payrollperiodauditevidencecoverage
                    WHERE payrollperiodid = :period_id AND evidencedomain = 'SOURCE'
                """), {"period_id": period_id})
            with pytest.raises(IntegrityError):
                async with check_db.begin_nested():
                    await check_db.execute(text("""
                    INSERT INTO payroll.payrollperiodauditevidencecoverage
                        (companyid, branchid, payrollperiodid, evidencedomain, coveragestate)
                    VALUES (999999, :branch_id, :period_id, 'SOURCE', 'COMPLETE')
                """), {"branch_id": paytest_branch_id, "period_id": period_id})
            with pytest.raises(IntegrityError):
                async with check_db.begin_nested():
                    await check_db.execute(text("""
                    INSERT INTO payroll.payrollperiodauditevidencecoverage
                        (companyid, branchid, payrollperiodid, evidencedomain, coveragestate)
                    VALUES (1, :wrong_branch_id, :period_id, 'STATUS_NOTE', 'COMPLETE')
                """), {"wrong_branch_id": paytest_branch_id + 999999, "period_id": period_id})
            with pytest.raises(IntegrityError):
                async with check_db.begin_nested():
                    await check_db.execute(text("""
                    DELETE FROM payroll.payrollperiods WHERE payrollperiodid = :period_id
                """), {"period_id": period_id})
            counts = (await check_db.execute(text("""
                SELECT
                    (SELECT COUNT(*) FROM payroll.payrollperiods WHERE payrollperiodid = :period_id) AS periods,
                    (SELECT COUNT(*) FROM payroll.payrollperiodauditevidencecoverage WHERE payrollperiodid = :period_id) AS coverage,
                    (SELECT COUNT(*) FROM payroll.payrollperiodauditevidenceevents WHERE payrollperiodid = :period_id) AS events,
                    (SELECT COUNT(*) FROM payroll.payrollperiodauditevidencesnapshotevents WHERE payrollperiodauditevidenceeventid = :event_id) AS memberships
            """), {"period_id": period_id, "event_id": event_id})).mappings().one()
            assert dict(counts) == {"periods": 1, "coverage": 1, "events": 1, "memberships": 1}
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_evidence_bearing_period_rejects_delete_after_status_tampered_to_draft(
    direct_db, paytest_branch_id: int,
):
    await _release_draft_slot(direct_db, paytest_branch_id)
    period_id = int((await direct_db.execute(text("""
        INSERT INTO payroll.payrollperiods
            (companyid, branchid, status, periodcode, periodname, periodtype, startdate, enddate)
        VALUES (1, :branch_id, 'Draft', :code, 'P6D draft cascade', 'Week', '2098-03-01', '2098-03-07')
        RETURNING payrollperiodid
    """), {"branch_id": paytest_branch_id, "code": f"P6D-CASCADE-{uuid4().hex}"})).scalar_one())
    event_id = await _insert_event(
        direct_db, period_id=period_id, branch_id=paytest_branch_id, snapshot_id=0,
    )
    await direct_db.execute(text("""
        UPDATE payroll.payrollperiods SET status = 'Open'
        WHERE payrollperiodid = :period_id
    """), {"period_id": period_id})
    await direct_db.commit()
    engine = create_async_engine(direct_db.engine.url, echo=False)
    try:
        async with engine.begin() as check_db:
            with pytest.raises(IntegrityError):
                async with check_db.begin_nested():
                    await check_db.execute(text("""
                    DELETE FROM payroll.payrollperiods WHERE payrollperiodid = :period_id
                """), {"period_id": period_id})
        await direct_db.execute(text("""
            UPDATE payroll.payrollperiods SET status = 'Draft'
            WHERE payrollperiodid = :period_id
        """), {"period_id": period_id})
        await direct_db.commit()
        async with engine.begin() as check_db:
            with pytest.raises(IntegrityError):
                async with check_db.begin_nested():
                    await check_db.execute(text("""
                    DELETE FROM payroll.payrollperiods WHERE payrollperiodid = :period_id
                """), {"period_id": period_id})
    finally:
        await engine.dispose()
    counts = (await direct_db.execute(text("""
        SELECT
            (SELECT COUNT(*) FROM payroll.payrollperiods WHERE payrollperiodid = :period_id) AS periods,
            (SELECT COUNT(*) FROM payroll.payrollperiodauditevidencecoverage WHERE payrollperiodid = :period_id) AS coverage,
            (SELECT COUNT(*) FROM payroll.payrollperiodauditevidenceevents WHERE payrollperiodauditevidenceeventid = :event_id) AS events
    """), {"period_id": period_id, "event_id": event_id})).mappings().one()
    assert dict(counts) == {"periods": 1, "coverage": 1, "events": 1}
    await direct_db.execute(text("""
        UPDATE payroll.payrollperiods SET status = 'Cancelled'
        WHERE payrollperiodid = :period_id
    """), {"period_id": period_id})
    await direct_db.commit()


@pytest.mark.asyncio
async def test_unevidenced_unsnapshotted_draft_period_can_be_deleted(
    direct_db, paytest_branch_id: int,
):
    await _release_draft_slot(direct_db, paytest_branch_id)
    period_id = int((await direct_db.execute(text("""
        INSERT INTO payroll.payrollperiods
            (companyid, branchid, status, periodcode, periodname, periodtype, startdate, enddate)
        VALUES (1, :branch_id, 'Draft', :code, 'P6D empty draft', 'Week', '2098-03-08', '2098-03-14')
        RETURNING payrollperiodid
    """), {"branch_id": paytest_branch_id, "code": f"P6D-EMPTY-{uuid4().hex}"})).scalar_one())
    await direct_db.execute(text("""
        DELETE FROM payroll.payrollperiods WHERE payrollperiodid = :period_id
    """), {"period_id": period_id})
    await direct_db.commit()
    remaining = await direct_db.execute(text("""
        SELECT COUNT(*) FROM payroll.payrollperiods WHERE payrollperiodid = :period_id
    """), {"period_id": period_id})
    assert remaining.scalar_one() == 0


@pytest.mark.asyncio
async def test_0065_downgrade_refuses_when_immutable_evidence_exists(
    direct_db, paytest_branch_id: int, test_database_url: str,
) -> None:
    period_id, snapshot_id, _, _ = await _seed_finalized_period(
        direct_db, test_database_url, paytest_branch_id,
    )
    await _insert_event(
        direct_db, period_id=period_id, branch_id=paytest_branch_id, snapshot_id=snapshot_id,
    )
    with pytest.raises(RuntimeError, match="Downgrade of 0065 refused: immutable P6D evidence exists"):
        await direct_db.run_sync(_run_0065_migration, "downgrade")


@pytest.mark.asyncio
async def test_0065_empty_downgrade_and_reupgrade_succeeds_in_isolated_database(
    isolated_0065_database,
):
    """A clean 0065 schema can downgrade and return to the current schema."""
    engine = create_async_engine(isolated_0065_database, echo=False)
    try:
        async with engine.connect() as db:
            await db.execution_options(isolation_level="AUTOCOMMIT")
            evidence_count = await db.execute(text("""
                SELECT COUNT(*)
                FROM payroll.payrollperiodauditevidenceevents
            """))
            assert evidence_count.scalar_one() == 0

            await db.run_sync(_run_0065_migration, "downgrade")
            dropped = await db.execute(text("""
                SELECT to_regclass('payroll.payrollperiodauditevidenceevents')
            """))
            assert dropped.scalar_one() is None

            await db.run_sync(_run_0065_migration, "upgrade")
            restored = await db.execute(text("""
                SELECT to_regclass('payroll.payrollperiodauditevidenceevents')
            """))
            assert restored.scalar_one() == "payroll.payrollperiodauditevidenceevents"

            trigger_exists = await db.execute(text("""
                SELECT EXISTS (
                    SELECT 1
                    FROM pg_trigger t
                    JOIN pg_class c ON c.oid = t.tgrelid
                    JOIN pg_namespace n ON n.oid = c.relnamespace
                    WHERE n.nspname = 'payroll'
                      AND c.relname = 'payrollperiods'
                      AND t.tgname = 'trg_payrollperiods_auditevidencedelete'
                )
            """))
            assert trigger_exists.scalar_one() is True
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_review_comment_evidence_requires_period_valid_review_and_exact_membership(
    direct_db, paytest_branch_id: int, test_database_url: str,
):
    period_a, snapshot_a, _, review_a = await _seed_finalized_period(
        direct_db, test_database_url, paytest_branch_id,
    )
    period_b, snapshot_b, _, review_b = await _seed_finalized_period(
        direct_db, test_database_url, paytest_branch_id,
    )
    wrong_type_review = int((await direct_db.execute(text("""
        INSERT INTO review.managerreviewitems
            (companyid, branchid, requestedbyuserid, requesttype, entityschema, entityname,
             entityid, title, priority, status, payrollcalculationsnapshotid)
        VALUES (1, :branch_id, 1, 'Other', 'payroll', 'PayrollPeriods', :period_id,
                'P6D wrong request type', 'Normal', 'Approved', :snapshot_id)
        RETURNING reviewitemid
    """), {
            "branch_id": paytest_branch_id, "period_id": str(period_a), "snapshot_id": None,
    })).scalar_one())
    missing_snapshot_review = int((await direct_db.execute(text("""
        INSERT INTO review.managerreviewitems
            (companyid, branchid, requestedbyuserid, requesttype, entityschema, entityname,
             entityid, title, priority, status)
        VALUES (1, :branch_id, 1, 'PeriodApproval', 'payroll', 'PayrollPeriods', :period_id,
                'P6D missing snapshot', 'Normal', 'Approved')
        RETURNING reviewitemid
    """), {
        "branch_id": paytest_branch_id, "period_id": str(period_a),
    })).scalar_one())
    await direct_db.commit()

    for invalid_review_id in (review_b, wrong_type_review, missing_snapshot_review):
        with pytest.raises(IntegrityError):
            await _insert_event(
                direct_db, period_id=period_a, branch_id=paytest_branch_id, snapshot_id=snapshot_a,
                domain="REVIEW_COMMENT", action="REVIEW_COMMENT_ADDED",
                review_item_id=invalid_review_id,
            )

    correct_event = await _insert_event(
        direct_db, period_id=period_a, branch_id=paytest_branch_id, snapshot_id=snapshot_a,
        domain="REVIEW_COMMENT", action="REVIEW_COMMENT_ADDED", review_item_id=review_a,
    )
    assert correct_event > 0

    with pytest.raises(IntegrityError):
        await _insert_event(
            direct_db, period_id=period_a, branch_id=paytest_branch_id, snapshot_id=snapshot_b,
            domain="REVIEW_COMMENT", action="REVIEW_COMMENT_ADDED", review_item_id=review_a,
        )
    with pytest.raises(IntegrityError):
        await _insert_event(
            direct_db, period_id=period_a, branch_id=paytest_branch_id, snapshot_id=snapshot_a,
            domain="REVIEW_COMMENT", action="REVIEW_COMMENT_ADDED", review_item_id=review_a,
            link_membership=False,
        )


@pytest.mark.asyncio
async def test_daily_source_create_update_void_capture_real_writer_history(
    session_client: httpx.AsyncClient, auth_token: str, paytest_branch_id: int, direct_db,
):
    period_id, driver_id, work_date = await _seed_open_period(direct_db, paytest_branch_id)
    created = await session_client.post(
        f"/payroll/periods/{period_id}/lines", headers=_auth(auth_token), json={
            "driver_id": driver_id, "work_date": work_date.isoformat(),
            "line_type": "HOURS", "quantity": "2", "notes": "created",
        },
    )
    assert created.status_code == 201, created.text
    line_id = created.json()["draft_line_id"]
    updated = await session_client.patch(
        f"/payroll/periods/{period_id}/lines/{line_id}", headers=_auth(auth_token),
        json={"quantity": "3", "notes": "updated"},
    )
    assert updated.status_code == 200, updated.text
    voided = await session_client.delete(
        f"/payroll/periods/{period_id}/lines/{line_id}", headers=_auth(auth_token),
    )
    assert voided.status_code == 204, voided.text

    events = [event for event in await _events(direct_db, period_id) if event["evidencedomain"] == "SOURCE"]
    assert [event["actioncode"] for event in events] == [
        "SOURCE_CREATED", "SOURCE_UPDATED", "SOURCE_VOIDED",
    ]
    assert all(event["driverid"] == driver_id and event["workdate"] == work_date for event in events)
    assert all(event["sourceentityid"] == str(line_id) for event in events)
    assert all(event["payitemid"] is not None for event in events)
    assert events[0]["beforestatejson"] is None
    assert events[0]["afterstatejson"]["quantity"] == "2"
    assert Decimal(events[1]["beforestatejson"]["quantity"]) == Decimal("2")
    assert Decimal(events[1]["afterstatejson"]["quantity"]) == Decimal("3")
    assert events[2]["afterstatejson"] == {"status": "Void"}
    assert all(event["actordisplaynamesnapshot"] for event in events)
    assert len({event["actordisplaynamesnapshot"] for event in events}) == 1


@pytest.mark.asyncio
async def test_canonical_status_and_note_transitions_capture_once_each(
    session_client: httpx.AsyncClient, auth_token: str, paytest_branch_id: int, direct_db,
):
    period_id, driver_id, work_date = await _seed_open_period(direct_db, paytest_branch_id)
    status_a, status_b = (f"P6D-A-{uuid4().hex[:12]}", f"P6D-B-{uuid4().hex[:12]}")
    for code in (status_a, status_b):
        await direct_db.execute(text("""
            INSERT INTO payroll.payrollstatuskeys
                (companyid, branchid, statuscode, normalizedstatuscode, keyname,
                 isoffreason, hoursvalue, isactive, displayorder)
            VALUES (1, :branch_id, :code, :normalized_code, :label, FALSE, 0, TRUE, 999)
        """), {
            "branch_id": paytest_branch_id, "code": code, "normalized_code": code.upper(),
            "label": f"Frozen {code}",
        })
    await direct_db.commit()

    async def save(status_key: str | None, notes: str | None) -> None:
        response = await session_client.post(
            f"/payroll/periods/{period_id}/day-grid", headers=_auth(auth_token), json={
                "work_date": work_date.isoformat(),
                "rows": [{"driver_id": driver_id, "values": {}, "status_key": status_key, "notes": notes}],
            },
        )
        assert response.status_code == 200, response.text

    await save(status_a, "Note A")
    await save(status_b, "Note B")
    await save(None, None)
    events = [event for event in await _events(direct_db, period_id) if event["evidencedomain"] == "STATUS_NOTE"]
    assert [event["actioncode"] for event in events] == [
        "STATUS_SET", "NOTE_SET", "STATUS_CHANGED", "NOTE_CHANGED", "STATUS_CLEARED", "NOTE_CLEARED",
    ]
    assert all(event["driverid"] == driver_id and event["workdate"] == work_date for event in events)
    assert all(event["payitemid"] is None for event in events)
    assert events[0]["afterstatejson"]["status_label"] == f"Frozen {status_a}"
    assert events[2]["beforestatejson"]["status_code"] == status_a
    assert events[2]["afterstatejson"]["status_code"] == status_b
    assert events[3]["beforestatejson"] == {"note": "Note A"}
    assert events[3]["afterstatejson"] == {"note": "Note B"}


@pytest.mark.asyncio
async def test_day_grid_combined_work_status_and_note_capture_logical_events_once(
    session_client: httpx.AsyncClient, auth_token: str, paytest_branch_id: int, direct_db,
):
    period_id, driver_id, work_date = await _seed_open_period(direct_db, paytest_branch_id)
    status_code = f"P6D-COMBINED-{uuid4().hex[:12]}"
    await direct_db.execute(text("""
        INSERT INTO payroll.payrollstatuskeys
            (companyid, branchid, statuscode, normalizedstatuscode, keyname,
             isoffreason, hoursvalue, isactive, displayorder)
        VALUES (1, :branch_id, :code, :normalized_code, 'P6D combined', FALSE, 0, TRUE, 999)
    """), {
        "branch_id": paytest_branch_id, "code": status_code,
        "normalized_code": status_code.upper(),
    })
    await direct_db.commit()
    response = await session_client.post(
        f"/payroll/periods/{period_id}/day-grid", headers=_auth(auth_token), json={
            "work_date": work_date.isoformat(),
            "rows": [{
                "driver_id": driver_id, "values": {"HOURS": "2"},
                "status_key": status_code, "notes": "Combined note",
            }],
        },
    )
    assert response.status_code == 200, response.text
    events = await _events(direct_db, period_id)
    source = [event for event in events if event["evidencedomain"] == "SOURCE"]
    status_note = [event for event in events if event["evidencedomain"] == "STATUS_NOTE"]
    assert [event["actioncode"] for event in source] == ["SOURCE_CREATED"]
    assert [event["actioncode"] for event in status_note] == ["STATUS_SET", "NOTE_SET"]
    assert all(event["driverid"] == driver_id and event["workdate"] == work_date for event in events)


@pytest.mark.asyncio
async def test_real_review_comment_is_frozen_and_linked_to_its_review_snapshot(
    session_client: httpx.AsyncClient, auth_token: str, paytest_branch_id: int,
    direct_db, test_database_url: str,
):
    await _remove_p6d_writer_periods(direct_db, paytest_branch_id)
    period_id, snapshot_id, _, review_item_id = await _seed_finalized_period(
        direct_db, test_database_url, paytest_branch_id, finalize=False,
    )
    comment = await session_client.post(
        f"/review/items/{review_item_id}/decide",
        json={"decision": "Comment", "decision_reason": "Frozen review comment"},
        headers=_auth(auth_token),
    )
    assert comment.status_code == 200, comment.text
    row = (await direct_db.execute(text("""
        SELECT payrollperiodauditevidenceeventid, actoruserid, actordisplaynamesnapshot,
               responsibilitycontextsnapshot, occurredatutc
        FROM payroll.payrollperiodauditevidenceevents
        WHERE payrollperiodid = :period_id AND evidencedomain = 'REVIEW_COMMENT'
          AND actioncode = 'REVIEW_COMMENT_ADDED'
    """), {"period_id": period_id})).mappings().one()
    membership = (await direct_db.execute(text("""
        SELECT payrollcalculationsnapshotid
        FROM payroll.payrollperiodauditevidencesnapshotevents
        WHERE payrollperiodauditevidenceeventid = :event_id
    """), {"event_id": row["payrollperiodauditevidenceeventid"]})).scalar_one()
    assert membership == snapshot_id
    assert row["actoruserid"] == 1
    assert row["actordisplaynamesnapshot"]
    assert row["responsibilitycontextsnapshot"] is not None
    assert row["occurredatutc"] is not None
    # This fixture needs no further workflow evidence. Moving it out of the
    # exclusive InReview slot avoids creating a competing Returned work item.
    await direct_db.execute(text("""
        UPDATE payroll.payrollperiods SET status = 'Approved'
        WHERE payrollperiodid = :period_id
    """), {"period_id": period_id})
    await direct_db.commit()


@pytest.mark.asyncio
async def test_non_bonus_period_pay_create_update_void_capture_source_history(
    session_client: httpx.AsyncClient, auth_token: str, paytest_branch_id: int, direct_db,
):
    pay_item = await _create_legacy_period_pay_item(direct_db, paytest_branch_id)
    period_id, driver_id, _ = await _seed_open_period(direct_db, paytest_branch_id, status="Open")
    snapshot_item = (await direct_db.execute(text("""
        SELECT payitemcode
        FROM payroll.payrollperiodpayitems
        WHERE payrollperiodid = :period_id AND itemscope = 'Period'
          AND payitemcode = :pay_item AND isactiveinperiod = TRUE
        ORDER BY sortorder, payrollperiodpayitemid
        LIMIT 1
    """), {"period_id": period_id, "pay_item": pay_item})).scalar_one_or_none()
    assert snapshot_item == pay_item, "Legacy Period Pay item must be frozen active in the period layout"
    created = await session_client.post(
        f"/payroll/periods/{period_id}/period-pay", headers=_auth(auth_token),
        json={"driver_id": driver_id, "line_type": pay_item, "amount": "10", "notes": "created"},
    )
    assert created.status_code == 201, created.text
    line_id = created.json()["draft_line_id"]
    updated = await session_client.patch(
        f"/payroll/periods/{period_id}/period-pay/{line_id}", headers=_auth(auth_token),
        json={"amount": "12", "notes": "updated"},
    )
    assert updated.status_code == 200, updated.text
    voided = await session_client.delete(
        f"/payroll/periods/{period_id}/period-pay/{line_id}", headers=_auth(auth_token),
    )
    assert voided.status_code == 200, voided.text
    events = [event for event in await _events(direct_db, period_id) if event["evidencedomain"] == "SOURCE"]
    assert [event["actioncode"] for event in events] == [
        "SOURCE_CREATED", "SOURCE_UPDATED", "SOURCE_VOIDED",
    ]
    assert all(event["workdate"] is None and event["driverid"] == driver_id for event in events)
    assert all(event["payitemid"] is not None for event in events)
    assert events[0]["afterstatejson"]["line_scope"] == "Period"
    assert Decimal(events[1]["beforestatejson"]["calculated_amount"]) == Decimal("10")
    # The current writer records SQL column names in the immutable evidence
    # payload for the update; the source-of-truth amount is calculatedamount.
    assert Decimal(events[1]["afterstatejson"]["calculatedamount"]) == Decimal("12")


@pytest.mark.asyncio
async def test_single_bonus_create_update_void_captures_canonical_history(
    session_client: httpx.AsyncClient, auth_token: str, paytest_branch_id: int, direct_db,
):
    period_id, driver_id, _ = await _seed_open_period(direct_db, paytest_branch_id, status="Open")
    created = await session_client.post(
        f"/payroll/periods/{period_id}/bonuses", headers=_auth(auth_token),
        json={"driver_id": driver_id, "amount": "10", "reason": "Created", "notes": "first"},
    )
    assert created.status_code == 201, created.text
    bonus_event_id = created.json()["bonus_event_id"]
    updated = await session_client.patch(
        f"/payroll/periods/{period_id}/bonuses/{bonus_event_id}", headers=_auth(auth_token),
        json={"expected_data_revision": 1, "amount": "12", "reason": "Updated", "notes": "second"},
    )
    assert updated.status_code == 200, updated.text
    voided = await session_client.delete(
        f"/payroll/periods/{period_id}/bonuses/{bonus_event_id}", headers=_auth(auth_token),
    )
    assert voided.status_code == 200, voided.text
    events = [event for event in await _events(direct_db, period_id) if event["evidencedomain"] == "BONUS"]
    assert [event["actioncode"] for event in events] == [
        "BONUS_CREATED", "BONUS_UPDATED", "BONUS_VOIDED",
    ]
    assert all(event["sourceentitytype"] == "PayrollBonusEvents" for event in events)
    assert all(event["sourceentityid"] == str(bonus_event_id) for event in events)
    assert events[0]["afterstatejson"]["reason"] == "Created"
    assert events[1]["beforestatejson"]["notes"] == "first"
    assert events[1]["afterstatejson"]["notes"] == "second"
    assert events[2]["afterstatejson"]["status"] == "Voided"
    assert [event["sourcerevision"] for event in events] == [1, 2, 3]


@pytest.mark.asyncio
async def test_bonus_batch_captures_correlation_and_idempotent_replay_without_duplicates(
    session_client: httpx.AsyncClient, auth_token: str, paytest_branch_id: int, direct_db,
):
    period_id, first_driver_id, _ = await _seed_open_period(direct_db, paytest_branch_id, status="Open")
    second_driver_id = await _seed_driver(direct_db, paytest_branch_id)
    await direct_db.commit()
    body = {
        "idempotency_key": str(uuid4()), "expected_bonus_data_revision": 0,
        "items": [
            {"driver_id": first_driver_id, "amount": "10", "reason": "Batch one"},
            {"driver_id": second_driver_id, "amount": "20", "reason": "Batch two"},
        ],
    }
    created = await session_client.post(
        f"/payroll/periods/{period_id}/bonuses/batch", headers=_auth(auth_token), json=body,
    )
    assert created.status_code == 201, created.text
    correlation_id = created.json()["batch_correlation_id"]
    events_after_create = [event for event in await _events(direct_db, period_id) if event["evidencedomain"] == "BONUS"]
    assert len(events_after_create) == 2
    assert {str(event["correlationid"]) for event in events_after_create} == {correlation_id}
    assert {event["actioncode"] for event in events_after_create} == {"BONUS_CREATED"}

    replay = await session_client.post(
        f"/payroll/periods/{period_id}/bonuses/batch", headers=_auth(auth_token), json=body,
    )
    assert replay.status_code == 200, replay.text
    events_after_replay = [event for event in await _events(direct_db, period_id) if event["evidencedomain"] == "BONUS"]
    assert events_after_replay == events_after_create


@pytest.mark.asyncio
async def test_finalized_audit_groups_returned_resubmission_by_exact_snapshot_revision(
    session_client: httpx.AsyncClient, auth_token: str, paytest_branch_id: int,
    direct_db, test_database_url: str,
):
    await _remove_p6d_writer_periods(direct_db, paytest_branch_id)
    period_id, first_snapshot_id, _, first_review_item_id = await _seed_finalized_period(
        direct_db, test_database_url, paytest_branch_id, finalize=False,
    )
    first_event_id = await _insert_event(
        direct_db, period_id=period_id, branch_id=paytest_branch_id, snapshot_id=first_snapshot_id,
        domain="SOURCE", action="SOURCE_CREATED",
    )
    returned = await session_client.post(
        f"/review/items/{first_review_item_id}/decide",
        json={"decision": "EditRequested", "decision_reason": "Correct source"},
        headers=_auth(auth_token),
    )
    assert returned.status_code == 200, returned.text
    lines = await session_client.get(f"/payroll/periods/{period_id}/lines", headers=_auth(auth_token))
    assert lines.status_code == 200, lines.text
    corrected = await session_client.patch(
        f"/payroll/periods/{period_id}/lines/{lines.json()[0]['draft_line_id']}",
        json={"quantity": "3.0000"}, headers=_auth(auth_token),
    )
    assert corrected.status_code == 200, corrected.text
    resubmitted = await session_client.post(
        f"/payroll/periods/{period_id}/resubmissions", headers=_auth(auth_token),
    )
    assert resubmitted.status_code == 200, resubmitted.text
    second_review = (await direct_db.execute(text("""
        SELECT reviewitemid, payrollcalculationsnapshotid
        FROM review.managerreviewitems
        WHERE entityschema = 'payroll' AND entityname = 'PayrollPeriods'
          AND entityid = :period_id AND status = 'Pending'
        ORDER BY reviewitemid DESC
        LIMIT 1
    """), {"period_id": str(period_id)})).mappings().one()
    second_snapshot_id = int(second_review["payrollcalculationsnapshotid"])
    assert second_snapshot_id != first_snapshot_id
    approved = await session_client.post(
        f"/review/items/{second_review['reviewitemid']}/decide",
        json={"decision": "Approved", "decision_reason": "Approved corrected source"},
        headers=_auth(auth_token),
    )
    assert approved.status_code == 200, approved.text
    engine = create_async_engine(test_database_url, echo=False)
    try:
        async with engine.begin() as finalize_db:
            result = await finalize_period(period_id, 1, 1, finalize_db)
            assert result.status == "Locked"
    finally:
        await engine.dispose()

    response = await session_client.get(
        f"/payroll/finalized/{period_id}/audit", headers=_auth(auth_token),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["metadata"]["snapshot_id"] == second_snapshot_id
    groups = {group["snapshot_id"]: group for group in body["revision_groups"]}
    assert groups[first_snapshot_id]["revision_number"] == 1
    assert groups[first_snapshot_id]["event_ids"] == [first_event_id]
    assert groups[first_snapshot_id]["is_final_approved_revision"] is False
    assert groups[second_snapshot_id]["revision_number"] == 2
    assert groups[second_snapshot_id]["is_final_approved_revision"] is True
    corrected_event = next(
        event for event in body["source_events"] if event["action_code"] == "SOURCE_UPDATED"
    )
    assert corrected_event["snapshot_id"] == second_snapshot_id
    assert corrected_event["revision_number"] == 2
    assert Decimal(corrected_event["after_state"]["quantity"]) == Decimal("3")
    assert any(event["action_code"] == "RESUBMITTED" for event in body["lifecycle_events"])
    assert any(event["action_code"] == "REVIEW_EDIT_REQUESTED" for event in body["lifecycle_events"])
    assert any(event["action_code"] == "REVIEW_APPROVED" for event in body["lifecycle_events"])
    assert any(event["action_code"] == "FINALIZED" for event in body["lifecycle_events"])


@pytest.mark.asyncio
async def test_finalized_audit_status_and_responsibility_do_not_fallback_to_mutable_catalogs(
    session_client: httpx.AsyncClient, auth_token: str, paytest_branch_id: int,
    direct_db, test_database_url: str,
):
    period_id, snapshot_id, _, _ = await _seed_finalized_period(
        direct_db, test_database_url, paytest_branch_id,
    )
    status_code = f"P6D-FROZEN-{uuid4().hex[:12]}"
    status_key_id = int((await direct_db.execute(text("""
        INSERT INTO payroll.payrollstatuskeys
            (companyid, branchid, statuscode, normalizedstatuscode, keyname,
             isoffreason, hoursvalue, isactive, displayorder)
        VALUES (1, :branch_id, :code, :normalized_code, 'Frozen Status Label', FALSE, 0, TRUE, 999)
        RETURNING statuskeyid
    """), {
        "branch_id": paytest_branch_id, "code": status_code,
        "normalized_code": status_code.upper(),
    })).scalar_one())
    event_id = await _insert_event(
        direct_db, period_id=period_id, branch_id=paytest_branch_id, snapshot_id=snapshot_id,
        domain="STATUS_NOTE", action="STATUS_SET",
        after_state={"status_code": status_code, "status_label": "Frozen Status Label", "is_off": False},
        responsibility_context={"role_code": "FROZEN_ROLE", "role_name": "Frozen Role"},
    )
    before = await session_client.get(
        f"/payroll/finalized/{period_id}/audit", headers=_auth(auth_token),
    )
    assert before.status_code == 200, before.text
    before_event = next(item for item in before.json()["status_note_events"] if item["event_id"] == event_id)

    previous_role_name = (await direct_db.execute(text("""
        SELECT rolename FROM sec.companyroles
        WHERE companyid = 1 AND rolecode = 'COMPANY_OWNER'
    """))).scalar_one()
    await direct_db.execute(text("""
        UPDATE payroll.payrollstatuskeys
        SET keyname = 'Mutable replacement', isactive = FALSE
        WHERE statuskeyid = :status_key_id
    """), {"status_key_id": status_key_id})
    await direct_db.execute(text("""
        UPDATE sec.companyroles
        SET rolename = 'Mutable role replacement'
        WHERE companyid = 1 AND rolecode = 'COMPANY_OWNER'
    """))
    await direct_db.commit()

    after = await session_client.get(
        f"/payroll/finalized/{period_id}/audit", headers=_auth(auth_token),
    )
    assert after.status_code == 200, after.text
    after_event = next(item for item in after.json()["status_note_events"] if item["event_id"] == event_id)
    assert after_event["after_state"] == before_event["after_state"]
    assert after_event["after_state"]["status_label"] == "Frozen Status Label"
    assert after_event["responsibility_context"] == before_event["responsibility_context"]
    assert after_event["responsibility_context"]["role_name"] == "Frozen Role"

    legacy_period_id, _, _, _ = await _seed_finalized_period(
        direct_db, test_database_url, paytest_branch_id,
    )
    legacy = await session_client.get(
        f"/payroll/finalized/{legacy_period_id}/audit", headers=_auth(auth_token),
    )
    assert legacy.status_code == 200, legacy.text
    assert legacy.json()["metadata"]["section_availability"]["status_note"]["state"] == "UNAVAILABLE"
    assert legacy.json()["status_note_events"] == []
    await direct_db.execute(text("""
        UPDATE sec.companyroles
        SET rolename = :role_name
        WHERE companyid = 1 AND rolecode = 'COMPANY_OWNER'
    """), {"role_name": previous_role_name})
    await direct_db.commit()


@pytest.mark.asyncio
async def test_finalized_audit_bonus_history_does_not_fallback_to_mutable_bonus_event(
    session_client: httpx.AsyncClient, auth_token: str, paytest_branch_id: int,
    direct_db, test_database_url: str,
):
    seed = await _seed_finalized_rates_period(direct_db, test_database_url, paytest_branch_id)
    event_id = await _insert_event(
        direct_db, period_id=seed["period_id"], branch_id=paytest_branch_id,
        snapshot_id=seed["snapshot_id"], domain="BONUS", action="BONUS_UPDATED",
        after_state={"amount": "4", "reason": "Frozen audit reason", "notes": "Frozen audit notes"},
    )
    await direct_db.execute(text("""
        UPDATE payroll.payrollbonusevents
        SET reason = 'Mutable replacement', notes = 'Mutable replacement'
        WHERE payrollbonuseventid = :bonus_id
    """), {"bonus_id": seed["bonus_id"]})
    await direct_db.commit()

    response = await session_client.get(
        f"/payroll/finalized/{seed['period_id']}/audit", headers=_auth(auth_token),
    )
    assert response.status_code == 200, response.text
    event = next(item for item in response.json()["bonus_events"] if item["event_id"] == event_id)
    assert event["after_state"] == {
        "amount": "4", "reason": "Frozen audit reason", "notes": "Frozen audit notes",
    }


@pytest.mark.asyncio
async def test_finalized_audit_uses_frozen_used_rate_provenance(
    session_client: httpx.AsyncClient, auth_token: str, paytest_branch_id: int,
    direct_db, test_database_url: str,
):
    seed = await _seed_finalized_rates_period(direct_db, test_database_url, paytest_branch_id)
    before = await session_client.get(
        f"/payroll/finalized/{seed['period_id']}/audit", headers=_auth(auth_token),
    )
    assert before.status_code == 200, before.text
    before_body = before.json()
    assert before_body["metadata"]["snapshot_id"] == seed["snapshot_id"]
    assert {item["evidence_kind"] for item in before_body["rate_rule_provenance"]} == {
        "DriverRate", "DriverPayRule",
    }

    # Existing finalized-rate protections reject the mutable source change.
    # P6D reads only snapshot-used definitions, never the current rate catalog.
    with pytest.raises(IntegrityError):
        await direct_db.execute(text("""
            UPDATE payroll.driverrates SET amount = 99.0000
            WHERE driverrateid = :rate_id
        """), {"rate_id": seed["rate_id"]})

    after = await session_client.get(
        f"/payroll/finalized/{seed['period_id']}/audit", headers=_auth(auth_token),
    )
    assert after.status_code == 200, after.text
    assert after.json()["metadata"]["snapshot_id"] == seed["snapshot_id"]
    assert after.json()["rate_rule_provenance"] == before_body["rate_rule_provenance"]


@pytest.mark.asyncio
async def test_finalized_audit_reads_multi_event_history_with_set_based_queries(
    session_client: httpx.AsyncClient, auth_token: str, paytest_branch_id: int,
    direct_db, test_database_url: str, monkeypatch: pytest.MonkeyPatch,
):
    period_id, snapshot_id, _, review_item_id = await _seed_finalized_period(
        direct_db, test_database_url, paytest_branch_id,
    )
    driver_ids = [await _seed_driver(direct_db, paytest_branch_id) for _ in range(2)]
    domain_actions = {
        "SOURCE": "SOURCE_CREATED",
        "STATUS_NOTE": "STATUS_SET",
        "BONUS": "BONUS_CREATED",
        "REVIEW_COMMENT": "REVIEW_COMMENT_ADDED",
    }
    for index in range(12):
        domain = ("SOURCE", "STATUS_NOTE", "BONUS", "REVIEW_COMMENT")[index % 4]
        await _insert_event(
            direct_db, period_id=period_id, branch_id=paytest_branch_id, snapshot_id=snapshot_id,
            domain=domain, action=domain_actions[domain], driver_id=driver_ids[index % len(driver_ids)],
            after_state={"sequence": index},
            review_item_id=review_item_id if domain == "REVIEW_COMMENT" else None,
        )

    statements: list[str] = []
    original_execute = AsyncConnection.execute

    async def counted_execute(connection, statement, *args, **kwargs):
        statements.append(str(statement).lower())
        return await original_execute(connection, statement, *args, **kwargs)

    monkeypatch.setattr(AsyncConnection, "execute", counted_execute)
    response = await session_client.get(
        f"/payroll/finalized/{period_id}/audit", headers=_auth(auth_token),
    )
    assert response.status_code == 200, response.text
    assert len(response.json()["chronology"]) == 12
    event_payload_reads = [
        statement for statement in statements
        if "from payroll.payrollperiodauditevidenceevents e" in statement
    ]
    workflow_reads = [
        statement for statement in statements
        if "from payroll.payrollperiodworkflowactionevidence w" in statement
    ]
    assert len(event_payload_reads) == 1
    assert len(workflow_reads) == 1
