"""Shared authorized period context and exact finalized snapshot provenance."""
from __future__ import annotations

from typing import Any

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.access.branch_access import check_branch_access
from app.access.permissions import check_permission
from app.access.policy import require_non_driver_subject
from app.company_currency import frozen_currency
from app.payroll.final_line_projection import verify_final_lines
from app.payroll.snapshot_verification import SnapshotEvidenceError, verify_snapshot

FINALIZED_STATUSES = {"Locked", "Archived"}


def availability(state: str, reason_code: str | None = None) -> dict[str, str | None]:
    return {"state": state, "reason_code": reason_code}


def finalized_error(code: str, message: str) -> HTTPException:
    return HTTPException(status_code=422, detail=f"{code}: {message}")


async def _approved_currency(period: dict[str, Any], db: AsyncConnection):
    """Resolve finalized denomination from the exact approved snapshot."""
    final_rows = (await db.execute(text("""
        SELECT DISTINCT currencycode, currencyminorunitdigits
        FROM payroll.payrollfinallines
        WHERE companyid = :cid AND branchid = :bid AND payrollperiodid = :pid
    """), {"cid": period["companyid"], "bid": period["branchid"],
           "pid": period["payrollperiodid"]})).mappings().all()
    if len(final_rows) > 1:
        raise finalized_error("SNAPSHOT_CURRENCY_MISMATCH", "FinalLines contain inconsistent frozen currency.")
    if final_rows:
        return frozen_currency(final_rows[0]["currencycode"], final_rows[0]["currencyminorunitdigits"])
    rows = (await db.execute(text("""
        SELECT s.currencycode, s.currencyminorunitdigits
        FROM review.managerreviewitems ri
        JOIN payroll.payrollcalculationsnapshots s
          ON s.payrollcalculationsnapshotid = ri.payrollcalculationsnapshotid
         AND s.companyid = ri.companyid AND s.branchid = ri.branchid
         AND s.payrollperiodid = :pid
        WHERE ri.companyid = :cid AND ri.branchid = :bid
          AND ri.entityschema = 'payroll' AND ri.entityname = 'PayrollPeriods'
          AND ri.entityid = :entity_id AND ri.requesttype = 'PeriodApproval'
          AND ri.status = 'Approved'
    """), {"cid": period["companyid"], "bid": period["branchid"],
           "pid": period["payrollperiodid"], "entity_id": str(period["payrollperiodid"])})).mappings().all()
    if len(rows) != 1:
        raise finalized_error("SNAPSHOT_CURRENCY_REQUIRED", "Approved snapshot currency is unavailable.")
    return frozen_currency(rows[0]["currencycode"], rows[0]["currencyminorunitdigits"])


async def get_finalized_period_context(
    *, period_id: int, company_id: int, user_id: int, db: AsyncConnection,
) -> dict[str, Any]:
    """Authorize a finalized-library request before loading child data."""
    await require_non_driver_subject(company_id, user_id, db)
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
    can_see_all, branch_ids = await check_branch_access(company_id, user_id, db)
    if not can_see_all and int(row["branchid"]) not in branch_ids:
        raise HTTPException(status_code=403, detail="Access denied to this period's branch.")
    await check_permission(company_id, user_id, int(row["branchid"]), "ledger.view", db)
    if str(row["status"]) not in FINALIZED_STATUSES:
        raise finalized_error(
            "FINALIZED_LIBRARY_UNAVAILABLE",
            "Finalized payroll information is available only for Locked or Archived periods.",
        )
    result = dict(row)
    currency = await _approved_currency(result, db)
    result["currency_code"] = currency.code
    result["currency_minor_unit_digits"] = currency.minor_unit_digits
    return result


async def resolve_originating_snapshot(
    period: dict[str, Any], db: AsyncConnection,
) -> tuple[dict[str, Any] | None, dict[str, str | None]]:
    """The one snapshot a finalized period's FinalLines name, verified; never a "latest".

    The snapshot is selected by ``PayrollFinalLines.PayrollCalculationSnapshotID`` (exactly one
    snapshot per finalized period), then verified: calculation version, the hash recomputed from
    its persisted rows, the evidence arithmetic, and the FinalLines being exactly its component
    projection. Any failure raises its stable code. The returned mapping carries the header
    fields readers use and the verified ``content``.
    """
    rows = (await db.execute(text("""
        SELECT DISTINCT payrollcalculationsnapshotid
        FROM payroll.payrollfinallines
        WHERE payrollperiodid = :period_id AND companyid = :company_id AND branchid = :branch_id
    """), {
        "period_id": period["payrollperiodid"], "company_id": period["companyid"],
        "branch_id": period["branchid"],
    })).all()
    if not rows:
        return None, availability("UNAVAILABLE", "FINAL_LINES_EMPTY")
    if len(rows) != 1:
        return None, availability("UNAVAILABLE", "PROVENANCE_UNAVAILABLE")
    try:
        content = await verify_snapshot(
            db, snapshot_id=int(rows[0][0]), company_id=int(period["companyid"]),
            branch_id=int(period["branchid"]), period_id=int(period["payrollperiodid"]))
        await verify_final_lines(db, content)
    except SnapshotEvidenceError as exc:
        raise finalized_error(exc.code, exc.message) from exc
    return {
        "payrollcalculationsnapshotid": content["PayrollCalculationSnapshotID"],
        "revisionnumber": content["RevisionNumber"],
        "calculationversion": content["CalculationVersion"],
        "snapshothash": content["SnapshotHash"],
        "sourceconfighash": content["SourceConfigHash"],
        "currencycode": content["CurrencyCode"],
        "currencyminorunitdigits": content["CurrencyMinorUnitDigits"],
        "reportevidenceversion": content["ReportEvidenceVersion"],
        "reportevidencehash": content["ReportEvidenceHash"],
        "content": content,
    }, availability("AVAILABLE")
