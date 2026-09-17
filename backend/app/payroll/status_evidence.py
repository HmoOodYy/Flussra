"""
Shared read-only primitives for immutable Status evidence.

CP-4D/CP-5C capture immutable per-driver/day Status evidence (StatusKeyID,
frozen code/label/off-reason) into payroll.PayrollCalculationSnapshotStatusEntries
at Submit/Resubmit time, versioned via PayrollCalculationSnapshots.ReportEvidenceVersion.
This module exists only to stop that read pattern from being duplicated --
it introduces no new evidence, no new state, and no new vocabulary.

Two existing readers each re-implemented the same query with slightly
different field names and a differently-scoped availability check:
  - app.payroll.finalized_library_read_model._finalized_status_entries
  - app.payroll.report_read_model._snapshot_evidence
Both are migrated to the primitives here (Stage B3 Unit 8C-2). Their
external return shapes are preserved exactly; only the duplicated SQL and
availability logic move here.

Leaf module: intentionally imports nothing from app.payroll.service,
finalized_library_read_model, or report_read_model, so it can be imported by
all three (and by service.py in the future) without any circular-import risk.
"""
from __future__ import annotations

from typing import Any, Mapping

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
        {driver_id, work_date, status_key_id, status_code, status_label,
         is_off_reason}

    Does not raise, does not interpret ReportEvidenceVersion, does not decide
    availability -- callers that need those already have their own snapshot
    lookup and validation; use status_evidence_availability() below only if
    it fits.
    """
    rows = (await db.execute(text("""
        SELECT driverid, workdate, statuskeyid, statuscodesnapshot,
               statuslabelsnapshot, statusisoffreasonsnapshot
        FROM payroll.payrollcalculationsnapshotstatusentries
        WHERE payrollcalculationsnapshotid = :snapshot_id
          AND companyid = :company_id
          AND branchid = :branch_id
          AND payrollperiodid = :period_id
        ORDER BY driverid, workdate
    """), {
        "snapshot_id": snapshot_id,
        "company_id": company_id,
        "branch_id": branch_id,
        "period_id": period_id,
    })).mappings().all()
    return [{
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
