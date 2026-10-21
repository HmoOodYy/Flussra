"""Every reader that returns frozen money goes through the one verified financial authority.

A Locked period is read through the snapshot its FinalLines name, and the FinalLines must be
exactly the projection of that snapshot (every field). A tampered snapshot, used-rate row, report
evidence or FinalLine fails every reader closed with a stable code; a period whose FinalLines name
several snapshots has no financial output anywhere.
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import text

from tests.calculation_snapshot_helpers import (
    add_bonus,
    add_rule,
    allow_self_approval,
    capture,
    decide,
    enter,
    finalize,
    open_period,
    pending_review_item,
    snapshot_ids,
    status_pay_setup,
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
from tests.payroll_runtime_fixtures import build_payroll_tenant

pytestmark = pytest.mark.asyncio
D = Decimal

REPORT_VIEWS = ("drivers", "period-work", "period-pay", "mixed")
FINALIZED_VIEWS = ("overview", "reports/drivers", "reports/period-work", "reports/period-pay",
                   "reports/mixed", "off-drivers", "rates-used", "audit")


@pytest.fixture(name="tenant")
def usd_tenant(p3b_dsn):
    return build_payroll_tenant(p3b_dsn)


async def _full_period(client, engine, tenant, state: str = "Locked"):
    """A period with ordinary, Status and Bonus sources and an applied minimum, driven through
    the real lifecycle to ``state`` (InReview, Approved or Locked)."""
    status_pay_setup(tenant, "0.5000")
    period, ppd, _ = await open_period(
        client, engine, tenant, hire_offset=3, rate="1.0000",
        definition_code="HAUL", definition_name="Haul")
    await enter(client, tenant, period, 3, ppd=ppd, quantity="2")
    await enter(client, tenant, period, 4, status_key="PAID")
    await enter(client, tenant, period, 5, status_key="PAID")
    await add_rule(client, tenant, period, "MinimumPay", "70")
    await add_bonus(client, tenant, period, "1.00")
    allow_self_approval(tenant)
    await submit(client, tenant, period)
    if state in ("Approved", "Locked"):
        await decide(client, tenant, await pending_review_item(tenant, period))
    if state == "Locked":
        await finalize(client, tenant, period)
    return period


async def _tamper(engine, statement: str, **params) -> None:
    async with engine.begin() as conn:
        async with replica_replication_role(conn):
            result = await conn.execute(text(statement), params)
            assert result.rowcount >= 1, statement


async def _get(client, tenant, path: str):
    return await client.get(path, headers=tenant.admin)


def _paths(period: dict) -> list[str]:
    pid = period["payroll_period_id"]
    return [f"/payroll/periods/{pid}/final-lines",
            *[f"/payroll/periods/{pid}/reports/{view}" for view in REPORT_VIEWS],
            *[f"/payroll/finalized/{pid}/{view}" for view in FINALIZED_VIEWS]]


async def test_the_untampered_finalized_period_is_served_everywhere(
    p3c_client, p3c_engine, tenant,
):
    period = await _full_period(p3c_client, p3c_engine, tenant)
    for path in _paths(period):
        response = await _get(p3c_client, tenant, path)
        assert response.status_code == 200, (path, response.text[:200])


# Each statement alters one persisted field of the first FinalLine of the period. The columns a
# component line must keep fixed (line type, kind, scope, source type, work date and the legacy
# per-source columns) are guarded by the table CHECK constraint and cannot be changed at all.
_FINAL_LINE_TAMPERING = {
    "amount": "UPDATE payroll.payrollfinallines SET finalamount = finalamount + 1 "
              "WHERE finallineid = (SELECT min(finallineid) FROM payroll.payrollfinallines "
              "WHERE payrollperiodid = :period)",
    "driver": "UPDATE payroll.payrollfinallines SET driverid = driverid + 1000 "
              "WHERE finallineid = (SELECT min(finallineid) FROM payroll.payrollfinallines "
              "WHERE payrollperiodid = :period)",
    "employee": "UPDATE payroll.payrollfinallines SET employeeid = employeeid + 1000 "
                "WHERE finallineid = (SELECT min(finallineid) FROM payroll.payrollfinallines "
                "WHERE payrollperiodid = :period)",
    "driver assignment": "UPDATE payroll.payrollfinallines SET driverassignmentid = "
                         "driverassignmentid + 1000 WHERE finallineid = (SELECT min(finallineid) "
                         "FROM payroll.payrollfinallines WHERE payrollperiodid = :period)",
    "source id": "UPDATE payroll.payrollfinallines SET sourceid = '999' "
                 "WHERE finallineid = (SELECT min(finallineid) FROM payroll.payrollfinallines "
                 "WHERE payrollperiodid = :period)",
    "period definition": "UPDATE payroll.payrollfinallines SET payrollperioddefinitionid = "
                         "payrollperioddefinitionid + 1000 WHERE payrollperioddefinitionid IS NOT NULL "
                         "AND payrollperiodid = :period",
    "pay definition": "UPDATE payroll.payrollfinallines SET paydefinitionid = paydefinitionid + 1000 "
                      "WHERE paydefinitionid IS NOT NULL AND payrollperiodid = :period",
    "quantity": "UPDATE payroll.payrollfinallines SET quantity = 5 "
                "WHERE finallineid = (SELECT min(finallineid) FROM payroll.payrollfinallines "
                "WHERE payrollperiodid = :period)",
    "rate": "UPDATE payroll.payrollfinallines SET rateamount = 1 "
            "WHERE finallineid = (SELECT min(finallineid) FROM payroll.payrollfinallines "
            "WHERE payrollperiodid = :period)",
    "currency": "UPDATE payroll.payrollfinallines SET currencycode = 'JPY', currencyminorunitdigits = 0 "
                "WHERE finallineid = (SELECT min(finallineid) FROM payroll.payrollfinallines "
                "WHERE payrollperiodid = :period)",
    "notes": "UPDATE payroll.payrollfinallines SET notes = 'edited' "
             "WHERE finallineid = (SELECT min(finallineid) FROM payroll.payrollfinallines "
             "WHERE payrollperiodid = :period)",
    "provenance": "UPDATE payroll.payrollfinallines SET sourcesnapshot = jsonb_set(sourcesnapshot, "
                  "'{snapshot_hash}', '\"0\"') WHERE finallineid = (SELECT min(finallineid) FROM "
                  "payroll.payrollfinallines WHERE payrollperiodid = :period)",
}


@pytest.mark.parametrize("field", sorted(_FINAL_LINE_TAMPERING))
async def test_a_tampered_final_line_field_fails_every_finalized_reader_closed(
    p3c_client, p3c_engine, tenant, field,
):
    period = await _full_period(p3c_client, p3c_engine, tenant)
    await _tamper(p3c_engine, _FINAL_LINE_TAMPERING[field], period=period["payroll_period_id"])

    # A mixed currency is refused by the currency context of the finalized routes first.
    codes = ("FINAL_LINES_INTEGRITY_ERROR", "SNAPSHOT_CURRENCY_MISMATCH")
    for path in _paths(period):
        response = await _get(p3c_client, tenant, path)
        assert response.status_code == 422, (field, path, response.status_code)
        assert response.json()["detail"].startswith(codes), (field, path)
    summary = (await _get(
        p3c_client, tenant, f"/payroll/periods/{period['payroll_period_id']}")).json()
    assert summary["final_gross"] is None and summary["final_lines"] == 0
    assert summary["financial_authority"] == "UNAVAILABLE"
    assert summary["financial_authority_reason"] == "FINAL_LINES_INTEGRITY_ERROR"


async def test_an_extra_final_line_naming_another_snapshot_leaves_no_financial_output(
    p3c_client, p3c_engine, tenant,
):
    period = await _full_period(p3c_client, p3c_engine, tenant)
    newer = (await capture(p3c_engine, tenant, period)).snapshot_id
    await _tamper(p3c_engine, """
        UPDATE payroll.payrollfinallines SET payrollcalculationsnapshotid = :newer,
               payrollcalculationsnapshotcomponentid = (
                   SELECT min(payrollcalculationsnapshotcomponentid)
                   FROM payroll.payrollcalculationsnapshotcomponents
                   WHERE payrollcalculationsnapshotid = :newer)
        WHERE finallineid = (SELECT min(finallineid) FROM payroll.payrollfinallines
                             WHERE payrollperiodid = :period)
    """, newer=newer, period=period["payroll_period_id"])
    pid = period["payroll_period_id"]

    ledger = await _get(p3c_client, tenant, f"/payroll/periods/{pid}/final-lines")
    assert ledger.status_code == 422
    assert ledger.json()["detail"].startswith("PROVENANCE_UNAVAILABLE")
    for view in REPORT_VIEWS:
        response = await _get(p3c_client, tenant, f"/payroll/periods/{pid}/reports/{view}")
        assert response.status_code == 422, view
        assert response.json()["detail"].startswith("PROVENANCE_UNAVAILABLE"), view

    overview = (await _get(p3c_client, tenant, f"/payroll/finalized/{pid}/overview")).json()
    unavailable = {"state": "UNAVAILABLE", "reason_code": "PROVENANCE_UNAVAILABLE"}
    assert overview["financial_summary"]["total_pay"] is None
    assert overview["financial_summary"]["final_line_count"] == 0
    assert overview["section_availability"]["financials"] == unavailable
    assert overview["section_availability"]["reports"] == unavailable
    for view in ("drivers", "period-pay", "mixed"):
        report = (await _get(p3c_client, tenant, f"/payroll/finalized/{pid}/reports/{view}")).json()
        assert report["metadata"]["financials_available"] is False
        assert report["pay_totals"] is None and report["definition_totals"] is None
        assert report["metadata"]["section_availability"]["financials"] == unavailable
        assert all(driver["pay"] is None for driver in report["drivers"])
    summary = (await _get(p3c_client, tenant, f"/payroll/periods/{pid}")).json()
    assert summary["final_gross"] is None and summary["financial_authority"] == "UNAVAILABLE"
    assert summary["financial_authority_reason"] == "PROVENANCE_UNAVAILABLE"


async def test_a_finalized_period_without_final_lines_has_no_financial_output(
    p3c_client, p3c_engine, tenant,
):
    period = await _full_period(p3c_client, p3c_engine, tenant)
    await _tamper(p3c_engine, "DELETE FROM payroll.payrollfinallines WHERE payrollperiodid = :period",
                  period=period["payroll_period_id"])
    pid = period["payroll_period_id"]

    ledger = await _get(p3c_client, tenant, f"/payroll/periods/{pid}/final-lines")
    assert ledger.status_code == 422 and ledger.json()["detail"].startswith("FINAL_LINES_EMPTY")
    overview = (await _get(p3c_client, tenant, f"/payroll/finalized/{pid}/overview")).json()
    assert overview["financial_summary"]["total_pay"] is None
    assert overview["section_availability"]["financials"]["state"] == "UNAVAILABLE"


# ---------------------------------------------------------------------------
# Evidence families reach the readers only after verification
# ---------------------------------------------------------------------------

_EVIDENCE_TAMPERING = {
    "used-rate amount": (
        "UPDATE payroll.payrollcalculationsnapshotusedratedefinitions SET rateamountsnapshot = 999 "
        "WHERE payrollcalculationsnapshotid = :snapshot AND evidencekind = 'DriverRateSchedule'",
        "SNAPSHOT_HASH_MISMATCH"),
    "Status used-rate amount": (
        "UPDATE payroll.payrollcalculationsnapshotusedratedefinitions SET rateamountsnapshot = 999 "
        "WHERE payrollcalculationsnapshotid = :snapshot AND sourcetypesnapshot = 'StatusEntryState'",
        "SNAPSHOT_HASH_MISMATCH"),
    "Bonus report amount": (
        "UPDATE payroll.payrollcalculationsnapshotbonusevents SET amount = 999 "
        "WHERE payrollcalculationsnapshotid = :snapshot",
        "REPORT_EVIDENCE_HASH_MISMATCH"),
    "Status report off-reason flag": (
        "UPDATE payroll.payrollcalculationsnapshotstatusentries "
        "SET statusisoffreasonsnapshot = NOT statusisoffreasonsnapshot "
        "WHERE payrollcalculationsnapshotid = :snapshot",
        "REPORT_EVIDENCE_HASH_MISMATCH"),
    "report evidence version": (
        "UPDATE payroll.payrollcalculationsnapshots SET reportevidenceversion = 1 "
        "WHERE payrollcalculationsnapshotid = :snapshot",
        "UNSUPPORTED_REPORT_EVIDENCE_VERSION"),
}


@pytest.mark.parametrize("family", sorted(_EVIDENCE_TAMPERING))
async def test_tampered_evidence_fails_the_frozen_readers_closed(
    p3c_client, p3c_engine, tenant, family,
):
    statement, code = _EVIDENCE_TAMPERING[family]
    period = await _full_period(p3c_client, p3c_engine, tenant)
    (snapshot_id,) = snapshot_ids(tenant, period)
    await _tamper(p3c_engine, statement, snapshot=snapshot_id)

    for path in _paths(period):
        response = await _get(p3c_client, tenant, path)
        assert response.status_code == 422, (family, path, response.status_code)
        assert response.json()["detail"].startswith(code), (family, path)


@pytest.mark.parametrize("state", ["InReview", "Approved"])
async def test_tampered_report_evidence_fails_the_live_reports_of_a_submitted_period(
    p3c_client, p3c_engine, tenant, state,
):
    period = await _full_period(p3c_client, p3c_engine, tenant, state)
    (snapshot_id,) = snapshot_ids(tenant, period)
    await _tamper(
        p3c_engine, "UPDATE payroll.payrollcalculationsnapshotbonusevents SET amount = 999 "
                    "WHERE payrollcalculationsnapshotid = :snapshot", snapshot=snapshot_id)

    for view in REPORT_VIEWS:
        response = await _get(
            p3c_client, tenant, f"/payroll/periods/{period['payroll_period_id']}/reports/{view}")
        assert response.status_code == 422, view
        assert response.json()["detail"].startswith("REPORT_EVIDENCE_HASH_MISMATCH"), view
