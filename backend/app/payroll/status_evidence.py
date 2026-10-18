"""
Read immutable Status evidence and resolve its captured availability and approved snapshot provenance.

Readers do not reconstruct historical labels or off-reason meaning from mutable Status configuration.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection


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

    Primary selector: the snapshot bound to the period's Approved
    PeriodApproval review item (review.ManagerReviewItems.
    PayrollCalculationSnapshotID). This is deliberately NOT FinalLines
    provenance: a valid historical Status snapshot can have no financial
    lines. Selecting only through FinalLines would incorrectly report its
    Status evidence as unavailable. The review binding resolves that packet
    independently of financial line count, using the same uniqueness and
    scope requirements as finalization._load_approved_snapshot_packet.

    When FinalLines do exist, their recorded provenance is cross-checked
    against the review-item-resolved snapshot (id + revision + hash) as a
    defensive integrity check; a mismatch is treated as unavailable rather
    than silently trusted. FinalLines being empty is not itself a reason to
    call evidence unavailable.

    Returns (None, UNAVAILABLE/PROVENANCE_UNAVAILABLE) when no exactly-one
    Approved PeriodApproval review item exists for the period, that item has
    no bound snapshot, the bound snapshot cannot be matched to this exact
    period/company/branch, or (when FinalLines exist) their provenance
    disagrees with it. Returns (snapshot, AVAILABLE) otherwise. Never falls
    back to a different snapshot when provenance is unusable.
    """
    reviews = (await db.execute(text("""
        SELECT ri.reviewitemid, ri.payrollcalculationsnapshotid
        FROM review.managerreviewitems ri
        WHERE ri.companyid = :company_id AND ri.branchid = :branch_id
          AND ri.requesttype = 'PeriodApproval'
          AND ri.entityschema = 'payroll' AND ri.entityname = 'PayrollPeriods'
          AND ri.entityid = :period_id AND ri.status = 'Approved'
    """), {
        "company_id": company_id, "branch_id": branch_id, "period_id": str(period_id),
    })).mappings().all()
    if len(reviews) != 1:
        return None, {"state": "UNAVAILABLE", "reason_code": "PROVENANCE_UNAVAILABLE"}
    snapshot_id = reviews[0]["payrollcalculationsnapshotid"]
    if snapshot_id is None:
        return None, {"state": "UNAVAILABLE", "reason_code": "PROVENANCE_UNAVAILABLE"}
    snapshot = (await db.execute(text("""
        SELECT payrollcalculationsnapshotid, revisionnumber, snapshothash, sourceconfighash,
               reportevidenceversion, reportevidencehash
        FROM payroll.payrollcalculationsnapshots
        WHERE payrollcalculationsnapshotid = :snapshot_id
          AND companyid = :company_id AND branchid = :branch_id AND payrollperiodid = :period_id
    """), {
        "snapshot_id": snapshot_id, "period_id": period_id,
        "company_id": company_id, "branch_id": branch_id,
    })).mappings().first()
    if snapshot is None:
        return None, {"state": "UNAVAILABLE", "reason_code": "PROVENANCE_UNAVAILABLE"}

    fl_rows = (await db.execute(text("""
        SELECT DISTINCT
               sourcesnapshot ->> 'payroll_calculation_snapshot_id' AS snapshot_id,
               sourcesnapshot ->> 'revision_number' AS revision_number,
               sourcesnapshot ->> 'snapshot_hash' AS snapshot_hash
        FROM payroll.payrollfinallines
        WHERE payrollperiodid = :period_id AND companyid = :company_id AND branchid = :branch_id
    """), {
        "period_id": period_id, "company_id": company_id, "branch_id": branch_id,
    })).mappings().all()
    if fl_rows:
        if len(fl_rows) != 1 or any(value is None for value in fl_rows[0].values()):
            return None, {"state": "UNAVAILABLE", "reason_code": "PROVENANCE_UNAVAILABLE"}
        fl_source = fl_rows[0]
        try:
            fl_snapshot_id = int(fl_source["snapshot_id"])
            fl_revision_number = int(fl_source["revision_number"])
        except (TypeError, ValueError):
            return None, {"state": "UNAVAILABLE", "reason_code": "PROVENANCE_UNAVAILABLE"}
        if (
            fl_snapshot_id != int(snapshot["payrollcalculationsnapshotid"])
            or fl_revision_number != int(snapshot["revisionnumber"])
            or str(fl_source["snapshot_hash"]) != str(snapshot["snapshothash"])
        ):
            return None, {"state": "UNAVAILABLE", "reason_code": "PROVENANCE_UNAVAILABLE"}

    return dict(snapshot), {"state": "AVAILABLE", "reason_code": None}
