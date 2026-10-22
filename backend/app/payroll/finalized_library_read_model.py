"""Immutable finalized-payroll overview and calculation-report read model.

Every read here uses only frozen evidence: the one snapshot the FinalLines name, verified
(version, persisted-row hash, FinalLines projection), with its frozen labels, money and
evidence. Nothing is rebuilt from live rates, definitions, Status configuration or Bonus rows.
Audit chronology is owned by finalized_audit.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.access.branch_access import check_branch_access
from app.access.policy import require_non_driver_subject
from app.company_currency import frozen_currency
from app.db.sql import build_in_clause
from app.payroll import status_evidence
from app.payroll.eligibility import _is_snapshot_row_eligible_for_workdate
from app.payroll.finalized_context import (
    FINALIZED_STATUSES as _FINALIZED_STATUSES,
)
from app.payroll.finalized_context import (
    availability as _availability,
)
from app.payroll.finalized_context import (
    finalized_error as _unavailable,
)
from app.payroll.finalized_context import (
    get_finalized_period_context as _period_context,
)
from app.payroll.finalized_context import (
    resolve_originating_snapshot as _originating_snapshot,
)
from app.payroll.report_components import (
    aggregate_definition_amounts,
    frozen_financial_packet,
    frozen_operational_rows,
    frozen_status_only_identities,
    load_financial_definition_columns,
    load_report_columns,
    summarize_status_entries,
)

_REPORT_TYPES = {"drivers", "period-work", "period-pay", "mixed"}


async def _approved_currencies(periods: list[dict[str, Any]], db: AsyncConnection) -> dict[int, Any]:
    """Resolve a page of finalized currencies with constant query count."""
    if not periods:
        return {}
    period_ids = [int(period["period_id"]) for period in periods]
    period_clause, period_params = build_in_clause(period_ids, "currency_period")
    final_rows = (await db.execute(text(f"""
        SELECT payrollperiodid AS period_id,
               COUNT(*) AS row_count,
               COUNT(DISTINCT (currencycode, currencyminorunitdigits)) AS pair_count,
               MIN(currencycode) AS currency_code,
               MIN(currencyminorunitdigits) AS minor_unit_digits
        FROM payroll.payrollfinallines
        WHERE companyid = :company_id AND payrollperiodid IN ({period_clause})
        GROUP BY payrollperiodid
    """), {"company_id": periods[0]["company_id"], **period_params})).mappings().all()
    by_final_period = {int(row["period_id"]): row for row in final_rows}

    missing_ids = [period_id for period_id in period_ids if period_id not in by_final_period]
    approved_by_period: dict[int, Any] = {}
    if missing_ids:
        missing_clause, missing_params = build_in_clause(missing_ids, "approved_currency_period")
        approved_rows = (await db.execute(text(f"""
            SELECT s.payrollperiodid AS period_id,
                   COUNT(*) AS row_count,
                   COUNT(DISTINCT (s.currencycode, s.currencyminorunitdigits)) AS pair_count,
                   MIN(s.currencycode) AS currency_code,
                   MIN(s.currencyminorunitdigits) AS minor_unit_digits
            FROM review.managerreviewitems ri
            JOIN payroll.payrollcalculationsnapshots s
              ON s.payrollcalculationsnapshotid = ri.payrollcalculationsnapshotid
             AND s.companyid = ri.companyid AND s.branchid = ri.branchid
            WHERE ri.companyid = :company_id
              AND ri.entityschema = 'payroll' AND ri.entityname = 'PayrollPeriods'
              AND ri.entityid = s.payrollperiodid::text
              AND s.payrollperiodid IN ({missing_clause})
              AND ri.requesttype = 'PeriodApproval' AND ri.status = 'Approved'
            GROUP BY s.payrollperiodid
        """), {"company_id": periods[0]["company_id"], **missing_params})).mappings().all()
        approved_by_period = {int(row["period_id"]): row for row in approved_rows}

    resolved: dict[int, Any] = {}
    for period in periods:
        period_id = int(period["period_id"])
        row = by_final_period.get(period_id)
        if row is not None:
            if int(row["pair_count"]) != 1:
                raise _unavailable("SNAPSHOT_CURRENCY_MISMATCH", "FinalLines contain inconsistent frozen currency.")
            resolved[period_id] = frozen_currency(row["currency_code"], row["minor_unit_digits"])
            continue
        row = approved_by_period.get(period_id)
        if row is None or int(row["row_count"]) != 1 or int(row["pair_count"]) != 1:
            raise _unavailable("SNAPSHOT_CURRENCY_REQUIRED", "Approved snapshot currency is unavailable.")
        resolved[period_id] = frozen_currency(row["currency_code"], row["minor_unit_digits"])
    return resolved


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
    await require_non_driver_subject(company_id, user_id, db)
    can_see_all, accessible_branch_ids = await check_branch_access(company_id, user_id, db)

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

    candidate_clause, candidate_params = build_in_clause(candidate_branch_ids, "candidate_branch")
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

    branch_clause, branch_params = build_in_clause(permitted_branch_ids, "finalized_branch")
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
    items = [dict(row) for row in result.mappings().all()]
    currencies = await _approved_currencies([
        {"period_id": item["period_id"], "company_id": company_id}
        for item in items
    ], db)
    for item in items:
        currency = currencies[int(item["period_id"])]
        item["currency_code"] = currency.code
        item["currency_minor_unit_digits"] = currency.minor_unit_digits
    return items


def _financial_summary(snapshot: dict[str, Any] | None) -> dict[str, Any]:
    """The finalized totals of the verified snapshot, or none when its provenance is unavailable.

    The totals are the snapshot's frozen ExpectedPay and the number of its official components
    (one FinalLine each); the FinalLines were proved equal to that projection, so these are the
    ledger's totals. Nothing is summed from FinalLines that did not verify.
    """
    if snapshot is None:
        return {"total_pay": None, "final_line_count": 0, "driver_count": 0}
    content = snapshot["content"]
    return {
        "total_pay": Decimal(str(content["TotalExpectedPay"])),
        "final_line_count": sum(len(total["Components"]) for total in content["DriverTotals"]),
        "driver_count": len(content["DriverTotals"]),
    }


async def _columns(period: dict[str, Any], db: AsyncConnection) -> tuple[list[dict[str, Any]], dict[str, str | None]]:
    """Read the frozen period definition layout only; never current PayDefinitions."""
    columns = await load_report_columns(period, db)
    return columns, _availability("AVAILABLE")


def _frozen_report_totals(
    snapshot: dict[str, Any] | None,
) -> tuple[list[dict[str, Any]], dict[int, dict[str, Any]]]:
    """Financial lines and driver totals from the verified snapshot content (FinalLines equal
    its component projection, so these official amounts are the ledger's)."""
    if snapshot is None:
        return [], {}
    return frozen_financial_packet(snapshot["content"])


def _snapshot_evidence(
    snapshot: dict[str, Any],
) -> tuple[bool, int | None, str | None, list[dict[str, Any]], list[dict[str, Any]]]:
    """The report evidence verified with the snapshot (never queried unverified)."""
    return status_evidence.report_evidence_from_content(snapshot["content"])


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
        ("payroll.payrollperioddefinitions", "PERIOD_DEFINITION_SNAPSHOT_UNAVAILABLE"),
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


async def _finalized_rate_evidence_availability(
    period: dict[str, Any], snapshot: dict[str, Any] | None,
    provenance: dict[str, str | None], db: AsyncConnection,
) -> dict[str, str | None]:
    """Rate evidence is part of every verified snapshot; without the snapshot it is unavailable."""
    if snapshot is None:
        return _availability("UNAVAILABLE", provenance["reason_code"])
    return _availability("AVAILABLE")


async def _finalized_used_rate_definitions(
    period: dict[str, Any], snapshot: dict[str, Any] | None,
    provenance: dict[str, str | None], db: AsyncConnection,
) -> tuple[list[dict[str, Any]], dict[str, str | None]]:
    """The rates the snapshot used (target schedules, legacy Status rates) and its Min/Max
    rule evidence, exactly as frozen. Pay definition labels come from the frozen period layout."""
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
        SELECT d.payrollcalculationsnapshotusedratedefinitionid, d.driverassignmentid, d.driverid,
               dt.drivernamesnapshot, dt.drivernumbersnapshot,
               d.evidencekind, d.sourcetypesnapshot,
               d.driverratescheduleid, d.ratedefinitionid, d.paydefinitionid,
               d.statusratecolumnid,
               d.ratecomponentdefinitionid, ppd.definitioncodesnapshot, ppd.definitionnamesnapshot,
               ppd.unitsnapshot, d.rateamountsnapshot,
               d.effectivefromsnapshot, d.effectivetosnapshot, d.ratestatussnapshot,
               d.definitionfingerprint, u.snapshot_line_ids, u.line_use_count
        FROM payroll.payrollcalculationsnapshotusedratedefinitions d
        JOIN usage u ON u.usedratedefinitionid = d.payrollcalculationsnapshotusedratedefinitionid
        LEFT JOIN payroll.payrollcalculationdrivertotals dt
          ON dt.payrollcalculationsnapshotid = d.payrollcalculationsnapshotid
         AND dt.companyid = d.companyid AND dt.branchid = d.branchid AND dt.driverassignmentid = d.driverassignmentid
        LEFT JOIN payroll.payrollperioddefinitions ppd
          ON ppd.payrollperiodid = d.payrollperiodid AND ppd.paydefinitionid = d.paydefinitionid
        WHERE d.payrollcalculationsnapshotid = :snapshot_id
          AND d.companyid = :company_id AND d.branchid = :branch_id
          AND d.payrollperiodid = :period_id
        ORDER BY d.driverassignmentid, d.evidencekind, d.paydefinitionid NULLS LAST,
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
    # Status rate columns are labelled by the name frozen on the Status component, never by the
    # live column.
    column_names = {
        (total["DriverAssignmentID"], component["StatusRateColumnID"]):
            component["StatusRateColumnNameSnapshot"]
        for total in snapshot["content"]["DriverTotals"]
        for component in total["Components"] if component["ComponentKind"] == "STATUS"
    }
    definitions = [{
        "used_rate_definition_id": int(row["payrollcalculationsnapshotusedratedefinitionid"]),
        "driver_assignment_id": int(row["driverassignmentid"]), "driver_id": int(row["driverid"]),
        "driver_name": row["drivernamesnapshot"],
        "driver_number": row["drivernumbersnapshot"], "evidence_kind": row["evidencekind"],
        "source_type": row["sourcetypesnapshot"],
        "driver_rate_schedule_id": row["driverratescheduleid"],
        "rate_definition_id": row["ratedefinitionid"], "pay_definition_id": row["paydefinitionid"],
        "rate_component_definition_id": row["ratecomponentdefinitionid"],
        "pay_definition_code": row["definitioncodesnapshot"],
        "pay_definition_label": row["definitionnamesnapshot"],
        "status_rate_column_id": row["statusratecolumnid"],
        "status_rate_column_name": column_names.get(
            (int(row["driverassignmentid"]), row["statusratecolumnid"])),
        "unit_name": row["unitsnapshot"],
        "rate_amount": row["rateamountsnapshot"],
        "effective_from": row["effectivefromsnapshot"], "effective_to": row["effectivetosnapshot"],
        "rate_status": row["ratestatussnapshot"],
        "definition_fingerprint": row["definitionfingerprint"],
        "snapshot_line_ids": [int(value) for value in row["snapshot_line_ids"]],
        "line_use_count": int(row["line_use_count"]),
    } for row in rows]
    # Min/Max rules: the evaluation evidence of each Driver total is their one authority.
    for total in snapshot["content"]["DriverTotals"]:
        evaluation = total["MinMax"]
        for rule in evaluation["Rules"]:
            definitions.append({
                "used_rate_definition_id": int(rule["PayrollCalculationSnapshotMinMaxRuleID"]),
                "driver_assignment_id": total["DriverAssignmentID"], "driver_id": total["DriverID"],
                "driver_name": total["DriverNameSnapshot"],
                "driver_number": total["DriverNumberSnapshot"],
                "evidence_kind": "DriverPayRule", "source_type": "MinMaxEvaluation",
                "driver_pay_rule_id": rule["DriverPayRuleID"], "rule_type": rule["RuleType"],
                "rule_amount": rule["Amount"], "rule_status": rule["RuleStatus"],
                "rule_prorated_amount": rule["ProratedAmount"],
                "min_max_status": evaluation["Status"],
                "effective_from": rule["EffectiveFrom"], "effective_to": rule["EffectiveTo"],
                "snapshot_line_ids": [], "line_use_count": 0,
            })
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
        SELECT b.payrollbonuseventid, b.driverassignmentid, b.driverid, dt.drivernamesnapshot, dt.drivernumbersnapshot,
               b.amount, b.reason, b.notes, b.datarevision, b.createdbyuserid,
               b.creatordisplaynamesnapshot, b.createdatutc
        FROM payroll.payrollcalculationsnapshotbonusevents b
        LEFT JOIN payroll.payrollcalculationdrivertotals dt
          ON dt.payrollcalculationsnapshotid = b.payrollcalculationsnapshotid
         AND dt.companyid = b.companyid AND dt.branchid = b.branchid AND dt.driverassignmentid = b.driverassignmentid
        WHERE b.payrollcalculationsnapshotid = :snapshot_id
          AND b.companyid = :company_id AND b.branchid = :branch_id
          AND b.payrollperiodid = :period_id
        ORDER BY b.driverassignmentid, b.payrollbonuseventid
    """), {
        "snapshot_id": snapshot["payrollcalculationsnapshotid"],
        "period_id": period["payrollperiodid"], "company_id": period["companyid"],
        "branch_id": period["branchid"],
    })).mappings().all()
    return [{
        "bonus_event_id": int(row["payrollbonuseventid"]), "driver_assignment_id": int(row["driverassignmentid"]),
        "driver_id": int(row["driverid"]),
        "driver_name": row["drivernamesnapshot"], "driver_number": row["drivernumbersnapshot"],
        "amount": row["amount"], "reason": row["reason"], "notes": row["notes"],
        "data_revision": int(row["datarevision"]), "creator_user_id": row["createdbyuserid"],
        "creator_display_name": row["creatordisplaynamesnapshot"],
        "created_at_utc": row["createdatutc"],
    } for row in rows], _availability("EMPTY" if not rows else "AVAILABLE")


def _work_totals(work_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    totals: dict[int, Decimal] = defaultdict(lambda: Decimal("0"))
    for row in work_rows:
        if row["quantity"] is not None:
            totals[int(row["payroll_period_definition_id"])] += Decimal(str(row["quantity"]))
    return [
        {"payroll_period_definition_id": definition_id, "quantity": quantity}
        for definition_id, quantity in sorted(totals.items())
    ]


def _pay_totals(totals: dict[int, dict[str, Any]]) -> dict[str, Decimal]:
    names = (
        "daily_pay", "status_pay", "minimum_adjustment",
        "maximum_adjustment", "bonus_total", "total_pay",
    )
    pay_totals = {
        name: sum((Decimal(str(total[name])) for total in totals.values()), Decimal("0"))
        for name in names
    }
    pay_totals["gross_pay"] = pay_totals["daily_pay"] + pay_totals["status_pay"]
    return pay_totals


async def build_overview(
    *, period_id: int, company_id: int, user_id: int, db: AsyncConnection,
) -> dict[str, Any]:
    period = await _period_context(period_id=period_id, company_id=company_id, user_id=user_id, db=db)
    snapshot, provenance = await _originating_snapshot(period, db)
    financial_summary = _financial_summary(snapshot)
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
    # Money exists only behind a verified snapshot: without one the financial sections are
    # unavailable for the same reason as the provenance, never an AVAILABLE zero.
    financial_availability = (
        _availability("UNAVAILABLE", provenance["reason_code"]) if snapshot is None
        else _availability("AVAILABLE" if financial_summary["final_line_count"] else "EMPTY")
    )
    return {
        "period_id": int(period["payrollperiodid"]), "period_code": period["periodcode"],
        "period_name": period["periodname"], "period_status": period["status"],
        "currency_code": period["currency_code"], "currency_minor_unit_digits": period["currency_minor_unit_digits"],
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
            "financials": financial_availability, "snapshot_provenance": provenance,
            "report_evidence": evidence, "reports": financial_availability,
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
    definition_columns = await load_financial_definition_columns(period, db)
    definition_column_ids = [c["payroll_period_definition_id"] for c in definition_columns]
    snapshot, provenance = await _originating_snapshot(period, db)
    final_lines, financial_totals = _frozen_report_totals(snapshot)
    per_driver_definitions: dict[int, dict[int, Decimal]] = {}
    definition_total_map: dict[int, Decimal] = {cid: Decimal("0") for cid in definition_column_ids}
    # Period Work is operational (quantities/Status), not financial, and must
    # stay independent of the per-Pay-Item financial integrity invariant.
    if report_type != "period-work":
        # The line sums keep the definition-integrity check; the amounts shown are the
        # official ones frozen once per DriverAssignment x definition.
        aggregate_definition_amounts(final_lines, definition_column_ids)
        for assignment_id, total in financial_totals.items():
            amounts = dict(total.get("official_definition_amounts", {}))
            per_driver_definitions[assignment_id] = amounts
            for definition_id, amount in amounts.items():
                if definition_id in definition_total_map:
                    definition_total_map[definition_id] += amount
    frozen_drivers: dict[int, dict[str, Any]] = {}
    work_rows: list[dict[str, Any]] = []
    statuses: list[dict[str, Any]] = []
    bonuses: list[dict[str, Any]] = []
    evidence_available = False
    evidence_version: int | None = None
    evidence_hash: str | None = None
    evidence_availability = _availability("UNAVAILABLE", provenance["reason_code"])
    if snapshot is not None:
        frozen_drivers, work_rows = frozen_operational_rows(
            snapshot["content"], await frozen_status_only_identities(
                snapshot["content"], period_id=int(period["payrollperiodid"]),
                company_id=int(period["companyid"]), branch_id=int(period["branchid"]), db=db))
        evidence_available, evidence_version, evidence_hash, statuses, bonuses = _snapshot_evidence(
            snapshot,
        )
        evidence_availability = (
            _availability("AVAILABLE") if evidence_available
            else _availability("UNAVAILABLE", "LEGACY_NOT_CAPTURED")
        )
    drivers = dict(frozen_drivers)
    for driver_assignment_id in financial_totals:
        drivers.setdefault(driver_assignment_id, {
            "driver_assignment_id": driver_assignment_id,
            "driver_id": financial_totals[driver_assignment_id].get("driver_id"),
            "driver_number": None, "driver_name": None,
        })
    by_driver_work: dict[int, list[dict[str, Any]]] = defaultdict(list)
    by_driver_lines: dict[int, list[dict[str, Any]]] = defaultdict(list)
    by_driver_status: dict[int, list[dict[str, Any]]] = defaultdict(list)
    by_driver_bonus: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in work_rows:
        by_driver_work[row["driver_assignment_id"]].append(row)
    for line in final_lines:
        by_driver_lines[line["driver_assignment_id"]].append(line)
    for item in statuses:
        by_driver_status[item["driver_assignment_id"]].append(item)
    for item in bonuses:
        by_driver_bonus[item["driver_assignment_id"]].append(item)
    summaries = summarize_status_entries(statuses)
    result_drivers = []
    for driver_assignment_id in sorted(drivers):
        total = financial_totals.get(driver_assignment_id)
        pay = None
        if total is not None:
            item_amounts = per_driver_definitions.get(driver_assignment_id, {})
            gross_pay = Decimal(str(total["daily_pay"])) + Decimal(str(total["status_pay"]))
            pay = {
                **{k: v for k, v in total.items() if k != "official_definition_amounts"},
                "gross_pay": gross_pay,
                # Period Work never carries per-item money: an empty list
                # here means "not computed for this view", never a verified
                # zero for every column.
                "definition_amounts": [] if report_type == "period-work" else [
                    {"payroll_period_definition_id": cid,
                     "amount": item_amounts.get(cid, Decimal("0"))}
                    for cid in definition_column_ids
                ],
                "financial_lines": by_driver_lines[driver_assignment_id],
            }
        result_drivers.append({
            **drivers[driver_assignment_id],
            "work": {
                "daily_rows": by_driver_work[driver_assignment_id],
                "status_entries": by_driver_status[driver_assignment_id],
                "status_summaries": summaries.get(driver_assignment_id, []),
            },
            "pay": pay,
            "bonus_events": by_driver_bonus[driver_assignment_id],
        })
    financials_available = snapshot is not None
    financial_availability = (
        _availability("UNAVAILABLE", provenance["reason_code"]) if snapshot is None
        else _availability("AVAILABLE" if final_lines else "EMPTY")
    )
    return {
        "metadata": {
            "period_id": int(period["payrollperiodid"]), "period_code": period["periodcode"],
            "period_name": period["periodname"], "period_status": period["status"],
        "currency_code": period["currency_code"], "currency_minor_unit_digits": period["currency_minor_unit_digits"],
            "branch_id": int(period["branchid"]), "report_type": report_type,
            "financials_available": financials_available,
            "snapshot_id": None if snapshot is None else int(snapshot["payrollcalculationsnapshotid"]),
            "revision_number": None if snapshot is None else int(snapshot["revisionnumber"]),
            "snapshot_hash": None if snapshot is None else str(snapshot["snapshothash"]),
            "report_evidence_available": evidence_available,
            "report_evidence_version": evidence_version, "report_evidence_hash": evidence_hash,
            "section_availability": {
                "financials": financial_availability,
                "snapshot_provenance": provenance, "columns": columns_availability,
                "report_evidence": evidence_availability,
            },
            "generated_at_utc": datetime.now(UTC),
        },
        "columns": columns, "definition_columns": definition_columns, "drivers": result_drivers,
        "work_totals": _work_totals(work_rows),
        "pay_totals": _pay_totals(financial_totals) if financials_available else None,
        "definition_totals": None if report_type == "period-work" or not financials_available else [
            {"payroll_period_definition_id": cid,
             "amount": definition_total_map.get(cid, Decimal("0"))}
            for cid in definition_column_ids
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
        SELECT pde.driverassignmentid,
               (SELECT a.driverid FROM core.driverassignments a
                 WHERE a.driverassignmentid = pde.driverassignmentid) AS permanentdriverid,
               pde.drivernumbersnapshot, pde.drivernamesnapshot,
               pde.iseligibleforperiod, pde.eligibilityreasoncode,
               pde.hiredatesnapshot, pde.terminationdatesnapshot,
               pde.drivereffectivefromatsnapshot, pde.drivereffectivetoatsnapshot,
               pde.branchtimezoneidsnapshot, pde.eligibleworkdates,
               dt.drivernumbersnapshot AS total_driver_number,
               dt.drivernamesnapshot AS total_driver_name
        FROM payroll.payrollperioddrivereligibility pde
        LEFT JOIN payroll.payrollcalculationdrivertotals dt
          ON dt.payrollcalculationsnapshotid = :snapshot_id
         AND dt.companyid = pde.companyid AND dt.branchid = pde.branchid
         AND dt.driverassignmentid = pde.driverassignmentid
        WHERE pde.payrollperiodid = :period_id
          AND pde.companyid = :company_id AND pde.branchid = :branch_id
          AND pde.iseligibleforperiod = TRUE
        ORDER BY pde.driverassignmentid
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
            driver_assignment_id = int(row["driverassignmentid"])
            result[driver_assignment_id] = {
                "driver_assignment_id": driver_assignment_id,
                "driver_id": int(row["permanentdriverid"]),
                "driver_number": row["drivernumbersnapshot"] or row["total_driver_number"],
                "driver_name": row["drivernamesnapshot"] or row["total_driver_name"],
                "eligible_days": days,
            }
    return result, _availability("EMPTY" if not result else "AVAILABLE")


def _finalized_status_entries(
    snapshot: dict[str, Any] | None,
) -> tuple[list[dict[str, Any]], dict[str, str | None]]:
    entries: list[dict[str, Any]] = []
    if snapshot is not None and snapshot["reportevidenceversion"] is not None:
        entries = status_evidence.status_entries_from_content(snapshot["content"])
    return entries, status_evidence.status_evidence_availability(snapshot, entries)


async def _finalized_normal_work_pairs(
    period: dict[str, Any], snapshot_id: int, db: AsyncConnection,
) -> tuple[set[tuple[int, date]] | None, dict[str, str | None]]:
    """Frozen normal work: ordinary source lines of the snapshot against a definition that was
    active in the period. Work identifies a day, never money."""
    rows = (await db.execute(text("""
        SELECT DISTINCT dt.driverassignmentid, sl.workdate
        FROM payroll.payrollcalculationsnapshotlines sl
        JOIN payroll.payrollcalculationdrivertotals dt
          ON dt.payrollcalculationdrivertotalid = sl.payrollcalculationdrivertotalid
        JOIN payroll.payrollperioddefinitions ppd
          ON ppd.payrollperioddefinitionid = sl.payrollperioddefinitionid
         AND ppd.payrollperiodid = :period_id
        WHERE dt.payrollcalculationsnapshotid = :snapshot_id
          AND dt.companyid = :company_id AND dt.branchid = :branch_id
          AND sl.sourcetype = 'DraftLine' AND sl.workdate IS NOT NULL
          AND sl.quantity IS NOT NULL AND sl.quantity <> 0
          AND ppd.isactiveinperiod = TRUE
    """), {
        "snapshot_id": snapshot_id, "period_id": period["payrollperiodid"],
        "company_id": period["companyid"], "branch_id": period["branchid"],
    })).mappings().all()
    return {(int(row["driverassignmentid"]), row["workdate"]) for row in rows}, _availability("AVAILABLE")


async def build_finalized_off_drivers(
    *, period_id: int, company_id: int, user_id: int, db: AsyncConnection,
) -> dict[str, Any]:
    """Build P6B's full-period immutable Off/Status source projection."""
    period = await _period_context(period_id=period_id, company_id=company_id, user_id=user_id, db=db)
    snapshot, provenance = await _originating_snapshot(period, db)
    statuses, status_availability = _finalized_status_entries(snapshot)
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
        driver_assignment_id: (driver["driver_name"], driver["driver_number"])
        for driver_assignment_id, driver in drivers.items()
    }
    status_by_driver_day = {(row["driver_assignment_id"], row["work_date"]): row for row in statuses}
    frozen_statuses = [{
        **row,
        "driver_name": identities.get(row["driver_assignment_id"], (None, None))[0],
        "driver_number": identities.get(row["driver_assignment_id"], (None, None))[1],
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
                if status_by_driver_day.get((driver["driver_assignment_id"], work_date), {}).get("is_off_reason")
                and (driver["driver_assignment_id"], work_date) not in normal_work
            )
            if off_days == len(days):
                fully_off.append({
                    "driver_assignment_id": driver["driver_assignment_id"], "driver_id": driver["driver_id"],
                    "driver_name": driver["driver_name"] or "",
                    "driver_number": driver["driver_number"], "eligible_scheduled_day_count": len(days),
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
        "currency_code": period["currency_code"], "currency_minor_unit_digits": period["currency_minor_unit_digits"],
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
        "currency_code": period["currency_code"], "currency_minor_unit_digits": period["currency_minor_unit_digits"],
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
