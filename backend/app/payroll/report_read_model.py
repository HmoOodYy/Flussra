"""CP-5C read-only calculation-report model.

This module deliberately delegates lifecycle financial authority to RP-1 and
only adapts the selected packet into report semantics.  It never recalculates
financial values or mutates payroll state.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.service import _check_branch_access, _check_permission, _require_not_driver_role
from app.payroll import service, status_evidence
from app.payroll.reporting import ReportAuthorityKind, resolve_report_financial_authority


def _unavailable(code: str, message: str) -> HTTPException:
    return HTTPException(status_code=422, detail=f"{code}: {message}")


async def _period_context(
    *, period_id: int, company_id: int, user_id: int, db: AsyncConnection,
) -> dict[str, Any]:
    """Authorize reports without requiring operational ``payroll.view``."""
    await _require_not_driver_role(company_id, user_id, db)
    row = (await db.execute(text("""
        SELECT p.payrollperiodid, p.companyid, p.branchid, p.status, p.periodcode,
               p.periodname, p.periodtype, p.startdate, p.enddate, b.branchname
        FROM payroll.payrollperiods p
        JOIN core.branches b ON b.branchid = p.branchid
        WHERE p.payrollperiodid = :period_id AND p.companyid = :company_id
    """), {"period_id": period_id, "company_id": company_id})).mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail="Payroll period not found.")
    can_see_all, branch_ids = await _check_branch_access(company_id, user_id, db)
    if not can_see_all and int(row["branchid"]) not in branch_ids:
        raise HTTPException(status_code=403, detail="Access denied to this period's branch.")
    await _check_permission(company_id, user_id, int(row["branchid"]), "reports.view", db)
    return dict(row)


async def _columns(period: dict[str, Any], db: AsyncConnection) -> list[dict[str, Any]]:
    rows = (await db.execute(text("""
        SELECT payitemid, payitemcode, payitemname, displaylabel, category,
               datatype, unit, itemscope, sortorder, appearsinreports
        FROM payroll.payrollperiodpayitems
        WHERE payrollperiodid = :period_id AND companyid = :company_id
          AND branchid = :branch_id AND appearsinreports = TRUE
        ORDER BY sortorder, payitemcode, payitemid
    """), {"period_id": period["payrollperiodid"], "company_id": period["companyid"],
          "branch_id": period["branchid"]})).mappings().all()
    # Periods created before CP-2C have no immutable layout rows.  This is a
    # compatibility-only fallback; snapshotted periods never use mutable catalog
    # metadata for their report columns.
    if not rows:
        rows = (await db.execute(text("""
            SELECT payitemid, payitemcode, payitemname, NULL::varchar AS displaylabel,
                   category, datatype, unit, itemscope, sortorder, appearsinreports
            FROM payroll.payitems
            WHERE (companyid IS NULL OR companyid = :company_id)
              AND (branchid IS NULL OR branchid = :branch_id)
              AND status != 'Retired' AND appearsinreports = TRUE
            ORDER BY sortorder, payitemcode, payitemid
        """), {"company_id": period["companyid"], "branch_id": period["branchid"]})).mappings().all()
    return [{
        "pay_item_id": int(row["payitemid"]), "code": row["payitemcode"],
        "label": row["displaylabel"] or row["payitemname"], "category": row["category"],
        "data_type": row["datatype"], "unit": row["unit"], "scope": row["itemscope"],
        "sort_order": row["sortorder"],
    } for row in rows]


async def _operational_rows(period: dict[str, Any], db: AsyncConnection) -> tuple[dict[int, dict[str, Any]], list[dict[str, Any]]]:
    rows = (await db.execute(text("""
        SELECT dl.driverid, d.drivercode, e.fullname AS drivername, dl.workdate,
               dl.linetype, dl.linescope, dl.quantity, dl.sourcetype, dl.sourceid,
               pppi.payitemid
        FROM payroll.payrolldraftlines dl
        JOIN core.drivers d ON d.driverid = dl.driverid
        JOIN core.employees e ON e.employeeid = d.employeeid
        LEFT JOIN payroll.payrollperiodpayitems pppi
          ON pppi.payrollperiodid = dl.payrollperiodid
         AND pppi.companyid = dl.companyid
         AND pppi.branchid = dl.branchid
         AND pppi.payitemcode = dl.linetype
        WHERE dl.payrollperiodid = :period_id AND dl.companyid = :company_id
          AND dl.branchid = :branch_id AND dl.status != 'Void'
        ORDER BY dl.driverid, dl.workdate NULLS LAST, dl.draftlineid
    """), {"period_id": period["payrollperiodid"], "company_id": period["companyid"],
          "branch_id": period["branchid"]})).mappings().all()
    drivers: dict[int, dict[str, Any]] = {}
    work: list[dict[str, Any]] = []
    for row in rows:
        driver_id = int(row["driverid"])
        drivers.setdefault(driver_id, {"driver_id": driver_id, "driver_code": row["drivercode"],
                                       "driver_name": row["drivername"]})
        if row["linescope"] == "Daily" and row["sourcetype"] not in {"System", "BonusEvent"}:
            work.append({"driver_id": driver_id, "work_date": row["workdate"],
                         "line_type": row["linetype"], "pay_item_id": row["payitemid"],
                         "quantity": row["quantity"], "line_scope": row["linescope"]})
    return drivers, work


async def _live_statuses(period: dict[str, Any], db: AsyncConnection) -> list[dict[str, Any]]:
    rows = (await db.execute(text("""
        SELECT es.driverid, es.workdate, es.statuskeyid, sk.statuscode, sk.keyname,
               sk.isoffreason
        FROM payroll.payrollperioddriverdayentrystate es
        JOIN payroll.payrollstatuskeys sk ON sk.statuskeyid = es.statuskeyid
        WHERE es.payrollperiodid = :period_id AND es.companyid = :company_id
          AND es.branchid = :branch_id AND es.isvoided = FALSE
          AND es.statuskeyid IS NOT NULL
        ORDER BY es.driverid, es.workdate
    """), {"period_id": period["payrollperiodid"], "company_id": period["companyid"],
          "branch_id": period["branchid"]})).mappings().all()
    return [{"driver_id": int(r["driverid"]), "work_date": r["workdate"],
             "status_key_id": int(r["statuskeyid"]), "code": r["statuscode"],
             "label": r["keyname"], "is_off": bool(r["isoffreason"])} for r in rows]


async def _snapshot_evidence(snapshot_id: int, period: dict[str, Any], db: AsyncConnection) -> tuple[bool, int | None, str | None, list[dict[str, Any]], list[dict[str, Any]]]:
    header = (await db.execute(text("""
        SELECT reportevidenceversion, reportevidencehash, companyid, branchid, payrollperiodid
        FROM payroll.payrollcalculationsnapshots
        WHERE payrollcalculationsnapshotid = :snapshot_id
    """), {"snapshot_id": snapshot_id})).mappings().first()
    if header is None or (
        int(header["companyid"]) != int(period["companyid"])
        or int(header["branchid"]) != int(period["branchid"])
        or int(header["payrollperiodid"]) != int(period["payrollperiodid"])
    ):
        raise _unavailable(
            "REPORT_FINANCIAL_AUTHORITY_INTEGRITY_ERROR",
            "the finalization provenance does not identify this payroll period's snapshot.",
        )
    version = header["reportevidenceversion"]
    if version is None:
        return False, None, None, [], []
    status_rows = await status_evidence.read_status_entries(
        db,
        snapshot_id=snapshot_id,
        company_id=int(header["companyid"]),
        branch_id=int(header["branchid"]),
        period_id=int(header["payrollperiodid"]),
    )
    statuses = [{
        "driver_id": row["driver_id"], "work_date": row["work_date"],
        "status_key_id": row["status_key_id"], "code": row["status_code"],
        "label": row["status_label"], "is_off": row["is_off_reason"],
    } for row in status_rows]
    bonuses = (await db.execute(text("""
        SELECT payrollbonuseventid, driverid, amount, reason, notes, datarevision,
               createdbyuserid, creatordisplaynamesnapshot, createdatutc
        FROM payroll.payrollcalculationsnapshotbonusevents
        WHERE payrollcalculationsnapshotid = :snapshot_id
        ORDER BY driverid, payrollbonuseventid
    """), {"snapshot_id": snapshot_id})).mappings().all()
    return True, int(version), str(header["reportevidencehash"]), statuses, [
        {"bonus_event_id": int(r["payrollbonuseventid"]), "driver_id": int(r["driverid"]),
         "amount": Decimal(str(r["amount"])), "reason": r["reason"], "notes": r["notes"],
         "data_revision": int(r["datarevision"]), "creator_user_id": r["createdbyuserid"],
         "creator_display_name": r["creatordisplaynamesnapshot"], "created_at_utc": r["createdatutc"]} for r in bonuses
    ]


async def _financial_packet(authority, period: dict[str, Any], db: AsyncConnection) -> tuple[list[dict[str, Any]], dict[int, dict[str, Any]], list[str], list[str], bool, int | None, str | None]:
    """Return authoritative financial lines and driver totals; never calculate them here."""
    kind = authority.authority_kind
    if kind is ReportAuthorityKind.SOURCE_ONLY:
        return [], {}, [], [], False, None, None
    if kind is ReportAuthorityKind.UNAVAILABLE:
        raise _unavailable("REPORT_UNAVAILABLE", "Cancelled payroll periods have no calculation reports.")
    if kind is ReportAuthorityKind.LIVE:
        summary = service.PeriodSummary.model_construct(
            payroll_period_id=int(period["payrollperiodid"]), branch_id=int(period["branchid"]),
            branch_name=period["branchname"], period_code=period["periodcode"], period_name=period["periodname"],
            period_type=period["periodtype"], start_date=period["startdate"], end_date=period["enddate"], status=period["status"],
        )
        packet = await service._build_live_calculation_packet(summary, int(period["companyid"]), db)
        lines = [dict(line.__dict__) for driver in packet.drivers for line in driver.lines]
        totals = {d.driver_id: {"daily_pay": d.daily_pay, "status_pay": d.status_pay,
                  "period_pay": d.period_pay, "minimum_adjustment": d.minimum_adjustment,
                  "maximum_adjustment": d.maximum_adjustment, "bonus_total": d.bonus_total,
                  "total_pay": d.expected_pay, "driver_code": d.driver_code, "driver_name": d.driver_name}
                  for d in packet.drivers}
        return lines, totals, packet.blockers, packet.warnings, True, None, None
    if kind in {ReportAuthorityKind.SUBMITTED_SNAPSHOT, ReportAuthorityKind.APPROVED_SNAPSHOT}:
        snapshot_id = authority.snapshot_id
        assert snapshot_id is not None
        rows = (await db.execute(text("""
            SELECT dt.driverid, dt.drivercodesnapshot, dt.drivernamesnapshot, dt.dailypay,
                   dt.statuspay, dt.periodpay, dt.minimumadjustment, dt.maximumadjustment,
                   dt.bonustotal, dt.expectedpay, sl.sourcetype, sl.sourceid, sl.linetype,
                   sl.linescope, sl.workdate, sl.payitemid, sl.quantity, sl.resolvedrateamount,
                   sl.calculatedamount, sl.bonuseventid
            FROM payroll.payrollcalculationdrivertotals dt
            LEFT JOIN payroll.payrollcalculationsnapshotlines sl
              ON sl.payrollcalculationdrivertotalid = dt.payrollcalculationdrivertotalid
            WHERE dt.payrollcalculationsnapshotid = :snapshot_id
            ORDER BY dt.driverid, sl.payrollcalculationsnapshotlineid
        """), {"snapshot_id": snapshot_id})).mappings().all()
        totals: dict[int, dict[str, Any]] = {}
        lines: list[dict[str, Any]] = []
        for r in rows:
            did = int(r["driverid"])
            totals.setdefault(did, {"daily_pay": r["dailypay"], "status_pay": r["statuspay"], "period_pay": r["periodpay"],
                                    "minimum_adjustment": r["minimumadjustment"], "maximum_adjustment": r["maximumadjustment"],
                                    "bonus_total": r["bonustotal"], "total_pay": r["expectedpay"], "driver_code": r["drivercodesnapshot"], "driver_name": r["drivernamesnapshot"]})
            if r["linetype"] is not None:
                lines.append({"driver_id": did, "source_type": r["sourcetype"], "source_id": r["sourceid"],
                              "line_type": r["linetype"], "line_scope": r["linescope"], "work_date": r["workdate"],
                              "pay_item_id": r["payitemid"], "quantity": r["quantity"], "resolved_rate_amount": r["resolvedrateamount"],
                              "calculated_amount": r["calculatedamount"], "bonus_event_id": r["bonuseventid"]})
        return lines, totals, [], [], True, snapshot_id, authority.snapshot_hash
    # Locked/Archived: FinalLines are the financial authority.  Snapshot identity, when
    # present, is read only for evidence below, never to replace the money source.
    rows = (await db.execute(text("""
        SELECT driverid, linetype, linescope, workdate, payitemid, quantity,
               resolvedrateamount, finalamount, sourcetype, sourceid, bonuseventid,
               sourcesnapshot
        FROM payroll.payrollfinallines
        WHERE payrollperiodid = :period_id AND companyid = :company_id AND branchid = :branch_id
        ORDER BY driverid, finallineid
    """), {"period_id": period["payrollperiodid"], "company_id": period["companyid"], "branch_id": period["branchid"]})).mappings().all()
    totals: dict[int, dict[str, Any]] = defaultdict(lambda: {"daily_pay": Decimal("0"), "status_pay": Decimal("0"), "period_pay": Decimal("0"), "minimum_adjustment": Decimal("0"), "maximum_adjustment": Decimal("0"), "bonus_total": Decimal("0"), "total_pay": Decimal("0"), "driver_code": None, "driver_name": None})
    lines: list[dict[str, Any]] = []
    for r in rows:
        did, amount = int(r["driverid"]), Decimal(str(r["finalamount"]))
        total = totals[did]
        total["total_pay"] += amount
        if r["linetype"] == "SYS_MIN_TOPUP":
            total["minimum_adjustment"] += amount
        elif r["linetype"] == "SYS_MAX_CAP":
            total["maximum_adjustment"] += amount
        elif r["sourcetype"] == "BonusEvent":
            total["bonus_total"] += amount
        elif r["linescope"] == "Daily":
            total["daily_pay"] += amount
        else:
            total["period_pay"] += amount
        lines.append({"driver_id": did, "source_type": r["sourcetype"], "source_id": r["sourceid"], "line_type": r["linetype"], "line_scope": r["linescope"], "work_date": r["workdate"], "pay_item_id": r["payitemid"], "quantity": r["quantity"], "resolved_rate_amount": r["resolvedrateamount"], "calculated_amount": amount, "bonus_event_id": r["bonuseventid"]})
    provenance = (await db.execute(text("""
        SELECT DISTINCT sourcesnapshot ->> 'payroll_calculation_snapshot_id' AS snapshot_id
        FROM payroll.payrollfinallines
        WHERE payrollperiodid = :period_id AND companyid = :company_id AND branchid = :branch_id
          AND sourcesnapshot IS NOT NULL
          AND sourcesnapshot ? 'payroll_calculation_snapshot_id'
    """), {"period_id": period["payrollperiodid"], "company_id": period["companyid"],
          "branch_id": period["branchid"]})).mappings().all()
    snapshot_ids = {int(row["snapshot_id"]) for row in provenance if row["snapshot_id"] is not None}
    # Legacy finalization rows can retain money authority without a snapshot;
    # report-only evidence is then marked unavailable by the caller.
    provenance_snapshot_id = next(iter(snapshot_ids)) if len(snapshot_ids) == 1 else None
    return lines, dict(totals), [], [], True, provenance_snapshot_id, None


def _status_summaries(statuses: list[dict[str, Any]]) -> dict[int, list[dict[str, Any]]]:
    grouped: dict[int, dict[tuple[str, str, bool], int]] = defaultdict(lambda: defaultdict(int))
    for status in statuses:
        grouped[status["driver_id"]][(status["code"], status["label"], status["is_off"])] += 1
    return {driver_id: [{"code": key[0], "label": key[1], "is_off": key[2], "count": count}
                        for key, count in values.items()] for driver_id, values in grouped.items()}


def _report_totals(
    work_rows: list[dict[str, Any]],
    financial_totals: dict[int, dict[str, Any]],
    financials_available: bool,
) -> tuple[dict[str, Decimal], dict[str, Decimal] | None]:
    work_totals: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    for row in work_rows:
        quantity = row.get("quantity")
        if quantity is not None:
            work_totals[str(row["line_type"])] += Decimal(str(quantity))
    if not financials_available:
        return dict(work_totals), None
    names = (
        "daily_pay", "status_pay", "period_pay", "minimum_adjustment",
        "maximum_adjustment", "bonus_total", "total_pay",
    )
    return dict(work_totals), {
        name: sum((Decimal(str(total[name])) for total in financial_totals.values()), Decimal("0"))
        for name in names
    }


async def build_report(*, report_type: str, period_id: int, company_id: int, user_id: int, db: AsyncConnection) -> dict[str, Any]:
    period = await _period_context(period_id=period_id, company_id=company_id, user_id=user_id, db=db)
    authority = await resolve_report_financial_authority(db=db, period_id=period_id, company_id=company_id, branch_id=int(period["branchid"]))
    if authority.authority_kind is ReportAuthorityKind.UNAVAILABLE:
        raise _unavailable("REPORT_UNAVAILABLE", "Cancelled payroll periods have no calculation reports.")
    columns = await _columns(period, db)
    lines, financial_totals, blockers, warnings, financials_available, snapshot_id, snapshot_hash = await _financial_packet(authority, period, db)
    if authority.authority_kind in {ReportAuthorityKind.SOURCE_ONLY, ReportAuthorityKind.LIVE}:
        operational_drivers, work_rows = await _operational_rows(period, db)
    else:
        # Frozen states must not reconstruct work from mutable DraftLines.  The
        # persisted packet carries the daily work values that had financial
        # effect; other frozen operational evidence is intentionally unavailable
        # rather than silently read from current source rows.
        operational_drivers = {}
        work_rows = [
            {"driver_id": line["driver_id"], "work_date": line["work_date"],
             "line_type": line["line_type"], "pay_item_id": line.get("pay_item_id"),
             "quantity": line.get("quantity"), "line_scope": line["line_scope"]}
            for line in lines
            if line["source_type"] == "DraftLine" and line["line_scope"] == "Daily"
        ]
    drivers = {**operational_drivers}
    for driver_id, total in financial_totals.items():
        drivers.setdefault(driver_id, {"driver_id": driver_id, "driver_code": total.get("driver_code"), "driver_name": total.get("driver_name")})
    evidence_available, evidence_version, evidence_hash, statuses, bonuses = (False, None, None, [], [])
    if snapshot_id is not None and authority.authority_kind in {
        ReportAuthorityKind.SUBMITTED_SNAPSHOT,
        ReportAuthorityKind.APPROVED_SNAPSHOT,
        ReportAuthorityKind.FINAL_LINES,
    }:
        evidence_available, evidence_version, evidence_hash, statuses, bonuses = await _snapshot_evidence(snapshot_id, period, db)
    elif authority.authority_kind is ReportAuthorityKind.LIVE:
        statuses = await _live_statuses(period, db)
        bonuses = [{"bonus_event_id": int(r["payrollbonuseventid"]), "driver_id": int(r["driverid"]),
                    "amount": Decimal(str(r["amount"])), "reason": r["reason"], "notes": r["notes"],
                    "data_revision": int(r["datarevision"]), "creator_user_id": r["createdbyuserid"],
                    "creator_display_name": r["creatordisplaynamesnapshot"], "created_at_utc": r["createdatutc"]}
                   for r in await service._load_active_bonus_events(int(period["payrollperiodid"]), company_id, db)]
        evidence_available = True
    summaries = _status_summaries(statuses)
    by_driver_work: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in work_rows:
        by_driver_work[row["driver_id"]].append(row)
    by_driver_lines: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for line in lines:
        by_driver_lines[line["driver_id"]].append(line)
    by_driver_status: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for item in statuses:
        by_driver_status[item["driver_id"]].append(item)
    by_driver_bonus: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for item in bonuses:
        by_driver_bonus[item["driver_id"]].append(item)
    result_drivers = []
    for driver_id in sorted(drivers):
        total = financial_totals.get(driver_id)
        pay = None if total is None else {**total, "financial_lines": by_driver_lines[driver_id]}
        work = {"daily_rows": by_driver_work[driver_id], "status_entries": by_driver_status[driver_id],
                "status_summaries": summaries.get(driver_id, [])}
        result_drivers.append({**drivers[driver_id], "work": work, "pay": pay, "bonus_events": by_driver_bonus[driver_id]})
    if report_type == "period-pay" and not financials_available:
        raise _unavailable("REPORT_FINANCIALS_UNAVAILABLE", "Prepared payroll periods have no financial report authority.")
    work_totals, pay_totals = _report_totals(work_rows, financial_totals, financials_available)
    metadata = {"period_id": int(period["payrollperiodid"]), "period_code": period["periodcode"], "period_name": period["periodname"],
                "period_status": period["status"], "branch_id": int(period["branchid"]), "report_type": report_type,
                "authority_kind": authority.authority_kind, "financials_available": financials_available,
                "unavailable_reason": None if financials_available else "SOURCE_ONLY_PERIOD",
                "snapshot_id": snapshot_id or authority.snapshot_id, "revision_number": authority.revision_number,
                "snapshot_hash": snapshot_hash or authority.snapshot_hash, "report_evidence_available": evidence_available,
                "report_evidence_version": evidence_version, "report_evidence_hash": evidence_hash,
                "blockers": blockers, "warnings": warnings, "generated_at_utc": datetime.now(UTC)}
    return {
        "metadata": metadata,
        "columns": columns,
        "drivers": result_drivers,
        "work_totals": work_totals,
        "pay_totals": pay_totals,
    }
