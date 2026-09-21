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

from app.core.service import (
    _build_in_clause,
    _check_branch_access,
    _check_permission,
    _require_not_driver_role,
)
from app.payroll import report_read_model, status_evidence
from app.payroll.eligibility import _is_snapshot_row_eligible_for_workdate

_REPORT_TYPES = {"drivers", "period-work", "period-pay", "mixed"}
_FINALIZED_STATUSES = {"Locked", "Archived"}


def _unavailable(code: str, message: str) -> HTTPException:
    return HTTPException(status_code=422, detail=f"{code}: {message}")


def _availability(state: str, reason_code: str | None = None) -> dict[str, str | None]:
    return {"state": state, "reason_code": reason_code}


async def list_finalized_periods(
    *,
    company_id: int,
    user_id: int,
    db: AsyncConnection,
    branch_id: int | None = None,
    period_status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """List only finalized periods visible through the ledger permission."""
    await _require_not_driver_role(company_id, user_id, db)
    can_see_all, accessible_branch_ids = await _check_branch_access(company_id, user_id, db)

    if period_status is not None and period_status not in _FINALIZED_STATUSES:
        raise HTTPException(
            status_code=422,
            detail="Finalized period status must be Locked or Archived.",
        )

    if branch_id is not None and not can_see_all and branch_id not in accessible_branch_ids:
        raise HTTPException(status_code=403, detail="Access denied to the requested branch.")

    if branch_id is not None:
        candidate_branch_ids = [branch_id]
    elif can_see_all:
        branch_rows = (await db.execute(text("""
            SELECT branchid
            FROM core.branches
            WHERE companyid = :company_id
            ORDER BY branchid
        """), {"company_id": company_id})).mappings().all()
        candidate_branch_ids = [int(row["branchid"]) for row in branch_rows]
    else:
        candidate_branch_ids = [int(value) for value in accessible_branch_ids]

    if not candidate_branch_ids:
        raise HTTPException(status_code=403, detail="No accessible branch for finalized payroll.")

    candidate_clause, candidate_params = _build_in_clause(candidate_branch_ids, "candidate_branch")
    permission_rows = (await db.execute(text(f"""
        SELECT b.branchid
        FROM core.branches b
        WHERE b.companyid = :company_id
          AND b.branchid IN ({candidate_clause})
          AND sec.fn_UserHasPermission(
              :user_id, :company_id, b.branchid, 'ledger.view'
          )
        ORDER BY b.branchid
    """), {
        "company_id": company_id,
        "user_id": user_id,
        **candidate_params,
    })).mappings().all()
    permitted_branch_ids = [int(row["branchid"]) for row in permission_rows]
    if not permitted_branch_ids:
        raise HTTPException(
            status_code=403,
            detail="You do not have ledger.view permission on an accessible branch.",
        )

    branch_clause, branch_params = _build_in_clause(permitted_branch_ids, "finalized_branch")
    conditions = [
        "p.companyid = :company_id",
        f"p.branchid IN ({branch_clause})",
        "p.status IN ('Locked', 'Archived')",
    ]
    params: dict[str, Any] = {"company_id": company_id, **branch_params}
    if period_status is not None:
        conditions.append("p.status = :period_status")
        params["period_status"] = period_status
    params.update({"limit": limit, "offset": offset})
    result = await db.execute(text(f"""
        SELECT p.payrollperiodid AS period_id,
               p.periodcode AS period_code,
               p.periodname AS period_name,
               p.status AS period_status,
               p.periodtype AS period_type,
               p.branchid AS branch_id,
               b.branchname AS branch_name,
               p.startdate AS start_date,
               p.enddate AS end_date,
               p.paydate AS pay_date,
               p.lockedatutc AS finalized_at_utc
        FROM payroll.payrollperiods p
        JOIN core.branches b ON b.branchid = p.branchid AND b.companyid = p.companyid
        WHERE {' AND '.join(conditions)}
        ORDER BY p.startdate DESC, b.branchname, p.payrollperiodid DESC
        LIMIT :limit OFFSET :offset
    """), params)
    return [dict(row) for row in result.mappings().all()]


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
            "bonus_event_id": row["bonuseventid"], "snapshot_source_type": row["sourcetype"],
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
    pay_totals = {
        name: sum((Decimal(str(total[name])) for total in totals.values()), Decimal("0"))
        for name in names
    }
    pay_totals["gross_pay"] = pay_totals["daily_pay"] + pay_totals["status_pay"] + pay_totals["period_pay"]
    return pay_totals


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
    audit_permission = await db.execute(text("""
        SELECT sec.fn_UserHasPermission(
            :user_id, :company_id, :branch_id, 'ledger.audit.view'
        )
    """), {
        "user_id": user_id, "company_id": period["companyid"],
        "branch_id": period["branchid"],
    })
    audit_availability = (
        _availability("AVAILABLE") if audit_permission.scalar_one()
        else _availability("UNAVAILABLE", "AUDIT_PERMISSION_REQUIRED")
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
            "audit": audit_availability,
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
    pay_item_columns = await report_read_model._pay_item_columns(period, db)
    pay_item_column_ids = [c["pay_item_id"] for c in pay_item_columns]
    final_lines, financial_totals = await _final_lines(period, db)
    per_driver_pay_items: dict[int, dict[int, Decimal]] = {}
    pay_item_total_map: dict[int, Decimal] = {cid: Decimal("0") for cid in pay_item_column_ids}
    # Period Work is operational (quantities/Status), not financial, and must
    # stay independent of the per-Pay-Item financial integrity invariant.
    if report_type != "period-work":
        # A missing frozen Daily Pay Item layout (pre-CP-2C period) must fail
        # explicitly rather than silently reporting zero columns as if this
        # were a legitimate modern period with no active Daily items.
        if not await report_read_model._period_has_pay_item_snapshot(int(period["payrollperiodid"]), db):
            raise _unavailable(
                "REPORT_PAY_ITEM_LAYOUT_UNAVAILABLE",
                "this period has no frozen Daily Pay Item layout; per-item financial reporting is unavailable.",
            )
        per_driver_pay_items, pay_item_total_map = report_read_model._pay_item_amounts(final_lines, pay_item_column_ids)
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
        pay = None
        if total is not None:
            item_amounts = per_driver_pay_items.get(driver_id, {})
            gross_pay = Decimal(str(total["daily_pay"])) + Decimal(str(total["status_pay"])) + Decimal(str(total["period_pay"]))
            pay = {
                **total,
                "gross_pay": gross_pay,
                # Period Work never carries per-item money: an empty list
                # here means "not computed for this view", never a verified
                # zero for every column.
                "pay_item_amounts": [] if report_type == "period-work" else [
                    {"pay_item_id": cid, "amount": item_amounts.get(cid, Decimal("0"))}
                    for cid in pay_item_column_ids
                ],
                "financial_lines": by_driver_lines[driver_id],
            }
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
        "columns": columns, "pay_item_columns": pay_item_columns, "drivers": result_drivers,
        "work_totals": _work_totals(work_rows), "pay_totals": _pay_totals(financial_totals),
        "pay_item_totals": None if report_type == "period-work" else [
            {"pay_item_id": cid, "amount": pay_item_total_map.get(cid, Decimal("0"))}
            for cid in pay_item_column_ids
        ],
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
            if _is_snapshot_row_eligible_for_workdate(eligibility, work_date)
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
    entries: list[dict[str, Any]] = []
    if snapshot is not None and snapshot["reportevidenceversion"] is not None:
        entries = await status_evidence.read_status_entries(
            db,
            snapshot_id=snapshot["payrollcalculationsnapshotid"],
            company_id=period["companyid"],
            branch_id=period["branchid"],
            period_id=period["payrollperiodid"],
        )
    return entries, status_evidence.status_evidence_availability(snapshot, entries)


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


async def _audit_domain_availability(
    period: dict[str, Any], db: AsyncConnection,
) -> dict[str, dict[str, str | None]]:
    """Expose P6D coverage without reconstructing legacy mutable history."""
    coverage = (await db.execute(text("""
        SELECT evidencedomain, coveragestate
        FROM payroll.payrollperiodauditevidencecoverage
        WHERE companyid = :company_id AND branchid = :branch_id
          AND payrollperiodid = :period_id
    """), {
        "company_id": period["companyid"], "branch_id": period["branchid"],
        "period_id": period["payrollperiodid"],
    })).mappings().all()
    counts = {
        row["evidencedomain"]: int(row["event_count"])
        for row in (await db.execute(text("""
            SELECT evidencedomain, COUNT(*) AS event_count
            FROM payroll.payrollperiodauditevidenceevents
            WHERE companyid = :company_id AND branchid = :branch_id
              AND payrollperiodid = :period_id
            GROUP BY evidencedomain
        """), {
            "company_id": period["companyid"], "branch_id": period["branchid"],
            "period_id": period["payrollperiodid"],
        })).mappings().all()
    }
    by_domain = {row["evidencedomain"]: row["coveragestate"] for row in coverage}
    result: dict[str, dict[str, str | None]] = {}
    for domain in ("SOURCE", "STATUS_NOTE", "BONUS", "REVIEW_COMMENT"):
        state = by_domain.get(domain)
        if state is None:
            result[domain.lower()] = _availability("UNAVAILABLE", "LEGACY_NOT_CAPTURED")
        elif state == "PARTIAL":
            result[domain.lower()] = _availability("PARTIAL", "HISTORY_PRECEDES_P6D_CAPTURE")
        elif counts.get(domain, 0) == 0:
            result[domain.lower()] = _availability("EMPTY")
        else:
            result[domain.lower()] = _availability("AVAILABLE")
    return result


async def build_finalized_audit(
    *, period_id: int, company_id: int, user_id: int, db: AsyncConnection,
) -> dict[str, Any]:
    """Build P6D's lazy immutable change chronology and revision grouping."""
    period = await _period_context(period_id=period_id, company_id=company_id, user_id=user_id, db=db)
    await _check_permission(company_id, user_id, int(period["branchid"]), "ledger.audit.view", db)
    snapshot, provenance = await _originating_snapshot(period, db)
    availability = await _audit_domain_availability(period, db)

    event_rows = (await db.execute(text("""
        SELECT e.payrollperiodauditevidenceeventid, e.evidencedomain, e.actioncode,
               e.sourceentitytype, e.sourceentityid, e.reviewitemid, e.driverid, e.workdate, e.payitemid,
               e.beforestatejson, e.afterstatejson, e.actoruserid,
               e.actordisplaynamesnapshot, e.responsibilitycontextsnapshot,
               e.reasonsnapshot, e.correlationid, e.sourcerevision, e.occurredatutc,
               m.payrollcalculationsnapshotid, s.revisionnumber
        FROM payroll.payrollperiodauditevidenceevents e
        LEFT JOIN payroll.payrollperiodauditevidencesnapshotevents m
          ON m.payrollperiodauditevidenceeventid = e.payrollperiodauditevidenceeventid
        LEFT JOIN payroll.payrollcalculationsnapshots s
          ON s.payrollcalculationsnapshotid = m.payrollcalculationsnapshotid
         AND s.companyid = e.companyid AND s.branchid = e.branchid
         AND s.payrollperiodid = e.payrollperiodid
        WHERE e.companyid = :company_id AND e.branchid = :branch_id
          AND e.payrollperiodid = :period_id
        ORDER BY e.occurredatutc, e.payrollperiodauditevidenceeventid
    """), {
        "company_id": period["companyid"], "branch_id": period["branchid"],
        "period_id": period["payrollperiodid"],
    })).mappings().all()
    events = [{
        "event_id": int(row["payrollperiodauditevidenceeventid"]),
        "domain": row["evidencedomain"], "action_code": row["actioncode"],
        "source_entity_type": row["sourceentitytype"], "source_entity_id": row["sourceentityid"],
        "review_item_id": row["reviewitemid"],
        "driver_id": row["driverid"], "work_date": row["workdate"], "pay_item_id": row["payitemid"],
        "before_state": row["beforestatejson"], "after_state": row["afterstatejson"],
        "actor_user_id": int(row["actoruserid"]), "actor_display_name": row["actordisplaynamesnapshot"],
        "responsibility_context": row["responsibilitycontextsnapshot"], "reason": row["reasonsnapshot"],
        "correlation_id": None if row["correlationid"] is None else str(row["correlationid"]),
        "source_revision": row["sourcerevision"], "occurred_at_utc": row["occurredatutc"],
        "snapshot_id": row["payrollcalculationsnapshotid"], "revision_number": row["revisionnumber"],
    } for row in event_rows]

    workflow_rows = (await db.execute(text("""
        SELECT w.actioncode, w.actoruserid, w.actordisplaynamesnapshot,
               w.responsibilitycontextsnapshot, w.requiredpermissioncode, w.reasonsnapshot,
               w.actionatutc, w.payrollcalculationsnapshotid, s.revisionnumber,
               w.reviewitemid, w.reviewdecisionid
        FROM payroll.payrollperiodworkflowactionevidence w
        LEFT JOIN payroll.payrollcalculationsnapshots s
          ON s.payrollcalculationsnapshotid = w.payrollcalculationsnapshotid
         AND s.companyid = w.companyid AND s.branchid = w.branchid
         AND s.payrollperiodid = w.payrollperiodid
        WHERE w.companyid = :company_id AND w.branchid = :branch_id
          AND w.payrollperiodid = :period_id
        ORDER BY w.actionatutc, w.payrollperiodworkflowactionevidenceid
    """), {
        "company_id": period["companyid"], "branch_id": period["branchid"],
        "period_id": period["payrollperiodid"],
    })).mappings().all()
    lifecycle = [{
        "action_code": row["actioncode"], "actor_user_id": int(row["actoruserid"]),
        "actor_display_name": row["actordisplaynamesnapshot"],
        "responsibility_context": row["responsibilitycontextsnapshot"],
        "required_permission_code": row["requiredpermissioncode"], "reason": row["reasonsnapshot"],
        "action_at_utc": row["actionatutc"], "snapshot_id": row["payrollcalculationsnapshotid"],
        "revision_number": row["revisionnumber"], "review_item_id": row["reviewitemid"],
        "review_decision_id": row["reviewdecisionid"],
    } for row in workflow_rows]

    groups: dict[int, dict[str, Any]] = {}
    for item in lifecycle:
        snapshot_id = item["snapshot_id"]
        if snapshot_id is not None and item["action_code"] in {"SUBMITTED", "RESUBMITTED"}:
            groups[int(snapshot_id)] = {
                "snapshot_id": int(snapshot_id), "revision_number": int(item["revision_number"]),
                "submit_action": item["action_code"],
                "is_final_approved_revision": snapshot is not None and int(snapshot_id) == int(snapshot["payrollcalculationsnapshotid"]),
                "event_ids": [], "review_comment_event_ids": [],
            }
    for event in events:
        snapshot_id = event["snapshot_id"]
        if snapshot_id is not None and int(snapshot_id) not in groups:
            groups[int(snapshot_id)] = {
                "snapshot_id": int(snapshot_id),
                "revision_number": int(event["revision_number"]),
                "submit_action": None,
                "is_final_approved_revision": (
                    snapshot is not None
                    and int(snapshot_id) == int(snapshot["payrollcalculationsnapshotid"])
                ),
                "event_ids": [],
                "review_comment_event_ids": [],
            }
        if snapshot_id is not None and int(snapshot_id) in groups:
            target = "review_comment_event_ids" if event["domain"] == "REVIEW_COMMENT" else "event_ids"
            groups[int(snapshot_id)][target].append(event["event_id"])

    rate_rows = []
    if snapshot is not None:
        rate_rows = [{
            "used_rate_definition_id": int(row["payrollcalculationsnapshotusedratedefinitionid"]),
            "evidence_kind": row["evidencekind"], "definition_fingerprint": row["definitionfingerprint"],
        } for row in (await db.execute(text("""
            SELECT payrollcalculationsnapshotusedratedefinitionid, evidencekind, definitionfingerprint
            FROM payroll.payrollcalculationsnapshotusedratedefinitions
            WHERE payrollcalculationsnapshotid = :snapshot_id
              AND companyid = :company_id AND branchid = :branch_id AND payrollperiodid = :period_id
            ORDER BY payrollcalculationsnapshotusedratedefinitionid
        """), {
            "snapshot_id": snapshot["payrollcalculationsnapshotid"],
            "company_id": period["companyid"], "branch_id": period["branchid"],
            "period_id": period["payrollperiodid"],
        })).mappings().all()]

    complete = all(value["state"] in {"AVAILABLE", "EMPTY"} for value in availability.values())
    metadata = {
        "period_id": int(period["payrollperiodid"]), "period_code": period["periodcode"],
        "period_name": period["periodname"], "period_status": period["status"],
        "branch_id": int(period["branchid"]),
        "snapshot_id": None if snapshot is None else int(snapshot["payrollcalculationsnapshotid"]),
        "revision_number": None if snapshot is None else int(snapshot["revisionnumber"]),
        "snapshot_hash": None if snapshot is None else str(snapshot["snapshothash"]),
        "complete_period_chronology_available": complete,
        "evidence_version": 1 if any(v["state"] != "UNAVAILABLE" for v in availability.values()) else None,
        "section_availability": {"snapshot_provenance": provenance, **availability},
        "generated_at_utc": datetime.now(UTC),
    }
    return {
        "metadata": metadata, "lifecycle_events": lifecycle,
        "source_events": [event for event in events if event["domain"] == "SOURCE"],
        "status_note_events": [event for event in events if event["domain"] == "STATUS_NOTE"],
        "bonus_events": [event for event in events if event["domain"] == "BONUS"],
        "review_events": [event for event in events if event["domain"] == "REVIEW_COMMENT"],
        "chronology": events, "revision_groups": list(groups.values()),
        "rate_rule_provenance": rate_rows,
    }
