"""The financial hash of a calculation snapshot: a pure function of its frozen content."""
from __future__ import annotations

import copy
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from app.payroll.snapshot_hash import (
    CURRENT_PAYROLL_CALCULATION_VERSION,
    SOURCE_CONFIG_PACKET_CONTRACT,
    UnsupportedCalculationVersionError,
    calculate_report_evidence_hash,
    calculate_snapshot_hash,
    calculate_source_config_hash,
    canonical_json,
    require_supported_calculation_version,
)
from app.payroll.snapshot_verification import (
    SnapshotEvidenceError,
    load_snapshot_content,
    reconcile_snapshot_content,
)

D = Decimal


def _component(kind, official, *, pre=None, **identity):
    return {
        "ComponentKind": kind,
        "PayrollPeriodDefinitionID": None, "PayDefinitionID": None, "RateDefinitionID": None,
        "CalculationMethod": None, "CalculationMethodVersion": None,
        "DefinitionCodeSnapshot": None, "DefinitionNameSnapshot": None,
        "StatusRateColumnID": None, "StatusRateColumnNameSnapshot": None, "DriverPayRuleID": None,
        "PreRoundingAmount": D(official) if pre is None else D(pre), "OfficialAmount": D(official),
        "SourceCount": 1, "UnresolvedLineCount": 0, **identity,
    }


def _line(source_id, amount, *, component_id, work_date=date(2040, 1, 2), kind="DraftLine"):
    return {
        "PayrollCalculationSnapshotLineID": 900 + int(source_id),
        "PayrollCalculationSnapshotComponentID": component_id,
        "UsedRateDefinitionID": 5,
        "SourceType": kind, "SourceID": source_id,
        "LineType": {"DraftLine": "ORDINARY", "BonusEvent": "BONUS"}[kind], "LineScope": "Daily",
        "WorkDate": work_date, "PayrollPeriodDefinitionID": 11, "PayDefinitionID": 21,
        "RateDefinitionID": 31, "DriverRateScheduleID": 41, "CalculationStatus": "Calculated",
        "CalculationVersion": "per-unit-v1", "StatusRateColumnID": None, "RateTypeID": None,
        "DriverRateID": None, "BonusEventID": None, "Quantity": D("2"),
        "ResolvedRateAmount": D("1.5"), "CalculatedAmount": D(amount), "SourceEvidenceJSONB": {},
    }


def _content() -> dict:
    ordinary = _component(
        "ORDINARY", "3.00", pre="3.0000", PayrollPeriodDefinitionID=11, PayDefinitionID=21,
        RateDefinitionID=31, CalculationMethod="PerUnit", CalculationMethodVersion=1,
        DefinitionCodeSnapshot="HAUL", DefinitionNameSnapshot="Haul", SourceCount=2,
        PayrollCalculationSnapshotComponentID=701)
    bonus = _component("BONUS", "2.00", SourceCount=1, PayrollCalculationSnapshotComponentID=702)
    return {
        "PayrollCalculationSnapshotID": 1, "RevisionNumber": 1, "SnapshotHash": "0" * 64,
        "CreatedByUserID": 7, "CreatedAtUtc": datetime(2040, 1, 9, tzinfo=UTC),
        "CompanyID": 1, "BranchID": 2, "PayrollPeriodID": 3,
        "PeriodStartDate": date(2040, 1, 1), "PeriodEndDate": date(2040, 1, 7),
        "CurrencyCode": "USD", "CurrencyMinorUnitDigits": 2,
        "CalculationVersion": CURRENT_PAYROLL_CALCULATION_VERSION,
        "PayableRoundingVersion": "payable-rounding-v1",
        "MinMaxProrationVersion": "minmax-proration-v1", "SourceConfigHash": "a" * 64,
        "ScheduledWorkDates": [date(2040, 1, 1), date(2040, 1, 2)], "Warnings": ["w"],
        "TotalExpectedPay": D("5.00"),
        "DriverTotals": [{
            "PayrollCalculationDriverTotalID": 501, "DriverAssignmentID": 51, "DriverID": 61,
            "EmployeeID": 71, "BranchID": 2, "DriverNumberSnapshot": "D-1",
            "DriverNameSnapshot": "Ada", "DailyPay": D("3.00"), "StatusPay": D("0"),
            "MinimumAdjustment": D("0"), "MaximumAdjustment": D("0"), "BonusTotal": D("2.00"),
            "ExpectedPay": D("5.00"),
            "Components": [bonus, ordinary],
            "MinMax": {
                "PayrollCalculationSnapshotMinMaxEvaluationID": 601, "Status": "NO_RULE",
                "SelectionDate": date(2040, 1, 1), "ProrationVersion": "minmax-proration-v1",
                "Numerator": None, "Denominator": None, "BaseAmount": D("3.00"),
                "BaseOrdinaryAmount": D("3.00"), "BaseStatusAmount": D("0"),
                "ExcludedBonusAmount": D("2.00"), "CountedWorkDates": [], "Rules": [],
            },
            "Lines": [
                _line("1", "1.5000", component_id=701),
                _line("2", "1.5000", component_id=701, work_date=date(2040, 1, 3)),
                _line("3", "2.00", component_id=702, work_date=None, kind="BonusEvent"),
            ],
        }],
    }


def test_the_hash_is_deterministic_and_a_sha256_hex_digest():
    first = calculate_snapshot_hash(_content())
    assert first == calculate_snapshot_hash(_content())
    assert len(first) == 64 and set(first) <= set("0123456789abcdef")


def test_row_ids_revision_and_audit_metadata_are_outside_the_hash():
    baseline = calculate_snapshot_hash(_content())
    changed = _content()
    changed["PayrollCalculationSnapshotID"] = 999
    changed["RevisionNumber"] = 7
    changed["SnapshotHash"] = "f" * 64
    changed["CreatedByUserID"] = 8
    changed["CreatedAtUtc"] = datetime(2041, 5, 5, tzinfo=UTC)
    total = changed["DriverTotals"][0]
    total["PayrollCalculationDriverTotalID"] = 12345
    total["MinMax"]["PayrollCalculationSnapshotMinMaxEvaluationID"] = 4242
    for component in total["Components"]:
        component["PayrollCalculationSnapshotComponentID"] += 1000
    for line in total["Lines"]:
        line["PayrollCalculationSnapshotLineID"] += 1000
        line["PayrollCalculationSnapshotComponentID"] += 1000
        line["UsedRateDefinitionID"] = 99
    assert calculate_snapshot_hash(changed) == baseline


def test_the_order_of_components_lines_and_driver_totals_does_not_change_the_hash():
    baseline = calculate_snapshot_hash(_content())
    shuffled = _content()
    total = shuffled["DriverTotals"][0]
    total["Components"].reverse()
    total["Lines"].reverse()
    other = copy.deepcopy(total)
    other["DriverAssignmentID"] = 52
    both = _content()
    both["DriverTotals"] = [copy.deepcopy(other), copy.deepcopy(both["DriverTotals"][0])]
    reordered = _content()
    reordered["DriverTotals"] = [copy.deepcopy(both["DriverTotals"][1]), copy.deepcopy(other)]
    assert calculate_snapshot_hash(shuffled) == baseline
    assert calculate_snapshot_hash(both) == calculate_snapshot_hash(reordered)


def test_decimal_scale_is_canonical_and_does_not_change_the_hash():
    baseline = calculate_snapshot_hash(_content())
    rescaled = _content()
    rescaled["TotalExpectedPay"] = D("5.0000")
    rescaled["DriverTotals"][0]["DailyPay"] = D("3.0")
    assert calculate_snapshot_hash(rescaled) == baseline


@pytest.mark.parametrize("mutate", [
    lambda c: c.__setitem__("CurrencyCode", "EUR"),
    lambda c: c.__setitem__("CurrencyMinorUnitDigits", 3),
    lambda c: c.__setitem__("PayableRoundingVersion", "payable-rounding-v2"),
    lambda c: c.__setitem__("MinMaxProrationVersion", "minmax-proration-v2"),
    lambda c: c.__setitem__("SourceConfigHash", "b" * 64),
    lambda c: c.__setitem__("ScheduledWorkDates", [date(2040, 1, 1)]),
    lambda c: c.__setitem__("Warnings", []),
    lambda c: c.__setitem__("TotalExpectedPay", D("5.01")),
    lambda c: c.__setitem__("PeriodEndDate", date(2040, 1, 8)),
    lambda c: c.__setitem__("BranchID", 3),
    lambda c: c["DriverTotals"][0].__setitem__("EmployeeID", 72),
    lambda c: c["DriverTotals"][0].__setitem__("DriverID", 62),
    lambda c: c["DriverTotals"][0].__setitem__("DriverNameSnapshot", "Grace"),
    lambda c: c["DriverTotals"][0].__setitem__("DriverNumberSnapshot", "D-2"),
    lambda c: c["DriverTotals"][0].__setitem__("BranchID", 3),
    lambda c: c["DriverTotals"][0]["Components"][0].__setitem__("OfficialAmount", D("2.01")),
    lambda c: c["DriverTotals"][0]["Components"][1].__setitem__("PreRoundingAmount", D("3.0001")),
    lambda c: c["DriverTotals"][0]["Components"][1].__setitem__("DefinitionNameSnapshot", "Other"),
    lambda c: c["DriverTotals"][0]["MinMax"].__setitem__("Status", "APPLIED"),
    lambda c: c["DriverTotals"][0]["MinMax"].__setitem__("ExcludedBonusAmount", D("2.01")),
    lambda c: c["DriverTotals"][0]["MinMax"].__setitem__("CountedWorkDates", [date(2040, 1, 1)]),
    lambda c: c["DriverTotals"][0]["Lines"][0].__setitem__("ResolvedRateAmount", D("1.6")),
    lambda c: c["DriverTotals"][0]["Lines"][0].__setitem__("DriverRateScheduleID", 42),
    lambda c: c["DriverTotals"][0]["Lines"][0].__setitem__("SourceID", "3"),
    lambda c: c["DriverTotals"][0]["Lines"][0].__setitem__("CalculatedAmount", D("1.5001")),
])
def test_every_financial_field_changes_the_hash(mutate):
    changed = _content()
    mutate(changed)
    assert calculate_snapshot_hash(changed) != calculate_snapshot_hash(_content())


def test_min_max_rule_evidence_is_part_of_the_hash_in_a_deterministic_order():
    def rule(rule_type, prorated):
        return {
            "DriverPayRuleID": 81 if rule_type == "MinimumPay" else 82, "RuleType": rule_type,
            "Amount": D("70"), "RuleStatus": "Active", "EffectiveFrom": date(2040, 1, 1),
            "EffectiveTo": None, "ProratedAmount": D(prorated), "CalendarFrequency": "Week",
            "CalendarAnchorStartDate": date(2000, 1, 3), "CalendarCustomIntervalDays": None,
            "CalendarSourceBranchID": 2, "CalendarSourcePayrollSetupVersionID": 9,
        }

    content = _content()
    content["DriverTotals"][0]["MinMax"]["Rules"] = [rule("MinimumPay", "40"), rule("MaximumPay", "90")]
    reversed_rules = copy.deepcopy(content)
    reversed_rules["DriverTotals"][0]["MinMax"]["Rules"].reverse()
    assert calculate_snapshot_hash(content) == calculate_snapshot_hash(reversed_rules)
    amended = copy.deepcopy(content)
    amended["DriverTotals"][0]["MinMax"]["Rules"][0]["ProratedAmount"] = D("41")
    assert calculate_snapshot_hash(amended) != calculate_snapshot_hash(content)
    calendar = copy.deepcopy(content)
    calendar["DriverTotals"][0]["MinMax"]["Rules"][0]["CalendarAnchorStartDate"] = date(2000, 1, 10)
    assert calculate_snapshot_hash(calendar) != calculate_snapshot_hash(content)


def test_canonical_serialization_rejects_floats_and_unknown_types():
    with pytest.raises(TypeError):
        canonical_json({"amount": 1.5})
    with pytest.raises(TypeError):
        canonical_json({"value": object()})
    assert canonical_json({"b": D("1.50"), "a": date(2040, 1, 2)}) == '{"a":"2040-01-02","b":"1.5"}'


def test_only_the_current_calculation_version_is_supported():
    require_supported_calculation_version(CURRENT_PAYROLL_CALCULATION_VERSION)
    assert CURRENT_PAYROLL_CALCULATION_VERSION == "payroll-calculation-v2"
    for version in ("payroll-calculation-v1", "payroll-calculation-v3", None, ""):
        with pytest.raises(UnsupportedCalculationVersionError):
            require_supported_calculation_version(version)


def test_the_source_config_and_report_evidence_hashes_are_separate_helpers():
    assert SOURCE_CONFIG_PACKET_CONTRACT == "payroll-source-config-v2"
    payload = {"PacketContract": SOURCE_CONFIG_PACKET_CONTRACT, "Sources": []}
    assert calculate_source_config_hash(payload) == calculate_source_config_hash(dict(payload))
    empty = calculate_report_evidence_hash(status_entries=[], bonus_events=[])
    assert len(empty) == 64 and empty != calculate_snapshot_hash(_content())


# ---------------------------------------------------------------------------
# Reconciliation of the persisted evidence arithmetic (pure)
# ---------------------------------------------------------------------------

def test_consistent_evidence_reconciles():
    reconcile_snapshot_content(_content())


@pytest.mark.parametrize("mutate, message", [
    (lambda c: c.__setitem__("TotalExpectedPay", D("5.01")), "snapshot total"),
    (lambda c: c["DriverTotals"][0].__setitem__("DailyPay", D("3.01")), "official components"),
    (lambda c: c["DriverTotals"][0].__setitem__("ExpectedPay", D("5.01")), "snapshot total"),
    (lambda c: c["DriverTotals"][0]["Components"][0].__setitem__("OfficialAmount", D("2.01")),
     "official components"),
    (lambda c: c["DriverTotals"][0]["Lines"][0].__setitem__("CalculatedAmount", D("1.5001")),
     "ORDINARY component"),
    (lambda c: c["DriverTotals"][0]["MinMax"].__setitem__("ExcludedBonusAmount", D("1")),
     "Min/Max base"),
    (lambda c: c["DriverTotals"][0]["MinMax"].__setitem__("Status", "APPLIED"),
     "recorded rules"),
])
def test_inconsistent_evidence_fails_reconciliation(mutate, message):
    content = _content()
    mutate(content)
    with pytest.raises(SnapshotEvidenceError) as failed:
        reconcile_snapshot_content(content)
    assert failed.value.code == "SNAPSHOT_INTEGRITY_ERROR" and message in failed.value.message


def test_an_unsupported_payable_rounding_version_fails_closed():
    content = _content()
    content["PayableRoundingVersion"] = "payable-rounding-v9"
    with pytest.raises(SnapshotEvidenceError) as failed:
        reconcile_snapshot_content(content)
    assert failed.value.code == "UNSUPPORTED_PAYABLE_ROUNDING_VERSION"


def test_an_adjustment_without_its_min_max_evidence_fails_reconciliation():
    content = _content()
    total = content["DriverTotals"][0]
    total["MinimumAdjustment"] = D("1.00")
    total["ExpectedPay"] = D("6.00")
    content["TotalExpectedPay"] = D("6.00")
    total["Components"].append(_component(
        "MINIMUM_ADJUSTMENT", "1.00", SourceCount=0, DriverPayRuleID=81))
    with pytest.raises(SnapshotEvidenceError) as failed:
        reconcile_snapshot_content(content)
    assert failed.value.code == "SNAPSHOT_INTEGRITY_ERROR"
    assert "Min/Max evidence does not produce" in failed.value.message


@pytest.mark.asyncio
async def test_a_snapshot_of_another_calculation_version_is_not_parsed():
    class _Result:
        def mappings(self):
            return self

        def first(self):
            return {
                "payrollcalculationsnapshotid": 1, "companyid": 1, "branchid": 1,
                "payrollperiodid": 1, "calculationversion": "payroll-calculation-v1",
            }

    class _Connection:
        executed = 0

        async def execute(self, *_args, **_kwargs):
            _Connection.executed += 1
            return _Result()

    with pytest.raises(SnapshotEvidenceError) as failed:
        await load_snapshot_content(_Connection(), snapshot_id=1)
    assert failed.value.code == "UNSUPPORTED_CALCULATION_VERSION"
    assert _Connection.executed == 1                         # nothing past the header is read
    assert failed.value.http().detail.startswith("UNSUPPORTED_CALCULATION_VERSION: ")
