"""The canonical demo seed and the development reset, on a fresh disposable database.

A fresh database is upgraded through the single Alembic head and seeded with the canonical
demo exactly as ``scripts/reset_dev_database.py`` does. The seeded Company is then driven
through the real payroll workflow, and the development reset restores the canonical
payroll-ready state from that payroll history.
"""
from __future__ import annotations

import os
import subprocess
import sys
from datetime import date, timedelta
from decimal import Decimal

import psycopg2
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.db.schema_guard import _check_target_physical_authorities, run_schema_guard
from app.payroll.final_line_projection import verify_final_lines
from app.payroll.snapshot_verification import verify_snapshot
from scripts import canonical_demo
from scripts.canonical_demo import (
    PAYROLL_HISTORY_TABLES,
    SeedError,
    seed_canonical_demo,
    verify_canonical_demo,
)
from tests.test_company_status_catalog_migration import _alembic, _disposable_database, _version

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest_asyncio.fixture(scope="module")
async def seeded(pg_instance):
    with _disposable_database(pg_instance, "canonical") as (env, dsn):
        heads = _alembic(env, "heads")
        assert heads.returncode == 0, heads.stderr
        upgrade = _alembic(env, "upgrade", "head")
        assert upgrade.returncode == 0, upgrade.stderr
        summary = await seed_canonical_demo(env["DATABASE_URL"])
        yield {"env": env, "dsn": dsn, "heads": heads.stdout, "summary": summary}


def _count(dsn, statement: str, *params) -> int:
    with psycopg2.connect(**dsn) as conn, conn.cursor() as cursor:
        cursor.execute(statement, params)
        return cursor.fetchone()[0]


async def test_a_fresh_database_upgrades_to_the_single_head_and_holds_the_canonical_demo(seeded):
    dsn = seeded["dsn"]
    assert [line.split()[0] for line in seeded["heads"].splitlines() if "(head)" in line] == ["0093"]
    assert _version(dsn) == "0093"
    run_schema_guard(seeded["env"]["DATABASE_URL"], is_dev=True)
    with psycopg2.connect(**dsn) as conn, conn.cursor() as cursor:
        assert _check_target_physical_authorities(cursor) == []
    assert await verify_canonical_demo(seeded["env"]["DATABASE_URL"]) == []
    for table in PAYROLL_HISTORY_TABLES:
        assert _count(dsn, f"SELECT count(*) FROM {table}") == 0, table


async def test_the_seed_refuses_a_database_that_already_holds_data(seeded):
    users = _count(seeded["dsn"], "SELECT count(*) FROM sec.users")
    with pytest.raises(SeedError, match="freshly migrated, empty database"):
        await seed_canonical_demo(seeded["env"]["DATABASE_URL"])
    assert _count(seeded["dsn"], "SELECT count(*) FROM sec.users") == users


def _strip_generated(value):
    """A read model without its generation timestamps, for before/after comparison."""
    if isinstance(value, dict):
        return {k: _strip_generated(v) for k, v in value.items() if k != "generated_at_utc"}
    if isinstance(value, list):
        return [_strip_generated(v) for v in value]
    return value


async def test_the_seeded_company_runs_payroll_from_entry_to_the_finalized_library(seeded):
    """The canonical actors drive the real workflow on the seeded, payroll-ready Company."""
    url, dsn, seed = seeded["env"]["DATABASE_URL"], seeded["dsn"], seeded["summary"]
    engine = create_async_engine(url)
    client = canonical_demo.application_client(engine)
    try:
        async def login(username: str) -> dict[str, str]:
            response = await client.post("/auth/login", json={
                "username": username, "password": canonical_demo.PASSWORD,
                "company_code": canonical_demo.COMPANY_CODE})
            assert response.status_code == 200, response.text
            return {"Authorization": f"Bearer {response.json()['access_token']}"}

        async def call(method, path, headers, expected=200, **kwargs):
            response = await client.request(method, path, headers=headers, **kwargs)
            assert response.status_code == expected, f"{method} {path}: {response.text}"
            return response.json() if response.content else None

        owner = await login(canonical_demo.OWNER_USERNAME)
        manager = await login(canonical_demo.PAYROLL_MANAGER_USERNAME)
        driver = await login(canonical_demo.DRIVER_USERNAME)
        hq, north = seed.hq_branch_id, seed.north_branch_id

        # Access: the staged account cannot sign in; the Owner is not an Employee.
        staged = await client.post("/auth/login", json={
            "username": canonical_demo.STAGED_USERNAME, "password": canonical_demo.PASSWORD,
            "company_code": canonical_demo.COMPANY_CODE})
        assert staged.status_code in (401, 403), staged.text
        me = await call("GET", "/auth/me", owner)
        assert me["currency_code"] == "USD" and me["self_assignments"] == []
        assert [b["scope"] for b in me["branches"]] == ["AllCompanyBranches"]
        assert (await call("GET", f"/admin/users/{seed.owner_user_id}", owner))["employee_id"] is None
        staged_user = await call("GET", f"/admin/users/{seed.staged_user_id}", owner)
        assert staged_user["is_staged"] and not staged_user["can_login"]

        # Branch scope: the HQ Payroll Manager sees HQ only; the Owner sees both Branches.
        assert [b["branch_id"] for b in await call("GET", "/core/branches", manager)] == [hq]
        assert {b["branch_id"] for b in await call("GET", "/core/branches", owner)} == {hq, north}
        owner_roster = await call("GET", "/core/driver-assignments", owner)
        assert {(r["driver_assignment_id"], r["state"]) for r in owner_roster} == {
            (seed.current_driver_assignment_id, "current"),
            (seed.future_driver_assignment_id, "pending")}
        manager_roster = await call("GET", "/core/driver-assignments", manager)
        assert [r["driver_assignment_id"] for r in manager_roster] == [seed.current_driver_assignment_id]
        await call("GET", f"/payroll/branches/{north}/period-candidates", manager, 403,
                   params={"mode": "OPEN_CREATION"})
        await call("GET", "/workforce/employees", manager, 403)

        # DRIVER/Self: its own Driver through the permanent Driver, nothing operational.
        driver_me = await call("GET", "/auth/me", driver)
        assert [(a["role_code"], a["scope"]) for a in driver_me["self_assignments"]] == [
            ("DRIVER", "Self")]
        for path in ("/core/driver-assignments", "/payroll/periods", "/payroll/finalized",
                     "/review/items", "/workforce/employees"):
            await call("GET", path, driver, 403)

        # Period creation under the published default Payroll Setup.
        candidates = await call("GET", f"/payroll/branches/{hq}/period-candidates", manager,
                                params={"mode": "OPEN_CREATION"})
        period = await call("POST", f"/payroll/branches/{hq}/period-creations", manager, 201,
                            json={"candidate_key": candidates["selected"]["candidate_key"]})
        period_id = period["payroll_period_id"]
        start = date.fromisoformat(period["start_date"])
        assert start == seed.payroll_setup_anchor and period["status"] == "Open"
        assert date.fromisoformat(period["end_date"]) == start + timedelta(days=6)

        # Entry: the frozen layout is the one applicable definition; the future Driver is absent.
        eligible = await call("GET", f"/payroll/periods/{period_id}/eligible-drivers", manager)
        assert [d["driver_assignment_id"] for d in eligible["drivers"]] == [
            seed.current_driver_assignment_id]
        grid = await call("GET", f"/payroll/periods/{period_id}/day-grid", manager,
                          params={"work_date": start.isoformat()})
        assert [c["definition_code"] for c in grid["columns"]] == ["DELIVERY"]
        ppd = grid["columns"][0]["payroll_period_definition_id"]
        for offset, stops in ((0, "10"), (1, "12")):
            await call("POST", f"/payroll/periods/{period_id}/day-grid", manager, json={
                "work_date": (start + timedelta(days=offset)).isoformat(),
                "rows": [{"driver_assignment_id": seed.current_driver_assignment_id,
                          "values": {str(ppd): stops}}]})
        preview = await call("GET", f"/payroll/periods/{period_id}/calculation-preview", manager)
        assert not preview["has_blockers"], preview["blockers"]
        assert Decimal(str(preview["total_expected_pay"])) == Decimal("275.00")  # 22 x 12.50

        # Submit, review and finalize: the Manager submits, the Owner approves.
        submitted = await call("PATCH", f"/payroll/periods/{period_id}/status", manager,
                               json={"status": "InReview"})
        assert submitted["status"] == "InReview"
        items = [i for i in await call("GET", "/review/items", owner)
                 if i["entity_id"] == str(period_id) and i["request_type"] == "PeriodApproval"
                 and i["status"] == "Pending"]
        assert len(items) == 1
        await call("POST", f"/review/items/{items[0]['review_item_id']}/decide", driver, 403,
                   json={"decision": "Approved", "decision_reason": "not allowed"})
        await call("POST", f"/review/items/{items[0]['review_item_id']}/decide", owner, json={
            "decision": "Approved", "decision_reason": "Canonical demo review"})
        await call("POST", f"/payroll/periods/{period_id}/finalize", driver, 403)
        finalized = await call("POST", f"/payroll/periods/{period_id}/finalize", manager)
        assert finalized["status"] == "Locked"
        assert Decimal(str(finalized["final_gross"])) == Decimal("275.00")

        # The Finalized Payroll Library and reports read the frozen evidence.
        library = await call("GET", "/payroll/finalized", manager)
        assert [p["period_id"] for p in library] == [period_id]
        assert library[0]["currency_code"] == "USD"

        async def finalized_reads(headers):
            return _strip_generated({
                "overview": await call("GET", f"/payroll/finalized/{period_id}/overview", headers),
                "rates": await call("GET", f"/payroll/finalized/{period_id}/rates-used", headers),
                **{view: await call("GET", f"/payroll/finalized/{period_id}/reports/{view}", headers)
                   for view in ("drivers", "period-work", "period-pay", "mixed")},
            })

        before = await finalized_reads(manager)
        assert Decimal(str(before["overview"]["financial_summary"]["total_pay"])) == Decimal("275.00")
        (used,) = before["rates"]["used_rate_definitions"]
        assert used["driver_rate_schedule_id"] == seed.driver_rate_schedule_id
        assert Decimal(str(used["rate_amount"])) == canonical_demo.RATE_AMOUNT
        assert [d["driver_assignment_id"] for d in before["drivers"]["drivers"]] == [
            seed.current_driver_assignment_id]
        await call("GET", f"/payroll/finalized/{period_id}/audit", manager, 403)
        assert (await call("GET", f"/payroll/finalized/{period_id}/audit", owner))["metadata"]

        # Later live changes never reach finalized evidence: a new Approved rate and a rename.
        later = await call("POST", "/compensation/driver-rate-schedules", owner, 201, json={
            "driver_id": seed.current_driver_id, "rate_definition_id": seed.rate_definition_id,
            "effective_from": (start + timedelta(days=14)).isoformat(), "effective_to": None})
        definition = await call("GET", f"/compensation/pay-definitions/{seed.pay_definition_id}", owner)
        schedule_path = f"/compensation/driver-rate-schedules/{later['driver_rate_schedule_id']}"
        await call("PUT", f"{schedule_path}/values", owner, json={"values": [{
            "rate_component_definition_id":
                definition["components"][0]["rate_component_definition_id"],
            "amount": "20.00"}]})
        await call("POST", f"{schedule_path}/approve", owner)
        employee = await call("GET", "/workforce/employees", owner)
        current = next(e for e in employee if e["driver"]
                       and e["driver"]["driver_id"] == seed.current_driver_id)
        await call("PATCH", f"/workforce/employees/{current['employee_id']}", owner,
                   json={"full_name": "Jordan Rivera-Lee"})
        assert await finalized_reads(manager) == before

        async with engine.connect() as db:
            (snapshot_id,) = (await db.execute(text(
                "SELECT payrollcalculationsnapshotid FROM payroll.payrollcalculationsnapshots "
                "WHERE payrollperiodid = :p"), {"p": period_id})).scalars().all()
            content = await verify_snapshot(db, snapshot_id=snapshot_id)
            assert len(await verify_final_lines(db, content)) == 1
    finally:
        await client.aclose()
        await engine.dispose()

    # Immutable evidence and audit history reject rewrites at the database boundary.
    with psycopg2.connect(**dsn) as conn, conn.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM audit.auditlog WHERE actioncode = 'CALCULATION_SNAPSHOT_CAPTURED'")
        assert cursor.fetchone()[0] == 1
        for statement in (
            "UPDATE payroll.payrollfinallines SET finalamount = 0",
            "UPDATE payroll.payrollcalculationsnapshotcomponents SET officialamount = 0",
            "UPDATE audit.auditlog SET reason = 'rewritten'",
            "DELETE FROM audit.auditlog",
        ):
            cursor.execute("SAVEPOINT rewrite")
            with pytest.raises(psycopg2.Error):
                cursor.execute(statement)
            cursor.execute("ROLLBACK TO SAVEPOINT rewrite")
        assert _check_target_physical_authorities(cursor) == []
    run_schema_guard(url, is_dev=True)
    # The payroll history now present is exactly what the canonical verification rejects.
    problems = await verify_canonical_demo(url)
    assert any("payroll history" in problem for problem in problems), problems


def test_the_development_reset_restores_the_canonical_demo(seeded, tmp_path):
    """The provisioning and reset tools, as a developer runs them, against this database."""
    env = {**seeded["env"], "ENVIRONMENT": "development"}
    database = seeded["dsn"]["database"]
    script = [sys.executable, os.path.join(BACKEND, "scripts", "reset_dev_database.py")]

    def run(*args, command=script):
        return subprocess.run([*command, *args], cwd=tmp_path, env=env,
                              capture_output=True, text=True, check=False)

    unprovisioned = run("--confirm", database)
    assert unprovisioned.returncode == 1
    assert "flussra.environment is None" in unprovisioned.stderr
    dsn = seeded["dsn"]
    provisioned = run("--admin-url", f"postgresql://{dsn['user']}@{dsn['host']}:{dsn['port']}/{database}",
                      "--confirm", database,
                      command=[sys.executable, os.path.join(BACKEND, "scripts", "provision_dev_database.py")])
    assert provisioned.returncode == 0, provisioned.stdout + provisioned.stderr
    refused = run("--confirm", "another_database")
    assert refused.returncode == 1 and "Refusing to reset" in refused.stderr
    reset = run("--confirm", database)
    assert reset.returncode == 0, reset.stdout + reset.stderr
    assert "canonical demo verified" in reset.stdout
    assert _version(seeded["dsn"]) == "0093"
    for table in PAYROLL_HISTORY_TABLES:
        assert _count(seeded["dsn"], f"SELECT count(*) FROM {table}") == 0, table
    verified = run("--verify-only")
    assert verified.returncode == 0, verified.stdout + verified.stderr
    assert "canonical demo is intact" in verified.stdout
