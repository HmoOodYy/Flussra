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

from app.company_currency import frozen_currency, get_company_currency
from app.core.service import _check_branch_access, _check_permission, _require_not_driver_role
from app.payroll import period_calculation, status_evidence
from app.payroll.evidence_gate import evidence_not_ready
from app.payroll.period_definitions import list_period_definition_rows
from app.payroll.reporting import ReportAuthorityKind, resolve_report_financial_authority
from app.payroll.schemas import PeriodSummary


def _unavailable(code: str, message: str) -> HTTPException:
    return HTTPException(status_code=422, detail=f"{code}: {message}")


def classify_final_line_component(row: Any) -> str:
    """Map one immutable FinalLine to its canonical financial component.

    Every monetary FinalLine must belong to an explicit canonical owner; an
    unrecognized source fails closed instead of falling into a catch-all.
    """
    if row["linetype"] == "SYS_MIN_TOPUP":
        return "minimum_adjustment"
    if row["linetype"] == "SYS_MAX_CAP":
        return "maximum_adjustment"
    if row["sourcetype"] == "BonusEvent":
        return "bonus_total"
    if row["sourcetype"] == "StatusEntryState":
        return "status_pay"
    if row["sourcetype"] == "DraftLine" and row["linescope"] == "Daily":
        return "daily_pay"
    raise _unavailable(
        "REPORT_FINANCIAL_AUTHORITY_INTEGRITY_ERROR",
        "a FinalLine does not belong to a canonical payroll component "
        f"(sourcetype={row['sourcetype']!r}, linetype={row['linetype']!r}, "
        f"linescope={row['linescope']!r}).",
    )


async def _final_lines_currency(company_id: int, period_id: int, db: AsyncConnection):
    """Use one coherent immutable FinalLines denomination or fail closed."""
    row = (await db.execute(text("""
        SELECT COUNT(*) AS row_count,
               COUNT(DISTINCT (currencycode, currencyminorunitdigits)) AS pair_count,
               MIN(currencycode) AS currency_code,
               MIN(currencyminorunitdigits) AS minor_unit_digits
        FROM payroll.payrollfinallines
        WHERE companyid = :cid AND payrollperiodid = :pid
    """), {"cid": company_id, "pid": period_id})).mappings().one()
    if int(row["row_count"]) == 0:
        raise _unavailable("SNAPSHOT_CURRENCY_REQUIRED", "Finalized currency evidence is unavailable.")
    if int(row["pair_count"]) != 1:
        raise _unavailable("SNAPSHOT_CURRENCY_MISMATCH", "FinalLines contain inconsistent frozen currency.")
    return frozen_currency(row["currency_code"], row["minor_unit_digits"])


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


def _definition_column(row) -> dict[str, Any]:
    return {
        "payroll_period_definition_id": int(row["payrollperioddefinitionid"]),
        "code": row["definitioncodesnapshot"],
        "label": row["definitionnamesnapshot"],
        "input_type": row["inputtypesnapshot"],
        "unit": row["unitsnapshot"],
        "sort_order": int(row["sortorder"]),
    }


async def _columns(period: dict[str, Any], db: AsyncConnection) -> list[dict[str, Any]]:
    """The period's frozen definition layout (quantity columns). Zero rows is valid."""
    rows = await list_period_definition_rows(
        int(period["payrollperiodid"]), int(period["companyid"]), db)
    return [_definition_column(row) for row in rows]


async def _definition_columns(period: dict[str, Any], db: AsyncConnection) -> list[dict[str, Any]]:
    """Financial columns: the period's ACTIVE frozen definitions only."""
    rows = await list_period_definition_rows(
        int(period["payrollperiodid"]), int(period["companyid"]), db, active_only=True)
    return [_definition_column(row) for row in rows]


def _definition_amounts(
    lines: list[dict[str, Any]],
    column_ids: list[int],
) -> tuple[dict[int, dict[int, Decimal]], dict[int, Decimal]]:
    """Aggregate live Daily source money by frozen period definition column.

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
            raise _unavailable(
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


async def _operational_rows(period: dict[str, Any], db: AsyncConnection) -> tuple[dict[int, dict[str, Any]], list[dict[str, Any]]]:
    rows = (await db.execute(text("""
        SELECT dl.driverid, d.drivercode, e.fullname AS drivername, dl.workdate,
               dl.linetype, dl.quantity, dl.sourcetype, dl.sourceid,
               dl.payrollperioddefinitionid, ppd.definitionnamesnapshot
        FROM payroll.payrolldraftlines dl
        LEFT JOIN payroll.payrollperioddefinitions ppd
          ON ppd.payrollperioddefinitionid = dl.payrollperioddefinitionid
        JOIN core.drivers d ON d.driverid = dl.driverid
        JOIN core.employees e ON e.employeeid = d.employeeid
        WHERE dl.payrollperiodid = :period_id AND dl.companyid = :company_id
          AND dl.branchid = :branch_id AND dl.status != 'Void'
        ORDER BY dl.driverid, dl.workdate, dl.draftlineid
    """), {"period_id": period["payrollperiodid"], "company_id": period["companyid"],
          "branch_id": period["branchid"]})).mappings().all()
    drivers: dict[int, dict[str, Any]] = {}
    work: list[dict[str, Any]] = []
    for row in rows:
        driver_id = int(row["driverid"])
        drivers.setdefault(driver_id, {"driver_id": driver_id, "driver_code": row["drivercode"],
                                       "driver_name": row["drivername"]})
        if row["sourcetype"] != "System" and row["payrollperioddefinitionid"] is not None:
            work.append({"driver_id": driver_id, "work_date": row["workdate"],
                         "payroll_period_definition_id": int(row["payrollperioddefinitionid"]),
                         "definition_name": row["definitionnamesnapshot"],
                         "quantity": row["quantity"], "line_scope": "Daily"})
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
        summary = PeriodSummary.model_construct(
            payroll_period_id=int(period["payrollperiodid"]), branch_id=int(period["branchid"]),
            branch_name=period["branchname"], period_code=period["periodcode"], period_name=period["periodname"],
            period_type=period["periodtype"], start_date=period["startdate"], end_date=period["enddate"], status=period["status"],
        )
        packet = await period_calculation._build_live_calculation_packet(summary, int(period["companyid"]), db)
        # Preserve the existing public `source_type`/`source_id` provenance
        # values exactly as before (e.g. "Manual" for a manual DraftLine) —
        # only the effective money amount needs the same fallback the
        # packet's own totals already use. `snapshot_source_type` is added
        # as a separate, additive classification field (DraftLine /
        # StatusEntryState / BonusEvent / System) for internal aggregation
        # (see _pay_item_amounts); it does not replace source_type.
        lines = [
            {
                "driver_id": line.driver_id,
                "source_type": line.source_type,
                "source_id": line.source_id,
                "line_type": line.line_type,
                "line_scope": line.line_scope,
                "work_date": line.work_date,
                "payroll_period_definition_id": line.payroll_period_definition_id,
                "definition_name": line.definition_name,
                "calculation_status": line.calculation_status,
                "quantity": line.quantity,
                "resolved_rate_amount": line.resolved_rate_amount,
                "calculated_amount": (
                    line.snapshot_calculated_amount
                    if line.snapshot_calculated_amount is not None
                    else line.calculated_amount
                ),
                "bonus_event_id": line.bonus_event_id,
                "snapshot_source_type": line.snapshot_source_type or line.source_type,
            }
            for driver in packet.drivers for line in driver.lines
        ]
        totals = {d.driver_id: {"daily_pay": d.daily_pay, "status_pay": d.status_pay,
                  "minimum_adjustment": d.minimum_adjustment,
                  "maximum_adjustment": d.maximum_adjustment, "bonus_total": d.bonus_total,
                  "total_pay": d.expected_pay, "driver_code": d.driver_code, "driver_name": d.driver_name}
                  for d in packet.drivers}
        return lines, totals, packet.blockers, packet.warnings, True, None, None
    # Submitted, approved and final authorities freeze calculation evidence, which target
    # payroll does not have yet. They are refused rather than rebuilt from mutable state.
    raise evidence_not_ready()


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
) -> tuple[list[dict[str, Any]], dict[str, Decimal] | None]:
    totals: dict[int, Decimal] = defaultdict(lambda: Decimal("0"))
    for row in work_rows:
        quantity = row.get("quantity")
        if quantity is not None:
            totals[int(row["payroll_period_definition_id"])] += Decimal(str(quantity))
    work_totals = [
        {"payroll_period_definition_id": definition_id, "quantity": quantity}
        for definition_id, quantity in sorted(totals.items())
    ]
    if not financials_available:
        return work_totals, None
    names = (
        "daily_pay", "status_pay", "minimum_adjustment",
        "maximum_adjustment", "bonus_total", "total_pay",
    )
    pay_totals = {
        name: sum((Decimal(str(total[name])) for total in financial_totals.values()), Decimal("0"))
        for name in names
    }
    pay_totals["gross_pay"] = pay_totals["daily_pay"] + pay_totals["status_pay"]
    return work_totals, pay_totals


async def build_report(*, report_type: str, period_id: int, company_id: int, user_id: int, db: AsyncConnection) -> dict[str, Any]:
    period = await _period_context(period_id=period_id, company_id=company_id, user_id=user_id, db=db)
    authority = await resolve_report_financial_authority(db=db, period_id=period_id, company_id=company_id, branch_id=int(period["branchid"]))
    if authority.authority_kind is ReportAuthorityKind.UNAVAILABLE:
        raise _unavailable("REPORT_UNAVAILABLE", "Cancelled payroll periods have no calculation reports.")
    columns = await _columns(period, db)
    definition_columns = await _definition_columns(period, db)
    definition_column_ids = [c["payroll_period_definition_id"] for c in definition_columns]
    lines, financial_totals, blockers, warnings, financials_available, snapshot_id, snapshot_hash = await _financial_packet(authority, period, db)
    per_driver_definitions: dict[int, dict[int, Decimal]] = {}
    definition_total_map: dict[int, Decimal] = {cid: Decimal("0") for cid in definition_column_ids}
    # Period Work is operational (quantities/Status), not financial, and stays
    # independent of the per-definition financial integrity invariant.
    if financials_available and report_type != "period-work":
        per_driver_definitions, definition_total_map = _definition_amounts(
            lines, definition_column_ids)
    operational_drivers, work_rows = await _operational_rows(period, db)
    drivers = {**operational_drivers}
    for driver_id, total in financial_totals.items():
        drivers.setdefault(driver_id, {"driver_id": driver_id, "driver_code": total.get("driver_code"), "driver_name": total.get("driver_name")})
    evidence_available, evidence_version, evidence_hash, statuses, bonuses = (False, None, None, [], [])
    if authority.authority_kind is ReportAuthorityKind.LIVE:
        statuses = await _live_statuses(period, db)
        bonuses = [{"bonus_event_id": int(r["payrollbonuseventid"]), "driver_id": int(r["driverid"]),
                    "amount": Decimal(str(r["amount"])), "reason": r["reason"], "notes": r["notes"],
                    "data_revision": int(r["datarevision"]), "creator_user_id": r["createdbyuserid"],
                    "creator_display_name": r["creatordisplaynamesnapshot"], "created_at_utc": r["createdatutc"]}
                   for r in await period_calculation._load_active_bonus_events(int(period["payrollperiodid"]), company_id, db)]
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
        pay = None
        if total is not None:
            item_amounts = per_driver_definitions.get(driver_id, {})
            gross_pay = Decimal(str(total["daily_pay"])) + Decimal(str(total["status_pay"]))
            pay = {
                **total,
                "gross_pay": gross_pay,
                # Period Work never carries per-item money: an empty list
                # here means "not computed for this view", never a verified
                # zero for every column.
                "definition_amounts": [] if report_type == "period-work" else [
                    {"payroll_period_definition_id": cid,
                     "amount": item_amounts.get(cid, Decimal("0"))}
                    for cid in definition_column_ids
                ],
                "financial_lines": by_driver_lines[driver_id],
            }
        work = {"daily_rows": by_driver_work[driver_id], "status_entries": by_driver_status[driver_id],
                "status_summaries": summaries.get(driver_id, [])}
        result_drivers.append({**drivers[driver_id], "work": work, "pay": pay, "bonus_events": by_driver_bonus[driver_id]})
    if report_type == "period-pay" and not financials_available:
        raise _unavailable("REPORT_FINANCIALS_UNAVAILABLE", "Prepared payroll periods have no financial report authority.")
    work_totals, pay_totals = _report_totals(work_rows, financial_totals, financials_available)
    definition_totals = None if not financials_available or report_type == "period-work" else [
        {"payroll_period_definition_id": cid, "amount": definition_total_map.get(cid, Decimal("0"))}
        for cid in definition_column_ids
    ]
    currency = await get_company_currency(company_id, db)
    metadata = {"currency_code": None if currency is None else currency.code,
                "currency_minor_unit_digits": None if currency is None else currency.minor_unit_digits,
                "period_id": int(period["payrollperiodid"]), "period_code": period["periodcode"], "period_name": period["periodname"],
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
        "definition_columns": definition_columns,
        "drivers": result_drivers,
        "work_totals": work_totals,
        "pay_totals": pay_totals,
        "definition_totals": definition_totals,
    }
