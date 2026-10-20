"""
Read-only financial authority selection for Current Payroll reports.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.company_currency import frozen_currency
from app.payroll.snapshot_hash import (
    UnsupportedCalculationVersionError,
    require_supported_calculation_version,
)


class ReportAuthorityKind(StrEnum):
    SOURCE_ONLY = "SOURCE_ONLY"
    LIVE = "LIVE"
    SUBMITTED_SNAPSHOT = "SUBMITTED_SNAPSHOT"
    APPROVED_SNAPSHOT = "APPROVED_SNAPSHOT"
    FINAL_LINES = "FINAL_LINES"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True)
class ReportFinancialAuthority:
    period_id: int
    company_id: int
    branch_id: int
    period_status: str
    authority_kind: ReportAuthorityKind
    review_item_id: int | None = None
    snapshot_id: int | None = None
    revision_number: int | None = None
    snapshot_hash: str | None = None
    captured_at_utc: datetime | None = None
    currency_code: str | None = None
    currency_minor_unit_digits: int | None = None


def _authority_error(code: str, message: str) -> HTTPException:
    return HTTPException(status_code=422, detail=f"{code}: {message}")


async def _resolve_review_snapshot_authority(
    *,
    db: AsyncConnection,
    period_id: int,
    company_id: int,
    branch_id: int,
    period_status: str,
    review_status: str,
    authority_kind: ReportAuthorityKind,
) -> ReportFinancialAuthority:
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
        "company_id": company_id,
        "branch_id": branch_id,
        "period_id": str(period_id),
        "review_status": review_status,
    })).mappings().all()

    if not reviews:
        raise _authority_error(
            "REPORT_FINANCIAL_AUTHORITY_UNAVAILABLE",
            f"no {review_status} PeriodApproval review item authorizes this period.",
        )
    if len(reviews) != 1:
        raise _authority_error(
            "REPORT_FINANCIAL_AUTHORITY_INTEGRITY_ERROR",
            f"more than one {review_status} PeriodApproval review item exists for this period.",
        )

    review = reviews[0]
    snapshot_id = review["payrollcalculationsnapshotid"]
    if snapshot_id is None:
        raise _authority_error(
            "SNAPSHOT_REQUIRED_FOR_REPORTING",
            f"the {review_status} PeriodApproval review item has no immutable calculation snapshot.",
        )
    return await _snapshot_authority(
        db=db, snapshot_id=int(snapshot_id), period_id=period_id, company_id=company_id,
        branch_id=branch_id, period_status=period_status, authority_kind=authority_kind,
        review_item_id=int(review["reviewitemid"]),
    )


async def _resolve_final_lines_snapshot_authority(
    *,
    db: AsyncConnection,
    period_id: int,
    company_id: int,
    branch_id: int,
    period_status: str,
) -> ReportFinancialAuthority:
    """The snapshot a finalized period's FinalLines name: exactly one, never a "latest"."""
    rows = (await db.execute(text("""
        SELECT DISTINCT payrollcalculationsnapshotid
        FROM payroll.payrollfinallines
        WHERE payrollperiodid = :period_id AND companyid = :company_id AND branchid = :branch_id
    """), {"period_id": period_id, "company_id": company_id, "branch_id": branch_id})).all()
    if not rows:
        raise _authority_error(
            "REPORT_FINANCIAL_AUTHORITY_UNAVAILABLE",
            "the finalized period has no FinalLines.",
        )
    if len(rows) != 1:
        raise _authority_error(
            "REPORT_FINANCIAL_AUTHORITY_INTEGRITY_ERROR",
            "the FinalLines of the finalized period name more than one calculation snapshot.",
        )
    return await _snapshot_authority(
        db=db, snapshot_id=int(rows[0][0]), period_id=period_id, company_id=company_id,
        branch_id=branch_id, period_status=period_status,
        authority_kind=ReportAuthorityKind.FINAL_LINES, review_item_id=None,
    )


async def _snapshot_authority(
    *,
    db: AsyncConnection,
    snapshot_id: int,
    period_id: int,
    company_id: int,
    branch_id: int,
    period_status: str,
    authority_kind: ReportAuthorityKind,
    review_item_id: int | None,
) -> ReportFinancialAuthority:
    snapshot = (await db.execute(text("""
        SELECT payrollcalculationsnapshotid, companyid, branchid, payrollperiodid,
               revisionnumber, calculationversion, snapshothash, createdatutc, currencycode, currencyminorunitdigits
        FROM payroll.payrollcalculationsnapshots
        WHERE payrollcalculationsnapshotid = :snapshot_id
    """), {"snapshot_id": snapshot_id})).mappings().first()
    if snapshot is None:
        raise _authority_error(
            "REPORT_FINANCIAL_AUTHORITY_INTEGRITY_ERROR",
            "the financial authority references an unavailable calculation snapshot.",
        )
    try:
        require_supported_calculation_version(snapshot["calculationversion"])
    except UnsupportedCalculationVersionError as exc:
        raise _authority_error("UNSUPPORTED_CALCULATION_VERSION", str(exc)) from exc
    if (
        int(snapshot["companyid"]) != company_id
        or int(snapshot["branchid"]) != branch_id
        or int(snapshot["payrollperiodid"]) != period_id
    ):
        raise _authority_error(
            "REPORT_FINANCIAL_AUTHORITY_INTEGRITY_ERROR",
            "the calculation snapshot does not belong to the resolved period scope.",
        )

    currency = frozen_currency(snapshot['currencycode'], snapshot['currencyminorunitdigits'])
    return ReportFinancialAuthority(
        period_id=period_id,
        company_id=company_id,
        branch_id=branch_id,
        period_status=period_status,
        authority_kind=authority_kind,
        review_item_id=review_item_id,
        snapshot_id=int(snapshot["payrollcalculationsnapshotid"]),
        revision_number=int(snapshot["revisionnumber"]),
        snapshot_hash=str(snapshot["snapshothash"]),
        captured_at_utc=snapshot["createdatutc"],
        currency_code=currency.code, currency_minor_unit_digits=currency.minor_unit_digits,
    )


async def resolve_report_financial_authority(
    *,
    db: AsyncConnection,
    period_id: int,
    company_id: int,
    branch_id: int,
) -> ReportFinancialAuthority:
    """Resolve the current period's financial authority without reading money."""
    period = (await db.execute(text("""
        SELECT payrollperiodid, companyid, branchid, status
        FROM payroll.payrollperiods
        WHERE payrollperiodid = :period_id
          AND companyid = :company_id
          AND branchid = :branch_id
    """), {
        "period_id": period_id,
        "company_id": company_id,
        "branch_id": branch_id,
    })).mappings().first()
    if period is None:
        raise _authority_error(
            "REPORT_FINANCIAL_AUTHORITY_UNAVAILABLE",
            "the payroll period is unavailable in the requested company and branch.",
        )

    status = str(period["status"])
    base = {
        "period_id": int(period["payrollperiodid"]),
        "company_id": int(period["companyid"]),
        "branch_id": int(period["branchid"]),
        "period_status": status,
    }
    if status == "Draft":
        return ReportFinancialAuthority(**base, authority_kind=ReportAuthorityKind.SOURCE_ONLY)
    if status in {"Open", "Returned"}:
        return ReportFinancialAuthority(**base, authority_kind=ReportAuthorityKind.LIVE)
    if status == "InReview":
        return await _resolve_review_snapshot_authority(
            db=db, review_status="Pending", authority_kind=ReportAuthorityKind.SUBMITTED_SNAPSHOT, **base,
        )
    if status == "Approved":
        return await _resolve_review_snapshot_authority(
            db=db, review_status="Approved", authority_kind=ReportAuthorityKind.APPROVED_SNAPSHOT, **base,
        )
    if status in {"Locked", "Archived"}:
        return await _resolve_final_lines_snapshot_authority(
            db=db, period_id=period_id, company_id=company_id, branch_id=branch_id,
            period_status=status,
        )
    if status == "Cancelled":
        return ReportFinancialAuthority(**base, authority_kind=ReportAuthorityKind.UNAVAILABLE)
    raise _authority_error(
        "REPORT_FINANCIAL_AUTHORITY_UNAVAILABLE",
        f"the payroll period has unsupported status '{status}'.",
    )
