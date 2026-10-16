"""P6D immutable finalized payroll audit read model."""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.access.permissions import check_permission
from app.payroll.evidence_gate import require_target_payroll_evidence
from app.payroll.finalized_context import (
    availability as _availability,
)
from app.payroll.finalized_context import (
    get_finalized_period_context as _period_context,
)
from app.payroll.finalized_context import (
    resolve_originating_snapshot as _originating_snapshot,
)


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
    require_target_payroll_evidence()
    await check_permission(company_id, user_id, int(period["branchid"]), "ledger.audit.view", db)
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
        "currency_code": period["currency_code"], "currency_minor_unit_digits": period["currency_minor_unit_digits"],
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
