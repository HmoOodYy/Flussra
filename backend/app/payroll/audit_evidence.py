"""P6D immutable, period-scoped audit-evidence writers.

These rows preserve authoritative mutation facts while the source is editable.
They are deliberately separate from generic ``audit.AuditLog`` and from the
immutable lifecycle evidence captured by the Phase 6 foundation.
"""
from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.payroll.immutable_evidence import _participant_snapshot

_DOMAINS = ("SOURCE", "STATUS_NOTE", "BONUS", "REVIEW_COMMENT")


def _json(value: dict[str, Any] | None) -> str | None:
    if value is None:
        return None
    return json.dumps(value, default=str, separators=(",", ":"), sort_keys=True)


async def initialize_period_audit_evidence_coverage(
    *, company_id: int, branch_id: int, period_id: int, db: AsyncConnection,
) -> None:
    """Mark a newly created period as fully covered, even with zero events."""
    for domain in _DOMAINS:
        await db.execute(text("""
            INSERT INTO payroll.payrollperiodauditevidencecoverage
                (companyid, branchid, payrollperiodid, evidencedomain, coveragestate)
            VALUES (:company_id, :branch_id, :period_id, :domain, 'COMPLETE')
            ON CONFLICT (payrollperiodid, evidencedomain) DO NOTHING
        """), {
            "company_id": company_id, "branch_id": branch_id,
            "period_id": period_id, "domain": domain,
        })


async def _ensure_partial_coverage(
    *, company_id: int, branch_id: int, period_id: int, domain: str, db: AsyncConnection,
) -> None:
    """Existing periods begin at their first P6D mutation without fabricated past."""
    await db.execute(text("""
        INSERT INTO payroll.payrollperiodauditevidencecoverage
            (companyid, branchid, payrollperiodid, evidencedomain, coveragestate)
        VALUES (:company_id, :branch_id, :period_id, :domain, 'PARTIAL')
        ON CONFLICT (payrollperiodid, evidencedomain) DO NOTHING
    """), {
        "company_id": company_id, "branch_id": branch_id,
        "period_id": period_id, "domain": domain,
    })


async def capture_period_audit_evidence(
    *, company_id: int, branch_id: int, period_id: int, domain: str, action_code: str,
    source_entity_type: str, source_entity_id: int | str, user_id: int,
    required_permission_code: str, db: AsyncConnection, before_state: dict[str, Any] | None = None,
    after_state: dict[str, Any] | None = None, driver_id: int | None = None,
    work_date: Any | None = None, pay_item_id: int | None = None, reason: str | None = None,
    correlation_id: str | None = None, source_revision: int | None = None,
    review_item_id: int | None = None,
) -> int:
    """Capture one self-contained business mutation in its owning transaction."""
    await _ensure_partial_coverage(
        company_id=company_id, branch_id=branch_id, period_id=period_id,
        domain=domain, db=db,
    )
    display_name, role_context = await _participant_snapshot(
        company_id=company_id, branch_id=branch_id, user_id=user_id,
        required_permission_code=required_permission_code, db=db,
    )
    result = await db.execute(text("""
        INSERT INTO payroll.payrollperiodauditevidenceevents
            (companyid, branchid, payrollperiodid, evidencedomain, actioncode,
             sourceentitytype, sourceentityid, reviewitemid, driverid, workdate, payitemid,
             beforestatejson, afterstatejson, actoruserid, actordisplaynamesnapshot,
             responsibilitycontextsnapshot, reasonsnapshot, correlationid, sourcerevision)
        VALUES
            (:company_id, :branch_id, :period_id, :domain, :action_code,
             :entity_type, :entity_id, :review_item_id, :driver_id, :work_date, :pay_item_id,
             CAST(:before_state AS jsonb), CAST(:after_state AS jsonb), :user_id, :display_name,
             CAST(:role_context AS jsonb), :reason, CAST(:correlation_id AS uuid), :source_revision)
        RETURNING payrollperiodauditevidenceeventid
    """), {
        "company_id": company_id, "branch_id": branch_id, "period_id": period_id,
        "domain": domain, "action_code": action_code, "entity_type": source_entity_type,
        "entity_id": str(source_entity_id), "review_item_id": review_item_id,
        "driver_id": driver_id, "work_date": work_date,
        "pay_item_id": pay_item_id, "before_state": _json(before_state),
        "after_state": _json(after_state), "user_id": user_id, "display_name": display_name,
        "role_context": role_context, "reason": reason, "correlation_id": correlation_id,
        "source_revision": source_revision,
    })
    return int(result.scalar_one())


async def link_unmapped_audit_evidence_to_snapshot(
    *, company_id: int, branch_id: int, period_id: int, snapshot_id: int, db: AsyncConnection,
) -> None:
    """Freeze the current change cycle against the exact snapshot just captured."""
    await db.execute(text("""
        INSERT INTO payroll.payrollperiodauditevidencesnapshotevents
            (payrollperiodauditevidenceeventid, payrollcalculationsnapshotid,
             companyid, branchid, payrollperiodid)
        SELECT e.payrollperiodauditevidenceeventid, :snapshot_id,
               :company_id, :branch_id, :period_id
        FROM payroll.payrollperiodauditevidenceevents e
        LEFT JOIN payroll.payrollperiodauditevidencesnapshotevents m
          ON m.payrollperiodauditevidenceeventid = e.payrollperiodauditevidenceeventid
        WHERE e.companyid = :company_id AND e.branchid = :branch_id
          AND e.payrollperiodid = :period_id AND m.payrollperiodauditevidenceeventid IS NULL
        ORDER BY e.occurredatutc, e.payrollperiodauditevidenceeventid
    """), {
        "snapshot_id": snapshot_id, "company_id": company_id,
        "branch_id": branch_id, "period_id": period_id,
    })


async def link_audit_evidence_to_snapshot(
    *, event_id: int, snapshot_id: int, company_id: int, branch_id: int, period_id: int,
    db: AsyncConnection,
) -> None:
    """Link a post-submit review comment to its exact ReviewItem snapshot."""
    await db.execute(text("""
        INSERT INTO payroll.payrollperiodauditevidencesnapshotevents
            (payrollperiodauditevidenceeventid, payrollcalculationsnapshotid,
             companyid, branchid, payrollperiodid)
        VALUES (:event_id, :snapshot_id, :company_id, :branch_id, :period_id)
    """), {
        "event_id": event_id, "snapshot_id": snapshot_id,
        "company_id": company_id, "branch_id": branch_id, "period_id": period_id,
    })
