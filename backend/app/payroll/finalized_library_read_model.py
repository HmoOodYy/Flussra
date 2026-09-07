"""P6A immutable finalized-payroll overview and calculation-report read model."""
from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.service import _check_branch_access, _check_permission, _require_not_driver_role
from app.payroll import report_read_model

_REPORT_TYPES = {"drivers", "period-work", "period-pay", "mixed"}
_FINALIZED_STATUSES = {"Locked", "Archived"}


def _unavailable(code: str, message: str) -> HTTPException:
    return HTTPException(status_code=422, detail=f"{code}: {message}")


def _availability(state: str, reason_code: str | None = None) -> dict[str, str | None]:
    return {"state": state, "reason_code": reason_code}


async def _period_context(
    *, period_id: int, company_id: int, user_id: int, db: AsyncConnection,
) -> dict[str, Any]:
    """Authorize a finalized-library request before loading child data."""
    await _require_not_driver_role(company_id, user_id, db)
    row = (await db.execute(text("""
        SELECT p.payrollperiodid, p.companyid, p.branchid, p.status, p.periodcode,
               p.periodname, p.periodtype, p.startdate, p.enddate, p.lockedatutc,
               p.lockedbyuserid, b.branchname
        FROM payroll.payrollperiods p
        JOIN core.branches b ON b.branchid = p.branchid
        WHERE p.payrollperiodid = :period_id AND p.companyid = :company_id
    """), {"period_id": period_id, "company_id": company_id})).mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail="Payroll period not found.")
    can_see_all, branch_ids = await _check_branch_access(company_id, user_id, db)
    if not can_see_all and int(row["branchid"]) not in branch_ids:
        raise HTTPException(status_code=403, detail="Access denied to this period's branch.")
    await _check_permission(company_id, user_id, int(row["branchid"]), "ledger.view", db)
    if str(row["status"]) not in _FINALIZED_STATUSES:
        raise _unavailable(
            "FINALIZED_LIBRARY_UNAVAILABLE",
            "Finalized payroll information is available only for Locked or Archived periods.",
        )
    return dict(row)


async def _financial_summary(period: dict[str, Any], db: AsyncConnection) -> dict[str, Any]:
    row = (await db.execute(text("""
        SELECT COUNT(*) AS final_line_count,
               COUNT(DISTINCT driverid) AS driver_count,
               COALESCE(SUM(finalamount), 0) AS total_pay
        FROM payroll.payrollfinallines
        WHERE payrollperiodid = :period_id AND companyid = :company_id AND branchid = :branch_id
    """), {
        "period_id": period["payrollperiodid"], "company_id": period["companyid"],
        "branch_id": period["branchid"],
    })).mappings().one()
    return {
        "total_pay": Decimal(str(row["total_pay"])),
        "final_line_count": int(row["final_line_count"]),
        "driver_count": int(row["driver_count"]),
    }


async def _originating_snapshot(
    period: dict[str, Any], db: AsyncConnection,
) -> tuple[dict[str, Any] | None, dict[str, str | None]]:
    """Resolve FinalLines provenance exactly; never select a latest snapshot."""
    rows = (await db.execute(text("""
        SELECT DISTINCT
               sourcesnapshot ->> 'payroll_calculation_snapshot_id' AS snapshot_id,
               sourcesnapshot ->> 'revision_number' AS revision_number,
               sourcesnapshot ->> 'snapshot_hash' AS snapshot_hash
        FROM payroll.payrollfinallines
        WHERE payrollperiodid = :period_id AND companyid = :company_id AND branchid = :branch_id
    """), {
        "period_id": period["payrollperiodid"], "company_id": period["companyid"],
        "branch_id": period["branchid"],
    })).mappings().all()
    if not rows:
        return None, _availability("UNAVAILABLE", "FINAL_LINES_EMPTY")
    if len(rows) != 1 or any(value is None for value in rows[0].values()):
        return None, _availability("UNAVAILABLE", "PROVENANCE_UNAVAILABLE")
    source = rows[0]
    try:
        snapshot_id = int(source["snapshot_id"])
        revision_number = int(source["revision_number"])
    except (TypeError, ValueError):
        return None, _availability("UNAVAILABLE", "PROVENANCE_UNAVAILABLE")
    snapshot = (await db.execute(text("""
        SELECT payrollcalculationsnapshotid, revisionnumber, snapshothash, sourceconfighash,
               reportevidenceversion, reportevidencehash
        FROM payroll.payrollcalculationsnapshots
        WHERE payrollcalculationsnapshotid = :snapshot_id
          AND companyid = :company_id AND branchid = :branch_id AND payrollperiodid = :period_id
    """), {
        "snapshot_id": snapshot_id, "period_id": period["payrollperiodid"],
        "company_id": period["companyid"], "branch_id": period["branchid"],
    })).mappings().first()
    if snapshot is None or (
        int(snapshot["revisionnumber"]) != revision_number
        or str(snapshot["snapshothash"]) != str(source["snapshot_hash"])
    ):
        return None, _availability("UNAVAILABLE", "PROVENANCE_UNAVAILABLE")
    return dict(snapshot), _availability("AVAILABLE")


async def _columns(period: dict[str, Any], db: AsyncConnection) -> tuple[list[dict[str, Any]], dict[str, str | None]]:
    """Read frozen period metadata only; finalized reads never fall back to current PayItems."""
    rows = (await db.execute(text("""
        SELECT payitemid, payitemcode, payitemname, displaylabel, category,
               datatype, unit, itemscope, sortorder
        FROM payroll.payrollperiodpayitems
        WHERE payrollperiodid = :period_id AND companyid = :company_id
          AND branchid = :branch_id AND appearsinreports = TRUE
        ORDER BY sortorder, payitemcode, payitemid
    """), {
        "period_id": period["payrollperiodid"], "company_id": period["companyid"],
        "branch_id": period["branchid"],
    })).mappings().all()
    if not rows:
        return [], _availability("UNAVAILABLE", "PERIOD_PAY_ITEM_SNAPSHOT_UNAVAILABLE")
    return [{
        "pay_item_id": int(row["payitemid"]), "code": row["payitemcode"],
        "label": row["displaylabel"] or row["payitemname"], "category": row["category"],
        "data_type": row["datatype"], "unit": row["unit"], "scope": row["itemscope"],
        "sort_order": row["sortorder"],
    } for row in rows], _availability("AVAILABLE")


async def _final_lines(period: dict[str, Any], db: AsyncConnection) -> tuple[list[dict[str, Any]], dict[int, dict[str, Any]]]:
    rows = (await db.execute(text("""
        SELECT driverid, linetype, linescope, workdate, payitemid, quantity,
               resolvedrateamount, finalamount, sourcetype, sourceid, bonuseventid
        FROM payroll.payrollfinallines
        WHERE payrollperiodid = :period_id AND companyid = :company_id AND branchid = :branch_id
        ORDER BY driverid, finallineid
    """), {
        "period_id": period["payrollperiodid"], "company_id": period["companyid"],
        "branch_id": period["branchid"],
    })).mappings().all()
    totals: dict[int, dict[str, Any]] = defaultdict(lambda: {
        "daily_pay": Decimal("0"), "status_pay": Decimal("0"), "period_pay": Decimal("0"),
        "minimum_adjustment": Decimal("0"), "maximum_adjustment": Decimal("0"),
        "bonus_total": Decimal("0"), "total_pay": Decimal("0"),
    })
    lines: list[dict[str, Any]] = []
    for row in rows:
        driver_id = int(row["driverid"])
        amount = Decimal(str(row["finalamount"]))
        total = totals[driver_id]
        total["total_pay"] += amount
        if row["linetype"] == "SYS_MIN_TOPUP":
            total["minimum_adjustment"] += amount
        elif row["linetype"] == "SYS_MAX_CAP":
            total["maximum_adjustment"] += amount
        elif row["sourcetype"] == "BonusEvent":
            total["bonus_total"] += amount
        elif row["sourcetype"] in {"StatusEntryState", "Status"}:
            total["status_pay"] += amount
        elif row["linescope"] == "Daily":
            total["daily_pay"] += amount
        else:
            total["period_pay"] += amount
        lines.append({
            "driver_id": driver_id, "source_type": row["sourcetype"],
            "source_id": row["sourceid"], "line_type": row["linetype"],
            "line_scope": row["linescope"], "work_date": row["workdate"],
            "pay_item_id": row["payitemid"], "quantity": row["quantity"],
            "resolved_rate_amount": row["resolvedrateamount"], "calculated_amount": amount,
            "bonus_event_id": row["bonuseventid"],
        })
    return lines, dict(totals)


async def _snapshot_work_and_drivers(
    snapshot_id: int, period: dict[str, Any], db: AsyncConnection,
) -> tuple[dict[int, dict[str, Any]], list[dict[str, Any]]]:
    rows = (await db.execute(text("""
        SELECT dt.driverid, dt.drivercodesnapshot, dt.drivernamesnapshot,
               sl.sourcetype, sl.linetype, sl.linescope, sl.workdate, sl.payitemid, sl.quantity
        FROM payroll.payrollcalculationdrivertotals dt
        LEFT JOIN payroll.payrollcalculationsnapshotlines sl
          ON sl.payrollcalculationdrivertotalid = dt.payrollcalculationdrivertotalid
        WHERE dt.payrollcalculationsnapshotid = :snapshot_id
          AND dt.companyid = :company_id AND dt.branchid = :branch_id
        ORDER BY dt.driverid, sl.payrollcalculationsnapshotlineid
    """), {
        "snapshot_id": snapshot_id, "company_id": period["companyid"],
        "branch_id": period["branchid"],
    })).mappings().all()
    drivers: dict[int, dict[str, Any]] = {}
    work_rows: list[dict[str, Any]] = []
    for row in rows:
        driver_id = int(row["driverid"])
        drivers.setdefault(driver_id, {
            "driver_id": driver_id, "driver_code": row["drivercodesnapshot"],
            "driver_name": row["drivernamesnapshot"],
        })
        if row["sourcetype"] == "DraftLine" and row["linescope"] == "Daily":
            work_rows.append({
                "driver_id": driver_id, "work_date": row["workdate"],
                "line_type": row["linetype"], "pay_item_id": row["payitemid"],
                "quantity": row["quantity"], "line_scope": row["linescope"],
            })
    return drivers, work_rows


def _work_totals(work_rows: list[dict[str, Any]]) -> dict[str, Decimal]:
    totals: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    for row in work_rows:
        if row["quantity"] is not None:
            totals[str(row["line_type"])] += Decimal(str(row["quantity"]))
    return dict(totals)


def _pay_totals(totals: dict[int, dict[str, Any]]) -> dict[str, Decimal]:
    names = (
        "daily_pay", "status_pay", "period_pay", "minimum_adjustment",
        "maximum_adjustment", "bonus_total", "total_pay",
    )
    return {
        name: sum((Decimal(str(total[name])) for total in totals.values()), Decimal("0"))
        for name in names
    }


async def build_overview(
    *, period_id: int, company_id: int, user_id: int, db: AsyncConnection,
) -> dict[str, Any]:
    period = await _period_context(period_id=period_id, company_id=company_id, user_id=user_id, db=db)
    financial_summary = await _financial_summary(period, db)
    snapshot, provenance = await _originating_snapshot(period, db)
    if snapshot is None:
        evidence = _availability("UNAVAILABLE", provenance["reason_code"])
    elif snapshot["reportevidenceversion"] is None:
        evidence = _availability("UNAVAILABLE", "LEGACY_NOT_CAPTURED")
    else:
        evidence = _availability("AVAILABLE")
    financial_state = "AVAILABLE" if financial_summary["final_line_count"] else "EMPTY"
    return {
        "period_id": int(period["payrollperiodid"]), "period_code": period["periodcode"],
        "period_name": period["periodname"], "period_status": period["status"],
        "company_id": int(period["companyid"]), "branch_id": int(period["branchid"]),
        "branch_name": period["branchname"], "finalized_at_utc": period["lockedatutc"],
        "finalized_by_user_id": period["lockedbyuserid"], "financial_summary": financial_summary,
        "snapshot_provenance": {
            "snapshot_id": None if snapshot is None else int(snapshot["payrollcalculationsnapshotid"]),
            "revision_number": None if snapshot is None else int(snapshot["revisionnumber"]),
            "snapshot_hash": None if snapshot is None else str(snapshot["snapshothash"]),
            "source_config_hash": None if snapshot is None else str(snapshot["sourceconfighash"]),
        },
        "section_availability": {
            "financials": _availability(financial_state), "snapshot_provenance": provenance,
            "report_evidence": evidence, "reports": _availability(financial_state),
            "off_status": _availability("UNAVAILABLE", "P6B_NOT_IMPLEMENTED"),
            "rates_used": _availability("UNAVAILABLE", "P6C_NOT_IMPLEMENTED"),
            "audit": _availability("UNAVAILABLE", "P6D_NOT_IMPLEMENTED"),
        },
        "generated_at_utc": datetime.now(UTC),
    }


async def build_finalized_report(
    *, report_type: str, period_id: int, company_id: int, user_id: int, db: AsyncConnection,
) -> dict[str, Any]:
    if report_type not in _REPORT_TYPES:
        raise _unavailable("FINALIZED_REPORT_VIEW_UNAVAILABLE", "Unknown finalized report view.")
    period = await _period_context(period_id=period_id, company_id=company_id, user_id=user_id, db=db)
    columns, columns_availability = await _columns(period, db)
    final_lines, financial_totals = await _final_lines(period, db)
    snapshot, provenance = await _originating_snapshot(period, db)
    frozen_drivers: dict[int, dict[str, Any]] = {}
    work_rows: list[dict[str, Any]] = []
    statuses: list[dict[str, Any]] = []
    bonuses: list[dict[str, Any]] = []
    evidence_available = False
    evidence_version: int | None = None
    evidence_hash: str | None = None
    evidence_availability = _availability("UNAVAILABLE", provenance["reason_code"])
    if snapshot is not None:
        snapshot_id = int(snapshot["payrollcalculationsnapshotid"])
        frozen_drivers, work_rows = await _snapshot_work_and_drivers(snapshot_id, period, db)
        evidence_available, evidence_version, evidence_hash, statuses, bonuses = await report_read_model._snapshot_evidence(
            snapshot_id, period, db,
        )
        evidence_availability = (
            _availability("AVAILABLE") if evidence_available
            else _availability("UNAVAILABLE", "LEGACY_NOT_CAPTURED")
        )
    drivers = dict(frozen_drivers)
    for driver_id in financial_totals:
        drivers.setdefault(driver_id, {"driver_id": driver_id, "driver_code": None, "driver_name": None})
    by_driver_work: dict[int, list[dict[str, Any]]] = defaultdict(list)
    by_driver_lines: dict[int, list[dict[str, Any]]] = defaultdict(list)
    by_driver_status: dict[int, list[dict[str, Any]]] = defaultdict(list)
    by_driver_bonus: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in work_rows:
        by_driver_work[row["driver_id"]].append(row)
    for line in final_lines:
        by_driver_lines[line["driver_id"]].append(line)
    for item in statuses:
        by_driver_status[item["driver_id"]].append(item)
    for item in bonuses:
        by_driver_bonus[item["driver_id"]].append(item)
    summaries = report_read_model._status_summaries(statuses)
    result_drivers = []
    for driver_id in sorted(drivers):
        total = financial_totals.get(driver_id)
        pay = None if total is None else {**total, "financial_lines": by_driver_lines[driver_id]}
        result_drivers.append({
            **drivers[driver_id],
            "work": {
                "daily_rows": by_driver_work[driver_id],
                "status_entries": by_driver_status[driver_id],
                "status_summaries": summaries.get(driver_id, []),
            },
            "pay": pay,
            "bonus_events": by_driver_bonus[driver_id],
        })
    financial_state = "AVAILABLE" if final_lines else "EMPTY"
    return {
        "metadata": {
            "period_id": int(period["payrollperiodid"]), "period_code": period["periodcode"],
            "period_name": period["periodname"], "period_status": period["status"],
            "branch_id": int(period["branchid"]), "report_type": report_type,
            "financials_available": True,
            "snapshot_id": None if snapshot is None else int(snapshot["payrollcalculationsnapshotid"]),
            "revision_number": None if snapshot is None else int(snapshot["revisionnumber"]),
            "snapshot_hash": None if snapshot is None else str(snapshot["snapshothash"]),
            "report_evidence_available": evidence_available,
            "report_evidence_version": evidence_version, "report_evidence_hash": evidence_hash,
            "section_availability": {
                "financials": _availability(financial_state),
                "snapshot_provenance": provenance, "columns": columns_availability,
                "report_evidence": evidence_availability,
            },
            "generated_at_utc": datetime.now(UTC),
        },
        "columns": columns, "drivers": result_drivers,
        "work_totals": _work_totals(work_rows), "pay_totals": _pay_totals(financial_totals),
    }
