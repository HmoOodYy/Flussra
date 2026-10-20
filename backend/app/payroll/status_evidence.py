"""
Read immutable Status evidence and resolve its captured availability and approved snapshot provenance.

Readers do not reconstruct historical labels or off-reason meaning from mutable Status configuration.
"""
from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.payroll.snapshot_hash import (
    UnsupportedCalculationVersionError,
    require_supported_calculation_version,
)


async def read_status_entries(
    db: AsyncConnection,
    *,
    snapshot_id: int,
    company_id: int,
    branch_id: int,
    period_id: int,
) -> list[dict[str, Any]]:
    """
    Read immutable captured Status evidence rows for one calculation snapshot.

    Scoped by company/branch/period in addition to snapshot_id. This is
    provably a no-op filter today -- PayrollCalculationSnapshotStatusEntries
    has a composite FK to PayrollCalculationSnapshots(ID, CompanyID, BranchID,
    PayrollPeriodID), so a row for this snapshot_id can never carry a
    mismatched company/branch/period -- but keeping the scope explicit here
    matches the stricter of the two call sites this replaces and costs
    nothing.

    Returns one dict per captured entry, using exactly the fields this table
    captures (no note text, no hours value, no void state -- not part of
    this table's contract):
        {driver_assignment_id, driver_id, work_date, status_key_id, status_code, status_label,
         is_off_reason}

    Does not raise, does not interpret ReportEvidenceVersion, does not decide
    availability -- callers that need those already have their own snapshot
    lookup and validation; use status_evidence_availability() below only if
    it fits.
    """
    rows = (await db.execute(text("""
        SELECT driverassignmentid, driverid, workdate, statuskeyid, statuscodesnapshot,
               statuslabelsnapshot, statusisoffreasonsnapshot
        FROM payroll.payrollcalculationsnapshotstatusentries
        WHERE payrollcalculationsnapshotid = :snapshot_id
          AND companyid = :company_id
          AND branchid = :branch_id
          AND payrollperiodid = :period_id
        ORDER BY driverassignmentid, workdate
    """), {
        "snapshot_id": snapshot_id,
        "company_id": company_id,
        "branch_id": branch_id,
        "period_id": period_id,
    })).mappings().all()
    return [{
        "driver_assignment_id": int(row["driverassignmentid"]),
        "driver_id": int(row["driverid"]),
        "work_date": row["workdate"],
        "status_key_id": int(row["statuskeyid"]),
        "status_code": row["statuscodesnapshot"],
        "status_label": row["statuslabelsnapshot"],
        "is_off_reason": bool(row["statusisoffreasonsnapshot"]),
    } for row in rows]


def status_evidence_availability(
    snapshot: Mapping[str, Any] | None,
    entries: list[dict[str, Any]],
    *,
    unavailable_reason: str = "PROVENANCE_UNAVAILABLE",
) -> dict[str, str | None]:
    """
    Resolve {state, reason_code} for a Status-evidence read.

    Reuses the existing AVAILABLE/EMPTY/UNAVAILABLE vocabulary already
    established by finalized_library_read_model._availability -- this is not
    a new vocabulary, just the same {"state": ..., "reason_code": ...} shape
    produced independently of that function (to avoid importing a read model
    into this leaf module).

    - snapshot is None                          -> UNAVAILABLE / unavailable_reason
    - snapshot["reportevidenceversion"] is None -> UNAVAILABLE / LEGACY_NOT_CAPTURED
    - entries is empty                          -> EMPTY
    - otherwise                                 -> AVAILABLE
    """
    if snapshot is None:
        return {"state": "UNAVAILABLE", "reason_code": unavailable_reason}
    if snapshot.get("reportevidenceversion") is None:
        return {"state": "UNAVAILABLE", "reason_code": "LEGACY_NOT_CAPTURED"}
    return {"state": "EMPTY" if not entries else "AVAILABLE", "reason_code": None}


async def resolve_finalized_snapshot(
    db: AsyncConnection,
    *,
    period_id: int,
    company_id: int,
    branch_id: int,
) -> tuple[dict[str, Any] | None, dict[str, str | None]]:
    """
    Resolve the one authoritative calculation snapshot for a Locked/Archived
    period -- never MAX(revision), never latest snapshot by timestamp, never
    an arbitrary snapshot lookup by period.

    Selector: ``PayrollFinalLines.PayrollCalculationSnapshotID``, the snapshot the finalized
    lines project. Every finalized period has FinalLines (one per official component), so the
    selection is exact. The snapshot must be exactly one, belong to this period/company/branch
    and carry the supported calculation version; otherwise the evidence is unavailable rather
    than taken from another snapshot. This light resolution serves per-day Status reads; the
    finalized library readers additionally verify the snapshot hash and the FinalLines
    projection (``finalized_context.resolve_originating_snapshot``).

    Returns (None, UNAVAILABLE/PROVENANCE_UNAVAILABLE) when the finalized lines do not name
    exactly one usable snapshot. Returns (snapshot, AVAILABLE) otherwise.
    """
    ids = (await db.execute(text("""
        SELECT DISTINCT payrollcalculationsnapshotid
        FROM payroll.payrollfinallines
        WHERE payrollperiodid = :period_id AND companyid = :company_id AND branchid = :branch_id
    """), {"period_id": period_id, "company_id": company_id, "branch_id": branch_id})).all()
    if len(ids) != 1:
        return None, {"state": "UNAVAILABLE", "reason_code": "PROVENANCE_UNAVAILABLE"}
    snapshot = (await db.execute(text("""
        SELECT payrollcalculationsnapshotid, revisionnumber, calculationversion, snapshothash,
               sourceconfighash, reportevidenceversion, reportevidencehash
        FROM payroll.payrollcalculationsnapshots
        WHERE payrollcalculationsnapshotid = :snapshot_id
          AND companyid = :company_id AND branchid = :branch_id AND payrollperiodid = :period_id
    """), {
        "snapshot_id": int(ids[0][0]), "period_id": period_id,
        "company_id": company_id, "branch_id": branch_id,
    })).mappings().first()
    if snapshot is None:
        return None, {"state": "UNAVAILABLE", "reason_code": "PROVENANCE_UNAVAILABLE"}
    try:
        require_supported_calculation_version(snapshot["calculationversion"])
    except UnsupportedCalculationVersionError:
        return None, {"state": "UNAVAILABLE", "reason_code": "UNSUPPORTED_CALCULATION_VERSION"}
    return dict(snapshot), {"state": "AVAILABLE", "reason_code": None}


async def read_report_evidence(
    db: AsyncConnection,
    *,
    snapshot_id: int,
    company_id: int,
    branch_id: int,
    period_id: int,
) -> tuple[bool, int | None, str | None, list[dict[str, Any]], list[dict[str, Any]]]:
    """The report-only Status and Bonus evidence frozen with one snapshot.

    Returns (captured, version, hash, statuses, bonuses). A snapshot that carries no report
    evidence version reports ``captured`` False and no rows. Rows are exactly what was frozen;
    nothing is reconstructed from mutable Status configuration or live Bonus rows.
    """
    header = (await db.execute(text("""
        SELECT reportevidenceversion, reportevidencehash
        FROM payroll.payrollcalculationsnapshots
        WHERE payrollcalculationsnapshotid = :snapshot_id
          AND companyid = :company_id AND branchid = :branch_id AND payrollperiodid = :period_id
    """), {
        "snapshot_id": snapshot_id, "company_id": company_id, "branch_id": branch_id,
        "period_id": period_id,
    })).mappings().first()
    if header is None or header["reportevidenceversion"] is None:
        return False, None, None, [], []
    status_rows = await read_status_entries(
        db, snapshot_id=snapshot_id, company_id=company_id, branch_id=branch_id,
        period_id=period_id)
    statuses = [{
        "driver_assignment_id": row["driver_assignment_id"], "driver_id": row["driver_id"],
        "work_date": row["work_date"], "status_key_id": row["status_key_id"],
        "code": row["status_code"], "label": row["status_label"], "is_off": row["is_off_reason"],
    } for row in status_rows]
    bonus_rows = (await db.execute(text("""
        SELECT payrollbonuseventid, driverassignmentid, driverid, amount, reason, notes,
               datarevision, createdbyuserid, creatordisplaynamesnapshot, createdatutc
        FROM payroll.payrollcalculationsnapshotbonusevents
        WHERE payrollcalculationsnapshotid = :snapshot_id
        ORDER BY driverassignmentid, payrollbonuseventid
    """), {"snapshot_id": snapshot_id})).mappings().all()
    bonuses = [{
        "bonus_event_id": int(r["payrollbonuseventid"]),
        "driver_assignment_id": int(r["driverassignmentid"]), "driver_id": int(r["driverid"]),
        "amount": Decimal(str(r["amount"])), "reason": r["reason"], "notes": r["notes"],
        "data_revision": int(r["datarevision"]), "creator_user_id": r["createdbyuserid"],
        "creator_display_name": r["creatordisplaynamesnapshot"], "created_at_utc": r["createdatutc"],
    } for r in bonus_rows]
    return (True, int(header["reportevidenceversion"]), str(header["reportevidencehash"]),
            statuses, bonuses)
