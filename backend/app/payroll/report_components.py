"""Shared report column and projection components used by live and finalized readers."""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from typing import Any

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.payroll.period_definitions import list_period_definition_rows


def report_error(code: str, message: str) -> HTTPException:
    return HTTPException(status_code=422, detail=f"{code}: {message}")


def _definition_column(row) -> dict[str, Any]:
    return {
        "payroll_period_definition_id": int(row["payrollperioddefinitionid"]),
        "code": row["definitioncodesnapshot"],
        "label": row["definitionnamesnapshot"],
        "input_type": row["inputtypesnapshot"],
        "unit": row["unitsnapshot"],
        "sort_order": int(row["sortorder"]),
    }


async def load_report_columns(period: dict[str, Any], db: AsyncConnection) -> list[dict[str, Any]]:
    """The period's frozen definition layout (quantity columns). Zero rows is valid."""
    rows = await list_period_definition_rows(
        int(period["payrollperiodid"]), int(period["companyid"]), db)
    return [_definition_column(row) for row in rows]


async def load_financial_definition_columns(period: dict[str, Any], db: AsyncConnection) -> list[dict[str, Any]]:
    """Financial columns: the period's ACTIVE frozen definitions only."""
    rows = await list_period_definition_rows(
        int(period["payrollperiodid"]), int(period["companyid"]), db, active_only=True)
    return [_definition_column(row) for row in rows]


def aggregate_definition_amounts(
    lines: list[dict[str, Any]],
    column_ids: list[int],
) -> tuple[dict[int, dict[int, Decimal]], dict[int, Decimal]]:
    """Aggregate already-derived Daily source money by frozen definition column.

    Only sums already-derived line money. Classification uses `snapshot_source_type`
    (DraftLine/StatusEntryState/BonusEvent/System). A Daily financial line whose
    PayrollPeriodDefinitionID is not part of the period's active layout is a
    data-integrity failure, not something to silently drop.
    """
    column_id_set = set(column_ids)
    per_driver: dict[int, dict[int, Decimal]] = defaultdict(dict)
    totals: dict[int, Decimal] = {cid: Decimal("0") for cid in column_ids}
    for line in lines:
        if line["line_scope"] != "Daily" or line["snapshot_source_type"] != "DraftLine":
            continue
        driver_assignment_id = int(line["driver_assignment_id"])
        definition_id = line.get("payroll_period_definition_id")
        if definition_id is None or int(definition_id) not in column_id_set:
            raise report_error(
                "REPORT_DEFINITION_INTEGRITY_ERROR",
                "a Daily financial line does not carry a PayrollPeriodDefinitionID within "
                "the period's active definition layout.",
            )
        definition_id = int(definition_id)
        amount = Decimal(str(line["calculated_amount"])) if line["calculated_amount"] is not None else Decimal("0")
        bucket = per_driver[driver_assignment_id]
        bucket[definition_id] = bucket.get(definition_id, Decimal("0")) + amount
        totals[definition_id] += amount
    return dict(per_driver), totals


def summarize_status_entries(statuses: list[dict[str, Any]]) -> dict[int, list[dict[str, Any]]]:
    grouped: dict[int, dict[tuple, int]] = defaultdict(lambda: defaultdict(int))
    for status in statuses:
        grouped[status["driver_assignment_id"]][(
            status["code"], status["label"], status["is_off"],
            status["hours_value"], status["category"])] += 1
    return {driver_assignment_id: [{"code": key[0], "label": key[1], "is_off": key[2],
                                    "hours_value": key[3], "category": key[4], "count": count}
                        for key, count in values.items()] for driver_assignment_id, values in grouped.items()}


def _status_only_assignment_ids(content: dict[str, Any]) -> set[int]:
    """DriverAssignments that the frozen Status evidence names but no Driver total does."""
    with_totals = {int(total["DriverAssignmentID"]) for total in content["DriverTotals"]}
    return {
        int(entry["DriverAssignmentID"]) for entry in content["ReportEvidence"]["StatusEntries"]
    } - with_totals


async def frozen_status_only_identities(
    content: dict[str, Any], *, period_id: int, company_id: int, branch_id: int,
    db: AsyncConnection,
) -> dict[int, dict[str, Any]]:
    """Number and name of the Drivers that only frozen Status evidence represents.

    A Driver whose Statuses are all non-monetary has Status evidence but no Driver total, so the
    snapshot holds no number or name for it. They come from the period's frozen eligibility
    roster (DriverNumberSnapshot / DriverNameSnapshot), the same frozen source the finalized
    Off-drivers and library readers use for a Driver without totals; nothing is read from the
    live Driver or Employee.
    """
    ids = sorted(_status_only_assignment_ids(content))
    if not ids:
        return {}
    rows = (await db.execute(text("""
        SELECT driverassignmentid, drivernumbersnapshot, drivernamesnapshot
        FROM payroll.payrollperioddrivereligibility
        WHERE payrollperiodid = :period_id AND companyid = :company_id
          AND branchid = :branch_id AND driverassignmentid = ANY(:ids)
    """), {"period_id": period_id, "company_id": company_id, "branch_id": branch_id,
           "ids": ids})).mappings().all()
    return {
        int(row["driverassignmentid"]): {
            "driver_number": row["drivernumbersnapshot"],
            "driver_name": row["drivernamesnapshot"],
        } for row in rows
    }


def frozen_operational_rows(
    content: dict[str, Any],
    status_only_identities: dict[int, dict[str, Any]] | None = None,
) -> tuple[dict[int, dict[str, Any]], list[dict[str, Any]]]:
    """Drivers and per-day work of a frozen period: identity and source lines of the snapshot.

    The roster is every Driver the verified frozen evidence represents: the Drivers with a total,
    and the Drivers that only a frozen Status entry names (all their Statuses non-monetary). The
    latter have no total, component or work line; they appear with their frozen Status entries
    and their financial sections stay empty. ``status_only_identities`` supplies their frozen
    number and name (see ``frozen_status_only_identities``).
    """
    drivers: dict[int, dict[str, Any]] = {}
    work: list[dict[str, Any]] = []
    for total in content["DriverTotals"]:
        assignment_id = int(total["DriverAssignmentID"])
        drivers[assignment_id] = {
            "driver_assignment_id": assignment_id, "driver_id": int(total["DriverID"]),
            "driver_number": total["DriverNumberSnapshot"],
            "driver_name": total["DriverNameSnapshot"],
        }
        names = {
            c["PayrollPeriodDefinitionID"]: c["DefinitionNameSnapshot"]
            for c in total["Components"] if c["ComponentKind"] == "ORDINARY"
        }
        for line in total["Lines"]:
            if line["SourceType"] == "DraftLine":
                work.append({
                    "driver_assignment_id": assignment_id, "work_date": line["WorkDate"],
                    "payroll_period_definition_id": int(line["PayrollPeriodDefinitionID"]),
                    "definition_name": names.get(line["PayrollPeriodDefinitionID"]),
                    "quantity": line["Quantity"], "line_scope": "Daily",
                })
    identities = status_only_identities or {}
    for entry in content["ReportEvidence"]["StatusEntries"]:
        assignment_id = int(entry["DriverAssignmentID"])
        if assignment_id not in drivers:
            identity = identities.get(assignment_id, {})
            drivers[assignment_id] = {
                "driver_assignment_id": assignment_id, "driver_id": int(entry["DriverID"]),
                "driver_number": identity.get("driver_number"),
                "driver_name": identity.get("driver_name"),
            }
    return drivers, work


def frozen_financial_packet(content: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[int, dict[str, Any]]]:
    """Financial lines and driver totals exactly as frozen: official amounts, never rebuilt."""
    lines: list[dict[str, Any]] = []
    totals: dict[int, dict[str, Any]] = {}
    for total in content["DriverTotals"]:
        assignment_id = int(total["DriverAssignmentID"])
        names = {
            c["PayrollCalculationSnapshotComponentID"]: c["DefinitionNameSnapshot"]
            for c in total["Components"]
        }
        for line in total["Lines"]:
            lines.append({
                "driver_assignment_id": assignment_id,
                "source_type": line["SourceType"], "source_id": line["SourceID"],
                "line_type": None, "line_scope": line["LineScope"],
                "work_date": line["WorkDate"],
                "payroll_period_definition_id": line["PayrollPeriodDefinitionID"],
                "definition_name": names.get(line["PayrollCalculationSnapshotComponentID"]),
                "calculation_status": line["CalculationStatus"],
                "quantity": line["Quantity"], "resolved_rate_amount": line["ResolvedRateAmount"],
                "calculated_amount": line["CalculatedAmount"],
                "bonus_event_id": line["BonusEventID"], "snapshot_source_type": line["SourceType"],
            })
        totals[assignment_id] = {
            "daily_pay": total["DailyPay"], "status_pay": total["StatusPay"],
            "minimum_adjustment": total["MinimumAdjustment"],
            "maximum_adjustment": total["MaximumAdjustment"],
            "bonus_total": total["BonusTotal"], "total_pay": total["ExpectedPay"],
            "driver_id": int(total["DriverID"]), "driver_number": total["DriverNumberSnapshot"],
            "driver_name": total["DriverNameSnapshot"],
            "official_definition_amounts": {
                int(c["PayrollPeriodDefinitionID"]): c["OfficialAmount"]
                for c in total["Components"] if c["ComponentKind"] == "ORDINARY"},
        }
    return lines, totals
