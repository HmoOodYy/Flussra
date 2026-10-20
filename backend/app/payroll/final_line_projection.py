"""The FinalLines projection of a verified calculation snapshot.

FinalLines are a projection of the snapshot's official components, one line per component. A
FinalLine's FinalAmount is the component's official amount, so the lines of a
DriverAssignment sum exactly to its frozen ExpectedPay. Finalization writes this projection,
the finalization preview renders it, and the finalized readers check the persisted FinalLines
against it. Pure functions plus one read-only check; no transaction is opened.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.payroll.snapshot_verification import SnapshotEvidenceError

# The ledger display vocabulary of a FinalLine's LineType. ComponentKind, with the component
# and Min/Max evidence, is the classification authority: nothing derives money or meaning
# from these strings.
LEDGER_LINE_TYPES = {
    "ORDINARY": "ORDINARY",
    "STATUS": "STATUS_PAY",
    "BONUS": "BONUS",
    "MINIMUM_ADJUSTMENT": "SYS_MIN_TOPUP",
    "MAXIMUM_ADJUSTMENT": "SYS_MAX_CAP",
}
_KIND_ORDER = {kind: index for index, kind in enumerate(LEDGER_LINE_TYPES)}
_SOURCE_TYPE_BY_KIND = {
    "ORDINARY": "DraftLine", "STATUS": "StatusEntryState", "BONUS": "BonusEvent",
}



def _component_label(component: dict[str, Any]) -> str | None:
    kind = component["ComponentKind"]
    if kind == "ORDINARY":
        return component["DefinitionNameSnapshot"]
    if kind == "STATUS":
        return component["StatusRateColumnNameSnapshot"]
    return None


def _source_ids(total: dict[str, Any], component: dict[str, Any]) -> list[str]:
    """The source ids of the lines that contribute to one component."""
    return [
        line["SourceID"] for line in total["Lines"]
        if line["PayrollCalculationSnapshotComponentID"]
        == component["PayrollCalculationSnapshotComponentID"]
    ]


def project_final_lines(content: dict[str, Any]) -> list[dict[str, Any]]:
    """One FinalLine per official component of a verified snapshot, in a stable order.

    The FinalAmount of a line is the component's official amount, so the lines of a
    DriverAssignment sum exactly to its frozen ExpectedPay. Finalization writes these rows and
    the preview renders them: one projection serves both.
    """
    rows: list[dict[str, Any]] = []
    for total in content["DriverTotals"]:
        for component in sorted(total["Components"], key=lambda c: (
            _KIND_ORDER[c["ComponentKind"]],
            c["PayrollPeriodDefinitionID"] or 0, c["StatusRateColumnID"] or 0,
            c["DriverPayRuleID"] or 0,
        )):
            kind = component["ComponentKind"]
            component_id = int(component["PayrollCalculationSnapshotComponentID"])
            source_ids = _source_ids(total, component)
            rows.append({
                "component_id": component_id,
                "component_kind": kind,
                "line_type": LEDGER_LINE_TYPES[kind],
                "driver_assignment_id": total["DriverAssignmentID"],
                "driver_id": total["DriverID"],
                "employee_id": total["EmployeeID"],
                "driver_name": total["DriverNameSnapshot"],
                "driver_number": total["DriverNumberSnapshot"],
                "payroll_period_definition_id": component["PayrollPeriodDefinitionID"],
                "pay_definition_id": component["PayDefinitionID"],
                "label": _component_label(component),
                "pre_rounding_amount": component["PreRoundingAmount"],
                "amount": component["OfficialAmount"],
                "source_ids": source_ids,
                "source_snapshot": {
                    "payroll_calculation_snapshot_id": content["PayrollCalculationSnapshotID"],
                    "revision_number": content["RevisionNumber"],
                    "snapshot_hash": content["SnapshotHash"],
                    "currency_code": content["CurrencyCode"],
                    "currency_minor_unit_digits": content["CurrencyMinorUnitDigits"],
                    "snapshot_component_id": component_id,
                    "component_kind": kind,
                    "payroll_period_definition_id": component["PayrollPeriodDefinitionID"],
                    "pay_definition_id": component["PayDefinitionID"],
                    "status_rate_column_id": component["StatusRateColumnID"],
                    "driver_pay_rule_id": component["DriverPayRuleID"],
                    "pre_rounding_amount": str(component["PreRoundingAmount"]),
                    "source_type": _SOURCE_TYPE_BY_KIND.get(kind),
                    "source_ids": source_ids,
                },
            })
    return rows


async def verify_final_lines(
    db: AsyncConnection, content: dict[str, Any],
) -> list[dict[str, Any]]:
    """The persisted FinalLines of a snapshot, proved to be exactly its component projection.

    Every official component has exactly one FinalLine with the same DriverAssignment, kind
    and official amount, and nothing else is stored for the snapshot. Raises
    ``SnapshotEvidenceError`` (FINAL_LINES_INTEGRITY_ERROR) otherwise; returns the rows.
    """
    rows = (await db.execute(text("""
        SELECT finallineid, driverassignmentid, driverid, employeeid, componentkind, linetype,
               finalamount, payrollcalculationsnapshotcomponentid, payrollperioddefinitionid,
               paydefinitionid, currencycode, currencyminorunitdigits
        FROM payroll.payrollfinallines
        WHERE payrollcalculationsnapshotid = :snapshot_id
          AND companyid = :company_id AND branchid = :branch_id AND payrollperiodid = :period_id
        ORDER BY finallineid
    """), {
        "snapshot_id": content["PayrollCalculationSnapshotID"], "company_id": content["CompanyID"],
        "branch_id": content["BranchID"], "period_id": content["PayrollPeriodID"],
    })).mappings().all()
    expected = {row["component_id"]: row for row in project_final_lines(content)}
    persisted = {int(row["payrollcalculationsnapshotcomponentid"]): row for row in rows}
    if set(persisted) != set(expected) or len(persisted) != len(rows) or any(
        persisted[component_id]["finalamount"] != row["amount"]
        or persisted[component_id]["componentkind"] != row["component_kind"]
        or persisted[component_id]["linetype"] != row["line_type"]
        or int(persisted[component_id]["driverassignmentid"]) != row["driver_assignment_id"]
        or persisted[component_id]["currencycode"] != content["CurrencyCode"]
        or int(persisted[component_id]["currencyminorunitdigits"])
        != content["CurrencyMinorUnitDigits"]
        for component_id, row in expected.items() if component_id in persisted
    ):
        raise SnapshotEvidenceError(
            "FINAL_LINES_INTEGRITY_ERROR",
            "the finalized lines are not the projection of their calculation snapshot.")
    return [dict(row) for row in rows]
