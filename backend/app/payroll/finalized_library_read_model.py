"""P6A immutable finalized-payroll overview and calculation-report read model."""
from __future__ import annotations

from collections import defaultdict
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.service import _check_branch_access, _check_permission, _require_not_driver_role
from app.payroll import report_read_model, service

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


async def _finalized_off_status_availability(
    period: dict[str, Any], snapshot: dict[str, Any] | None,
    provenance: dict[str, str | None], db: AsyncConnection,
) -> dict[str, str | None]:
    """Expose P6B capability without reading the finalized Off/Status payload."""
    if snapshot is None:
        return _availability("UNAVAILABLE", provenance["reason_code"])
    if snapshot["reportevidenceversion"] is None:
        return _availability("UNAVAILABLE", "LEGACY_NOT_CAPTURED")
    for table, reason_code in (
        ("payroll.payrollperioddays", "PERIOD_CALENDAR_UNAVAILABLE"),
        ("payroll.payrollperiodeligibilitysnapshots", "ELIGIBILITY_SNAPSHOT_UNAVAILABLE"),
        ("payroll.payrollperiodpayitems", "PERIOD_PAY_ITEM_SNAPSHOT_UNAVAILABLE"),
    ):
        exists = (await db.execute(text(f"""
            SELECT 1
            FROM {table}
            WHERE payrollperiodid = :period_id AND companyid = :company_id AND branchid = :branch_id
            LIMIT 1
        """), {
            "period_id": period["payrollperiodid"], "company_id": period["companyid"],
            "branch_id": period["branchid"],
        })).first()
        if exists is None:
            return _availability("UNAVAILABLE", reason_code)
    return _availability("AVAILABLE")


async def _rate_evidence_capture_marker(
    snapshot: dict[str, Any], period: dict[str, Any], db: AsyncConnection,
) -> bool:
    """A workflow action proves the 0064 capture transaction ran for a zero-rate snapshot."""
    marker = (await db.execute(text("""
        SELECT 1
        FROM payroll.payrollperiodworkflowactionevidence
        WHERE payrollcalculationsnapshotid = :snapshot_id
          AND companyid = :company_id AND branchid = :branch_id
          AND payrollperiodid = :period_id
          AND actioncode IN ('SUBMITTED', 'RESUBMITTED')
        LIMIT 1
    """), {
        "snapshot_id": snapshot["payrollcalculationsnapshotid"],
        "period_id": period["payrollperiodid"], "company_id": period["companyid"],
        "branch_id": period["branchid"],
    })).first()
    return marker is not None


async def _finalized_rate_evidence_availability(
    period: dict[str, Any], snapshot: dict[str, Any] | None,
    provenance: dict[str, str | None], db: AsyncConnection,
) -> dict[str, str | None]:
    """Keep zero new evidence distinct from a pre-0064 snapshot with no capture proof."""
    if snapshot is None:
        return _availability("UNAVAILABLE", provenance["reason_code"])
    exists = (await db.execute(text("""
        SELECT 1
        FROM payroll.payrollcalculationsnapshotusedratedefinitions
        WHERE payrollcalculationsnapshotid = :snapshot_id
          AND companyid = :company_id AND branchid = :branch_id
          AND payrollperiodid = :period_id
        LIMIT 1
    """), {
        "snapshot_id": snapshot["payrollcalculationsnapshotid"],
        "period_id": period["payrollperiodid"], "company_id": period["companyid"],
        "branch_id": period["branchid"],
    })).first()
    if exists is not None or await _rate_evidence_capture_marker(snapshot, period, db):
        return _availability("AVAILABLE")
    return _availability("UNAVAILABLE", "LEGACY_NOT_CAPTURED")


async def _finalized_used_rate_definitions(
    period: dict[str, Any], snapshot: dict[str, Any] | None,
    provenance: dict[str, str | None], db: AsyncConnection,
) -> tuple[list[dict[str, Any]], dict[str, str | None]]:
    availability = await _finalized_rate_evidence_availability(period, snapshot, provenance, db)
    if snapshot is None or availability["state"] == "UNAVAILABLE":
        return [], availability
    rows = (await db.execute(text("""
        WITH usage AS (
            SELECT sl.usedratedefinitionid,
                   ARRAY_AGG(sl.payrollcalculationsnapshotlineid
                             ORDER BY sl.payrollcalculationsnapshotlineid) AS snapshot_line_ids,
                   COUNT(*) AS line_use_count
            FROM payroll.payrollcalculationsnapshotlines sl
            JOIN payroll.payrollcalculationdrivertotals dt
              ON dt.payrollcalculationdrivertotalid = sl.payrollcalculationdrivertotalid
            WHERE dt.payrollcalculationsnapshotid = :snapshot_id
              AND dt.companyid = :company_id AND dt.branchid = :branch_id
              AND sl.usedratedefinitionid IS NOT NULL
            GROUP BY sl.usedratedefinitionid
        )
        SELECT d.payrollcalculationsnapshotusedratedefinitionid, d.driverid,
               dt.drivernamesnapshot, dt.drivercodesnapshot,
               d.evidencekind, d.sourcetypesnapshot, d.payitemid,
               pppi.payitemcode, COALESCE(pppi.displaylabel, pppi.payitemname) AS payitemlabel,
               d.ratetypeid, d.ratetypecodesnapshot, d.ratetypenamesnapshot, d.unitnamesnapshot,
               d.driverrateid, d.driverpayruleid, d.ratebehaviorsnapshot, d.rateamountsnapshot,
               d.effectivefromsnapshot, d.effectivetosnapshot, d.ratestatussnapshot,
               d.blocksizesnapshot, d.roundingrulesnapshot, d.ruletypeSnapshot,
               d.ruleamountsnapshot, d.rulestatussnapshot, d.definitionfingerprint,
               u.snapshot_line_ids, u.line_use_count
        FROM payroll.payrollcalculationsnapshotusedratedefinitions d
        JOIN usage u ON u.usedratedefinitionid = d.payrollcalculationsnapshotusedratedefinitionid
        LEFT JOIN payroll.payrollcalculationdrivertotals dt
          ON dt.payrollcalculationsnapshotid = d.payrollcalculationsnapshotid
         AND dt.companyid = d.companyid AND dt.branchid = d.branchid AND dt.driverid = d.driverid
        LEFT JOIN payroll.payrollperiodpayitems pppi
          ON pppi.payrollperiodid = d.payrollperiodid
         AND pppi.companyid = d.companyid AND pppi.branchid = d.branchid
         AND pppi.payitemid = d.payitemid
        WHERE d.payrollcalculationsnapshotid = :snapshot_id
          AND d.companyid = :company_id AND d.branchid = :branch_id
          AND d.payrollperiodid = :period_id
        ORDER BY d.driverid, d.evidencekind, d.payitemid NULLS LAST,
                 d.effectivefromsnapshot NULLS LAST,
                 d.payrollcalculationsnapshotusedratedefinitionid
    """), {
        "snapshot_id": snapshot["payrollcalculationsnapshotid"],
        "period_id": period["payrollperiodid"], "company_id": period["companyid"],
        "branch_id": period["branchid"],
    })).mappings().all()
    count = (await db.execute(text("""
        SELECT COUNT(*)
        FROM payroll.payrollcalculationsnapshotusedratedefinitions
        WHERE payrollcalculationsnapshotid = :snapshot_id
          AND companyid = :company_id AND branchid = :branch_id
          AND payrollperiodid = :period_id
    """), {
        "snapshot_id": snapshot["payrollcalculationsnapshotid"],
        "period_id": period["payrollperiodid"], "company_id": period["companyid"],
        "branch_id": period["branchid"],
    })).scalar_one()
    # A definition not linked from a financial snapshot line cannot be truthfully called used.
    if int(count) != len(rows):
        return [], _availability("UNAVAILABLE", "RATE_EVIDENCE_LINKAGE_UNAVAILABLE")
    definitions = [{
        "used_rate_definition_id": int(row["payrollcalculationsnapshotusedratedefinitionid"]),
        "driver_id": int(row["driverid"]), "driver_name": row["drivernamesnapshot"],
        "driver_code": row["drivercodesnapshot"], "evidence_kind": row["evidencekind"],
        "source_type": row["sourcetypesnapshot"], "pay_item_id": row["payitemid"],
        "pay_item_code": row["payitemcode"], "pay_item_label": row["payitemlabel"],
        "rate_type_id": row["ratetypeid"], "rate_type_code": row["ratetypecodesnapshot"],
        "rate_type_name": row["ratetypenamesnapshot"], "unit_name": row["unitnamesnapshot"],
        "driver_rate_id": row["driverrateid"], "driver_pay_rule_id": row["driverpayruleid"],
        "rate_behavior": row["ratebehaviorsnapshot"], "rate_amount": row["rateamountsnapshot"],
        "effective_from": row["effectivefromsnapshot"], "effective_to": row["effectivetosnapshot"],
        "rate_status": row["ratestatussnapshot"], "block_size": row["blocksizesnapshot"],
        "rounding_rule": row["roundingrulesnapshot"], "rule_type": row["ruletypesnapshot"],
        "rule_amount": row["ruleamountsnapshot"], "rule_status": row["rulestatussnapshot"],
        "definition_fingerprint": row["definitionfingerprint"],
        "snapshot_line_ids": [int(value) for value in row["snapshot_line_ids"]],
        "line_use_count": int(row["line_use_count"]),
    } for row in rows]
    return definitions, _availability("EMPTY" if not definitions else "AVAILABLE")


async def _finalized_bonus_evidence(
    period: dict[str, Any], snapshot: dict[str, Any] | None,
    provenance: dict[str, str | None], db: AsyncConnection,
) -> tuple[list[dict[str, Any]], dict[str, str | None]]:
    if snapshot is None:
        return [], _availability("UNAVAILABLE", provenance["reason_code"])
    if snapshot["reportevidenceversion"] is None:
        return [], _availability("UNAVAILABLE", "LEGACY_NOT_CAPTURED")
    rows = (await db.execute(text("""
        SELECT b.payrollbonuseventid, b.driverid, dt.drivernamesnapshot, dt.drivercodesnapshot,
               b.amount, b.reason, b.notes, b.datarevision, b.createdbyuserid,
               b.creatordisplaynamesnapshot, b.createdatutc
        FROM payroll.payrollcalculationsnapshotbonusevents b
        LEFT JOIN payroll.payrollcalculationdrivertotals dt
          ON dt.payrollcalculationsnapshotid = b.payrollcalculationsnapshotid
         AND dt.companyid = b.companyid AND dt.branchid = b.branchid AND dt.driverid = b.driverid
        WHERE b.payrollcalculationsnapshotid = :snapshot_id
          AND b.companyid = :company_id AND b.branchid = :branch_id
          AND b.payrollperiodid = :period_id
        ORDER BY b.driverid, b.payrollbonuseventid
    """), {
        "snapshot_id": snapshot["payrollcalculationsnapshotid"],
        "period_id": period["payrollperiodid"], "company_id": period["companyid"],
        "branch_id": period["branchid"],
    })).mappings().all()
    return [{
        "bonus_event_id": int(row["payrollbonuseventid"]), "driver_id": int(row["driverid"]),
        "driver_name": row["drivernamesnapshot"], "driver_code": row["drivercodesnapshot"],
        "amount": row["amount"], "reason": row["reason"], "notes": row["notes"],
        "data_revision": int(row["datarevision"]), "creator_user_id": row["createdbyuserid"],
        "creator_display_name": row["creatordisplaynamesnapshot"],
        "created_at_utc": row["createdatutc"],
    } for row in rows], _availability("EMPTY" if not rows else "AVAILABLE")


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
    off_status_availability = await _finalized_off_status_availability(
        period, snapshot, provenance, db,
    )
    rates_used_availability = await _finalized_rate_evidence_availability(
        period, snapshot, provenance, db,
    )
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
            "off_status": off_status_availability,
            "rates_used": rates_used_availability,
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


async def _finalized_scheduled_work_days(
    period: dict[str, Any], db: AsyncConnection,
) -> tuple[set[date] | None, dict[str, str | None]]:
    rows = (await db.execute(text("""
        SELECT workdate, isdefaultworkday, isaddedworkday
        FROM payroll.payrollperioddays
        WHERE payrollperiodid = :period_id AND companyid = :company_id AND branchid = :branch_id
        ORDER BY workdate
    """), {
        "period_id": period["payrollperiodid"], "company_id": period["companyid"],
        "branch_id": period["branchid"],
    })).mappings().all()
    if not rows:
        return None, _availability("UNAVAILABLE", "PERIOD_CALENDAR_UNAVAILABLE")
    return {
        row["workdate"] for row in rows if row["isdefaultworkday"] or row["isaddedworkday"]
    }, _availability("AVAILABLE")


async def _finalized_eligible_driver_days(
    period: dict[str, Any], snapshot_id: int, scheduled_days: set[date], db: AsyncConnection,
) -> tuple[dict[int, dict[str, Any]] | None, dict[str, str | None]]:
    marker = (await db.execute(text("""
        SELECT 1
        FROM payroll.payrollperiodeligibilitysnapshots
        WHERE payrollperiodid = :period_id AND companyid = :company_id AND branchid = :branch_id
    """), {
        "period_id": period["payrollperiodid"], "company_id": period["companyid"],
        "branch_id": period["branchid"],
    })).first()
    if marker is None:
        return None, _availability("UNAVAILABLE", "ELIGIBILITY_SNAPSHOT_UNAVAILABLE")
    rows = (await db.execute(text("""
        SELECT pde.driverid, pde.drivercodesnapshot, pde.drivernamesnapshot,
               pde.iseligibleforperiod, pde.eligibilityreasoncode,
               pde.hiredatesnapshot, pde.terminationdatesnapshot,
               pde.drivereffectivefromsnapshot, pde.drivereffectivetosnapshot,
               dt.drivercodesnapshot AS total_driver_code,
               dt.drivernamesnapshot AS total_driver_name
        FROM payroll.payrollperioddrivereligibility pde
        LEFT JOIN payroll.payrollcalculationdrivertotals dt
          ON dt.payrollcalculationsnapshotid = :snapshot_id
         AND dt.companyid = pde.companyid AND dt.branchid = pde.branchid
         AND dt.driverid = pde.driverid
        WHERE pde.payrollperiodid = :period_id
          AND pde.companyid = :company_id AND pde.branchid = :branch_id
          AND pde.iseligibleforperiod = TRUE
        ORDER BY pde.driverid
    """), {
        "snapshot_id": snapshot_id, "period_id": period["payrollperiodid"],
        "company_id": period["companyid"], "branch_id": period["branchid"],
    })).mappings().all()
    result: dict[int, dict[str, Any]] = {}
    for row in rows:
        # This legacy inclusion reason needs mutable-source date checks in CP-5B.
        # Finalized reads must fail closed instead of recreating those checks from live rows.
        if row["eligibilityreasoncode"] == "IncludedByExistingData":
            return None, _availability("UNAVAILABLE", "ELIGIBILITY_DATE_WINDOW_UNAVAILABLE")
        eligibility = SimpleNamespace(**dict(row))
        days = {
            work_date for work_date in scheduled_days
            if service._is_snapshot_row_eligible_for_workdate(eligibility, work_date)
        }
        if days:
            driver_id = int(row["driverid"])
            result[driver_id] = {
                "driver_id": driver_id,
                "driver_code": row["drivercodesnapshot"] or row["total_driver_code"],
                "driver_name": row["drivernamesnapshot"] or row["total_driver_name"],
                "eligible_days": days,
            }
    return result, _availability("EMPTY" if not result else "AVAILABLE")


async def _finalized_status_entries(
    snapshot: dict[str, Any] | None, period: dict[str, Any], db: AsyncConnection,
) -> tuple[list[dict[str, Any]], dict[str, str | None]]:
    if snapshot is None:
        return [], _availability("UNAVAILABLE", "PROVENANCE_UNAVAILABLE")
    if snapshot["reportevidenceversion"] is None:
        return [], _availability("UNAVAILABLE", "LEGACY_NOT_CAPTURED")
    rows = (await db.execute(text("""
        SELECT driverid, workdate, statuskeyid, statuscodesnapshot, statuslabelsnapshot,
               statusisoffreasonsnapshot
        FROM payroll.payrollcalculationsnapshotstatusentries
        WHERE payrollcalculationsnapshotid = :snapshot_id
          AND companyid = :company_id AND branchid = :branch_id
          AND payrollperiodid = :period_id
        ORDER BY driverid, workdate
    """), {
        "snapshot_id": snapshot["payrollcalculationsnapshotid"],
        "period_id": period["payrollperiodid"], "company_id": period["companyid"],
        "branch_id": period["branchid"],
    })).mappings().all()
    return [{
        "driver_id": int(row["driverid"]), "work_date": row["workdate"],
        "status_key_id": int(row["statuskeyid"]), "status_code": row["statuscodesnapshot"],
        "status_label": row["statuslabelsnapshot"], "is_off_reason": bool(row["statusisoffreasonsnapshot"]),
    } for row in rows], _availability("EMPTY" if not rows else "AVAILABLE")


async def _finalized_normal_work_pairs(
    period: dict[str, Any], snapshot_id: int, db: AsyncConnection,
) -> tuple[set[tuple[int, date]] | None, dict[str, str | None]]:
    pay_item_snapshot = (await db.execute(text("""
        SELECT 1
        FROM payroll.payrollperiodpayitems
        WHERE payrollperiodid = :period_id AND companyid = :company_id AND branchid = :branch_id
        LIMIT 1
    """), {
        "period_id": period["payrollperiodid"], "company_id": period["companyid"],
        "branch_id": period["branchid"],
    })).first()
    if pay_item_snapshot is None:
        return None, _availability("UNAVAILABLE", "PERIOD_PAY_ITEM_SNAPSHOT_UNAVAILABLE")
    rows = (await db.execute(text("""
        SELECT DISTINCT dt.driverid, sl.workdate
        FROM payroll.payrollcalculationsnapshotlines sl
        JOIN payroll.payrollcalculationdrivertotals dt
          ON dt.payrollcalculationdrivertotalid = sl.payrollcalculationdrivertotalid
        JOIN payroll.payrollperiodpayitems pppi
          ON pppi.payrollperiodid = :period_id
         AND pppi.companyid = :company_id AND pppi.branchid = :branch_id
         AND (pppi.payitemid = sl.payitemid OR pppi.payitemcode = sl.linetype)
        WHERE dt.payrollcalculationsnapshotid = :snapshot_id
          AND dt.companyid = :company_id AND dt.branchid = :branch_id
          AND sl.sourcetype = 'DraftLine' AND sl.linescope = 'Daily'
          AND sl.workdate IS NOT NULL AND sl.quantity IS NOT NULL AND sl.quantity <> 0
          AND sl.linetype NOT IN ('DailyStatus', 'DailyNote', 'STATUS_PAYMENT', 'STATUS_PAY',
                                  'BONUS', 'ADJUSTMENT', 'MINIMUM', 'MAXIMUM',
                                  'SYS_MIN_TOPUP', 'SYS_MAX_CAP')
          AND pppi.itemscope = 'Daily' AND pppi.appearsinpayrollentry = TRUE
          AND pppi.isactiveinperiod = TRUE
    """), {
        "snapshot_id": snapshot_id, "period_id": period["payrollperiodid"],
        "company_id": period["companyid"], "branch_id": period["branchid"],
    })).mappings().all()
    return {(int(row["driverid"]), row["workdate"]) for row in rows}, _availability("AVAILABLE")


async def build_finalized_off_drivers(
    *, period_id: int, company_id: int, user_id: int, db: AsyncConnection,
) -> dict[str, Any]:
    """Build P6B's full-period immutable Off/Status source projection."""
    period = await _period_context(period_id=period_id, company_id=company_id, user_id=user_id, db=db)
    snapshot, provenance = await _originating_snapshot(period, db)
    statuses, status_availability = await _finalized_status_entries(snapshot, period, db)
    calendar_days, calendar_availability = await _finalized_scheduled_work_days(period, db)
    eligibility: dict[int, dict[str, Any]] | None = None
    eligibility_availability = _availability("UNAVAILABLE", "PROVENANCE_UNAVAILABLE")
    normal_work: set[tuple[int, date]] | None = None
    work_availability = _availability("UNAVAILABLE", "PROVENANCE_UNAVAILABLE")
    if snapshot is not None and calendar_days is not None:
        snapshot_id = int(snapshot["payrollcalculationsnapshotid"])
        eligibility, eligibility_availability = await _finalized_eligible_driver_days(
            period, snapshot_id, calendar_days, db,
        )
        normal_work, work_availability = await _finalized_normal_work_pairs(period, snapshot_id, db)

    drivers = eligibility or {}
    identities = {
        driver_id: (driver["driver_name"], driver["driver_code"])
        for driver_id, driver in drivers.items()
    }
    status_by_driver_day = {(row["driver_id"], row["work_date"]): row for row in statuses}
    frozen_statuses = [{
        **row,
        "driver_name": identities.get(row["driver_id"], (None, None))[0],
        "driver_code": identities.get(row["driver_id"], (None, None))[1],
    } for row in statuses]

    can_resolve_off = (
        snapshot is not None and calendar_days is not None and eligibility is not None
        and normal_work is not None and status_availability["state"] in {"AVAILABLE", "EMPTY"}
    )
    fully_off = []
    if can_resolve_off:
        for driver in drivers.values():
            days = driver["eligible_days"]
            off_days = sum(
                1 for work_date in days
                if status_by_driver_day.get((driver["driver_id"], work_date), {}).get("is_off_reason")
                and (driver["driver_id"], work_date) not in normal_work
            )
            if off_days == len(days):
                fully_off.append({
                    "driver_id": driver["driver_id"], "driver_name": driver["driver_name"] or "",
                    "driver_code": driver["driver_code"], "eligible_scheduled_day_count": len(days),
                    "off_day_count": off_days,
                })
    off_availability = (
        _availability("EMPTY" if not fully_off else "AVAILABLE") if can_resolve_off
        else _availability("UNAVAILABLE", next(
            availability["reason_code"] for availability in (
                provenance, status_availability, calendar_availability,
                eligibility_availability, work_availability,
            ) if availability["state"] == "UNAVAILABLE"
        ))
    )
    return {
        "metadata": {
            "period_id": int(period["payrollperiodid"]), "period_code": period["periodcode"],
            "period_name": period["periodname"], "period_status": period["status"],
            "branch_id": int(period["branchid"]),
            "snapshot_id": None if snapshot is None else int(snapshot["payrollcalculationsnapshotid"]),
            "revision_number": None if snapshot is None else int(snapshot["revisionnumber"]),
            "snapshot_hash": None if snapshot is None else str(snapshot["snapshothash"]),
            "report_evidence_available": snapshot is not None and snapshot["reportevidenceversion"] is not None,
            "report_evidence_version": None if snapshot is None else snapshot["reportevidenceversion"],
            "report_evidence_hash": None if snapshot is None else snapshot["reportevidencehash"],
            "section_availability": {
                "snapshot_provenance": provenance, "status_evidence": status_availability,
                "period_calendar": calendar_availability, "eligibility": eligibility_availability,
                "normal_work": work_availability, "off_drivers": off_availability,
            },
            "generated_at_utc": datetime.now(UTC),
        },
        "total_fully_off_drivers": len(fully_off), "fully_off_drivers": fully_off,
        "status_entries": frozen_statuses,
    }


async def build_finalized_rates_used(
    *, period_id: int, company_id: int, user_id: int, db: AsyncConnection,
) -> dict[str, Any]:
    """Build P6C's immutable used-rate/rule and Bonus evidence projection."""
    period = await _period_context(period_id=period_id, company_id=company_id, user_id=user_id, db=db)
    snapshot, provenance = await _originating_snapshot(period, db)
    definitions, rate_availability = await _finalized_used_rate_definitions(
        period, snapshot, provenance, db,
    )
    bonuses, bonus_availability = await _finalized_bonus_evidence(
        period, snapshot, provenance, db,
    )
    return {
        "metadata": {
            "period_id": int(period["payrollperiodid"]), "period_code": period["periodcode"],
            "period_name": period["periodname"], "period_status": period["status"],
            "branch_id": int(period["branchid"]),
            "snapshot_id": None if snapshot is None else int(snapshot["payrollcalculationsnapshotid"]),
            "revision_number": None if snapshot is None else int(snapshot["revisionnumber"]),
            "snapshot_hash": None if snapshot is None else str(snapshot["snapshothash"]),
            "rate_evidence_available": rate_availability["state"] in {"AVAILABLE", "EMPTY"},
            "report_evidence_available": snapshot is not None and snapshot["reportevidenceversion"] is not None,
            "report_evidence_version": None if snapshot is None else snapshot["reportevidenceversion"],
            "report_evidence_hash": None if snapshot is None else snapshot["reportevidencehash"],
            "section_availability": {
                "snapshot_provenance": provenance,
                "rates_rules": rate_availability,
                "bonus_evidence": bonus_availability,
            },
            "generated_at_utc": datetime.now(UTC),
        },
        "used_rate_definitions": definitions,
        "bonus_events": bonuses,
    }
