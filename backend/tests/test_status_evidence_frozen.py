"""Status pay of a submitted period is the frozen evidence, not the live catalog or rates.

Submission freezes every Status line with its target provenance (Status rate column,
RateDefinition, the exact Driver schedule, the rate and the hours) and every Status selection with
its canonical name, off semantic, hours and Category. Approval, finalization and every finalized
reader use only that evidence, so a later edit of the live Status, its column, the Driver's
schedule or the selections changes nothing.
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import text

from app.payroll.snapshot_verification import verify_snapshot
from tests.calculation_snapshot_helpers import (
    allow_self_approval,
    bound_snapshot_id,
    decide,
    enter,
    finalization_preview,
    finalize,
    open_period,
    pending_review_item,
    rows,
    submit,
)
from tests.compensation_api_fixtures import (  # noqa: F401 - register fixtures
    p3c_application,
    p3c_database_engine,
    p3c_http_client,
)
from tests.compensation_database_fixtures import (  # noqa: F401 - register fixtures
    p3b_cursor,
    p3b_database,
)
from tests.db_state import replica_replication_role
from tests.payroll_runtime_fixtures import (
    build_payroll_tenant,
    seed_status_key,
    seed_status_rate,
    status_column_of,
)

pytestmark = pytest.mark.asyncio
D = Decimal


@pytest.fixture(name="tenant")
def usd_tenant(p3b_dsn):
    return build_payroll_tenant(p3b_dsn)


async def _submitted(client, engine, tenant):
    """Two 8-hour Holiday Status days at 25 an hour: 400.00, submitted and awaiting review."""
    column_id, _, _ = status_column_of(tenant)
    seed_status_key(tenant, "PAID", off=False, hoursvalue=D("8"), category="Holiday",
                    statusratecolumnid=column_id)
    schedule_id = seed_status_rate(tenant, D("25"))
    period, _, _ = await open_period(client, engine, tenant)
    for offset in (0, 1):
        await enter(client, tenant, period, offset, status_key="PAID")
    allow_self_approval(tenant)
    await submit(client, tenant, period)
    item_id = await pending_review_item(tenant, period)
    return period, item_id, column_id, schedule_id


async def _edit_every_live_status_source(engine, tenant, column_id) -> None:
    statements = (
        "UPDATE payroll.driverrateschedulevalues SET amount = amount * 7 WHERE amount IS NOT NULL",
        "UPDATE payroll.payrollstatuskeys SET hoursvalue = 1, allowancecategory = 'Sick', "
        "keyname = 'Renamed status', isoffreason = TRUE WHERE companyid = :company",
        "UPDATE payroll.statusratecolumns SET columnname = 'Renamed column' "
        "WHERE statusratecolumnid = :column",
        "UPDATE payroll.payrollperioddriverdayentrystate SET isvoided = TRUE "
        "WHERE companyid = :company",
    )
    async with engine.begin() as conn:
        async with replica_replication_role(conn):
            for statement in statements:
                await conn.execute(text(statement), {
                    "company": tenant.company_id, "column": column_id})


async def test_the_submitted_snapshot_freezes_the_status_provenance_hours_and_category(
    p3c_client, p3c_engine, tenant,
):
    period, item_id, column_id, schedule_id = await _submitted(p3c_client, p3c_engine, tenant)
    snapshot_id = bound_snapshot_id(tenant, item_id)

    lines = rows(tenant, """
        SELECT l.statusratecolumnid, l.driverratescheduleid, l.quantity, l.resolvedrateamount,
               l.calculatedamount, l.paydefinitionid
        FROM payroll.payrollcalculationsnapshotlines l
        JOIN payroll.payrollcalculationdrivertotals t
          ON t.payrollcalculationdrivertotalid = l.payrollcalculationdrivertotalid
        WHERE t.payrollcalculationsnapshotid = %s AND l.sourcetype = 'StatusEntryState'
        ORDER BY l.workdate
    """, snapshot_id)
    assert lines == [(column_id, schedule_id, D("8"), D("25"), D("200"), None)] * 2
    entries = rows(tenant, """
        SELECT statushoursvaluesnapshot, statuscategorysnapshot, statusisoffreasonsnapshot
        FROM payroll.payrollcalculationsnapshotstatusentries
        WHERE payrollcalculationsnapshotid = %s
    """, snapshot_id)
    assert entries == [(D("8"), "Holiday", False)] * 2
    async with p3c_engine.connect() as conn:
        await verify_snapshot(conn, snapshot_id=snapshot_id)


async def test_approval_and_finalization_pay_the_frozen_status_whatever_the_live_sources_became(
    p3c_client, p3c_engine, tenant,
):
    period, item_id, column_id, schedule_id = await _submitted(p3c_client, p3c_engine, tenant)
    await _edit_every_live_status_source(p3c_engine, tenant, column_id)

    await decide(p3c_client, tenant, item_id)
    preview = (await finalization_preview(p3c_client, tenant, period)).json()
    assert D(str(preview["total_final_gross"])) == D("400.00")
    await finalize(p3c_client, tenant, period)

    final = rows(tenant, """
        SELECT sum(finalamount) FROM payroll.payrollfinallines WHERE payrollperiodid = %s
    """, period["payroll_period_id"])
    assert final == [(D("400.00"),)]
    # The snapshot still verifies: nothing it holds was rewritten by the live edits.
    async with p3c_engine.connect() as conn:
        await verify_snapshot(conn, snapshot_id=bound_snapshot_id(tenant, item_id))


async def test_the_finalized_rates_used_and_status_readers_show_the_frozen_status(
    p3c_client, p3c_engine, tenant,
):
    period, item_id, column_id, schedule_id = await _submitted(p3c_client, p3c_engine, tenant)
    await decide(p3c_client, tenant, item_id)
    await finalize(p3c_client, tenant, period)
    await _edit_every_live_status_source(p3c_engine, tenant, column_id)

    pid = period["payroll_period_id"]
    rates = (await p3c_client.get(
        f"/payroll/finalized/{pid}/rates-used", headers=tenant.admin)).json()
    (status_rate,) = [row for row in rates["used_rate_definitions"]
                      if row["source_type"] == "StatusEntryState"]
    assert status_rate["evidence_kind"] == "DriverRateSchedule"
    assert status_rate["status_rate_column_id"] == column_id
    assert status_rate["status_rate_column_name"] == "Status Pay"      # not the live rename
    assert D(str(status_rate["rate_amount"])) == D("25")               # not the live x7
    assert status_rate["driver_rate_schedule_id"] == schedule_id
    assert status_rate["pay_definition_id"] is None

    for path in (f"/payroll/finalized/{pid}/overview", f"/payroll/finalized/{pid}/reports/drivers",
                 f"/payroll/finalized/{pid}/audit"):
        response = await p3c_client.get(path, headers=tenant.admin)
        assert response.status_code == 200, (path, response.text)
