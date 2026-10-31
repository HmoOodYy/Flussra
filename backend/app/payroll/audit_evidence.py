"""P6D immutable, period-scoped audit-evidence writers.

These rows preserve authoritative mutation facts while the source is editable.
They are deliberately separate from generic ``audit.AuditLog`` and from the
immutable lifecycle evidence captured by the Phase 6 foundation.
"""
from __future__ import annotations

import json
from collections.abc import Iterable
from itertools import islice
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
    after_state: dict[str, Any] | None = None, driver_assignment_id: int | None = None,
    work_date: Any | None = None, reason: str | None = None,
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
             sourceentitytype, sourceentityid, reviewitemid, driverassignmentid, driverid,
             workdate,
             beforestatejson, afterstatejson, actoruserid, actordisplaynamesnapshot,
             responsibilitycontextsnapshot, reasonsnapshot, correlationid, sourcerevision)
        VALUES
            (:company_id, :branch_id, :period_id, :domain, :action_code,
             :entity_type, :entity_id, :review_item_id, :driver_assignment_id,
             (SELECT a.driverid FROM core.driverassignments a
               WHERE a.driverassignmentid = :driver_assignment_id AND a.companyid = :company_id),
             :work_date,
             CAST(:before_state AS jsonb), CAST(:after_state AS jsonb), :user_id, :display_name,
             CAST(:role_context AS jsonb), :reason, CAST(:correlation_id AS uuid), :source_revision)
        RETURNING payrollperiodauditevidenceeventid
    """), {
        "company_id": company_id, "branch_id": branch_id, "period_id": period_id,
        "domain": domain, "action_code": action_code, "entity_type": source_entity_type,
        "entity_id": str(source_entity_id), "review_item_id": review_item_id,
        "driver_assignment_id": driver_assignment_id, "work_date": work_date,
        "before_state": _json(before_state),
        "after_state": _json(after_state), "user_id": user_id, "display_name": display_name,
        "role_context": role_context, "reason": reason, "correlation_id": correlation_id,
        "source_revision": source_revision,
    })
    return int(result.scalar_one())


async def capture_period_audit_evidence_batch(
    *, company_id: int, branch_id: int, period_id: int, domain: str,
    source_entity_type: str, user_id: int, required_permission_code: str,
    events: Iterable[dict[str, Any]], correlation_id: str, db: AsyncConnection,
    source_revision: int | None = None, batch_size: int = 1000,
) -> dict[str, int]:
    """Capture complete per-change evidence with one coverage/actor snapshot.

    The owning canonical mutation authorizes and locks first. This writer uses
    the same evidence columns/coverage policy as the single-event writer, keeps
    ordinary constraints active and never commits the caller's transaction.
    """
    if batch_size < 1 or domain not in _DOMAINS:
        raise ValueError("Invalid evidence batch parameters.")
    iterator = iter(events)
    chunk = list(islice(iterator, batch_size))
    if not chunk:
        return {}
    await _ensure_partial_coverage(company_id=company_id, branch_id=branch_id,
                                  period_id=period_id, domain=domain, db=db)
    display_name, role_context = await _participant_snapshot(
        company_id=company_id, branch_id=branch_id, user_id=user_id,
        required_permission_code=required_permission_code, db=db,
    )
    event_ids: dict[str, int] = {}
    while chunk:
        result = (await db.execute(text("""
            INSERT INTO payroll.payrollperiodauditevidenceevents
                (companyid, branchid, payrollperiodid, evidencedomain, actioncode,
                 sourceentitytype, sourceentityid, driverassignmentid, driverid, workdate,
                 beforestatejson, afterstatejson, actoruserid, actordisplaynamesnapshot,
                 responsibilitycontextsnapshot, correlationid, sourcerevision)
            SELECT :cid, :bid, :pid, :domain, x.action_code, :entity_type, x.entity_id,
                x.assignment_id, a.driverid, x.work_date, x.before_state, x.after_state,
                :uid, :display_name, CAST(:role_context AS jsonb), CAST(:correlation AS uuid), :revision
            FROM jsonb_to_recordset(CAST(:events AS jsonb)) AS x(action_code text,
                entity_id text, assignment_id integer, work_date date, before_state jsonb, after_state jsonb)
            LEFT JOIN core.driverassignments a ON a.driverassignmentid = x.assignment_id
                AND a.companyid = :cid AND a.branchid = :bid
            WHERE x.assignment_id IS NULL OR a.driverassignmentid IS NOT NULL
            RETURNING sourceentityid, payrollperiodauditevidenceeventid
        """), {"cid": company_id, "bid": branch_id, "pid": period_id, "domain": domain,
               "entity_type": source_entity_type, "uid": user_id, "display_name": display_name,
               "role_context": role_context, "correlation": correlation_id,
               "revision": source_revision, "events": _json(chunk)})).all()
        if len(result) != len(chunk):
            raise RuntimeError("Evidence batch contains an unowned assignment; roll back the mutation.")
        for entity_id, event_id in result:
            if entity_id in event_ids:
                raise RuntimeError("Evidence batch repeats an entity; roll back the mutation.")
            event_ids[entity_id] = event_id
        chunk = list(islice(iterator, batch_size))
    return event_ids


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
