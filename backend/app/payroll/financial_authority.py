"""The one resolver of a payroll period's financial authority.

Every reader that returns money for a period asks this module which authority governs it and
takes the money from that authority only:

* Draft: no financials (source only).
* Open / Returned: LIVE, the live calculation packet.
* InReview: the snapshot bound to the Pending PeriodApproval review item.
* Approved: the snapshot bound to the Approved PeriodApproval review item.
* Locked / Archived: the one snapshot the period's FinalLines name, whose FinalLines must be
  exactly the projection of that snapshot. A snapshot with no official component projects no
  FinalLines; then the snapshot is the one the period's single Approved PeriodApproval
  authorized, accepted only if it really projects nothing.
* Cancelled: no financials.
* Any other status fails closed.

A frozen authority runs the canonical verification (``snapshot_verification.verify_snapshot``,
plus ``final_line_projection.verify_final_lines`` for finalized periods) before it hands out the
content. The snapshot is selected by exact identity, never "latest", and a frozen authority
never falls back to live data: when it cannot be resolved or does not verify it fails closed
(``require_financial_authority``) or, for readers that keep their non-financial data, is reported
as unavailable with a stable reason code (``resolve_financial_authority(withhold=True)``).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.payroll.final_line_projection import project_final_lines, verify_final_lines
from app.payroll.snapshot_verification import SnapshotEvidenceError, verify_snapshot

ZERO = Decimal("0")


class AuthorityKind(StrEnum):
    SOURCE_ONLY = "SOURCE_ONLY"
    LIVE = "LIVE"
    SUBMITTED_SNAPSHOT = "SUBMITTED_SNAPSHOT"
    APPROVED_SNAPSHOT = "APPROVED_SNAPSHOT"
    FINAL_LINES = "FINAL_LINES"
    UNAVAILABLE = "UNAVAILABLE"


FROZEN_KINDS = frozenset({
    AuthorityKind.SUBMITTED_SNAPSHOT, AuthorityKind.APPROVED_SNAPSHOT, AuthorityKind.FINAL_LINES,
})

# The client-facing summary of an authority.
AUTHORITY_LIVE = "LIVE"
AUTHORITY_FROZEN = "FROZEN"
AUTHORITY_UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True)
class FinancialAuthority:
    period_id: int
    company_id: int
    branch_id: int
    period_status: str
    authority_kind: AuthorityKind
    review_item_id: int | None = None
    snapshot_id: int | None = None
    revision_number: int | None = None
    snapshot_hash: str | None = None
    captured_at_utc: datetime | None = None
    currency_code: str | None = None
    currency_minor_unit_digits: int | None = None
    # The verified content of a frozen authority; nothing else is a source of frozen money.
    content: dict[str, Any] | None = None
    # Why money is withheld (stable code) when the authority is unavailable.
    reason_code: str | None = None
    reason_message: str | None = None

    @property
    def is_live(self) -> bool:
        return self.authority_kind is AuthorityKind.LIVE

    @property
    def is_frozen(self) -> bool:
        return self.authority_kind in FROZEN_KINDS and self.content is not None

    @property
    def financials_available(self) -> bool:
        return self.is_live or self.is_frozen

    @property
    def financial_authority(self) -> str:
        """LIVE, FROZEN or UNAVAILABLE (a source-only period has no financials)."""
        if self.is_live:
            return AUTHORITY_LIVE
        if self.is_frozen:
            return AUTHORITY_FROZEN
        return AUTHORITY_UNAVAILABLE

    @property
    def financial_authority_reason(self) -> str | None:
        if self.financials_available:
            return None
        if self.reason_code is not None:
            return self.reason_code
        if self.authority_kind is AuthorityKind.SOURCE_ONLY:
            return "SOURCE_ONLY_PERIOD"
        return "FINANCIAL_AUTHORITY_UNAVAILABLE"


def _error(code: str, message: str) -> SnapshotEvidenceError:
    return SnapshotEvidenceError(code, message)


async def _bound_snapshot_id(
    db: AsyncConnection, *, period_id: int, company_id: int, branch_id: int, review_status: str,
) -> tuple[int, int]:
    """The review item and snapshot of the one PeriodApproval item in ``review_status``."""
    reviews = (await db.execute(text("""
        SELECT ri.reviewitemid, ri.payrollcalculationsnapshotid
        FROM review.managerreviewitems ri
        WHERE ri.companyid = :company_id
          AND ri.branchid = :branch_id
          AND ri.requesttype = 'PeriodApproval'
          AND ri.entityschema = 'payroll'
          AND ri.entityname = 'PayrollPeriods'
          AND ri.entityid = :period_id
          AND ri.status = :review_status
    """), {
        "company_id": company_id, "branch_id": branch_id,
        "period_id": str(period_id), "review_status": review_status,
    })).mappings().all()
    if not reviews:
        raise _error(
            "REPORT_FINANCIAL_AUTHORITY_UNAVAILABLE",
            f"no {review_status} PeriodApproval review item authorizes this period.")
    if len(reviews) != 1:
        raise _error(
            "REPORT_FINANCIAL_AUTHORITY_INTEGRITY_ERROR",
            f"more than one {review_status} PeriodApproval review item exists for this period.")
    snapshot_id = reviews[0]["payrollcalculationsnapshotid"]
    if snapshot_id is None:
        raise _error(
            "SNAPSHOT_REQUIRED_FOR_REPORTING",
            f"the {review_status} PeriodApproval review item has no immutable calculation snapshot.")
    return int(reviews[0]["reviewitemid"]), int(snapshot_id)


async def _finalized_snapshot_id(
    db: AsyncConnection, *, period_id: int, company_id: int, branch_id: int,
) -> tuple[int, bool]:
    """(snapshot id, whether FinalLines name it) for a finalized period; never a "latest".

    The snapshot is the one the period's FinalLines name: exactly one. A snapshot with no
    official component (it holds only Status evidence, for instance when every Status of the
    period is non-monetary) projects no FinalLines at all, so for a period with none the
    snapshot is the one the period's single Approved PeriodApproval review item authorized,
    which is the exact relationship finalization itself consumed. The caller then proves that
    the snapshot really has nothing to project.
    """
    rows = (await db.execute(text("""
        SELECT DISTINCT payrollcalculationsnapshotid
        FROM payroll.payrollfinallines
        WHERE payrollperiodid = :period_id AND companyid = :company_id AND branchid = :branch_id
    """), {"period_id": period_id, "company_id": company_id, "branch_id": branch_id})).all()
    if len(rows) > 1:
        raise _error(
            "PROVENANCE_UNAVAILABLE",
            "the FinalLines of the finalized period name more than one calculation snapshot.")
    if rows:
        return int(rows[0][0]), True
    approved = (await db.execute(text("""
        SELECT ri.payrollcalculationsnapshotid
        FROM review.managerreviewitems ri
        WHERE ri.companyid = :company_id AND ri.branchid = :branch_id
          AND ri.requesttype = 'PeriodApproval'
          AND ri.entityschema = 'payroll' AND ri.entityname = 'PayrollPeriods'
          AND ri.entityid = :period_id AND ri.status = 'Approved'
    """), {"company_id": company_id, "branch_id": branch_id,
           "period_id": str(period_id)})).all()
    if len(approved) != 1 or approved[0][0] is None:
        raise _error("FINAL_LINES_EMPTY", "the finalized period has no FinalLines.")
    return int(approved[0][0]), False


async def _verified_authority(
    db: AsyncConnection, *, base: dict[str, Any], kind: AuthorityKind, snapshot_id: int,
    review_item_id: int | None, has_final_lines: bool = True,
) -> FinancialAuthority:
    try:
        content = await verify_snapshot(
            db, snapshot_id=snapshot_id, company_id=base["company_id"],
            branch_id=base["branch_id"], period_id=base["period_id"])
        if kind is AuthorityKind.FINAL_LINES:
            if has_final_lines:
                await verify_final_lines(db, content)
            elif project_final_lines(content):
                # The snapshot has official components, so a finalized period must have
                # their FinalLines; their absence is not a zero-component period.
                raise _error("FINAL_LINES_EMPTY", "the finalized period has no FinalLines.")
    except SnapshotEvidenceError as exc:
        if exc.code == "SNAPSHOT_NOT_FOUND":
            raise _error(
                "REPORT_FINANCIAL_AUTHORITY_INTEGRITY_ERROR",
                "the calculation snapshot does not belong to the resolved period scope.") from exc
        raise
    return FinancialAuthority(
        **base, authority_kind=kind, review_item_id=review_item_id,
        snapshot_id=int(content["PayrollCalculationSnapshotID"]),
        revision_number=int(content["RevisionNumber"]),
        snapshot_hash=str(content["SnapshotHash"]),
        captured_at_utc=content["CreatedAtUtc"],
        currency_code=content["CurrencyCode"],
        currency_minor_unit_digits=int(content["CurrencyMinorUnitDigits"]),
        content=content,
    )


async def _resolve(
    db: AsyncConnection, *, period_id: int, company_id: int, branch_id: int,
) -> FinancialAuthority:
    period = (await db.execute(text("""
        SELECT payrollperiodid, companyid, branchid, status
        FROM payroll.payrollperiods
        WHERE payrollperiodid = :period_id AND companyid = :company_id AND branchid = :branch_id
    """), {"period_id": period_id, "company_id": company_id, "branch_id": branch_id})
    ).mappings().first()
    if period is None:
        raise _error(
            "REPORT_FINANCIAL_AUTHORITY_UNAVAILABLE",
            "the payroll period is unavailable in the requested company and branch.")
    status = str(period["status"])
    base = {
        "period_id": int(period["payrollperiodid"]),
        "company_id": int(period["companyid"]),
        "branch_id": int(period["branchid"]),
        "period_status": status,
    }
    if status == "Draft":
        return FinancialAuthority(**base, authority_kind=AuthorityKind.SOURCE_ONLY)
    if status in {"Open", "Returned"}:
        return FinancialAuthority(**base, authority_kind=AuthorityKind.LIVE)
    if status in {"InReview", "Approved"}:
        review_status = "Pending" if status == "InReview" else "Approved"
        review_item_id, snapshot_id = await _bound_snapshot_id(
            db, period_id=period_id, company_id=company_id, branch_id=branch_id,
            review_status=review_status)
        return await _verified_authority(
            db, base=base, snapshot_id=snapshot_id, review_item_id=review_item_id,
            kind=(AuthorityKind.SUBMITTED_SNAPSHOT if status == "InReview"
                  else AuthorityKind.APPROVED_SNAPSHOT))
    if status in {"Locked", "Archived"}:
        snapshot_id, has_final_lines = await _finalized_snapshot_id(
            db, period_id=period_id, company_id=company_id, branch_id=branch_id)
        return await _verified_authority(
            db, base=base, snapshot_id=snapshot_id, review_item_id=None,
            kind=AuthorityKind.FINAL_LINES, has_final_lines=has_final_lines)
    if status == "Cancelled":
        return FinancialAuthority(
            **base, authority_kind=AuthorityKind.UNAVAILABLE, reason_code="PERIOD_CANCELLED",
            reason_message="Cancelled payroll periods have no financials.")
    raise _error(
        "REPORT_FINANCIAL_AUTHORITY_UNAVAILABLE",
        f"the payroll period has unsupported status '{status}'.")


async def resolve_financial_authority(
    db: AsyncConnection, *, period_id: int, company_id: int, branch_id: int,
    withhold: bool = False,
) -> FinancialAuthority:
    """The financial authority of one period, verified when frozen.

    By default any failure to select or verify a frozen authority raises
    ``SnapshotEvidenceError``. With ``withhold`` the failure is returned instead as an
    UNAVAILABLE authority carrying the stable reason code, for readers that keep returning
    their non-financial data and withhold only the money.
    """
    try:
        return await _resolve(db, period_id=period_id, company_id=company_id, branch_id=branch_id)
    except SnapshotEvidenceError as exc:
        if not withhold:
            raise
        status = (await db.execute(text("""
            SELECT status FROM payroll.payrollperiods
            WHERE payrollperiodid = :period_id AND companyid = :company_id
        """), {"period_id": period_id, "company_id": company_id})).scalar_one_or_none()
        return FinancialAuthority(
            period_id=period_id, company_id=company_id, branch_id=branch_id,
            period_status=str(status), authority_kind=AuthorityKind.UNAVAILABLE,
            reason_code=exc.code, reason_message=exc.message)


async def require_financial_authority(
    db: AsyncConnection, *, period_id: int, company_id: int, branch_id: int,
) -> FinancialAuthority:
    """The authority, or a 422 carrying the stable code when it cannot be resolved or verified."""
    try:
        return await resolve_financial_authority(
            db, period_id=period_id, company_id=company_id, branch_id=branch_id)
    except SnapshotEvidenceError as exc:
        raise exc.http() from exc


# ---------------------------------------------------------------------------
# Frozen projections of a verified snapshot: exact source identity, never a calculation
# ---------------------------------------------------------------------------

def frozen_source_lines(content: dict[str, Any]) -> dict[int, dict[str, Any]]:
    """The snapshot lines of the DraftLines, by DraftLine id (the SourceID of the line).

    Each carries the frozen quantity, resolved rate, calculated amount and calculation status.
    A DraftLine with no snapshot line (a Void row, for example) is absent: it has no money.
    """
    lines: dict[int, dict[str, Any]] = {}
    for total in content["DriverTotals"]:
        for line in total["Lines"]:
            if line["SourceType"] != "DraftLine":
                continue
            lines[int(line["SourceID"])] = {
                **line, "DriverAssignmentID": total["DriverAssignmentID"]}
    return lines


def frozen_ordinary_totals(content: dict[str, Any]) -> dict[tuple[int, int], dict[str, Any]]:
    """The ORDINARY component of each (DriverAssignment, period definition) with its lines.

    ``amount`` is the component's PreRoundingAmount, the raw sum of its lines and exactly what
    the live summary shows; the quantity and line count come from the snapshot lines.
    """
    totals: dict[tuple[int, int], dict[str, Any]] = {}
    for total in content["DriverTotals"]:
        for component in total["Components"]:
            if component["ComponentKind"] != "ORDINARY":
                continue
            lines = [
                line for line in total["Lines"]
                if line["PayrollCalculationSnapshotComponentID"]
                == component["PayrollCalculationSnapshotComponentID"]
            ]
            totals[(total["DriverAssignmentID"], component["PayrollPeriodDefinitionID"])] = {
                "component": component,
                "quantity": sum((line["Quantity"] or ZERO for line in lines), ZERO),
                "amount": component["PreRoundingAmount"],
                "line_count": len(lines),
            }
    return totals
