"""Load, hash-verify and reconcile one immutable calculation snapshot.

``load_snapshot_content`` rebuilds, from the persisted rows alone, the content that the
financial hash covers (``snapshot_hash.calculate_snapshot_hash``) together with the report-only
Status and Bonus evidence (its own hash). The snapshot writer, approval, finalization, the
finalization preview and every frozen reader verify through ``verify_snapshot``, the one
canonical verification, so a tampered or corrupted snapshot, or one captured under another
contract, fails closed with a stable code instead of being consumed. It covers the financial
hash (which includes the used-rate evidence), the arithmetic of the evidence, the line to
used-rate links, the report evidence version and hash, and the overlaps between the report
evidence and the financial evidence.

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
    CURRENT_REPORT_EVIDENCE_VERSION,
    UnsupportedCalculationVersionError,
    calculate_report_evidence_hash,
    calculate_snapshot_hash,
    require_supported_calculation_version,
    used_rate_fingerprint,
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
               sl.sourceevidencejsonb, sl.usedratedefinitionid,
               ur.definitionfingerprint AS usedratefingerprint
        FROM payroll.payrollcalculationsnapshotlines sl
        JOIN payroll.payrollcalculationdrivertotals dt
          ON dt.payrollcalculationdrivertotalid = sl.payrollcalculationdrivertotalid
        LEFT JOIN payroll.payrollcalculationsnapshotusedratedefinitions ur
          ON ur.payrollcalculationsnapshotusedratedefinitionid = sl.usedratedefinitionid
        WHERE dt.payrollcalculationsnapshotid = :snapshot_id
        ORDER BY sl.payrollcalculationsnapshotlineid
    """), params)).mappings().all()
    used_rates = (await db.execute(text("""
        SELECT payrollcalculationsnapshotusedratedefinitionid, companyid, branchid,
               payrollperiodid, driverassignmentid, driverid, definitionfingerprint,
               evidencekind, sourcetypesnapshot, driverratescheduleid, ratedefinitionid,
               paydefinitionid, ratecomponentdefinitionid, ratetypeid, driverrateid,
               ratetypecodesnapshot, ratetypenamesnapshot, unitnamesnapshot,
               rateamountsnapshot, effectivefromsnapshot, effectivetosnapshot,
               ratestatussnapshot, currencycodesnapshot, currencyminorunitdigitssnapshot
        FROM payroll.payrollcalculationsnapshotusedratedefinitions
        WHERE payrollcalculationsnapshotid = :snapshot_id
        ORDER BY payrollcalculationsnapshotusedratedefinitionid
    """), params)).mappings().all()
    status_entries = (await db.execute(text("""
        SELECT payrollperioddriverdayentrystateid, companyid, branchid, payrollperiodid,
               driverassignmentid, driverid, workdate, statuskeyid, statuscodesnapshot,
               statuslabelsnapshot, statusisoffreasonsnapshot
        FROM payroll.payrollcalculationsnapshotstatusentries
        WHERE payrollcalculationsnapshotid = :snapshot_id
        ORDER BY driverassignmentid, workdate, payrollperioddriverdayentrystateid
    """), params)).mappings().all()
    bonus_events = (await db.execute(text("""
        SELECT payrollbonuseventid, companyid, branchid, payrollperiodid, driverassignmentid,
               driverid, amount, reason, notes, datarevision, createdbyuserid,
               creatordisplaynamesnapshot, createdatutc, currencycodesnapshot,
               currencyminorunitdigitssnapshot
        FROM payroll.payrollcalculationsnapshotbonusevents
        WHERE payrollcalculationsnapshotid = :snapshot_id
        ORDER BY driverassignmentid, payrollbonuseventid
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
            "UsedRateFingerprint": row["usedratefingerprint"],
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

    used_rate_content = [{
        "PayrollCalculationSnapshotUsedRateDefinitionID":
            int(row["payrollcalculationsnapshotusedratedefinitionid"]),
        "CompanyID": int(row["companyid"]),
        "BranchID": int(row["branchid"]),
        "PayrollPeriodID": int(row["payrollperiodid"]),
        "DefinitionFingerprint": row["definitionfingerprint"],
        "EvidenceKind": row["evidencekind"],
        "SourceType": row["sourcetypesnapshot"],
        "DriverAssignmentID": int(row["driverassignmentid"]),
        "DriverID": int(row["driverid"]),
        "DriverRateScheduleID": row["driverratescheduleid"],
        "RateDefinitionID": row["ratedefinitionid"],
        "PayDefinitionID": row["paydefinitionid"],
        "RateComponentDefinitionID": row["ratecomponentdefinitionid"],
        "RateTypeID": row["ratetypeid"],
        "DriverRateID": row["driverrateid"],
        "RateTypeCodeSnapshot": row["ratetypecodesnapshot"],
        "RateTypeNameSnapshot": row["ratetypenamesnapshot"],
        "UnitNameSnapshot": row["unitnamesnapshot"],
        "RateAmount": _optional_decimal(row["rateamountsnapshot"]),
        "EffectiveFrom": row["effectivefromsnapshot"],
        "EffectiveTo": row["effectivetosnapshot"],
        "RateStatus": row["ratestatussnapshot"],
        "CurrencyCodeSnapshot": row["currencycodesnapshot"],
        "CurrencyMinorUnitDigitsSnapshot": row["currencyminorunitdigitssnapshot"],
    } for row in used_rates]
    report_status_entries = [{
        "PayrollPeriodDriverDayEntryStateID": int(row["payrollperioddriverdayentrystateid"]),
        "CompanyID": int(row["companyid"]),
        "BranchID": int(row["branchid"]),
        "PayrollPeriodID": int(row["payrollperiodid"]),
        "DriverAssignmentID": int(row["driverassignmentid"]),
        "DriverID": int(row["driverid"]),
        "WorkDate": row["workdate"],
        "StatusKeyID": int(row["statuskeyid"]),
        "StatusCodeSnapshot": row["statuscodesnapshot"],
        "StatusLabelSnapshot": row["statuslabelsnapshot"],
        "StatusIsOffReasonSnapshot": bool(row["statusisoffreasonsnapshot"]),
    } for row in status_entries]
    report_bonus_events = [{
        "PayrollBonusEventID": int(row["payrollbonuseventid"]),
        "CompanyID": int(row["companyid"]),
        "BranchID": int(row["branchid"]),
        "PayrollPeriodID": int(row["payrollperiodid"]),
        "DriverAssignmentID": int(row["driverassignmentid"]),
        "DriverID": int(row["driverid"]),
        "Amount": _decimal(row["amount"]),
        "CurrencyCodeSnapshot": row["currencycodesnapshot"],
        "CurrencyMinorUnitDigitsSnapshot": row["currencyminorunitdigitssnapshot"],
        "Reason": row["reason"],
        "Notes": row["notes"],
        "DataRevision": int(row["datarevision"]),
        "CreatedByUserID": row["createdbyuserid"],
        "CreatorDisplayNameSnapshot": row["creatordisplaynamesnapshot"],
        "CreatedAtUtc": row["createdatutc"],
    } for row in bonus_events]

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
        "UsedRates": used_rate_content,
        "ReportEvidence": {
            "StatusEntries": report_status_entries,
            "BonusEvents": report_bonus_events,
        },
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


def _used_rate_error(message: str) -> SnapshotEvidenceError:
    return SnapshotEvidenceError("USED_RATE_EVIDENCE_INTEGRITY_ERROR", message)


def reconcile_used_rate_evidence(content: dict[str, Any]) -> None:
    """The used-rate evidence is self-consistent and agrees with the lines that link to it.

    * every row's fingerprint is the fingerprint of its own descriptor, rows are unique and
      belong to the snapshot scope, currency and a Driver total of the snapshot;
    * every ordinary line links to a schedule row of its own DriverAssignment that agrees on
      the schedule, the rate definition, the pay definition and the resolved rate amount;
    * every Status line links to a Status DriverRate row that agrees on the DriverRate, the
      RateType and the rate amount; a Bonus line links to no rate;
    * every row is used by at least one line (no orphans).
    """
    used_rates = content.get("UsedRates", [])
    by_fingerprint: dict[str, dict[str, Any]] = {}
    drivers = {t["DriverAssignmentID"]: t["DriverID"] for t in content["DriverTotals"]}
    for rate in used_rates:
        fingerprint = rate["DefinitionFingerprint"]
        if used_rate_fingerprint(rate) != fingerprint or fingerprint in by_fingerprint:
            raise _used_rate_error("a used-rate row does not match its own fingerprint.")
        by_fingerprint[fingerprint] = rate
        if (
            (rate["CompanyID"], rate["BranchID"], rate["PayrollPeriodID"])
            != (content["CompanyID"], content["BranchID"], content["PayrollPeriodID"])
            or rate["CurrencyCodeSnapshot"] != content["CurrencyCode"]
            or rate["CurrencyMinorUnitDigitsSnapshot"] != content["CurrencyMinorUnitDigits"]
            or drivers.get(rate["DriverAssignmentID"]) != rate["DriverID"]
        ):
            raise _used_rate_error("a used-rate row is outside the snapshot scope.")

    used: set[str] = set()
    for total in content["DriverTotals"]:
        for line in total["Lines"]:
            fingerprint = line.get("UsedRateFingerprint")
            source_type = line["SourceType"]
            if source_type == "BonusEvent":
                if fingerprint is not None:
                    raise _used_rate_error("a Bonus line links to a rate.")
                continue
            rate = by_fingerprint.get(fingerprint) if fingerprint is not None else None
            if rate is None or rate["DriverAssignmentID"] != total["DriverAssignmentID"]:
                raise _used_rate_error("a line does not link to a used-rate row of its Driver.")
            if source_type == "DraftLine":
                agrees = (
                    rate["EvidenceKind"] == "DriverRateSchedule"
                    and rate["SourceType"] == "DraftLine"
                    and rate["DriverRateScheduleID"] == line["DriverRateScheduleID"]
                    and rate["RateDefinitionID"] == line["RateDefinitionID"]
                    and rate["PayDefinitionID"] == line["PayDefinitionID"]
                    and rate["RateAmount"] == line["ResolvedRateAmount"]
                )
            elif source_type == "StatusEntryState":
                agrees = (
                    rate["EvidenceKind"] == "StatusDriverRate"
                    and rate["SourceType"] == "StatusEntryState"
                    and rate["DriverRateID"] == line["DriverRateID"]
                    and rate["RateTypeID"] == line["RateTypeID"]
                    and rate["RateAmount"] == line["ResolvedRateAmount"]
                )
            else:
                agrees = False
            if not agrees:
                raise _used_rate_error("a line disagrees with the used-rate row it links to.")
            used.add(fingerprint)
    if used != set(by_fingerprint):
        raise _used_rate_error("a used-rate row is not used by any line of the snapshot.")


def _report_error(code: str, message: str) -> SnapshotEvidenceError:
    return SnapshotEvidenceError(code, message)


def _require_report_evidence_version(content: dict[str, Any]) -> None:
    if content["ReportEvidenceVersion"] != CURRENT_REPORT_EVIDENCE_VERSION:
        raise _report_error(
            "UNSUPPORTED_REPORT_EVIDENCE_VERSION",
            f"unsupported report evidence version {content['ReportEvidenceVersion']!r}; "
            f"only {CURRENT_REPORT_EVIDENCE_VERSION!r} can be consumed",
        )


def reconcile_report_evidence(content: dict[str, Any]) -> None:
    """The report-only Status and Bonus evidence: version, own hash and its overlaps with the
    financial evidence.

    The report evidence keeps its own hash (it is not financial), but it is verified here so no
    reader can expose it unverified, and two facts that can disagree are never left unchecked:

    * the stored version is the supported one and the stored hash equals the hash recomputed
      from the persisted rows;
    * the Bonus events equal the BONUS lines one to one (event, DriverAssignment, Driver,
      amount) and carry the snapshot currency;
    * each STATUS line's entry state is a Status entry of the same DriverAssignment, WorkDate
      and Status key;
    * no work date counted by a Min/Max evaluation is an off-reason Status entry of that Driver;
    * every Driver named by the evidence is the Driver of that DriverAssignment's total.
    """
    _require_report_evidence_version(content)
    evidence = content["ReportEvidence"]
    statuses, bonuses = evidence["StatusEntries"], evidence["BonusEvents"]
    if calculate_report_evidence_hash(
        status_entries=statuses, bonus_events=bonuses,
    ) != content["ReportEvidenceHash"]:
        raise _report_error(
            "REPORT_EVIDENCE_HASH_MISMATCH",
            "the persisted report evidence does not match its frozen hash.",
        )

    scope = (content["CompanyID"], content["BranchID"], content["PayrollPeriodID"])
    drivers = {t["DriverAssignmentID"]: t["DriverID"] for t in content["DriverTotals"]}
    for item in (*statuses, *bonuses):
        if (item["CompanyID"], item["BranchID"], item["PayrollPeriodID"]) != scope:
            raise _report_error(
                "REPORT_EVIDENCE_INTEGRITY_ERROR", "report evidence is outside the snapshot scope.")
        driver_id = drivers.get(item["DriverAssignmentID"])
        if driver_id is not None and driver_id != item["DriverID"]:
            raise _report_error(
                "REPORT_EVIDENCE_INTEGRITY_ERROR",
                "report evidence names another Driver than the Driver total of its assignment.")

    bonus_lines = sorted(
        (int(line["BonusEventID"]), total["DriverAssignmentID"], total["DriverID"],
         line["CalculatedAmount"])
        for total in content["DriverTotals"] for line in total["Lines"]
        if line["SourceType"] == "BonusEvent"
    )
    bonus_events = sorted(
        (event["PayrollBonusEventID"], event["DriverAssignmentID"], event["DriverID"],
         event["Amount"])
        for event in bonuses
    )
    if (
        bonus_lines != bonus_events
        or any(
            event["CurrencyCodeSnapshot"] != content["CurrencyCode"]
            or event["CurrencyMinorUnitDigitsSnapshot"] != content["CurrencyMinorUnitDigits"]
            for event in bonuses
        )
    ):
        raise _report_error(
            "REPORT_EVIDENCE_INTEGRITY_ERROR",
            "the Bonus report evidence does not equal the Bonus lines of the snapshot.")

    entries = {entry["PayrollPeriodDriverDayEntryStateID"]: entry for entry in statuses}
    for total in content["DriverTotals"]:
        for line in total["Lines"]:
            if line["SourceType"] != "StatusEntryState":
                continue
            entry = entries.get(int(line["SourceID"]))
            status_key = (line["SourceEvidenceJSONB"] or {}).get("StatusKeyID")
            if (
                entry is None
                or entry["DriverAssignmentID"] != total["DriverAssignmentID"]
                or entry["WorkDate"] != line["WorkDate"]
                or entry["StatusKeyID"] != status_key
            ):
                raise _report_error(
                    "REPORT_EVIDENCE_INTEGRITY_ERROR",
                    "a Status line is not a Status entry of the report evidence.")
    off_days = {
        (entry["DriverAssignmentID"], entry["WorkDate"])
        for entry in statuses if entry["StatusIsOffReasonSnapshot"]
    }
    for total in content["DriverTotals"]:
        evaluation = total["MinMax"]
        if evaluation is not None and any(
            (total["DriverAssignmentID"], day) in off_days
            for day in evaluation["CountedWorkDates"]
        ):
            raise _report_error(
                "REPORT_EVIDENCE_INTEGRITY_ERROR",
                "a work date counted by Min/Max is an off-reason Status entry.")


async def verify_snapshot(
    db: AsyncConnection,
    *,
    snapshot_id: int,
    company_id: int | None = None,
    branch_id: int | None = None,
    period_id: int | None = None,
) -> dict[str, Any]:
    """Load one snapshot and fail closed unless its persisted evidence matches its frozen hash.

    Checks, in order: the scope, the calculation version, the report evidence version, the
    recomputed financial hash over the evidence and the rates it used (SNAPSHOT_HASH_MISMATCH),
    the arithmetic of the evidence, the used-rate links, the report evidence hash and its
    overlaps with the financial evidence. Returns the loaded and verified content, including the
    report evidence, so a reader never queries the evidence tables itself.
    """
    content = await load_snapshot_content(
        db, snapshot_id=snapshot_id, company_id=company_id, branch_id=branch_id,
        period_id=period_id)
    if any(total["MinMax"] is None for total in content["DriverTotals"]):
        raise _integrity("a Driver total has no Min/Max evaluation.")
    _require_report_evidence_version(content)
    if calculate_snapshot_hash(content) != content["SnapshotHash"]:
        raise SnapshotEvidenceError(
            "SNAPSHOT_HASH_MISMATCH",
            "the persisted calculation evidence does not match its frozen hash.",
        )
    reconcile_snapshot_content(content)
    reconcile_used_rate_evidence(content)
    reconcile_report_evidence(content)
    return content
