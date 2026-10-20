"""Load, hash-verify and reconcile one immutable calculation snapshot.

``load_snapshot_content`` rebuilds, from the persisted rows alone, the content that the
financial hash covers (``snapshot_hash.calculate_snapshot_hash``). The snapshot writer, approval,
finalization and finalization preview all verify through this one path, so a tampered or
corrupted snapshot, or one captured under another contract, fails closed with a stable code
instead of being consumed.

Nothing here consults live payroll configuration: a snapshot is read as it was frozen.
The functions are read-only and open no transaction; the caller owns the connection.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.payroll.calculation.payable_rounding import PAYABLE_ROUNDING_VERSION, round_payable
from app.payroll.snapshot_hash import (
    UnsupportedCalculationVersionError,
    calculate_snapshot_hash,
    require_supported_calculation_version,
)

ZERO = Decimal("0")


class SnapshotEvidenceError(Exception):
    """A snapshot cannot be consumed: unsupported contract, tampering or inconsistency."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message

    def http(self, status_code: int = 422) -> HTTPException:
        return HTTPException(status_code=status_code, detail=f"{self.code}: {self.message}")


def _decimal(value: Any) -> Decimal:
    return Decimal(str(value))


def _optional_decimal(value: Any) -> Decimal | None:
    return None if value is None else Decimal(str(value))


async def load_snapshot_content(
    db: AsyncConnection,
    *,
    snapshot_id: int,
    company_id: int | None = None,
    branch_id: int | None = None,
    period_id: int | None = None,
) -> dict[str, Any]:
    """The persisted evidence of one snapshot in the shape the hash covers.

    Besides the hashed fields every level carries its row ids and the extra header metadata
    (revision, stored hash, author), which the hash ignores. When ``company_id``,
    ``branch_id`` or ``period_id`` is given the snapshot must belong to exactly that scope.
    Raises ``SnapshotEvidenceError`` (SNAPSHOT_NOT_FOUND, UNSUPPORTED_CALCULATION_VERSION).
    """
    header = (await db.execute(text("""
        SELECT payrollcalculationsnapshotid, companyid, branchid, payrollperiodid,
               revisionnumber, calculationversion, sourceconfighash, snapshothash,
               createdbyuserid, createdatutc, totalexpectedpay,
               reportevidenceversion, reportevidencehash,
               currencycode, currencyminorunitdigits, periodstartdate, periodenddate,
               payableroundingversion, minmaxprorationversion, scheduledworkdates, warnings
        FROM payroll.payrollcalculationsnapshots
        WHERE payrollcalculationsnapshotid = :snapshot_id
    """), {"snapshot_id": snapshot_id})).mappings().first()
    if header is None or any(
        expected is not None and int(header[column]) != expected
        for column, expected in (
            ("companyid", company_id), ("branchid", branch_id), ("payrollperiodid", period_id),
        )
    ):
        raise SnapshotEvidenceError(
            "SNAPSHOT_NOT_FOUND", "the calculation snapshot does not exist in this scope.")
    try:
        require_supported_calculation_version(header["calculationversion"])
    except UnsupportedCalculationVersionError as exc:
        raise SnapshotEvidenceError("UNSUPPORTED_CALCULATION_VERSION", str(exc)) from exc

    params = {"snapshot_id": snapshot_id}
    totals = (await db.execute(text("""
        SELECT payrollcalculationdrivertotalid, driverassignmentid, driverid, employeeid, branchid,
               drivernumbersnapshot, drivernamesnapshot, dailypay, statuspay,
               minimumadjustment, maximumadjustment, bonustotal, expectedpay
        FROM payroll.payrollcalculationdrivertotals
        WHERE payrollcalculationsnapshotid = :snapshot_id
        ORDER BY driverassignmentid
    """), params)).mappings().all()
    components = (await db.execute(text("""
        SELECT payrollcalculationsnapshotcomponentid, payrollcalculationdrivertotalid,
               componentkind, payrollperioddefinitionid, paydefinitionid, ratedefinitionid,
               calculationmethod, calculationmethodversion, definitioncodesnapshot,
               definitionnamesnapshot, statusratecolumnid, statusratecolumnnamesnapshot,
               driverpayruleid, payrollcalculationsnapshotminmaxruleid,
               preroundingamount, officialamount, sourcecount, unresolvedlinecount
        FROM payroll.payrollcalculationsnapshotcomponents
        WHERE payrollcalculationsnapshotid = :snapshot_id
        ORDER BY payrollcalculationsnapshotcomponentid
    """), params)).mappings().all()
    evaluations = (await db.execute(text("""
        SELECT payrollcalculationsnapshotminmaxevaluationid, payrollcalculationdrivertotalid,
               status, selectiondate, prorationversion, numerator, denominator,
               baseamount, baseordinaryamount, basestatusamount, excludedbonusamount,
               countedworkdates
        FROM payroll.payrollcalculationsnapshotminmaxevaluations
        WHERE payrollcalculationsnapshotid = :snapshot_id
        ORDER BY payrollcalculationsnapshotminmaxevaluationid
    """), params)).mappings().all()
    rules = (await db.execute(text("""
        SELECT payrollcalculationsnapshotminmaxruleid, payrollcalculationsnapshotminmaxevaluationid,
               driverpayruleid, ruletype, amount, rulestatus, effectivefrom, effectiveto,
               proratedamount, calendarfrequency, calendaranchorstartdate,
               calendarcustomintervaldays, calendarsourcebranchid,
               calendarsourcepayrollsetupversionid
        FROM payroll.payrollcalculationsnapshotminmaxrules
        WHERE payrollcalculationsnapshotid = :snapshot_id
        ORDER BY payrollcalculationsnapshotminmaxruleid
    """), params)).mappings().all()
    lines = (await db.execute(text("""
        SELECT sl.payrollcalculationsnapshotlineid, sl.payrollcalculationdrivertotalid,
               sl.payrollcalculationsnapshotcomponentid, sl.sourcetype, sl.sourceid, sl.linetype,
               sl.linescope, sl.workdate, sl.payrollperioddefinitionid, sl.paydefinitionid,
               sl.ratedefinitionid, sl.driverratescheduleid, sl.calculationstatus,
               sl.calculationversion, sl.statusratecolumnid, sl.ratetypeid, sl.driverrateid,
               sl.bonuseventid, sl.quantity, sl.resolvedrateamount, sl.calculatedamount,
               sl.sourceevidencejsonb, sl.usedratedefinitionid
        FROM payroll.payrollcalculationsnapshotlines sl
        JOIN payroll.payrollcalculationdrivertotals dt
          ON dt.payrollcalculationdrivertotalid = sl.payrollcalculationdrivertotalid
        WHERE dt.payrollcalculationsnapshotid = :snapshot_id
        ORDER BY sl.payrollcalculationsnapshotlineid
    """), params)).mappings().all()

    rules_by_evaluation: dict[int, list[dict[str, Any]]] = {}
    for row in rules:
        rules_by_evaluation.setdefault(
            int(row["payrollcalculationsnapshotminmaxevaluationid"]), []).append({
                "PayrollCalculationSnapshotMinMaxRuleID": int(row["payrollcalculationsnapshotminmaxruleid"]),
                "DriverPayRuleID": int(row["driverpayruleid"]),
                "RuleType": row["ruletype"],
                "Amount": _decimal(row["amount"]),
                "RuleStatus": row["rulestatus"],
                "EffectiveFrom": row["effectivefrom"],
                "EffectiveTo": row["effectiveto"],
                "ProratedAmount": _optional_decimal(row["proratedamount"]),
                "CalendarFrequency": row["calendarfrequency"],
                "CalendarAnchorStartDate": row["calendaranchorstartdate"],
                "CalendarCustomIntervalDays": row["calendarcustomintervaldays"],
                "CalendarSourceBranchID": int(row["calendarsourcebranchid"]),
                "CalendarSourcePayrollSetupVersionID": int(row["calendarsourcepayrollsetupversionid"]),
            })
    minmax_by_total: dict[int, dict[str, Any]] = {}
    for row in evaluations:
        evaluation_id = int(row["payrollcalculationsnapshotminmaxevaluationid"])
        minmax_by_total[int(row["payrollcalculationdrivertotalid"])] = {
            "PayrollCalculationSnapshotMinMaxEvaluationID": evaluation_id,
            "Status": row["status"],
            "SelectionDate": row["selectiondate"],
            "ProrationVersion": row["prorationversion"],
            "Numerator": row["numerator"],
            "Denominator": row["denominator"],
            "BaseAmount": _decimal(row["baseamount"]),
            "BaseOrdinaryAmount": _decimal(row["baseordinaryamount"]),
            "BaseStatusAmount": _decimal(row["basestatusamount"]),
            "ExcludedBonusAmount": _decimal(row["excludedbonusamount"]),
            "CountedWorkDates": list(row["countedworkdates"]),
            "Rules": rules_by_evaluation.get(evaluation_id, []),
        }
    components_by_total: dict[int, list[dict[str, Any]]] = {}
    for row in components:
        components_by_total.setdefault(int(row["payrollcalculationdrivertotalid"]), []).append({
            "PayrollCalculationSnapshotComponentID": int(row["payrollcalculationsnapshotcomponentid"]),
            "ComponentKind": row["componentkind"],
            "PayrollPeriodDefinitionID": row["payrollperioddefinitionid"],
            "PayDefinitionID": row["paydefinitionid"],
            "RateDefinitionID": row["ratedefinitionid"],
            "CalculationMethod": row["calculationmethod"],
            "CalculationMethodVersion": row["calculationmethodversion"],
            "DefinitionCodeSnapshot": row["definitioncodesnapshot"],
            "DefinitionNameSnapshot": row["definitionnamesnapshot"],
            "StatusRateColumnID": row["statusratecolumnid"],
            "StatusRateColumnNameSnapshot": row["statusratecolumnnamesnapshot"],
            "DriverPayRuleID": row["driverpayruleid"],
            "PayrollCalculationSnapshotMinMaxRuleID": row["payrollcalculationsnapshotminmaxruleid"],
            "PreRoundingAmount": _decimal(row["preroundingamount"]),
            "OfficialAmount": _decimal(row["officialamount"]),
            "SourceCount": int(row["sourcecount"]),
            "UnresolvedLineCount": int(row["unresolvedlinecount"]),
        })
    lines_by_total: dict[int, list[dict[str, Any]]] = {}
    for row in lines:
        lines_by_total.setdefault(int(row["payrollcalculationdrivertotalid"]), []).append({
            "PayrollCalculationSnapshotLineID": int(row["payrollcalculationsnapshotlineid"]),
            "PayrollCalculationSnapshotComponentID": int(row["payrollcalculationsnapshotcomponentid"]),
            "UsedRateDefinitionID": row["usedratedefinitionid"],
            "SourceType": row["sourcetype"],
            "SourceID": row["sourceid"],
            "LineType": row["linetype"],
            "LineScope": row["linescope"],
            "WorkDate": row["workdate"],
            "PayrollPeriodDefinitionID": row["payrollperioddefinitionid"],
            "PayDefinitionID": row["paydefinitionid"],
            "RateDefinitionID": row["ratedefinitionid"],
            "DriverRateScheduleID": row["driverratescheduleid"],
            "CalculationStatus": row["calculationstatus"],
            "CalculationVersion": row["calculationversion"],
            "StatusRateColumnID": row["statusratecolumnid"],
            "RateTypeID": row["ratetypeid"],
            "DriverRateID": row["driverrateid"],
            "BonusEventID": row["bonuseventid"],
            "Quantity": _optional_decimal(row["quantity"]),
            "ResolvedRateAmount": _optional_decimal(row["resolvedrateamount"]),
            "CalculatedAmount": _decimal(row["calculatedamount"]),
            "SourceEvidenceJSONB": row["sourceevidencejsonb"],
        })

    driver_totals = []
    for row in totals:
        total_id = int(row["payrollcalculationdrivertotalid"])
        driver_totals.append({
            "PayrollCalculationDriverTotalID": total_id,
            "DriverAssignmentID": int(row["driverassignmentid"]),
            "DriverID": int(row["driverid"]),
            "EmployeeID": int(row["employeeid"]),
            "BranchID": int(row["branchid"]),
            "DriverNumberSnapshot": row["drivernumbersnapshot"],
            "DriverNameSnapshot": row["drivernamesnapshot"],
            "DailyPay": _decimal(row["dailypay"]),
            "StatusPay": _decimal(row["statuspay"]),
            "MinimumAdjustment": _decimal(row["minimumadjustment"]),
            "MaximumAdjustment": _decimal(row["maximumadjustment"]),
            "BonusTotal": _decimal(row["bonustotal"]),
            "ExpectedPay": _decimal(row["expectedpay"]),
            "Components": components_by_total.get(total_id, []),
            "MinMax": minmax_by_total.get(total_id),
            "Lines": lines_by_total.get(total_id, []),
        })
    return {
        "PayrollCalculationSnapshotID": int(header["payrollcalculationsnapshotid"]),
        "RevisionNumber": int(header["revisionnumber"]),
        "SnapshotHash": header["snapshothash"],
        "ReportEvidenceVersion": header["reportevidenceversion"],
        "ReportEvidenceHash": header["reportevidencehash"],
        "CreatedByUserID": int(header["createdbyuserid"]),
        "CreatedAtUtc": header["createdatutc"],
        "CompanyID": int(header["companyid"]),
        "BranchID": int(header["branchid"]),
        "PayrollPeriodID": int(header["payrollperiodid"]),
        "PeriodStartDate": header["periodstartdate"],
        "PeriodEndDate": header["periodenddate"],
        "CurrencyCode": header["currencycode"],
        "CurrencyMinorUnitDigits": int(header["currencyminorunitdigits"]),
        "CalculationVersion": header["calculationversion"],
        "PayableRoundingVersion": header["payableroundingversion"],
        "MinMaxProrationVersion": header["minmaxprorationversion"],
        "SourceConfigHash": header["sourceconfighash"],
        "ScheduledWorkDates": list(header["scheduledworkdates"]),
        "Warnings": list(header["warnings"]),
        "TotalExpectedPay": _decimal(header["totalexpectedpay"]),
        "DriverTotals": driver_totals,
    }


def _integrity(message: str) -> SnapshotEvidenceError:
    return SnapshotEvidenceError("SNAPSHOT_INTEGRITY_ERROR", message)


def reconcile_snapshot_content(content: dict[str, Any]) -> None:
    """Check the arithmetic of the persisted evidence; never consult any mutable source.

    * the header total is the sum of the Driver totals;
    * each Driver total's fields are the sums of its official components by kind, and its
      ExpectedPay the sum of all of them;
    * each component's pre-rounding amount is the sum of its source lines (or, for a rule
      adjustment, reproduced by its Min/Max evidence), and its official amount is that amount
      rounded once with the frozen rounding rule.

    Raises ``SnapshotEvidenceError`` (SNAPSHOT_INTEGRITY_ERROR, UNSUPPORTED_PAYABLE_ROUNDING_VERSION).
    """
    if content["PayableRoundingVersion"] != PAYABLE_ROUNDING_VERSION:
        raise SnapshotEvidenceError(
            "UNSUPPORTED_PAYABLE_ROUNDING_VERSION",
            f"unsupported payable rounding version {content['PayableRoundingVersion']!r}; "
            f"only {PAYABLE_ROUNDING_VERSION!r} can be consumed",
        )
    digits = content["CurrencyMinorUnitDigits"]
    totals = content["DriverTotals"]
    if content["TotalExpectedPay"] != sum((t["ExpectedPay"] for t in totals), ZERO):
        raise _integrity("the snapshot total does not equal the sum of its Driver totals.")

    for total in totals:
        label = f"DriverAssignment {total['DriverAssignmentID']}"
        components = total["Components"]
        by_kind: dict[str, list[dict[str, Any]]] = {}
        for component in components:
            by_kind.setdefault(component["ComponentKind"], []).append(component)

        def official_sum(*kinds: str, _by_kind: dict[str, list[dict[str, Any]]] = by_kind) -> Decimal:
            return sum((c["OfficialAmount"] for k in kinds for c in _by_kind.get(k, [])), ZERO)

        if (
            total["DailyPay"] != official_sum("ORDINARY")
            or total["StatusPay"] != official_sum("STATUS")
            or total["BonusTotal"] != official_sum("BONUS")
            or total["MinimumAdjustment"] != official_sum("MINIMUM_ADJUSTMENT")
            or total["MaximumAdjustment"] != official_sum("MAXIMUM_ADJUSTMENT")
            or total["ExpectedPay"] != sum((c["OfficialAmount"] for c in components), ZERO)
        ):
            raise _integrity(f"{label}: the Driver total does not equal its official components.")

        # Components against their source lines.
        for component in components:
            kind = component["ComponentKind"]
            if kind in ("MINIMUM_ADJUSTMENT", "MAXIMUM_ADJUSTMENT"):
                continue
            component_lines = [
                line for line in total["Lines"]
                if line["PayrollCalculationSnapshotComponentID"]
                == component["PayrollCalculationSnapshotComponentID"]
            ]
            if (
                len(component_lines) != component["SourceCount"]
                or sum((line["CalculatedAmount"] for line in component_lines), ZERO)
                != component["PreRoundingAmount"]
            ):
                raise _integrity(f"{label}: a {kind} component does not equal its source lines.")
        for component in components:
            if round_payable(component["PreRoundingAmount"], digits) != component["OfficialAmount"]:
                raise _integrity(f"{label}: a component official amount is not its rounded amount.")
        if any(
            line["PayrollCalculationSnapshotComponentID"] is None for line in total["Lines"]
        ):
            raise _integrity(f"{label}: a source line contributes to no component.")

        _reconcile_minmax(label, total, by_kind)


def _reconcile_minmax(
    label: str, total: dict[str, Any], by_kind: dict[str, list[dict[str, Any]]],
) -> None:
    """The adjustment components follow from the frozen Min/Max evidence alone."""
    evaluation = total["MinMax"]
    if evaluation is None:
        raise _integrity(f"{label}: the Driver total has no Min/Max evaluation.")
    base = total["DailyPay"] + total["StatusPay"]
    if (
        evaluation["BaseOrdinaryAmount"] != total["DailyPay"]
        or evaluation["BaseStatusAmount"] != total["StatusPay"]
        or evaluation["BaseAmount"] != base
        or evaluation["ExcludedBonusAmount"] != total["BonusTotal"]
    ):
        raise _integrity(f"{label}: the Min/Max base does not equal the official ordinary and Status pay.")
    rules = {rule["RuleType"]: rule for rule in evaluation["Rules"]}
    if (evaluation["Status"] == "NO_RULE") != (not rules):
        raise _integrity(f"{label}: the Min/Max evaluation does not match its recorded rules.")
    for rule_type, kind, sign_base in (
        ("MinimumPay", "MINIMUM_ADJUSTMENT", True), ("MaximumPay", "MAXIMUM_ADJUSTMENT", False),
    ):
        rule = rules.get(rule_type)
        expected = ZERO
        if evaluation["Status"] == "APPLIED" and rule is not None:
            prorated = rule["ProratedAmount"]
            if sign_base and prorated is not None and base < prorated:
                expected = prorated - base
            elif not sign_base and prorated is not None and base > prorated:
                expected = prorated - base
        adjustments = by_kind.get(kind, [])
        if expected == 0:
            if adjustments:
                raise _integrity(f"{label}: an adjustment exists that the Min/Max evidence does not produce.")
            continue
        if (
            len(adjustments) != 1
            or adjustments[0]["PreRoundingAmount"] != expected
            or adjustments[0]["DriverPayRuleID"] != rule["DriverPayRuleID"]
        ):
            raise _integrity(f"{label}: the {kind.lower()} is not reproduced by its Min/Max evidence.")


async def verify_snapshot(
    db: AsyncConnection,
    *,
    snapshot_id: int,
    company_id: int | None = None,
    branch_id: int | None = None,
    period_id: int | None = None,
) -> dict[str, Any]:
    """Load one snapshot and fail closed unless its persisted evidence matches its frozen hash.

    Checks, in order: the scope, the calculation version, the recomputed financial hash
    (SNAPSHOT_HASH_MISMATCH) and the arithmetic of the evidence. Returns the loaded content.
    """
    content = await load_snapshot_content(
        db, snapshot_id=snapshot_id, company_id=company_id, branch_id=branch_id,
        period_id=period_id)
    if calculate_snapshot_hash(content) != content["SnapshotHash"]:
        raise SnapshotEvidenceError(
            "SNAPSHOT_HASH_MISMATCH",
            "the persisted calculation evidence does not match its frozen hash.",
        )
    reconcile_snapshot_content(content)
    return content
