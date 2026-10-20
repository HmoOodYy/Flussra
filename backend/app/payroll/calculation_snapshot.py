"""Persist the immutable calculation snapshot and report evidence captured by lifecycle.

The caller owns the transaction and locks. The snapshot freezes the live calculation packet
exactly as built: official components (one rounding per component, ``payable-rounding-v1``),
the Min/Max evaluation with its proration and rule evidence, DriverAssignment / Driver /
Employee / Branch identity, and target definition and rate provenance. This writer never
recalculates, rounds or re-derives money. Before returning it reads back what it inserted and
proves that the hash recomputed from the persisted rows equals the hash computed from the
packet.
"""
from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.company_currency import CompanyCurrency, currency_error
from app.payroll import bonus as payroll_bonus
from app.payroll.calculation import minmax_proration as minmax
from app.payroll.immutable_evidence import capture_snapshot_used_rate_definitions
from app.payroll.period_calculation import (
    _CalculationPacketDriverTotal,
    _CalculationPacketLine,
    _LiveCalculationPacket,
    _MinMaxRule,
)
from app.payroll.schemas import PeriodSummary
from app.payroll.snapshot_hash import (
    CURRENT_PAYROLL_CALCULATION_VERSION,
    CURRENT_REPORT_EVIDENCE_VERSION,
    SOURCE_CONFIG_PACKET_CONTRACT,
    calculate_report_evidence_hash,
    calculate_snapshot_hash,
    calculate_source_config_hash,
    canonical_json,
)
from app.payroll.snapshot_verification import SnapshotEvidenceError, verify_snapshot

# Only outcomes that do not block a submission are frozen.
_FROZEN_MINMAX_STATUSES = frozenset({
    minmax.APPLIED, minmax.NO_RULE, minmax.NO_SCHEDULED_DAYS, minmax.NO_ELIGIBLE_SCHEDULED_DAYS,
})
_RULE_TYPE_BY_KIND = {"MINIMUM_ADJUSTMENT": "MinimumPay", "MAXIMUM_ADJUSTMENT": "MaximumPay"}
_LINE_TYPE_BY_SOURCE = {
    "DraftLine": "ORDINARY", "StatusEntryState": "STATUS", "BonusEvent": "BONUS",
}


async def _load_report_evidence(
    *,
    period: PeriodSummary,
    company_id: int,
    db: AsyncConnection,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Read the report-only Status and Bonus evidence from the same transaction view."""
    status_result = await db.execute(
        text("""
            SELECT
                ppdes.payrollperioddriverdayentrystateid,
                ppdes.driverassignmentid,
                a.driverid,
                ppdes.workdate,
                ppdes.statuskeyid,
                sk.statuscode,
                sk.keyname,
                sk.isoffreason
            FROM payroll.payrollperioddriverdayentrystate ppdes
            JOIN payroll.payrollstatuskeys sk ON sk.statuskeyid = ppdes.statuskeyid
            JOIN core.driverassignments a ON a.driverassignmentid = ppdes.driverassignmentid
            WHERE ppdes.payrollperiodid = :pid
              AND ppdes.companyid = :cid
              AND ppdes.branchid = :bid
              AND ppdes.isvoided = FALSE
              AND ppdes.statuskeyid IS NOT NULL
              AND ppdes.workdate BETWEEN :period_start AND :period_end
            ORDER BY ppdes.driverassignmentid, ppdes.workdate,
                     ppdes.payrollperioddriverdayentrystateid
        """),
        {
            "pid": period.payroll_period_id,
            "cid": company_id,
            "bid": period.branch_id,
            "period_start": period.start_date,
            "period_end": period.end_date,
        },
    )
    status_entries = [
        {
            "PayrollPeriodDriverDayEntryStateID": int(row["payrollperioddriverdayentrystateid"]),
            "DriverAssignmentID": int(row["driverassignmentid"]),
            "DriverID": int(row["driverid"]),
            "WorkDate": row["workdate"],
            "StatusKeyID": int(row["statuskeyid"]),
            "StatusCodeSnapshot": row["statuscode"],
            "StatusLabelSnapshot": row["keyname"],
            "StatusIsOffReasonSnapshot": bool(row["isoffreason"]),
        }
        for row in status_result.mappings().all()
    ]
    bonus_events = [
        {
            "PayrollBonusEventID": int(row["payrollbonuseventid"]),
            "DriverAssignmentID": int(row["driverassignmentid"]),
            "DriverID": int(row["driverid"]),
            "Amount": Decimal(str(row["amount"])),
            "Reason": row["reason"],
            "Notes": row["notes"],
            "DataRevision": int(row["datarevision"]),
            "CreatedByUserID": (
                int(row["createdbyuserid"])
                if row["createdbyuserid"] is not None
                else None
            ),
            "CreatorDisplayNameSnapshot": row["creatordisplaynamesnapshot"],
            "CreatedAtUtc": row["createdatutc"],
        }
        for row in await payroll_bonus._load_active_bonus_events(period.payroll_period_id, company_id, db)
    ]
    return status_entries, bonus_events


def _line_amount(line: _CalculationPacketLine) -> Decimal | None:
    if line.snapshot_calculated_amount is not None:
        return line.snapshot_calculated_amount
    return line.calculated_amount


def _persisted_lines(driver: _CalculationPacketDriverTotal) -> list[_CalculationPacketLine]:
    """The per-source lines that are frozen: System rule lines are explained by the Min/Max
    evidence and its adjustment components instead."""
    return [line for line in driver.lines if line.source_type != "System"]


def _line_content(line: _CalculationPacketLine) -> dict[str, Any]:
    source_type = line.snapshot_source_type or line.source_type
    return {
        "SourceType": source_type,
        "SourceID": line.snapshot_source_id if line.snapshot_source_id is not None else line.source_id,
        "LineType": _LINE_TYPE_BY_SOURCE[source_type],
        "LineScope": line.line_scope,
        "WorkDate": line.work_date,
        "PayrollPeriodDefinitionID": line.payroll_period_definition_id,
        "PayDefinitionID": line.pay_definition_id,
        "RateDefinitionID": line.rate_definition_id,
        "DriverRateScheduleID": line.driver_rate_schedule_id,
        "CalculationStatus": line.calculation_status if source_type == "DraftLine" else None,
        "CalculationVersion": line.calculation_version,
        "StatusRateColumnID": line.rate_column_id,
        "RateTypeID": line.rate_type_id,
        "DriverRateID": line.driver_rate_id,
        "BonusEventID": line.bonus_event_id,
        "Quantity": line.quantity,
        "ResolvedRateAmount": line.resolved_rate_amount,
        "CalculatedAmount": _line_amount(line),
        "SourceEvidenceJSONB": line.source_evidence or {},
        # Descriptors of the rate the line used, for the used-rate evidence. Not hashed: the
        # hash covers the schedule id and the resolved rate amount, which identify the rate.
        "RateComponentDefinitionID": line.rate_component_definition_id,
        "ScheduleStatus": line.schedule_status,
        "ScheduleEffectiveFrom": line.schedule_effective_from,
        "ScheduleEffectiveTo": line.schedule_effective_to,
    }


def _rule_content(rule: _MinMaxRule) -> dict[str, Any]:
    return {
        "DriverPayRuleID": rule.driver_pay_rule_id,
        "RuleType": rule.rule_type,
        "Amount": rule.amount,
        "RuleStatus": rule.status,
        "EffectiveFrom": rule.effective_from,
        "EffectiveTo": rule.effective_to,
        "ProratedAmount": rule.prorated_amount,
        "CalendarFrequency": rule.calendar_frequency,
        "CalendarAnchorStartDate": rule.calendar_anchor_start_date,
        "CalendarCustomIntervalDays": rule.calendar_custom_interval_days,
        "CalendarSourceBranchID": rule.calendar_source_branch_id,
        "CalendarSourcePayrollSetupVersionID": rule.calendar_source_payroll_setup_version_id,
    }


def _minmax_content(
    period: PeriodSummary, driver: _CalculationPacketDriverTotal,
) -> dict[str, Any]:
    proration = driver.minmax
    if proration is None or proration.status not in _FROZEN_MINMAX_STATUSES:
        raise HTTPException(
            status_code=422,
            detail=(
                "Cannot submit: the Min/Max evaluation of DriverAssignment "
                f"{driver.driver_assignment_id} has no freezable outcome."
            ),
        )
    return {
        "Status": proration.status,
        "SelectionDate": period.start_date,
        "ProrationVersion": proration.version,
        "Numerator": proration.numerator,
        "Denominator": proration.denominator,
        "BaseAmount": proration.base_amount,
        "BaseOrdinaryAmount": driver.daily_pay,
        "BaseStatusAmount": driver.status_pay,
        "ExcludedBonusAmount": driver.bonus_total,
        "CountedWorkDates": list(proration.counted_work_dates),
        "Rules": [
            _rule_content(rule)
            for rule in (proration.minimum_rule, proration.maximum_rule) if rule is not None
        ],
    }


def _component_content(driver: _CalculationPacketDriverTotal) -> list[dict[str, Any]]:
    return [
        {
            "ComponentKind": component.kind,
            "PayrollPeriodDefinitionID": component.payroll_period_definition_id,
            "PayDefinitionID": component.pay_definition_id,
            "RateDefinitionID": component.rate_definition_id,
            "CalculationMethod": component.calculation_method,
            "CalculationMethodVersion": component.calculation_method_version,
            "DefinitionCodeSnapshot": component.definition_code,
            "DefinitionNameSnapshot": component.definition_name,
            "StatusRateColumnID": component.status_rate_column_id,
            "StatusRateColumnNameSnapshot": component.status_rate_column_name,
            "DriverPayRuleID": component.driver_pay_rule_id,
            "RuleType": _RULE_TYPE_BY_KIND.get(component.kind),
            "PreRoundingAmount": component.pre_rounding_amount,
            "OfficialAmount": component.official_amount,
            "SourceCount": len(component.source_ids),
            "UnresolvedLineCount": component.unresolved_line_count,
        }
        for component in driver.components
    ]


def _snapshot_content(
    *,
    period: PeriodSummary,
    company_id: int,
    packet: _LiveCalculationPacket,
    currency: CompanyCurrency,
    source_config_hash: str,
) -> dict[str, Any]:
    """The financial content of the snapshot, straight from the packet."""
    return {
        "CompanyID": company_id,
        "BranchID": period.branch_id,
        "PayrollPeriodID": period.payroll_period_id,
        "PeriodStartDate": period.start_date,
        "PeriodEndDate": period.end_date,
        "CurrencyCode": currency.code,
        "CurrencyMinorUnitDigits": currency.minor_unit_digits,
        "CalculationVersion": CURRENT_PAYROLL_CALCULATION_VERSION,
        "PayableRoundingVersion": packet.payable_rounding_version,
        "MinMaxProrationVersion": packet.minmax_proration_version,
        "SourceConfigHash": source_config_hash,
        "ScheduledWorkDates": list(packet.scheduled_work_dates),
        "Warnings": list(packet.warnings),
        "TotalExpectedPay": packet.total_expected_pay,
        "DriverTotals": [
            {
                "DriverAssignmentID": driver.driver_assignment_id,
                "DriverID": driver.driver_id,
                "EmployeeID": driver.employee_id,
                "BranchID": period.branch_id,
                "DriverNumberSnapshot": driver.driver_number,
                "DriverNameSnapshot": driver.driver_name,
                "DailyPay": driver.daily_pay,
                "StatusPay": driver.status_pay,
                "MinimumAdjustment": driver.minimum_adjustment,
                "MaximumAdjustment": driver.maximum_adjustment,
                "BonusTotal": driver.bonus_total,
                "ExpectedPay": driver.expected_pay,
                "Components": _component_content(driver),
                "MinMax": _minmax_content(period, driver),
                "Lines": [_line_content(line) for line in _persisted_lines(driver)],
            }
            for driver in packet.drivers
        ],
    }


def _line_component_key(line: dict[str, Any]) -> tuple[str, int]:
    """Which component a line contributes to: the definition, the Status column or Bonus."""
    kind = line["LineType"]
    if kind == "ORDINARY":
        return kind, line["PayrollPeriodDefinitionID"]
    if kind == "STATUS":
        return kind, line["StatusRateColumnID"]
    return kind, 0


def _component_key(component: dict[str, Any]) -> tuple[str, int]:
    kind = component["ComponentKind"]
    if kind == "ORDINARY":
        return kind, component["PayrollPeriodDefinitionID"]
    if kind == "STATUS":
        return kind, component["StatusRateColumnID"]
    return kind, 0


async def _insert_scalar(db: AsyncConnection, statement: str, params: dict[str, Any]) -> int:
    return int((await db.execute(text(statement), params)).scalar_one())


async def _capture_calculation_snapshot(
    *,
    period: PeriodSummary,
    company_id: int,
    user_id: int,
    packet: _LiveCalculationPacket,
    db: AsyncConnection,
    context: str,
    currency: CompanyCurrency,
) -> int:
    """Persist one complete immutable submission snapshot and return its id.

    The caller already owns the period/workflow locks. This writer performs no calculation.
    """
    if packet.blockers:
        raise HTTPException(
            status_code=422,
            detail="Cannot submit an incomplete calculation packet: " + "; ".join(packet.blockers),
        )
    if any(
        _line_amount(line) is None
        for driver in packet.drivers
        for line in _persisted_lines(driver)
    ):
        raise HTTPException(
            status_code=422,
            detail="Cannot submit: an authoritative calculation line is unresolved.",
        )
    if (
        packet.currency_code != currency.code
        or packet.currency_minor_unit_digits != currency.minor_unit_digits
    ):
        raise currency_error(
            "SNAPSHOT_CURRENCY_MISMATCH",
            "The calculation packet currency does not match the Company currency.",
            409,
        )

    status_entries, bonus_events = await _load_report_evidence(
        period=period,
        company_id=company_id,
        db=db,
    )
    bonus_events = [
        {
            **event,
            "CurrencyCodeSnapshot": currency.code,
            "CurrencyMinorUnitDigitsSnapshot": currency.minor_unit_digits,
        }
        for event in bonus_events
    ]
    snapshot_bonus_lines = sorted(
        (line.bonus_event_id, line.driver_assignment_id, _line_amount(line))
        for driver in packet.drivers
        for line in driver.lines
        if line.source_type == "BonusEvent" and line.bonus_event_id is not None
    )
    evidence_bonus_lines = sorted(
        (event["PayrollBonusEventID"], event["DriverAssignmentID"], event["Amount"])
        for event in bonus_events
    )
    if snapshot_bonus_lines != evidence_bonus_lines:
        raise HTTPException(
            status_code=422,
            detail=(
                "Cannot submit: captured Bonus evidence does not reconcile "
                "with the authoritative calculation packet."
            ),
        )
    report_evidence_hash = calculate_report_evidence_hash(
        status_entries=status_entries,
        bonus_events=bonus_events,
    )

    eligibility_rows = (await db.execute(
        text("""
            SELECT driverassignmentid,
                   (SELECT a.driverid FROM core.driverassignments a
                     WHERE a.driverassignmentid = payroll.payrollperioddrivereligibility.driverassignmentid)
                       AS driverid,
                   iseligibleforperiod, eligibilityreasoncode,
                   hiredatesnapshot, terminationdatesnapshot,
                   drivereffectivefromatsnapshot, drivereffectivetoatsnapshot,
                   branchtimezoneidsnapshot, eligibleworkdates,
                   drivernumbersnapshot, drivernamesnapshot
            FROM payroll.payrollperioddrivereligibility
            WHERE payrollperiodid = :pid AND companyid = :cid AND branchid = :bid
            ORDER BY driverassignmentid
        """),
        {"pid": period.payroll_period_id, "cid": company_id, "bid": period.branch_id},
    )).mappings().all()

    source_config_payload = {
        "PacketContract": SOURCE_CONFIG_PACKET_CONTRACT,
        "CompanyCurrency": {
            "CurrencyCode": currency.code,
            "CurrencyMinorUnitDigits": currency.minor_unit_digits,
        },
        "PayrollPeriod": {
            "PayrollPeriodID": period.payroll_period_id,
            "CompanyID": company_id,
            "BranchID": period.branch_id,
            "PeriodCode": period.period_code,
            "PeriodType": period.period_type,
            "StartDate": period.start_date,
            "EndDate": period.end_date,
        },
        "ScheduledWorkDates": list(packet.scheduled_work_dates),
        "Eligibility": [dict(row) for row in eligibility_rows],
        "Sources": [
            {
                "DriverAssignmentID": driver.driver_assignment_id,
                "DriverID": driver.driver_id,
                "EmployeeID": driver.employee_id,
                "Lines": [
                    {
                        field: line[field]
                        for field in (
                            "SourceType", "SourceID", "LineScope", "WorkDate", "Quantity",
                            "PayrollPeriodDefinitionID", "PayDefinitionID", "RateDefinitionID",
                            "DriverRateScheduleID", "StatusRateColumnID", "RateTypeID",
                            "DriverRateID", "BonusEventID", "ResolvedRateAmount",
                            "SourceEvidenceJSONB",
                        )
                    }
                    for line in (_line_content(raw) for raw in _persisted_lines(driver))
                ],
            }
            for driver in packet.drivers
        ],
    }
    source_config_hash = calculate_source_config_hash(source_config_payload)
    content = _snapshot_content(
        period=period, company_id=company_id, packet=packet, currency=currency,
        source_config_hash=source_config_hash,
    )
    snapshot_hash = calculate_snapshot_hash(content)

    revision_number = int((await db.execute(
        text("""
            SELECT COALESCE(MAX(revisionnumber), 0) + 1
            FROM payroll.payrollcalculationsnapshots
            WHERE payrollperiodid = :pid
        """),
        {"pid": period.payroll_period_id},
    )).scalar_one())

    snapshot_id = await _insert_scalar(db, """
        INSERT INTO payroll.payrollcalculationsnapshots
            (companyid, branchid, payrollperiodid, revisionnumber,
             calculationversion, sourceconfighash, snapshothash,
             reportevidenceversion, reportevidencehash,
             currencycode, currencyminorunitdigits,
             periodstartdate, periodenddate,
             payableroundingversion, minmaxprorationversion,
             scheduledworkdates, warnings,
             createdbyuserid, totalexpectedpay)
        VALUES
            (:cid, :bid, :pid, :revision, :version, :source_hash,
             :snapshot_hash, :report_evidence_version, :report_evidence_hash,
             :currency_code, :currency_minor,
             :period_start, :period_end,
             :rounding_version, :proration_version,
             CAST(:scheduled AS DATE[]), CAST(:warnings AS TEXT[]),
             :uid, :total)
        RETURNING payrollcalculationsnapshotid
    """, {
        "cid": company_id,
        "bid": period.branch_id,
        "pid": period.payroll_period_id,
        "revision": revision_number,
        "version": content["CalculationVersion"],
        "source_hash": source_config_hash,
        "snapshot_hash": snapshot_hash,
        "report_evidence_version": CURRENT_REPORT_EVIDENCE_VERSION,
        "report_evidence_hash": report_evidence_hash,
        "currency_code": currency.code,
        "currency_minor": currency.minor_unit_digits,
        "period_start": period.start_date,
        "period_end": period.end_date,
        "rounding_version": content["PayableRoundingVersion"],
        "proration_version": content["MinMaxProrationVersion"],
        "scheduled": content["ScheduledWorkDates"],
        "warnings": content["Warnings"],
        "uid": user_id,
        "total": content["TotalExpectedPay"],
    })

    # Rate evidence first: every ordinary and Status line links to the rate it used.
    flat_lines = [
        {"DriverAssignmentID": total["DriverAssignmentID"], "DriverID": total["DriverID"], **line}
        for total in content["DriverTotals"] for line in total["Lines"]
    ]
    used_rate_definition_ids = await capture_snapshot_used_rate_definitions(
        snapshot_id=snapshot_id,
        company_id=company_id,
        branch_id=period.branch_id,
        period_id=period.payroll_period_id,
        lines=flat_lines,
        currency=currency,
        db=db,
    )

    line_ordinal = 0
    for total in content["DriverTotals"]:
        driver_total_id = await _insert_scalar(db, """
            INSERT INTO payroll.payrollcalculationdrivertotals
                (payrollcalculationsnapshotid, companyid, branchid, driverassignmentid,
                 driverid, employeeid, drivernumbersnapshot, drivernamesnapshot,
                 dailypay, statuspay, minimumadjustment, maximumadjustment, bonustotal,
                 expectedpay)
            VALUES
                (:snapshot_id, :cid, :bid, :driver_assignment_id, :driver_id, :employee_id,
                 :driver_number, :driver_name,
                 :daily, :status, :minimum, :maximum, :bonus, :expected)
            RETURNING payrollcalculationdrivertotalid
        """, {
            "snapshot_id": snapshot_id,
            "cid": company_id,
            "bid": total["BranchID"],
            "driver_assignment_id": total["DriverAssignmentID"],
            "driver_id": total["DriverID"],
            "employee_id": total["EmployeeID"],
            "driver_number": total["DriverNumberSnapshot"],
            "driver_name": total["DriverNameSnapshot"],
            "daily": total["DailyPay"],
            "status": total["StatusPay"],
            "minimum": total["MinimumAdjustment"],
            "maximum": total["MaximumAdjustment"],
            "bonus": total["BonusTotal"],
            "expected": total["ExpectedPay"],
        })

        evaluation = total["MinMax"]
        evaluation_id = await _insert_scalar(db, """
            INSERT INTO payroll.payrollcalculationsnapshotminmaxevaluations
                (payrollcalculationsnapshotid, companyid, branchid, payrollperiodid,
                 payrollcalculationdrivertotalid, status, selectiondate, prorationversion,
                 numerator, denominator, baseamount, baseordinaryamount, basestatusamount,
                 excludedbonusamount, countedworkdates)
            VALUES
                (:snapshot_id, :cid, :bid, :pid, :driver_total_id, :status, :selection_date,
                 :proration_version, :numerator, :denominator, :base, :base_ordinary,
                 :base_status, :excluded_bonus, CAST(:counted AS DATE[]))
            RETURNING payrollcalculationsnapshotminmaxevaluationid
        """, {
            "snapshot_id": snapshot_id, "cid": company_id, "bid": period.branch_id,
            "pid": period.payroll_period_id, "driver_total_id": driver_total_id,
            "status": evaluation["Status"], "selection_date": evaluation["SelectionDate"],
            "proration_version": evaluation["ProrationVersion"],
            "numerator": evaluation["Numerator"], "denominator": evaluation["Denominator"],
            "base": evaluation["BaseAmount"], "base_ordinary": evaluation["BaseOrdinaryAmount"],
            "base_status": evaluation["BaseStatusAmount"],
            "excluded_bonus": evaluation["ExcludedBonusAmount"],
            "counted": evaluation["CountedWorkDates"],
        })
        rule_ids: dict[str, int] = {}
        for rule in evaluation["Rules"]:
            rule_ids[rule["RuleType"]] = await _insert_scalar(db, """
                INSERT INTO payroll.payrollcalculationsnapshotminmaxrules
                    (payrollcalculationsnapshotminmaxevaluationid, payrollcalculationsnapshotid,
                     companyid, branchid, payrollperiodid, payrollcalculationdrivertotalid,
                     driverpayruleid, ruletype, amount, rulestatus, effectivefrom, effectiveto,
                     proratedamount, calendarfrequency, calendaranchorstartdate,
                     calendarcustomintervaldays, calendarsourcebranchid,
                     calendarsourcepayrollsetupversionid)
                VALUES
                    (:evaluation_id, :snapshot_id, :cid, :bid, :pid, :driver_total_id,
                     :rule_id, :rule_type, :amount, :rule_status, :effective_from, :effective_to,
                     :prorated, :frequency, :anchor, :interval, :source_branch, :source_version)
                RETURNING payrollcalculationsnapshotminmaxruleid
            """, {
                "evaluation_id": evaluation_id, "snapshot_id": snapshot_id, "cid": company_id,
                "bid": period.branch_id, "pid": period.payroll_period_id,
                "driver_total_id": driver_total_id, "rule_id": rule["DriverPayRuleID"],
                "rule_type": rule["RuleType"], "amount": rule["Amount"],
                "rule_status": rule["RuleStatus"], "effective_from": rule["EffectiveFrom"],
                "effective_to": rule["EffectiveTo"], "prorated": rule["ProratedAmount"],
                "frequency": rule["CalendarFrequency"],
                "anchor": rule["CalendarAnchorStartDate"],
                "interval": rule["CalendarCustomIntervalDays"],
                "source_branch": rule["CalendarSourceBranchID"],
                "source_version": rule["CalendarSourcePayrollSetupVersionID"],
            })

        component_ids: dict[tuple[str, int], int] = {}
        for component in total["Components"]:
            rule_evidence_id = (
                rule_ids.get(component["RuleType"]) if component["RuleType"] is not None else None
            )
            component_ids[_component_key(component)] = await _insert_scalar(db, """
                INSERT INTO payroll.payrollcalculationsnapshotcomponents
                    (payrollcalculationsnapshotid, companyid, branchid, payrollperiodid,
                     payrollcalculationdrivertotalid, componentkind, payrollperioddefinitionid,
                     paydefinitionid, ratedefinitionid, calculationmethod,
                     calculationmethodversion, definitioncodesnapshot, definitionnamesnapshot,
                     statusratecolumnid, statusratecolumnnamesnapshot, driverpayruleid,
                     payrollcalculationsnapshotminmaxruleid, ruletype,
                     preroundingamount, officialamount, sourcecount, unresolvedlinecount)
                VALUES
                    (:snapshot_id, :cid, :bid, :pid, :driver_total_id, :kind, :ppd_id,
                     :pay_definition_id, :rate_definition_id, :method, :method_version,
                     :code, :name, :status_column_id, :status_column_name, :rule_id,
                     :rule_evidence_id, :rule_type, :pre_rounding, :official, :source_count,
                     :unresolved)
                RETURNING payrollcalculationsnapshotcomponentid
            """, {
                "snapshot_id": snapshot_id, "cid": company_id, "bid": period.branch_id,
                "pid": period.payroll_period_id, "driver_total_id": driver_total_id,
                "kind": component["ComponentKind"],
                "ppd_id": component["PayrollPeriodDefinitionID"],
                "pay_definition_id": component["PayDefinitionID"],
                "rate_definition_id": component["RateDefinitionID"],
                "method": component["CalculationMethod"],
                "method_version": component["CalculationMethodVersion"],
                "code": component["DefinitionCodeSnapshot"],
                "name": component["DefinitionNameSnapshot"],
                "status_column_id": component["StatusRateColumnID"],
                "status_column_name": component["StatusRateColumnNameSnapshot"],
                "rule_id": component["DriverPayRuleID"],
                "rule_evidence_id": rule_evidence_id,
                "rule_type": component["RuleType"],
                "pre_rounding": component["PreRoundingAmount"],
                "official": component["OfficialAmount"],
                "source_count": component["SourceCount"],
                "unresolved": component["UnresolvedLineCount"],
            })

        for line in total["Lines"]:
            component_id = component_ids.get(_line_component_key(line))
            if component_id is None:
                raise HTTPException(
                    status_code=422,
                    detail="Cannot submit: a source line contributes to no official component.",
                )
            await db.execute(text("""
                INSERT INTO payroll.payrollcalculationsnapshotlines
                    (payrollcalculationdrivertotalid, payrollcalculationsnapshotcomponentid,
                     sourcetype, sourceid, linetype, linescope, workdate,
                     payrollperioddefinitionid, paydefinitionid, ratedefinitionid,
                     driverratescheduleid, calculationstatus, calculationversion,
                     statusratecolumnid, ratetypeid, driverrateid, bonuseventid, quantity,
                     resolvedrateamount, calculatedamount, sourceevidencejsonb,
                     usedratedefinitionid)
                VALUES
                    (:driver_total_id, :component_id, :source_type, :source_id, :line_type,
                     :line_scope, :work_date, :ppd_id, :pay_definition_id, :rate_definition_id,
                     :schedule_id, :calculation_status, :calculation_version,
                     :status_column_id, :rate_type_id, :driver_rate_id, :bonus_event_id,
                     :quantity, :resolved_rate, :calculated_amount, CAST(:evidence AS jsonb),
                     :used_rate_definition_id)
            """), {
                "driver_total_id": driver_total_id,
                "component_id": component_id,
                "source_type": line["SourceType"],
                "source_id": line["SourceID"],
                "line_type": line["LineType"],
                "line_scope": line["LineScope"],
                "work_date": line["WorkDate"],
                "ppd_id": line["PayrollPeriodDefinitionID"],
                "pay_definition_id": line["PayDefinitionID"],
                "rate_definition_id": line["RateDefinitionID"],
                "schedule_id": line["DriverRateScheduleID"],
                "calculation_status": line["CalculationStatus"],
                "calculation_version": line["CalculationVersion"],
                "status_column_id": line["StatusRateColumnID"],
                "rate_type_id": line["RateTypeID"],
                "driver_rate_id": line["DriverRateID"],
                "bonus_event_id": line["BonusEventID"],
                "quantity": line["Quantity"],
                "resolved_rate": line["ResolvedRateAmount"],
                "calculated_amount": line["CalculatedAmount"],
                "evidence": canonical_json(line["SourceEvidenceJSONB"]),
                "used_rate_definition_id": used_rate_definition_ids.get(line_ordinal),
            })
            line_ordinal += 1

    for entry in status_entries:
        await db.execute(
            text("""
                INSERT INTO payroll.payrollcalculationsnapshotstatusentries
                    (payrollcalculationsnapshotid, companyid, branchid,
                     payrollperiodid, driverassignmentid, driverid, workdate,
                     payrollperioddriverdayentrystateid, statuskeyid,
                     statuscodesnapshot, statuslabelsnapshot,
                     statusisoffreasonsnapshot)
                VALUES
                    (:snapshot_id, :cid, :bid, :pid, :driver_assignment_id, :driver_id, :work_date,
                     :entry_state_id, :status_key_id, :status_code,
                     :status_label, :status_is_off_reason)
            """),
            {
                "snapshot_id": snapshot_id,
                "cid": company_id,
                "bid": period.branch_id,
                "pid": period.payroll_period_id,
                "driver_assignment_id": entry["DriverAssignmentID"],
                "driver_id": entry["DriverID"],
                "work_date": entry["WorkDate"],
                "entry_state_id": entry["PayrollPeriodDriverDayEntryStateID"],
                "status_key_id": entry["StatusKeyID"],
                "status_code": entry["StatusCodeSnapshot"],
                "status_label": entry["StatusLabelSnapshot"],
                "status_is_off_reason": entry["StatusIsOffReasonSnapshot"],
            },
        )

    for event in bonus_events:
        await db.execute(
            text("""
                INSERT INTO payroll.payrollcalculationsnapshotbonusevents
                    (payrollcalculationsnapshotid, companyid, branchid,
                     payrollperiodid, payrollbonuseventid, driverassignmentid, driverid, amount,
                     reason, notes, datarevision, createdbyuserid,
                     creatordisplaynamesnapshot, createdatutc,
                     currencycodesnapshot, currencyminorunitdigitssnapshot)
                VALUES
                    (:snapshot_id, :cid, :bid, :pid, :bonus_event_id, :driver_assignment_id,
                     :driver_id, :amount, :reason, :notes, :data_revision, :created_by_user_id,
                     :creator_display_name, :created_at,
                     :currency_code, :currency_minor)
            """),
            {
                "snapshot_id": snapshot_id,
                "cid": company_id,
                "bid": period.branch_id,
                "pid": period.payroll_period_id,
                "bonus_event_id": event["PayrollBonusEventID"],
                "driver_assignment_id": event["DriverAssignmentID"],
                "driver_id": event["DriverID"],
                "amount": event["Amount"],
                "reason": event["Reason"],
                "notes": event["Notes"],
                "data_revision": event["DataRevision"],
                "created_by_user_id": event["CreatedByUserID"],
                "creator_display_name": event["CreatorDisplayNameSnapshot"],
                "created_at": event["CreatedAtUtc"],
                "currency_code": currency.code,
                "currency_minor": currency.minor_unit_digits,
            },
        )

    # What was inserted must hash to what was computed from the packet, and reconcile.
    try:
        await verify_snapshot(
            db, snapshot_id=snapshot_id, company_id=company_id,
            branch_id=period.branch_id, period_id=period.payroll_period_id)
    except SnapshotEvidenceError as exc:
        raise HTTPException(
            status_code=500,
            detail={
                "code": "SNAPSHOT_WRITE_VERIFICATION_FAILED",
                "message": f"The calculation snapshot did not verify as written: {exc}",
            },
        ) from exc

    await db.execute(
        text("""
            INSERT INTO audit.auditlog
                (companyid, branchid, actoruserid, actioncode,
                 entityschema, entityname, entityid, newvaluejson, reason, sourcetype)
            VALUES
                (:cid, :bid, :uid, 'CALCULATION_SNAPSHOT_CAPTURED',
                 'payroll', 'PayrollCalculationSnapshots', :snapshot_id, :new_value,
                 :reason, 'Application')
        """),
        {
            "cid": company_id,
            "bid": period.branch_id,
            "uid": user_id,
            "snapshot_id": str(snapshot_id),
            "new_value": json.dumps({
                "payroll_period_id": period.payroll_period_id,
                "snapshot_id": snapshot_id,
                "revision_number": revision_number,
                "source_config_hash": source_config_hash,
                "snapshot_hash": snapshot_hash,
                "report_evidence_version": CURRENT_REPORT_EVIDENCE_VERSION,
                "report_evidence_hash": report_evidence_hash,
                "context": context,
            }),
            "reason": "Immutable calculation snapshot captured for review submission",
        },
    )
    return snapshot_id
