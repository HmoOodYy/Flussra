"""The calculation snapshot freezes exactly what the live packet produced.

Real tenants and periods; the writer is called inside a caller-owned transaction as the
lifecycle calls it. These tests prove the frozen shape: identity, versions, official
components, Min/Max evidence, target rate provenance and the hash recomputed from the
persisted rows. Gate-independent: the writer is exercised directly.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
from fastapi import HTTPException

from app.payroll.snapshot_hash import CURRENT_PAYROLL_CALCULATION_VERSION, calculate_snapshot_hash
from app.payroll.snapshot_verification import load_snapshot_content, verify_snapshot
from tests.calculation_snapshot_helpers import (
    WEEKEND_OFF,
    add_bonus,
    add_rule,
    build_packet,
    capture,
    enter,
    open_period,
    rows,
    snapshot_table_counts,
    status_pay_setup,
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
from tests.payroll_runtime_fixtures import build_payroll_tenant, query, setup_anchor

pytestmark = pytest.mark.asyncio
D = Decimal


@pytest.fixture(name="tenant")
def usd_tenant(p3b_dsn):
    return build_payroll_tenant(p3b_dsn)


async def _content(engine, snapshot_id: int) -> dict:
    async with engine.connect() as conn:
        return await load_snapshot_content(conn, snapshot_id=snapshot_id)


def _driver_total(content: dict, assignment_id: int) -> dict:
    return next(t for t in content["DriverTotals"] if t["DriverAssignmentID"] == assignment_id)


def _kind(total: dict, kind: str) -> list[dict]:
    return [c for c in total["Components"] if c["ComponentKind"] == kind]


# ---------------------------------------------------------------------------
# The frozen packet: identity, versions, components, Min/Max evidence, rate provenance
# ---------------------------------------------------------------------------

async def _partial_period_with_minimum_and_bonus(client, engine, tenant):
    period, ppd, definition = await open_period(
        client, engine, tenant, hire_offset=3, rate="0.3333",
        definition_code="HAUL", definition_name="Haul")
    for offset in (3, 4):
        await enter(client, tenant, period, offset, ppd=ppd, quantity="1")
    rule = await add_rule(client, tenant, period, "MinimumPay", "70")
    await add_bonus(client, tenant, period, "2.00")
    return period, ppd, definition, rule


async def test_a_snapshot_freezes_versions_currency_identity_and_totals(
    p3c_client, p3c_engine, tenant,
):
    period, _, _, _ = await _partial_period_with_minimum_and_bonus(
        p3c_client, p3c_engine, tenant)
    frozen = await capture(p3c_engine, tenant, period)
    content = await _content(p3c_engine, frozen.snapshot_id)

    assert content["CalculationVersion"] == CURRENT_PAYROLL_CALCULATION_VERSION == "payroll-calculation-v4"
    assert (content["PayableRoundingVersion"], content["MinMaxProrationVersion"]) == (
        "payable-rounding-v1", "minmax-proration-v1")
    assert (content["CurrencyCode"], content["CurrencyMinorUnitDigits"]) == ("USD", 2)
    assert (content["CompanyID"], content["BranchID"], content["PayrollPeriodID"]) == (
        tenant.company_id, tenant.branch_a, period["payroll_period_id"])
    assert content["PeriodStartDate"] == date.fromisoformat(period["start_date"])
    assert content["PeriodEndDate"] == date.fromisoformat(period["end_date"])
    assert content["RevisionNumber"] == 1 and content["Warnings"] == []
    assert content["TotalExpectedPay"] == D("42.00") == frozen.packet.total_expected_pay
    assert len(content["ScheduledWorkDates"]) == 7

    total = _driver_total(content, tenant.driver_a)
    ((employee_id, full_name, driver_number),) = rows(tenant, """
        SELECT a.employeeid, e.fullname, d.drivernumber
        FROM core.driverassignments a
        JOIN core.employees e ON e.employeeid = a.employeeid
        JOIN core.drivers d ON d.driverid = a.driverid
        WHERE a.driverassignmentid = %s
    """, tenant.driver_a)
    assert (total["DriverID"], total["EmployeeID"], total["BranchID"]) == (
        tenant.driver_a_id, employee_id, tenant.branch_a)
    assert (total["DriverNameSnapshot"], total["DriverNumberSnapshot"]) == (full_name, driver_number)
    assert (total["DailyPay"], total["StatusPay"], total["BonusTotal"]) == (D("0.67"), D("0"), D("2.00"))
    assert (total["MinimumAdjustment"], total["MaximumAdjustment"], total["ExpectedPay"]) == (
        D("39.33"), D("0"), D("42.00"))

    stored_hash = rows(tenant, "SELECT snapshothash FROM payroll.payrollcalculationsnapshots "
                               "WHERE payrollcalculationsnapshotid = %s", frozen.snapshot_id)[0][0]
    assert stored_hash == calculate_snapshot_hash(content)


async def test_official_components_carry_target_definition_identity_and_reconcile(
    p3c_client, p3c_engine, tenant,
):
    period, ppd, definition, rule = await _partial_period_with_minimum_and_bonus(
        p3c_client, p3c_engine, tenant)
    frozen = await capture(p3c_engine, tenant, period)
    content = await _content(p3c_engine, frozen.snapshot_id)
    total = _driver_total(content, tenant.driver_a)

    (ordinary,) = _kind(total, "ORDINARY")
    assert ordinary["PayrollPeriodDefinitionID"] == ppd
    assert ordinary["PayDefinitionID"] == definition["pay_definition_id"]
    assert ordinary["RateDefinitionID"] is not None
    assert (ordinary["CalculationMethod"], ordinary["CalculationMethodVersion"]) == ("PerUnit", 1)
    assert (ordinary["DefinitionCodeSnapshot"], ordinary["DefinitionNameSnapshot"]) == ("HAUL", "Haul")
    assert (ordinary["PreRoundingAmount"], ordinary["OfficialAmount"]) == (D("0.6666"), D("0.67"))
    assert (ordinary["SourceCount"], ordinary["UnresolvedLineCount"]) == (2, 0)

    (bonus,) = _kind(total, "BONUS")
    assert (bonus["PreRoundingAmount"], bonus["OfficialAmount"], bonus["SourceCount"]) == (
        D("2.00"), D("2.00"), 1)
    (minimum,) = _kind(total, "MINIMUM_ADJUSTMENT")
    assert minimum["DriverPayRuleID"] == rule["driver_pay_rule_id"]
    # 70 x 4/7 = 40.0000, less the official ordinary base of 0.67.
    assert (minimum["PreRoundingAmount"], minimum["OfficialAmount"]) == (D("39.3300"), D("39.33"))
    assert not _kind(total, "MAXIMUM_ADJUSTMENT") and not _kind(total, "STATUS")

    assert sum(c["OfficialAmount"] for c in total["Components"]) == total["ExpectedPay"]
    # Reproduces from the frozen evidence alone, with no live lookup.
    async with p3c_engine.connect() as conn:
        verified = await verify_snapshot(conn, snapshot_id=frozen.snapshot_id)
    assert verified["TotalExpectedPay"] == D("42.00")


async def test_lines_are_per_source_detail_with_target_rate_provenance_and_no_system_lines(
    p3c_client, p3c_engine, tenant,
):
    period, ppd, definition, _ = await _partial_period_with_minimum_and_bonus(
        p3c_client, p3c_engine, tenant)
    frozen = await capture(p3c_engine, tenant, period)
    content = await _content(p3c_engine, frozen.snapshot_id)
    total = _driver_total(content, tenant.driver_a)

    ((schedule_id, rate_definition_id),) = rows(tenant, """
        SELECT s.driverratescheduleid, s.ratedefinitionid
        FROM payroll.driverrateschedules s
        WHERE s.paydefinitionid = %s AND s.driverid = %s
    """, definition["pay_definition_id"], tenant.driver_a_id)
    ordinary_lines = [ln for ln in total["Lines"] if ln["SourceType"] == "DraftLine"]
    assert len(ordinary_lines) == 2
    for line in ordinary_lines:
        assert line["LineType"] == "ORDINARY" and line["LineScope"] == "Daily"
        assert line["PayrollPeriodDefinitionID"] == ppd
        assert line["PayDefinitionID"] == definition["pay_definition_id"]
        assert line["RateDefinitionID"] == rate_definition_id
        assert line["DriverRateScheduleID"] == schedule_id
        assert line["CalculationStatus"] == "Calculated"
        assert line["CalculationVersion"] == "cp4a-per-unit-v1"
        assert line["ResolvedRateAmount"] == D("0.3333")
        assert line["Quantity"] == D("1") and line["CalculatedAmount"] == D("0.3333")
    (bonus_line,) = [ln for ln in total["Lines"] if ln["SourceType"] == "BonusEvent"]
    assert bonus_line["LineType"] == "BONUS" and bonus_line["LineScope"] == "Period"
    assert bonus_line["CalculatedAmount"] == D("2.00") and bonus_line["BonusEventID"] is not None
    # The Min/Max adjustment is explained by its component and evidence, never by a line.
    assert {ln["SourceType"] for ln in total["Lines"]} == {"DraftLine", "BonusEvent"}
    assert not rows(tenant, """
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'payroll' AND table_name = 'payrollcalculationsnapshotlines'
          AND column_name = 'payitemid'
    """)
    assert all(ln["PayrollCalculationSnapshotComponentID"] for ln in total["Lines"])


async def test_the_used_rate_evidence_records_the_target_schedule_and_never_a_pay_rule(
    p3c_client, p3c_engine, tenant,
):
    period, _, definition, rule = await _partial_period_with_minimum_and_bonus(
        p3c_client, p3c_engine, tenant)
    frozen = await capture(p3c_engine, tenant, period)

    evidence = rows(tenant, """
        SELECT evidencekind, sourcetypesnapshot, driverratescheduleid, ratedefinitionid,
               paydefinitionid, ratecomponentdefinitionid, rateamountsnapshot,
               ratestatussnapshot, effectivefromsnapshot, effectivetosnapshot,
               driverassignmentid, driverid, statusratecolumnid
        FROM payroll.payrollcalculationsnapshotusedratedefinitions
        WHERE payrollcalculationsnapshotid = %s
    """, frozen.snapshot_id)
    ((kind, source, schedule_id, rate_definition_id, pay_definition_id, component_id, amount,
      status, effective_from, effective_to, assignment_id, driver_id, status_column_id),) = evidence
    assert (kind, source) == ("DriverRateSchedule", "DraftLine")
    assert pay_definition_id == definition["pay_definition_id"] and component_id is not None
    assert amount == D("0.3333") and status == "Approved"
    assert (assignment_id, driver_id) == (tenant.driver_a, tenant.driver_a_id)
    assert status_column_id is None
    ((live_schedule, live_definition, live_from),) = rows(tenant, """
        SELECT driverratescheduleid, ratedefinitionid, effectivefrom
        FROM payroll.driverrateschedules WHERE driverratescheduleid = %s
    """, schedule_id)
    assert (live_schedule, live_definition) == (schedule_id, rate_definition_id)
    assert effective_from == live_from and effective_to is None
    # The Min/Max rule has one authority: its evidence rows, not the used-rate evidence.
    assert not rows(tenant, """
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'payroll'
          AND table_name = 'payrollcalculationsnapshotusedratedefinitions'
          AND column_name IN ('driverpayruleid', 'payitemid')
    """)
    assert rows(tenant, """
        SELECT 1 FROM payroll.payrollcalculationsnapshotminmaxrules
        WHERE payrollcalculationsnapshotid = %s AND driverpayruleid = %s
    """, frozen.snapshot_id, rule["driver_pay_rule_id"])


async def test_applied_min_max_evidence_records_the_rule_proration_and_calendar(
    p3c_client, p3c_engine, tenant,
):
    period, _, _, rule = await _partial_period_with_minimum_and_bonus(
        p3c_client, p3c_engine, tenant)
    frozen = await capture(p3c_engine, tenant, period)
    content = await _content(p3c_engine, frozen.snapshot_id)
    evaluation = _driver_total(content, tenant.driver_a)["MinMax"]

    assert evaluation["Status"] == "APPLIED"
    assert evaluation["SelectionDate"] == date.fromisoformat(period["start_date"])
    assert evaluation["ProrationVersion"] == "minmax-proration-v1"
    assert (evaluation["Numerator"], evaluation["Denominator"]) == (4, 7)
    assert evaluation["BaseOrdinaryAmount"] == D("0.67") and evaluation["BaseStatusAmount"] == D("0")
    assert evaluation["BaseAmount"] == D("0.67")
    # Bonus is outside the qualifying base, recorded explicitly.
    assert evaluation["ExcludedBonusAmount"] == D("2.00")
    start = date.fromisoformat(period["start_date"])
    assert evaluation["CountedWorkDates"] == [start + timedelta(days=n) for n in (3, 4, 5, 6)]
    assert set(evaluation["CountedWorkDates"]) <= set(content["ScheduledWorkDates"])

    (recorded,) = evaluation["Rules"]
    assert recorded["DriverPayRuleID"] == rule["driver_pay_rule_id"]
    assert (recorded["RuleType"], recorded["Amount"], recorded["RuleStatus"]) == (
        "MinimumPay", D("70"), "Active")
    assert recorded["EffectiveFrom"] == start and recorded["EffectiveTo"] is None
    assert recorded["ProratedAmount"] == D("40.0000")
    assert (recorded["CalendarFrequency"], recorded["CalendarAnchorStartDate"],
            recorded["CalendarCustomIntervalDays"]) == ("Week", setup_anchor(), None)
    assert recorded["CalendarSourceBranchID"] == tenant.branch_a
    assert recorded["CalendarSourcePayrollSetupVersionID"] > 0


async def test_a_driver_without_a_rule_freezes_a_no_rule_evaluation(
    p3c_client, p3c_engine, tenant,
):
    period, ppd, _ = await open_period(p3c_client, p3c_engine, tenant, rate="10")
    await enter(p3c_client, tenant, period, 0, ppd=ppd, quantity="3")
    frozen = await capture(p3c_engine, tenant, period)
    evaluation = _driver_total(
        await _content(p3c_engine, frozen.snapshot_id), tenant.driver_a)["MinMax"]

    assert evaluation["Status"] == "NO_RULE"
    assert (evaluation["Numerator"], evaluation["Denominator"]) == (None, None)
    assert evaluation["CountedWorkDates"] == [] and evaluation["Rules"] == []
    assert evaluation["BaseAmount"] == D("30.00")
    assert snapshot_table_counts(tenant, frozen.snapshot_id)["rules"] == 0


@pytest.mark.parametrize("rule_type, amount", [("MinimumPay", "700"), ("MaximumPay", "20")])
async def test_a_driver_with_no_eligible_scheduled_day_freezes_the_rule_unapplied(
    p3c_client, p3c_engine, tenant, rule_type, amount,
):
    period, ppd, _ = await open_period(
        p3c_client, p3c_engine, tenant, mask=WEEKEND_OFF, hire_offset=5, rate="10")
    await enter(p3c_client, tenant, period, 5, ppd=ppd, quantity="5")     # Saturday
    rule = await add_rule(p3c_client, tenant, period, rule_type, amount)
    frozen = await capture(p3c_engine, tenant, period)
    content = await _content(p3c_engine, frozen.snapshot_id)
    total = _driver_total(content, tenant.driver_a)
    evaluation = total["MinMax"]

    assert evaluation["Status"] == "NO_ELIGIBLE_SCHEDULED_DAYS"
    assert (evaluation["Numerator"], evaluation["Denominator"]) == (0, 5)
    assert evaluation["CountedWorkDates"] == [] and len(content["ScheduledWorkDates"]) == 5
    (recorded,) = evaluation["Rules"]
    assert recorded["DriverPayRuleID"] == rule["driver_pay_rule_id"]
    assert recorded["ProratedAmount"] is None                  # not applied: no prorated amount
    assert total["MinimumAdjustment"] == 0 and total["MaximumAdjustment"] == 0
    assert total["ExpectedPay"] == D("50.00") and content["Warnings"] == []
    assert not _kind(total, "MINIMUM_ADJUSTMENT") and not _kind(total, "MAXIMUM_ADJUSTMENT")


async def test_a_period_without_scheduled_days_freezes_the_warning_and_an_empty_schedule(
    p3c_client, p3c_engine, tenant,
):
    period, ppd, _ = await open_period(p3c_client, p3c_engine, tenant, rate="10")
    await enter(p3c_client, tenant, period, 0, ppd=ppd, quantity="5")
    minimum = await add_rule(p3c_client, tenant, period, "MinimumPay", "10")
    query(tenant, """
        UPDATE payroll.payrollperioddays SET isdefaultworkday = FALSE, isconfiguredoffday = TRUE
        WHERE payrollperiodid = %s
    """, (period["payroll_period_id"],))
    frozen = await capture(p3c_engine, tenant, period)
    content = await _content(p3c_engine, frozen.snapshot_id)

    assert content["ScheduledWorkDates"] == []
    evaluation = _driver_total(content, tenant.driver_a)["MinMax"]
    assert (evaluation["Status"], evaluation["Numerator"], evaluation["Denominator"]) == (
        "NO_SCHEDULED_DAYS", 0, 0)
    (warning,) = content["Warnings"]
    assert str(tenant.driver_a) in warning and str(minimum["driver_pay_rule_id"]) in warning
    assert warning in frozen.packet.warnings


async def test_a_negative_maximum_adjustment_is_frozen_with_its_rule_evidence(
    p3c_client, p3c_engine, tenant,
):
    period, ppd, _ = await open_period(p3c_client, p3c_engine, tenant, rate="10")
    await enter(p3c_client, tenant, period, 0, ppd=ppd, quantity="50")    # earned 500
    await add_rule(p3c_client, tenant, period, "MinimumPay", "100")
    maximum = await add_rule(p3c_client, tenant, period, "MaximumPay", "300")
    frozen = await capture(p3c_engine, tenant, period)
    total = _driver_total(await _content(p3c_engine, frozen.snapshot_id), tenant.driver_a)

    assert total["MinimumAdjustment"] == D("0") and total["MaximumAdjustment"] == D("-200.00")
    assert total["ExpectedPay"] == D("300.00")
    (cap,) = _kind(total, "MAXIMUM_ADJUSTMENT")
    assert cap["DriverPayRuleID"] == maximum["driver_pay_rule_id"]
    assert (cap["PreRoundingAmount"], cap["OfficialAmount"]) == (D("-200.0000"), D("-200.00"))
    assert not _kind(total, "MINIMUM_ADJUSTMENT")
    assert {r["RuleType"] for r in total["MinMax"]["Rules"]} == {"MinimumPay", "MaximumPay"}
    assert total["MinMax"]["Status"] == "APPLIED"


async def test_status_pay_and_bonus_freeze_as_components_with_reconciling_evidence(
    p3c_client, p3c_engine, tenant,
):
    column_id, schedule_id = status_pay_setup(tenant, "0.3333")
    period, _, _ = await open_period(p3c_client, p3c_engine, tenant)
    for offset in range(3):
        await enter(p3c_client, tenant, period, offset, status_key="PAID")
    await add_bonus(p3c_client, tenant, period, "1.50")
    frozen = await capture(p3c_engine, tenant, period)
    content = await _content(p3c_engine, frozen.snapshot_id)
    total = _driver_total(content, tenant.driver_a)

    (status,) = _kind(total, "STATUS")
    assert status["StatusRateColumnID"] == column_id
    assert status["StatusRateColumnNameSnapshot"] == "Status Pay"
    assert (status["PreRoundingAmount"], status["OfficialAmount"], status["SourceCount"]) == (
        D("0.9999"), D("1.00"), 3)
    assert (total["StatusPay"], total["DailyPay"], total["BonusTotal"]) == (D("1.00"), D("0"), D("1.50"))
    status_lines = [ln for ln in total["Lines"] if ln["SourceType"] == "StatusEntryState"]
    assert len(status_lines) == 3
    for line in status_lines:
        # A Status line freezes its target provenance: the Status rate column, its
        # RateDefinition and the exact Driver schedule; hours are the quantity.
        assert line["LineType"] == "STATUS" and line["StatusRateColumnID"] == column_id
        assert line["DriverRateScheduleID"] == schedule_id and line["RateDefinitionID"] is not None
        assert line["ResolvedRateAmount"] == D("0.3333") and line["PayrollPeriodDefinitionID"] is None
        assert line["PayDefinitionID"] is None and line["Quantity"] == D("1")
        assert line["CalculationStatus"] == "Calculated"
    assert total["MinMax"]["BaseStatusAmount"] == D("1.00")
    assert total["MinMax"]["BaseAmount"] == D("1.00")
    ((kind, source, used_schedule, used_column, pay_definition, amount),) = rows(tenant, """
        SELECT evidencekind, sourcetypesnapshot, driverratescheduleid, statusratecolumnid,
               paydefinitionid, rateamountsnapshot
        FROM payroll.payrollcalculationsnapshotusedratedefinitions
        WHERE payrollcalculationsnapshotid = %s
    """, frozen.snapshot_id)
    assert (kind, source, used_schedule, used_column, pay_definition, amount) == (
        "DriverRateSchedule", "StatusEntryState", schedule_id, column_id, None, D("0.3333"))

    counts = snapshot_table_counts(tenant, frozen.snapshot_id)
    assert counts["status_entries"] == 3 and counts["bonus_events"] == 1
    assert counts["components"] == 2 and counts["lines"] == 4
    async with p3c_engine.connect() as conn:
        await verify_snapshot(conn, snapshot_id=frozen.snapshot_id)


# ---------------------------------------------------------------------------
# Revisions and the hash
# ---------------------------------------------------------------------------

async def test_two_revisions_with_identical_content_hash_identically_and_leave_the_first_untouched(
    p3c_client, p3c_engine, tenant,
):
    period, _, _, _ = await _partial_period_with_minimum_and_bonus(
        p3c_client, p3c_engine, tenant)
    first = await capture(p3c_engine, tenant, period)
    before = await _content(p3c_engine, first.snapshot_id)
    second = await capture(p3c_engine, tenant, period)
    after_second = await _content(p3c_engine, second.snapshot_id)

    assert second.snapshot_id != first.snapshot_id
    assert (before["RevisionNumber"], after_second["RevisionNumber"]) == (1, 2)
    assert before["SnapshotHash"] == after_second["SnapshotHash"]
    assert before["SourceConfigHash"] == after_second["SourceConfigHash"]
    # Row ids and revisions are outside the hash, so the contents differ only there.
    first_total = _driver_total(before, tenant.driver_a)
    second_total = _driver_total(after_second, tenant.driver_a)
    assert first_total["PayrollCalculationDriverTotalID"] != second_total["PayrollCalculationDriverTotalID"]
    # The earlier revision is exactly as it was.
    assert await _content(p3c_engine, first.snapshot_id) == before


async def test_a_changed_financial_amount_gives_the_next_revision_another_hash(
    p3c_client, p3c_engine, tenant,
):
    period, _, _, _ = await _partial_period_with_minimum_and_bonus(
        p3c_client, p3c_engine, tenant)
    first = await capture(p3c_engine, tenant, period)
    await add_bonus(p3c_client, tenant, period, "1.00")
    second = await capture(p3c_engine, tenant, period)
    first_content = await _content(p3c_engine, first.snapshot_id)
    second_content = await _content(p3c_engine, second.snapshot_id)

    assert first_content["SnapshotHash"] != second_content["SnapshotHash"]
    assert second_content["TotalExpectedPay"] == D("43.00")
    assert _driver_total(first_content, tenant.driver_a)["BonusTotal"] == D("2.00")


async def test_the_writer_refuses_a_packet_with_blockers_and_leaves_no_evidence(
    p3c_client, p3c_engine, tenant,
):
    period, ppd, _ = await open_period(p3c_client, p3c_engine, tenant, rate=None)
    await enter(p3c_client, tenant, period, 0, ppd=ppd, quantity="3")
    packet = await build_packet(p3c_engine, tenant, period)
    assert packet.blockers

    with pytest.raises(HTTPException) as refused:
        await capture(p3c_engine, tenant, period)
    assert refused.value.status_code == 422
    assert "incomplete calculation packet" in refused.value.detail
    assert rows(tenant, "SELECT count(*) FROM payroll.payrollcalculationsnapshots "
                        "WHERE payrollperiodid = %s", period["payroll_period_id"]) == [(0,)]


async def test_the_packet_exposes_the_rate_used_and_the_identity_the_snapshot_freezes(
    p3c_client, p3c_engine, tenant,
):
    period, _, definition, _ = await _partial_period_with_minimum_and_bonus(
        p3c_client, p3c_engine, tenant)
    packet = await build_packet(p3c_engine, tenant, period)
    driver = next(d for d in packet.drivers if d.driver_assignment_id == tenant.driver_a)

    ordinary_lines = [ln for ln in driver.lines if ln.source_type == "DraftLine"]
    assert [ln.resolved_rate_amount for ln in ordinary_lines] == [D("0.3333")] * 2
    assert all(ln.driver_rate_schedule_id is not None and ln.rate_definition_id is not None
               and ln.rate_component_definition_id is not None
               and ln.schedule_status == "Approved" for ln in ordinary_lines)
    assert driver.employee_id is not None and driver.driver_name is not None
    proration = driver.minmax
    assert len(proration.scheduled_work_dates) == 7 and len(proration.counted_work_dates) == 4
    assert packet.scheduled_work_dates == proration.scheduled_work_dates
    assert definition["pay_definition_id"] == ordinary_lines[0].pay_definition_id


async def test_a_minimum_that_prorates_to_zero_is_frozen_with_a_zero_prorated_amount(
    p3c_client, p3c_engine, p3b_dsn,
):
    """CLF has four minor-unit digits: a 0.0001 Minimum over one eligible day of seven is
    0.0000 at internal precision. It is a valid, non-blocking outcome and must freeze."""
    tenant = build_payroll_tenant(p3b_dsn, currency="CLF")
    period, ppd, _ = await open_period(
        p3c_client, p3c_engine, tenant, hire_offset=6, rate="0.3333")
    await enter(p3c_client, tenant, period, 6, ppd=ppd, quantity="1")
    rule = await add_rule(p3c_client, tenant, period, "MinimumPay", "0.0001")

    packet = await build_packet(p3c_engine, tenant, period)
    assert packet.blockers == []
    frozen = await capture(p3c_engine, tenant, period)

    content = await _content(p3c_engine, frozen.snapshot_id)
    evaluation = _driver_total(content, tenant.driver_a)["MinMax"]
    assert (evaluation["Status"], evaluation["Numerator"], evaluation["Denominator"]) == (
        "APPLIED", 1, 7)
    (recorded,) = evaluation["Rules"]
    assert recorded["DriverPayRuleID"] == rule["driver_pay_rule_id"]
    assert recorded["Amount"] == D("0.0001") and recorded["ProratedAmount"] == D("0.0000")
    assert _kind(_driver_total(content, tenant.driver_a), "MINIMUM_ADJUSTMENT") == []
    async with p3c_engine.connect() as conn:
        await verify_snapshot(conn, snapshot_id=frozen.snapshot_id)
