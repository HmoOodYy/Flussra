"""Shared report column and projection components used by live and finalized readers."""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from typing import Any

from fastapi import HTTPException
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
        driver_id = int(line["driver_id"])
        definition_id = line.get("payroll_period_definition_id")
        if definition_id is None or int(definition_id) not in column_id_set:
            raise report_error(
                "REPORT_DEFINITION_INTEGRITY_ERROR",
                "a Daily financial line does not carry a PayrollPeriodDefinitionID within "
                "the period's active definition layout.",
            )
        definition_id = int(definition_id)
        amount = Decimal(str(line["calculated_amount"])) if line["calculated_amount"] is not None else Decimal("0")
        bucket = per_driver[driver_id]
        bucket[definition_id] = bucket.get(definition_id, Decimal("0")) + amount
        totals[definition_id] += amount
    return dict(per_driver), totals


def summarize_status_entries(statuses: list[dict[str, Any]]) -> dict[int, list[dict[str, Any]]]:
    grouped: dict[int, dict[tuple[str, str, bool], int]] = defaultdict(lambda: defaultdict(int))
    for status in statuses:
        grouped[status["driver_id"]][(status["code"], status["label"], status["is_off"])] += 1
    return {driver_id: [{"code": key[0], "label": key[1], "is_off": key[2], "count": count}
                        for key, count in values.items()] for driver_id, values in grouped.items()}
