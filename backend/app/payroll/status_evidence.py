"""
Read immutable Status evidence and resolve its captured availability and approved snapshot provenance.

Readers do not reconstruct historical labels or off-reason meaning from mutable Status configuration.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy.ext.asyncio import AsyncConnection

from app.payroll.financial_authority import FinancialAuthority, resolve_financial_authority


def status_entries_from_content(content: dict[str, Any]) -> list[dict[str, Any]]:
    """The Status report evidence of a verified snapshot.

    One dict per captured entry, using exactly the fields the evidence captures (no note text,
    no void state):
        {driver_assignment_id, driver_id, work_date, status_key_id, status_code, status_label,
         is_off_reason, hours_value, category}

    The content comes from ``verify_snapshot``: the report evidence is verified there (version,
    its own hash and its overlaps with the financial evidence), so a reader never queries the
    evidence tables itself and never exposes it unverified.
    """
    return [{
        "driver_assignment_id": entry["DriverAssignmentID"],
        "driver_id": entry["DriverID"],
        "work_date": entry["WorkDate"],
        "status_key_id": entry["StatusKeyID"],
        "status_code": entry["StatusCodeSnapshot"],
        "status_label": entry["StatusLabelSnapshot"],
        "is_off_reason": entry["StatusIsOffReasonSnapshot"],
        "hours_value": entry["StatusHoursValueSnapshot"],
        "category": entry["StatusCategorySnapshot"],
    } for entry in content["ReportEvidence"]["StatusEntries"]]


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


def status_evidence_of(
    authority: FinancialAuthority,
) -> tuple[list[dict[str, Any]] | None, dict[str, str | None]]:
    """The verified Status evidence of a finalized period's authority and its availability.

    The snapshot is the one the period's FinalLines name, resolved and verified by the shared
    financial authority (never "latest"). When it could not be resolved or did not verify, the
    evidence is UNAVAILABLE with the stable reason code, ``entries`` is ``None``, and nothing is
    taken from another snapshot or from mutable current state.
    """
    if not authority.is_frozen:
        reason = authority.financial_authority_reason
        if reason == "FINAL_LINES_EMPTY":
            reason = "PROVENANCE_UNAVAILABLE"
        return None, {"state": "UNAVAILABLE", "reason_code": reason}
    entries = status_entries_from_content(authority.content)
    return entries, status_evidence_availability(
        {"reportevidenceversion": authority.content["ReportEvidenceVersion"]}, entries)


async def finalized_status_evidence(
    db: AsyncConnection,
    *,
    period_id: int,
    company_id: int,
    branch_id: int,
) -> tuple[list[dict[str, Any]] | None, dict[str, str | None]]:
    """``status_evidence_of`` for a period, resolving its authority first."""
    return status_evidence_of(await resolve_financial_authority(
        db, period_id=period_id, company_id=company_id, branch_id=branch_id, withhold=True))


def report_evidence_from_content(
    content: dict[str, Any],
) -> tuple[bool, int | None, str | None, list[dict[str, Any]], list[dict[str, Any]]]:
    """The report-only Status and Bonus evidence frozen with a verified snapshot.

    Returns (captured, version, hash, statuses, bonuses). Rows are exactly what was frozen and
    verified; nothing is reconstructed from mutable Status configuration or live Bonus rows.
    """
    if content["ReportEvidenceVersion"] is None:
        return False, None, None, [], []
    statuses = [{
        "driver_assignment_id": row["driver_assignment_id"], "driver_id": row["driver_id"],
        "work_date": row["work_date"], "status_key_id": row["status_key_id"],
        "code": row["status_code"], "label": row["status_label"], "is_off": row["is_off_reason"],
        "hours_value": row["hours_value"], "category": row["category"],
    } for row in status_entries_from_content(content)]
    bonuses = [{
        "bonus_event_id": event["PayrollBonusEventID"],
        "driver_assignment_id": event["DriverAssignmentID"], "driver_id": event["DriverID"],
        "amount": event["Amount"], "reason": event["Reason"], "notes": event["Notes"],
        "data_revision": event["DataRevision"], "creator_user_id": event["CreatedByUserID"],
        "creator_display_name": event["CreatorDisplayNameSnapshot"],
        "created_at_utc": event["CreatedAtUtc"],
    } for event in content["ReportEvidence"]["BonusEvents"]]
    return (True, int(content["ReportEvidenceVersion"]), str(content["ReportEvidenceHash"]),
            statuses, bonuses)
