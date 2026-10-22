"""Status evidence of a finalized period is read from its verified snapshot only.

The Day Grid, the Off Drivers reads and Drivers Off report the Status evidence frozen with the
snapshot the period FinalLines name, after the canonical verification. A period without any Status
entry reports EMPTY (a positive fact); evidence that does not verify, or whose version is not
supported, is UNAVAILABLE with the stable reason and fabricates no row.
"""
from __future__ import annotations

import pytest
from sqlalchemy import text

from tests.calculation_snapshot_helpers import (
    day,
    finalize,
    snapshot_ids,
    standard_scenario,
    submit_and_approve,
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
from tests.payroll_runtime_fixtures import build_payroll_tenant

pytestmark = pytest.mark.asyncio


@pytest.fixture(name="tenant")
def usd_tenant(p3b_dsn):
    return build_payroll_tenant(p3b_dsn)


async def _finalized(client, engine, tenant) -> dict:
    scenario = await standard_scenario(client, engine, tenant)
    await submit_and_approve(client, tenant, scenario.period)
    await finalize(client, tenant, scenario.period)
    return scenario.period


async def _status_evidence_reads(client, tenant, period) -> dict[str, dict]:
    pid = period["payroll_period_id"]
    reads = {
        "day grid": (f"/payroll/periods/{pid}/day-grid", {"work_date": day(period, 3)}),
        "off drivers (day)": (f"/payroll/periods/{pid}/off-drivers", {"work_date": day(period, 3)}),
        "off drivers (summary)": (f"/payroll/periods/{pid}/off-drivers/summary", {}),
    }
    evidence = {}
    for label, (path, params) in reads.items():
        response = await client.get(path, params=params, headers=tenant.admin)
        assert response.status_code == 200, (label, response.text)
        body = response.json()
        evidence[label] = body.get("status_evidence")
        if label != "day grid":
            assert body.get("total_count", body.get("total_fully_off_drivers")) == 0, label
    return evidence


async def test_a_finalized_period_without_status_entries_reports_empty_evidence(
    p3c_client, p3c_engine, tenant,
):
    period = await _finalized(p3c_client, p3c_engine, tenant)

    evidence = await _status_evidence_reads(p3c_client, tenant, period)

    assert all(value == {"state": "EMPTY", "reason_code": None}
               for value in evidence.values()), evidence


@pytest.mark.parametrize("statement, reason", [
    ("UPDATE payroll.payrollcalculationsnapshots SET reportevidenceversion = 1 "
     "WHERE payrollcalculationsnapshotid = :snapshot", "UNSUPPORTED_REPORT_EVIDENCE_VERSION"),
    ("UPDATE payroll.payrollcalculationsnapshots SET reportevidencehash = repeat('a', 64) "
     "WHERE payrollcalculationsnapshotid = :snapshot", "REPORT_EVIDENCE_HASH_MISMATCH"),
    ("UPDATE payroll.payrollcalculationsnapshots SET totalexpectedpay = totalexpectedpay + 1 "
     "WHERE payrollcalculationsnapshotid = :snapshot", "SNAPSHOT_HASH_MISMATCH"),
])
async def test_status_evidence_that_does_not_verify_is_unavailable_and_fabricates_nothing(
    p3c_client, p3c_engine, tenant, statement, reason,
):
    period = await _finalized(p3c_client, p3c_engine, tenant)
    (snapshot_id,) = snapshot_ids(tenant, period)
    async with p3c_engine.begin() as conn:
        async with replica_replication_role(conn):
            await conn.execute(text(statement), {"snapshot": snapshot_id})

    evidence = await _status_evidence_reads(p3c_client, tenant, period)

    assert all(value == {"state": "UNAVAILABLE", "reason_code": reason}
               for value in evidence.values()), evidence
