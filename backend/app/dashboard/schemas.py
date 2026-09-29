"""
Dashboard response schemas.
"""
from datetime import date, datetime

from pydantic import BaseModel


class LastFinalizedPeriod(BaseModel):
    period_id: int
    period_name: str
    branch_id: int
    branch_name: str
    start_date: date
    end_date: date
    locked_at: datetime | None


class ApprovedPeriodItem(BaseModel):
    """An Approved period that is ready to finalize — shown only to payroll.finalize users."""
    period_id: int
    period_name: str
    branch_id: int
    branch_name: str
    start_date: date
    end_date: date


class SetupWarning(BaseModel):
    code: str
    """
    Known codes:
      BRANCH_NO_PAYROLL_SETTINGS    — no complete canonical Payroll Setup schedule
      OPEN_PERIOD_NEEDS_MANAGER_REVIEW — open/in-review period has NeedsManagerReview draft lines
      DRIVERS_NO_APPROVED_RATE      — active driver(s) have zero Approved DriverRates
                                       (broad check — does not validate per-rate-type completeness)
      PAY_ITEM_MISSING_RATE_TYPE_MAP — active Daily rate-requiring PayItems lack a PayItemRateTypeMap row
    """
    severity: str          # "Error" | "Warning" | "Info"
    message: str
    branch_id: int | None
    branch_name: str | None
    count: int | None      # number of entities affected, when applicable


class BranchPeriodSummary(BaseModel):
    branch_id: int
    branch_name: str
    draft_count: int
    open_count: int
    in_review_count: int
    approved_count: int
    locked_count: int
    pending_review_count: int
    edit_requested_count: int
    active_driver_count: int
    needs_manager_review_lines: int


class DashboardResponse(BaseModel):
    generated_at: datetime
    scope: str             # "AllCompanyBranches" | "Branch"

    # ── Section availability ──────────────────────────────────────────────────
    # Tells the frontend which sections are populated and should be rendered.
    # Values: "payroll_ops" | "review_queue" | "rates_health" | "setup_health"
    #         | "transfers" | "approved_periods"
    sections_available: list[str]

    # ── Payroll Ops section ───────────────────────────────────────────────────
    # Populated when user has payroll.view / payroll.entry / payroll.period.create /
    # payroll.finalize on at least one accessible branch.
    periods_draft: int
    periods_open: int
    periods_in_review: int
    periods_approved: int
    periods_locked: int

    # ── Ready to Finalize ─────────────────────────────────────────────────────
    # Full period list visible to payroll.finalize users (actionable).
    # Payroll.view/entry users see the count in periods_approved only.
    approved_periods: list[ApprovedPeriodItem]

    # ── Review Queue section ──────────────────────────────────────────────────
    # Populated when user has review.decide / payroll.view / payroll.entry.
    review_pending: int
    review_edit_requested: int

    # ── People & Drivers ──────────────────────────────────────────────────────
    # active_drivers: shown to payroll.view/entry users and drivers.view/edit users.
    # pending_transfers: None when the transfers section is not available.
    active_drivers: int
    pending_transfers: int | None

    # ── Rates Health section ──────────────────────────────────────────────────
    # pending_rates: PendingApproval DriverRates count.
    # None when user lacks payrates.view/edit / payroll.approve_rate.
    pending_rates: int | None

    # ── Last finalized period ─────────────────────────────────────────────────
    last_finalized_period: LastFinalizedPeriod | None

    # ── Setup warnings (permission-gated) ─────────────────────────────────────
    # Only warnings relevant to the user's permissions are included.
    setup_warnings: list[SetupWarning]

    # ── Per-branch breakdown ──────────────────────────────────────────────────
    # Only populated for payroll_ops users (payroll.view/entry/finalize scope).
    branch_summaries: list[BranchPeriodSummary]
