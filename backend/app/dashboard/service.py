"""
Dashboard service — read-only aggregation queries for GET /dashboard.

Permission model (Dashboard D1):
  - Driver / ODA users are blocked (calls _require_not_driver_role).
  - Any non-driver user with at least one relevant permission on at least one
    accessible branch may call GET /dashboard.
  - The response is divided into sections. Each section is included only when
    the user holds a qualifying permission for that section:

    payroll_ops      payroll.view | payroll.entry | payroll.period.create | payroll.finalize
    approved_periods payroll.finalize  (full period list; others get count via periods_approved)
    review_queue     review.decide | payroll.entry | payroll.view
    rates_health     payrates.view | payrates.edit | payroll.approve_rate
    setup_health     setup.manage | settings.manage
    transfers        drivers.view | drivers.edit | drivers.manage

  - Branch scope: each section is filtered to branches where the user holds a
    qualifying permission.  For AllCompanyBranches users a company-level check
    is performed (branch_id=None).  For SpecificBranch users only branches where
    they actually hold the permission are included.

  - If no section is available → 403 (user exists but has no dashboard
    permissions at all — should never happen for real operational users).
"""
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException
from fastapi import status as http_status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.service import (
    _check_branch_access,
    _has_any_permission,
    _require_not_driver_role,
)
from app.dashboard.schemas import (
    ApprovedPeriodItem,
    BranchPeriodSummary,
    DashboardResponse,
    LastFinalizedPeriod,
    SetupWarning,
)

# ── Permission groups per section ─────────────────────────────────────────────

_PAYROLL_OPS_PERMS = [
    "payroll.view",
    "payroll.entry",
    "payroll.period.create",
    "payroll.finalize",
]
_REVIEW_PERMS = [
    "review.decide",
    "payroll.entry",
    "payroll.view",
]
_RATES_PERMS = [
    "payrates.view",
    "payrates.edit",
    "payroll.approve_rate",
]
_SETUP_PERMS = [
    "setup.manage",
    "settings.manage",
]
_PEOPLE_PERMS = [
    "drivers.view",
    "drivers.edit",
    "drivers.manage",
]
_FINALIZE_PERMS = ["payroll.finalize"]


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

async def get_dashboard_summary(
    company_id: int,
    user_id: int,
    db: AsyncConnection,
) -> DashboardResponse:
    """Compute and return a permission-aware dashboard snapshot."""

    # 1. Block driver / ODA accounts from the operational dashboard.
    await _require_not_driver_role(company_id, user_id, db)

    # 2. Resolve base branch scope.
    can_see_all, branch_ids = await _check_branch_access(company_id, user_id, db)
    scope = "AllCompanyBranches" if can_see_all else "Branch"

    # 3. Determine per-section access.
    #    Returns (section_can_see_all, section_permitted_bids).
    #    For all-company users: (True, []).
    #    For specific-branch users: (False, [bid, ...]) — subset of branch_ids.
    payroll_all, payroll_bids = await _get_section_branches(
        company_id, user_id, can_see_all, branch_ids, _PAYROLL_OPS_PERMS, db
    )
    review_all, review_bids = await _get_section_branches(
        company_id, user_id, can_see_all, branch_ids, _REVIEW_PERMS, db
    )
    rates_all, rates_bids = await _get_section_branches(
        company_id, user_id, can_see_all, branch_ids, _RATES_PERMS, db
    )
    setup_all, setup_bids = await _get_section_branches(
        company_id, user_id, can_see_all, branch_ids, _SETUP_PERMS, db
    )
    people_all, people_bids = await _get_section_branches(
        company_id, user_id, can_see_all, branch_ids, _PEOPLE_PERMS, db
    )
    finalize_all, finalize_bids = await _get_section_branches(
        company_id, user_id, can_see_all, branch_ids, _FINALIZE_PERMS, db
    )

    has_payroll  = payroll_all  or bool(payroll_bids)
    has_review   = review_all   or bool(review_bids)
    has_rates    = rates_all    or bool(rates_bids)
    has_setup    = setup_all    or bool(setup_bids)
    has_people   = people_all   or bool(people_bids)
    has_finalize = finalize_all or bool(finalize_bids)

    # Deny if no section is available at all.
    if not any([has_payroll, has_review, has_rates, has_setup, has_people]):
        raise HTTPException(
            status_code=http_status.HTTP_403_FORBIDDEN,
            detail=(
                "You do not have any dashboard permissions. "
                "Contact your administrator to be granted access."
            ),
        )

    # Build section list (order matters — frontend uses this for rendering order).
    sections_available: list[str] = []
    if has_payroll:
        sections_available.append("payroll_ops")
    if has_finalize:
        sections_available.append("approved_periods")
    if has_review:
        sections_available.append("review_queue")
    if has_rates:
        sections_available.append("rates_health")
    if has_people:
        sections_available.append("transfers")
    if has_setup:
        sections_available.append("setup_health")

    # ── 4. Payroll Ops queries ────────────────────────────────────────────────
    periods_draft = periods_open = periods_in_review = periods_approved = periods_locked = 0
    branch_period: dict[int, dict[str, int]] = {}
    branch_nmr: dict[int, int] = {}
    branch_names: dict[int, str] = {}
    approved_periods: list[ApprovedPeriodItem] = []
    last_finalized: LastFinalizedPeriod | None = None

    if has_payroll:
        bf_p, bp_p = _make_branch_filter(payroll_all, payroll_bids, "p.branchid")
        bf_b, bp_b = _make_branch_filter(payroll_all, payroll_bids, "b.branchid")

        # Period counts
        period_rows = await _query_period_counts(db, company_id, bf_p, bp_p)
        for row in period_rows:
            bid = row["branchid"]
            if bid not in branch_period:
                branch_period[bid] = {
                    "draft": 0, "open": 0, "in_review": 0, "approved": 0, "locked": 0
                }
            st = row["status"]
            if st == "Draft":
                branch_period[bid]["draft"] += row["cnt"]
            elif st == "Open":
                branch_period[bid]["open"] += row["cnt"]
            elif st == "InReview":
                branch_period[bid]["in_review"] += row["cnt"]
            elif st == "Approved":
                branch_period[bid]["approved"] += row["cnt"]
            elif st in ("Locked", "Archived"):
                branch_period[bid]["locked"] += row["cnt"]

        periods_draft     = sum(v["draft"]     for v in branch_period.values())
        periods_open      = sum(v["open"]      for v in branch_period.values())
        periods_in_review = sum(v["in_review"] for v in branch_period.values())
        periods_approved  = sum(v["approved"]  for v in branch_period.values())
        periods_locked    = sum(v["locked"]    for v in branch_period.values())

        # NMR counts
        nmr_rows = await _query_needs_manager_review_counts(db, company_id, bf_p, bp_p)
        branch_nmr = {row["branchid"]: row["cnt"] for row in nmr_rows}

        # Branch names (for summaries)
        bn_rows = await _query_branch_names(db, company_id, bf_b, bp_b)
        branch_names = {row["branchid"]: row["branchname"] for row in bn_rows}

        # Last finalized period
        last_finalized = await _query_last_finalized_period(db, company_id, bf_p, bp_p)

    # ── 5. Approved periods list (payroll.finalize users only) ────────────────
    if has_finalize:
        bf_fin, bp_fin = _make_branch_filter(finalize_all, finalize_bids, "p.branchid")
        approved_periods = await _query_approved_periods(db, company_id, bf_fin, bp_fin)

    # ── 6. Review queue ───────────────────────────────────────────────────────
    review_pending = review_edit_requested = 0
    branch_review: dict[int, dict[str, int]] = {}

    if has_review:
        bf_r, bp_r = _make_branch_filter(review_all, review_bids, "r.branchid")
        review_rows = await _query_review_counts(db, company_id, bf_r, bp_r)
        for row in review_rows:
            bid = row["branchid"]
            if bid not in branch_review:
                branch_review[bid] = {"pending": 0, "edit_requested": 0}
            st = row["status"]
            if st == "Pending":
                branch_review[bid]["pending"] += row["cnt"]
            elif st == "EditRequested":
                branch_review[bid]["edit_requested"] += row["cnt"]
        review_pending        = sum(v["pending"]        for v in branch_review.values())
        review_edit_requested = sum(v["edit_requested"] for v in branch_review.values())

    # ── 7. Active drivers ──────────────────────────────────────────────────────
    # Show to payroll_ops users (their scope) or people users (their scope).
    # If both, payroll scope takes precedence (typically broader or overlapping).
    active_drivers = 0
    if has_payroll:
        bf_d, bp_d = _make_branch_filter(payroll_all, payroll_bids, "d.branchid")
        driver_rows = await _query_driver_counts(db, company_id, bf_d, bp_d)
        active_drivers = sum(row["cnt"] for row in driver_rows)
    elif has_people:
        bf_d, bp_d = _make_branch_filter(people_all, people_bids, "d.branchid")
        driver_rows = await _query_driver_counts(db, company_id, bf_d, bp_d)
        active_drivers = sum(row["cnt"] for row in driver_rows)

    # ── 8. Pending transfers ──────────────────────────────────────────────────
    pending_transfers: int | None = None
    if has_people:
        pending_transfers = await _query_pending_transfers(
            db, company_id, people_all, people_bids
        )

    # ── 9. Pending rates ──────────────────────────────────────────────────────
    pending_rates: int | None = None
    if has_rates:
        # Join through drivers to apply branch scope.
        bf_dr, bp_dr = _make_branch_filter(rates_all, rates_bids, "d.branchid")
        pending_rates = await _query_pending_rates(db, company_id, bf_dr, bp_dr)

    # ── 10. Setup warnings (permission-gated) ──────────────────────────────────
    setup_warnings: list[SetupWarning] = []
    if has_payroll or has_setup or has_rates:
        # Determine branch scope for warnings: broadest scope across the relevant sections.
        if has_setup:
            sw_all, sw_bids = setup_all, setup_bids
        elif has_payroll:
            sw_all, sw_bids = payroll_all, payroll_bids
        else:
            sw_all, sw_bids = rates_all, rates_bids
        setup_warnings = await _compute_setup_warnings(
            db,
            company_id,
            sw_all,
            sw_bids,
            branch_names,  # may be empty for non-payroll users — warnings use own name lookup
            has_setup=has_setup,
            has_payroll=has_payroll,
            has_rates=has_rates,
        )

    # ── 11. Branch summaries (payroll users only) ──────────────────────────────
    branch_summaries: list[BranchPeriodSummary] = []
    if has_payroll:
        all_branch_ids_in_scope = set(branch_names.keys())
        for bid in sorted(all_branch_ids_in_scope):
            bp = branch_period.get(bid, {})
            br = branch_review.get(bid, {})
            branch_summaries.append(
                BranchPeriodSummary(
                    branch_id=bid,
                    branch_name=branch_names[bid],
                    draft_count=bp.get("draft", 0),
                    open_count=bp.get("open", 0),
                    in_review_count=bp.get("in_review", 0),
                    approved_count=bp.get("approved", 0),
                    locked_count=bp.get("locked", 0),
                    pending_review_count=br.get("pending", 0),
                    edit_requested_count=br.get("edit_requested", 0),
                    active_driver_count=0,   # filled below if both sections available
                    needs_manager_review_lines=branch_nmr.get(bid, 0),
                )
            )
        # Fill driver counts into summaries (if driver data was fetched for payroll scope).
        if has_payroll:
            bf_d2, bp_d2 = _make_branch_filter(payroll_all, payroll_bids, "d.branchid")
            driver_rows2 = await _query_driver_counts(db, company_id, bf_d2, bp_d2)
            branch_driver_count = {row["branchid"]: row["cnt"] for row in driver_rows2}
            for s in branch_summaries:
                s.active_driver_count = branch_driver_count.get(s.branch_id, 0)

    return DashboardResponse(
        generated_at=datetime.now(UTC),
        scope=scope,
        sections_available=sections_available,
        periods_draft=periods_draft,
        periods_open=periods_open,
        periods_in_review=periods_in_review,
        periods_approved=periods_approved,
        periods_locked=periods_locked,
        approved_periods=approved_periods,
        review_pending=review_pending,
        review_edit_requested=review_edit_requested,
        active_drivers=active_drivers,
        pending_transfers=pending_transfers,
        pending_rates=pending_rates,
        last_finalized_period=last_finalized,
        setup_warnings=setup_warnings,
        branch_summaries=branch_summaries,
    )


# ---------------------------------------------------------------------------
# Section branch helper
# ---------------------------------------------------------------------------

async def _get_section_branches(
    company_id: int,
    user_id: int,
    can_see_all: bool,
    branch_ids: list[int],
    perm_codes: list[str],
    db: AsyncConnection,
) -> tuple[bool, list[int]]:
    """
    Return (section_can_see_all, permitted_branch_ids) for a section's permissions.

    For AllCompanyBranches users: company-level check (branch_id=None).
        → (True, [])  if any permission granted
        → (False, []) if no permission
    For SpecificBranch users: check each accessible branch.
        → (False, [bid, ...])  subset of branch_ids where permission holds
        → (False, [])          if none
    """
    if can_see_all:
        has_perm = await _has_any_permission(company_id, user_id, None, perm_codes, db)
        return (True, []) if has_perm else (False, [])

    permitted: list[int] = []
    for bid in branch_ids:
        if await _has_any_permission(company_id, user_id, bid, perm_codes, db):
            permitted.append(bid)
    return False, permitted


# ---------------------------------------------------------------------------
# Branch filter builder
# ---------------------------------------------------------------------------

def _make_branch_filter(
    can_see_all: bool,
    branch_ids: list[int],
    column: str,
) -> tuple[str, dict[str, Any]]:
    """
    Return (sql_fragment, params) for a WHERE clause branch filter.

    can_see_all=True  → ("", {})           — no filter
    can_see_all=False, branch_ids non-empty → ("{column} IN (...)", {params})
    can_see_all=False, branch_ids empty    → ("1=0", {})  — impossible condition
    """
    if can_see_all:
        return "", {}
    if not branch_ids:
        return "1=0", {}
    keys = [f"bf{i}" for i in range(len(branch_ids))]
    clause = ", ".join(f":{k}" for k in keys)
    params = dict(zip(keys, branch_ids))
    return f"{column} IN ({clause})", params


def _and(fragment: str) -> str:
    """Prefix fragment with ' AND ' if non-empty."""
    return f" AND {fragment}" if fragment else ""


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------

async def _query_period_counts(
    db: AsyncConnection,
    company_id: int,
    branch_filter: str,
    branch_params: dict,
):
    result = await db.execute(
        text(f"""
            SELECT p.branchid, p.status, COUNT(*) AS cnt
            FROM   payroll.payrollperiods p
            WHERE  p.companyid = :company_id
              AND  p.status NOT IN ('Cancelled')
              {_and(branch_filter)}
            GROUP  BY p.branchid, p.status
        """),
        {"company_id": company_id, **branch_params},
    )
    return result.mappings().all()


async def _query_approved_periods(
    db: AsyncConnection,
    company_id: int,
    branch_filter: str,
    branch_params: dict,
) -> list[ApprovedPeriodItem]:
    """Return Approved periods ordered by start date — for payroll.finalize users."""
    result = await db.execute(
        text(f"""
            SELECT p.payrollperiodid, p.periodname, p.branchid,
                   b.branchname, p.startdate, p.enddate
            FROM   payroll.payrollperiods p
            JOIN   core.branches          b ON b.branchid = p.branchid
            WHERE  p.companyid = :company_id
              AND  p.status    = 'Approved'
              {_and(branch_filter)}
            ORDER  BY p.startdate
        """),
        {"company_id": company_id, **branch_params},
    )
    rows = result.mappings().all()
    return [
        ApprovedPeriodItem(
            period_id=row["payrollperiodid"],
            period_name=row["periodname"],
            branch_id=row["branchid"],
            branch_name=row["branchname"],
            start_date=row["startdate"],
            end_date=row["enddate"],
        )
        for row in rows
    ]


async def _query_review_counts(
    db: AsyncConnection,
    company_id: int,
    branch_filter: str,
    branch_params: dict,
):
    result = await db.execute(
        text(f"""
            SELECT r.branchid, r.status, COUNT(*) AS cnt
            FROM   review.managerreviewitems r
            WHERE  r.companyid = :company_id
              AND  r.status IN ('Pending', 'EditRequested')
              {_and(branch_filter)}
            GROUP  BY r.branchid, r.status
        """),
        {"company_id": company_id, **branch_params},
    )
    return result.mappings().all()


async def _query_driver_counts(
    db: AsyncConnection,
    company_id: int,
    branch_filter: str,
    branch_params: dict,
):
    result = await db.execute(
        text(f"""
            SELECT d.branchid, COUNT(*) AS cnt
            FROM   core.drivers   d
            JOIN   core.employees e ON e.employeeid = d.employeeid
            WHERE  d.companyid        = :company_id
              AND  d.driverstatus     = 'Active'
              AND  e.employmentstatus = 'Active'
              {_and(branch_filter)}
            GROUP  BY d.branchid
        """),
        {"company_id": company_id, **branch_params},
    )
    return result.mappings().all()


async def _query_needs_manager_review_counts(
    db: AsyncConnection,
    company_id: int,
    branch_filter: str,
    branch_params: dict,
):
    result = await db.execute(
        text(f"""
            SELECT p.branchid, COUNT(dl.draftlineid) AS cnt
            FROM   payroll.payrolldraftlines dl
            JOIN   payroll.payrollperiods    p  ON p.payrollperiodid = dl.payrollperiodid
            WHERE  dl.companyid           = :company_id
              AND  dl.needsmanagerreview  = TRUE
              AND  dl.status              = 'Active'
              AND  p.status               IN ('Open', 'InReview')
              {_and(branch_filter)}
            GROUP  BY p.branchid
        """),
        {"company_id": company_id, **branch_params},
    )
    return result.mappings().all()


async def _query_branch_names(
    db: AsyncConnection,
    company_id: int,
    branch_filter: str,
    branch_params: dict,
):
    result = await db.execute(
        text(f"""
            SELECT b.branchid, b.branchname
            FROM   core.branches b
            WHERE  b.companyid = :company_id
              AND  b.status    = 'Active'
              {_and(branch_filter)}
            ORDER  BY b.isdefault DESC, b.branchname
        """),
        {"company_id": company_id, **branch_params},
    )
    return result.mappings().all()


async def _query_last_finalized_period(
    db: AsyncConnection,
    company_id: int,
    branch_filter: str,
    branch_params: dict,
) -> LastFinalizedPeriod | None:
    result = await db.execute(
        text(f"""
            SELECT p.payrollperiodid, p.periodname, p.branchid,
                   b.branchname, p.startdate, p.enddate, p.lockedatutc
            FROM   payroll.payrollperiods p
            JOIN   core.branches          b ON b.branchid = p.branchid
            WHERE  p.companyid = :company_id
              AND  p.status    IN ('Locked', 'Archived')
              {_and(branch_filter)}
            ORDER  BY COALESCE(p.lockedatutc, p.createdatutc) DESC
            LIMIT  1
        """),
        {"company_id": company_id, **branch_params},
    )
    row = result.mappings().first()
    if row is None:
        return None
    return LastFinalizedPeriod(
        period_id=row["payrollperiodid"],
        period_name=row["periodname"],
        branch_id=row["branchid"],
        branch_name=row["branchname"],
        start_date=row["startdate"],
        end_date=row["enddate"],
        locked_at=row["lockedatutc"],
    )


async def _query_pending_transfers(
    db: AsyncConnection,
    company_id: int,
    can_see_all: bool,
    branch_ids: list[int],
) -> int:
    """
    Count transfer requests awaiting action (PendingSourceApproval or PendingTargetApproval).

    A user with access to branch B sees transfers where B is the source OR target branch,
    so both columns are checked.  AllCompanyBranches users see all pending transfers.
    """
    if can_see_all:
        result = await db.execute(
            text("""
                SELECT COUNT(*) AS cnt
                FROM   core.drivertransferrequests t
                WHERE  t.companyid = :company_id
                  AND  t.status IN ('PendingSourceApproval', 'PendingTargetApproval')
            """),
            {"company_id": company_id},
        )
    elif not branch_ids:
        return 0
    else:
        keys = [f"btf{i}" for i in range(len(branch_ids))]
        clause = ", ".join(f":{k}" for k in keys)
        params = dict(zip(keys, branch_ids))
        result = await db.execute(
            text(f"""
                SELECT COUNT(*) AS cnt
                FROM   core.drivertransferrequests t
                WHERE  t.companyid = :company_id
                  AND  t.status IN ('PendingSourceApproval', 'PendingTargetApproval')
                  AND  (t.sourcebranchid IN ({clause}) OR t.targetbranchid IN ({clause}))
            """),
            {"company_id": company_id, **params},
        )
    return result.scalar_one() or 0


async def _query_pending_rates(
    db: AsyncConnection,
    company_id: int,
    branch_filter: str,
    branch_params: dict,
) -> int:
    """Count DriverRates in PendingApproval status, scoped by branch via driver join."""
    result = await db.execute(
        text(f"""
            SELECT COUNT(*) AS cnt
            FROM   payroll.driverrates dr
            JOIN   core.drivers        d ON d.driverid  = dr.driverid
            WHERE  dr.companyid = :company_id
              AND  dr.status    = 'PendingApproval'
              {_and(branch_filter)}
        """),
        {"company_id": company_id, **branch_params},
    )
    return result.scalar_one() or 0


# ---------------------------------------------------------------------------
# Setup warnings (permission-gated)
# ---------------------------------------------------------------------------

async def _compute_setup_warnings(
    db: AsyncConnection,
    company_id: int,
    can_see_all: bool,
    branch_ids: list[int],
    branch_names: dict[int, str],
    *,
    has_setup: bool,
    has_payroll: bool,
    has_rates: bool,
) -> list[SetupWarning]:
    """
    Compute setup warnings filtered by what the user can actually act on.

    Warning gating:
      BRANCH_NO_PAYROLL_SETTINGS    → setup.manage / settings.manage only
      OPEN_PERIOD_NEEDS_MANAGER_REVIEW → payroll.entry / payroll.finalize (has_payroll)
      DRIVERS_NO_APPROVED_RATE      → payrates.view/edit (has_rates) or setup.manage (has_setup)
      PAY_ITEM_MISSING_RATE_TYPE_MAP → setup.manage / settings.manage only
    """
    warnings: list[SetupWarning] = []

    bf_b, bp_b = _make_branch_filter(can_see_all, branch_ids, "b.branchid")
    bf_d, bp_d = _make_branch_filter(can_see_all, branch_ids, "d.branchid")
    bf_p, bp_p = _make_branch_filter(can_see_all, branch_ids, "p.branchid")

    # W1: BRANCH_NO_PAYROLL_SETTINGS — only for setup admins
    if has_setup:
        result = await db.execute(
            text(f"""
                SELECT b.branchid, b.branchname
                FROM   core.branches b
                WHERE  b.companyid = :company_id
                  AND  b.status    = 'Active'
                  {_and(bf_b)}
                  AND  NOT EXISTS (
                      SELECT 1
                      FROM   payroll.branchpayrollsettings s
                      WHERE  s.branchid  = b.branchid
                        AND  s.companyid = :company_id
                  )
            """),
            {"company_id": company_id, **bp_b},
        )
        for row in result.mappings().all():
            warnings.append(SetupWarning(
                code="BRANCH_NO_PAYROLL_SETTINGS",
                severity="Warning",
                message=f"Branch '{row['branchname']}' has no payroll settings configured.",
                branch_id=row["branchid"],
                branch_name=row["branchname"],
                count=None,
            ))

    # W2: OPEN_PERIOD_NEEDS_MANAGER_REVIEW — payroll users
    if has_payroll:
        result = await db.execute(
            text(f"""
                SELECT p.branchid, b.branchname, COUNT(dl.draftlineid) AS cnt
                FROM   payroll.payrolldraftlines dl
                JOIN   payroll.payrollperiods    p  ON p.payrollperiodid = dl.payrollperiodid
                JOIN   core.branches             b  ON b.branchid        = p.branchid
                WHERE  dl.companyid          = :company_id
                  AND  dl.needsmanagerreview = TRUE
                  AND  dl.status             = 'Active'
                  AND  p.status              IN ('Open', 'InReview')
                  {_and(bf_p)}
                GROUP  BY p.branchid, b.branchname
            """),
            {"company_id": company_id, **bp_p},
        )
        for row in result.mappings().all():
            warnings.append(SetupWarning(
                code="OPEN_PERIOD_NEEDS_MANAGER_REVIEW",
                severity="Warning",
                message=(
                    f"Branch '{row['branchname']}' has {row['cnt']} draft line(s) "
                    f"requiring manager review in an open period."
                ),
                branch_id=row["branchid"],
                branch_name=row["branchname"],
                count=row["cnt"],
            ))

    # W3: DRIVERS_NO_APPROVED_RATE — payrates users or setup admins
    if has_rates or has_setup:
        result = await db.execute(
            text(f"""
                SELECT d.branchid, b.branchname, COUNT(d.driverid) AS cnt
                FROM   core.drivers   d
                JOIN   core.employees e ON e.employeeid  = d.employeeid
                JOIN   core.branches  b ON b.branchid    = d.branchid
                WHERE  d.companyid        = :company_id
                  AND  d.driverstatus     = 'Active'
                  AND  e.employmentstatus = 'Active'
                  {_and(bf_d)}
                  AND  NOT EXISTS (
                      SELECT 1
                      FROM   payroll.driverrates dr
                      WHERE  dr.driverid  = d.driverid
                        AND  dr.status    = 'Approved'
                  )
                GROUP  BY d.branchid, b.branchname
            """),
            {"company_id": company_id, **bp_d},
        )
        for row in result.mappings().all():
            warnings.append(SetupWarning(
                code="DRIVERS_NO_APPROVED_RATE",
                severity="Warning",
                message=(
                    f"Branch '{row['branchname']}' has {row['cnt']} active driver(s) "
                    f"with no approved pay rates."
                ),
                branch_id=row["branchid"],
                branch_name=row["branchname"],
                count=row["cnt"],
            ))

    # W4: PAY_ITEM_MISSING_RATE_TYPE_MAP — setup admins only (they fix pay item config)
    if has_setup:
        result = await db.execute(
            text("""
                SELECT pi.payitemid, pi.payitemcode, pi.payitemname
                FROM   payroll.payitems pi
                WHERE  pi.status         = 'Active'
                  AND  pi.itemscope      = 'Daily'
                  AND  pi.requiresrate   = TRUE
                  AND  (pi.companyid IS NULL OR pi.companyid = :company_id)
                  AND  NOT EXISTS (
                      SELECT 1
                      FROM   payroll.payitemratetypemap m
                      WHERE  m.payitemid = pi.payitemid
                        AND  m.status    = 'Active'
                  )
            """),
            {"company_id": company_id},
        )
        missing_map_rows = result.mappings().all()
        if missing_map_rows:
            names = ", ".join(r["payitemcode"] for r in missing_map_rows)
            warnings.append(SetupWarning(
                code="PAY_ITEM_MISSING_RATE_TYPE_MAP",
                severity="Warning",
                message=(
                    f"{len(missing_map_rows)} active Daily rate-requiring pay item(s) "
                    f"have no rate type mapping: {names}."
                ),
                branch_id=None,
                branch_name=None,
                count=len(missing_map_rows),
            ))

    return warnings
