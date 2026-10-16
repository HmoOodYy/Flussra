"""Persist immutable calculation and report evidence captured by lifecycle.

The caller owns the transaction and locks; this module consumes the live packet
without recalculating its financial results.
Lifecycle callers retain their evidence gate before reaching this writer.
"""
from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.company_currency import CompanyCurrency
from app.payroll import bonus as payroll_bonus
from app.payroll.immutable_evidence import capture_snapshot_used_rate_definitions
from app.payroll.period_calculation import _LiveCalculationPacket
from app.payroll.schemas import PeriodSummary
from app.payroll.snapshot_hash import (
    CURRENT_PAYROLL_CALCULATION_VERSION,
    CURRENT_REPORT_EVIDENCE_VERSION,
    calculate_report_evidence_hash,
    calculate_snapshot_hash,
    calculate_source_config_hash,
    canonical_json,
)


async def _load_report_evidence(
    *,
    period: PeriodSummary,
    company_id: int,
    db: AsyncConnection,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Read CP-5C report evidence from the same CP-4D transaction view."""
    status_result = await db.execute(
        text("""
            SELECT
                ppdes.payrollperioddriverdayentrystateid,
                ppdes.driverid,
                ppdes.workdate,
                ppdes.statuskeyid,
                sk.statuscode,
                sk.keyname,
                sk.isoffreason
            FROM payroll.payrollperioddriverdayentrystate ppdes
            JOIN payroll.payrollstatuskeys sk ON sk.statuskeyid = ppdes.statuskeyid
            WHERE ppdes.payrollperiodid = :pid
              AND ppdes.companyid = :cid
              AND ppdes.branchid = :bid
              AND ppdes.isvoided = FALSE
              AND ppdes.statuskeyid IS NOT NULL
              AND ppdes.workdate BETWEEN :period_start AND :period_end
            ORDER BY ppdes.driverid, ppdes.workdate,
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


def _packet_driver_totals_for_hash(
    packet: _LiveCalculationPacket,
) -> list[dict[str, Any]]:
    """Project the shared live packet into CP-4C's hash contract."""
    return [
        {
            "DriverID": driver.driver_id,
            "DriverCodeSnapshot": driver.driver_code,
            "DriverNameSnapshot": driver.driver_name,
            "DailyPay": driver.daily_pay,
            "StatusPay": driver.status_pay,
            "MinimumAdjustment": driver.minimum_adjustment,
            "MaximumAdjustment": driver.maximum_adjustment,
            "BonusTotal": driver.bonus_total,
            "ExpectedPay": driver.expected_pay,
            "Lines": [
                {
                    "SourceType": line.snapshot_source_type or line.source_type,
                    "SourceID": line.snapshot_source_id if line.snapshot_source_id is not None else line.source_id,
                    "LineType": line.line_type,
                    "LineScope": line.line_scope,
                    "WorkDate": line.work_date,
                    "PayItemID": line.pay_item_id,
                    "RateTypeID": line.rate_type_id,
                    "DriverRateID": line.driver_rate_id,
                    "BonusEventID": line.bonus_event_id,
                    "Quantity": line.quantity,
                    "ResolvedRateAmount": line.resolved_rate_amount,
                    "CalculatedAmount": (
                        line.snapshot_calculated_amount
                        if line.snapshot_calculated_amount is not None
                        else line.calculated_amount
                    ),
                    "SourceEvidenceJSONB": line.source_evidence or {},
                }
                for line in driver.lines
            ],
        }
        for driver in packet.drivers
    ]


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
    """Persist one complete immutable CP-4D submission packet.

    The caller already owns the period/workflow locks.  This writer performs no
    calculation and never uses generated IDs in either hash.
    """
    if packet.blockers:
        raise HTTPException(
            status_code=422,
            detail="Cannot submit an incomplete calculation packet: " + "; ".join(packet.blockers),
        )
    if any(
        line.snapshot_calculated_amount is None and line.calculated_amount is None
        for driver in packet.drivers
        for line in driver.lines
    ):
        raise HTTPException(
            status_code=422,
            detail="Cannot submit: an authoritative calculation line is unresolved.",
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
        (
            line.bonus_event_id,
            line.driver_id,
            line.snapshot_calculated_amount
            if line.snapshot_calculated_amount is not None
            else line.calculated_amount,
        )
        for driver in packet.drivers
        for line in driver.lines
        if line.source_type == "BonusEvent" and line.bonus_event_id is not None
    )
    evidence_bonus_lines = sorted(
        (event["PayrollBonusEventID"], event["DriverID"], event["Amount"])
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
            SELECT driverid, iseligibleforperiod, eligibilityreasoncode,
                   hiredatesnapshot, terminationdatesnapshot,
                   drivereffectivefromsnapshot, drivereffectivetosnapshot,
                   drivercodesnapshot, drivernamesnapshot
            FROM payroll.payrollperioddrivereligibility
            WHERE payrollperiodid = :pid AND companyid = :cid AND branchid = :bid
            ORDER BY driverid
        """),
        {"pid": period.payroll_period_id, "cid": company_id, "bid": period.branch_id},
    )).mappings().all()

    source_config_payload = {
        "PacketContract": "cp4d-source-config-v2",
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
        "Eligibility": [dict(row) for row in eligibility_rows],
        "Sources": [
            {
                "DriverID": driver.driver_id,
                "Lines": [
                    {
                        "SourceType": line.snapshot_source_type or line.source_type,
                        "SourceID": line.snapshot_source_id if line.snapshot_source_id is not None else line.source_id,
                        "LineType": line.line_type,
                        "LineScope": line.line_scope,
                        "WorkDate": line.work_date,
                        "Quantity": line.quantity,
                        "ResolvedRateAmount": line.resolved_rate_amount,
                        "SourceEvidenceJSONB": line.source_evidence or {},
                    }
                    for line in driver.lines
                ],
            }
            for driver in packet.drivers
        ],
    }
    source_config_hash = calculate_source_config_hash(source_config_payload)
    revision_result = await db.execute(
        text("""
            SELECT COALESCE(MAX(revisionnumber), 0) + 1
            FROM payroll.payrollcalculationsnapshots
            WHERE payrollperiodid = :pid
        """),
        {"pid": period.payroll_period_id},
    )
    revision_number = int(revision_result.scalar_one())
    hash_totals = _packet_driver_totals_for_hash(packet)
    snapshot_hash = calculate_snapshot_hash(
        company_id=company_id,
        branch_id=period.branch_id,
        payroll_period_id=period.payroll_period_id,
        revision_number=revision_number,
        calculation_version=CURRENT_PAYROLL_CALCULATION_VERSION,
        source_config_hash=source_config_hash,
        driver_totals=hash_totals,
    )

    header_result = await db.execute(
        text("""
            INSERT INTO payroll.payrollcalculationsnapshots
                (companyid, branchid, payrollperiodid, revisionnumber,
                 calculationversion, sourceconfighash, snapshothash,
                 reportevidenceversion, reportevidencehash,
                 currencycode, currencyminorunitdigits,
                 createdbyuserid, totalexpectedpay)
            VALUES
                (:cid, :bid, :pid, :revision, :version, :source_hash,
                 :snapshot_hash, :report_evidence_version, :report_evidence_hash,
                 :currency_code, :currency_minor,
                 :uid, :total)
            RETURNING payrollcalculationsnapshotid
        """),
        {
            "cid": company_id,
            "bid": period.branch_id,
            "pid": period.payroll_period_id,
            "revision": revision_number,
            "version": CURRENT_PAYROLL_CALCULATION_VERSION,
            "source_hash": source_config_hash,
            "snapshot_hash": snapshot_hash,
            "report_evidence_version": CURRENT_REPORT_EVIDENCE_VERSION,
            "report_evidence_hash": report_evidence_hash,
            "currency_code": currency.code,
            "currency_minor": currency.minor_unit_digits,
            "uid": user_id,
            "total": packet.total_expected_pay,
        },
    )
    snapshot_id = int(header_result.scalar_one())
    snapshot_lines = [
        {**line, "DriverID": hash_total["DriverID"]}
        for hash_total in hash_totals
        for line in hash_total["Lines"]
    ]
    used_rate_definition_ids = await capture_snapshot_used_rate_definitions(
        snapshot_id=snapshot_id,
        company_id=company_id,
        branch_id=period.branch_id,
        period_id=period.payroll_period_id,
        snapshot_line_rows=snapshot_lines,
        currency=currency,
        db=db,
    )
    snapshot_line_ordinal = 0

    for driver, hash_total in zip(packet.drivers, hash_totals, strict=True):
        driver_result = await db.execute(
            text("""
                INSERT INTO payroll.payrollcalculationdrivertotals
                    (payrollcalculationsnapshotid, companyid, branchid, driverid,
                     drivercodesnapshot, drivernamesnapshot, dailypay, statuspay,
                     minimumadjustment, maximumadjustment, bonustotal,
                     expectedpay)
                VALUES
                    (:snapshot_id, :cid, :bid, :driver_id, :driver_code, :driver_name,
                     :daily, :status, :minimum, :maximum, :bonus, :expected)
                RETURNING payrollcalculationdrivertotalid
            """),
            {
                "snapshot_id": snapshot_id,
                "cid": company_id,
                "bid": period.branch_id,
                "driver_id": driver.driver_id,
                "driver_code": driver.driver_code,
                "driver_name": driver.driver_name,
                "daily": driver.daily_pay,
                "status": driver.status_pay,
                "minimum": driver.minimum_adjustment,
                "maximum": driver.maximum_adjustment,
                "bonus": driver.bonus_total,
                "expected": driver.expected_pay,
            },
        )
        driver_total_id = int(driver_result.scalar_one())
        for line in hash_total["Lines"]:
            await db.execute(
                text("""
                    INSERT INTO payroll.payrollcalculationsnapshotlines
                        (payrollcalculationdrivertotalid, sourcetype, sourceid,
                         linetype, linescope, workdate, payitemid, ratetypeid,
                         driverrateid, bonuseventid, quantity, resolvedrateamount,
                         calculatedamount, sourceevidencejsonb, usedratedefinitionid)
                    VALUES
                        (:driver_total_id, :source_type, :source_id, :line_type,
                         :line_scope, :work_date, :pay_item_id, :rate_type_id,
                         :driver_rate_id, :bonus_event_id, :quantity, :resolved_rate,
                         :calculated_amount, CAST(:evidence AS jsonb), :used_rate_definition_id)
                """),
                {
                    "driver_total_id": driver_total_id,
                    "source_type": line["SourceType"],
                    "source_id": line["SourceID"],
                    "line_type": line["LineType"],
                    "line_scope": line["LineScope"],
                    "work_date": line["WorkDate"],
                    "pay_item_id": line["PayItemID"],
                    "rate_type_id": line["RateTypeID"],
                    "driver_rate_id": line["DriverRateID"],
                    "bonus_event_id": line["BonusEventID"],
                    "quantity": line["Quantity"],
                    "resolved_rate": line["ResolvedRateAmount"],
                    "calculated_amount": line["CalculatedAmount"],
                    "evidence": canonical_json(line["SourceEvidenceJSONB"]),
                    "used_rate_definition_id": used_rate_definition_ids.get(snapshot_line_ordinal),
                },
            )
            snapshot_line_ordinal += 1

    for entry in status_entries:
        await db.execute(
            text("""
                INSERT INTO payroll.payrollcalculationsnapshotstatusentries
                    (payrollcalculationsnapshotid, companyid, branchid,
                     payrollperiodid, driverid, workdate,
                     payrollperioddriverdayentrystateid, statuskeyid,
                     statuscodesnapshot, statuslabelsnapshot,
                     statusisoffreasonsnapshot)
                VALUES
                    (:snapshot_id, :cid, :bid, :pid, :driver_id, :work_date,
                     :entry_state_id, :status_key_id, :status_code,
                     :status_label, :status_is_off_reason)
            """),
            {
                "snapshot_id": snapshot_id,
                "cid": company_id,
                "bid": period.branch_id,
                "pid": period.payroll_period_id,
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
                     payrollperiodid, payrollbonuseventid, driverid, amount,
                     reason, notes, datarevision, createdbyuserid,
                     creatordisplaynamesnapshot, createdatutc,
                     currencycodesnapshot, currencyminorunitdigitssnapshot)
                VALUES
                    (:snapshot_id, :cid, :bid, :pid, :bonus_event_id, :driver_id,
                     :amount, :reason, :notes, :data_revision, :created_by_user_id,
                     :creator_display_name, :created_at,
                     :currency_code, :currency_minor)
            """),
            {
                "snapshot_id": snapshot_id,
                "cid": company_id,
                "bid": period.branch_id,
                "pid": period.payroll_period_id,
                "bonus_event_id": event["PayrollBonusEventID"],
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
