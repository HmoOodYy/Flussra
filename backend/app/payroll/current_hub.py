"""CP-5A read model for the active Current Payroll workflow Hub."""
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.service import (
    _build_in_clause,
    _check_branch_access,
    _has_any_permission,
    _require_not_driver_role,
)
from app.payroll import off_drivers, period_calculation
from app.payroll.eligibility import (
    _is_snapshot_row_eligible_for_workdate,
    _period_has_driver_eligibility_snapshot,
)
from app.payroll.guards import _get_oda_own_driver_id
from app.payroll.period_creation import _check_slot_matrix
from app.payroll.schemas import (
    BranchWorkflowCapabilities,
    BranchWorkflowEntry,
    CurrentPayrollHubBranch,
    CurrentPayrollHubDriverSummary,
    CurrentPayrollHubFinancialSummary,
    CurrentPayrollHubMetrics,
    CurrentPayrollHubPeriodSlot,
    CurrentPayrollHubResponse,
    CurrentPayrollHubSlots,
    CurrentWorkflowResponse,
    PeriodSummary,
    PeriodWorkflowCapabilities,
    WorkflowAlert,
    WorkflowBranchSlots,
    WorkflowCapability,
    WorkflowSlotItem,
)

_TOP_DRIVER_LIMIT = 5
_NON_WORK_DAILY_LINE_TYPES = (
    "DailyStatus",
    "DailyNote",
    "STATUS_PAYMENT",
    "STATUS_PAY",
    "BONUS",
    "ADJUSTMENT",
    "MINIMUM",
    "MAXIMUM",
    "SYS_MIN_TOPUP",
    "SYS_MAX_CAP",
)


async def _eligible_driver_ids(
    period_id: int,
    company_id: int,
    branch_id: int,
    start_date: date,
    end_date: date,
    db: AsyncConnection,
) -> tuple[set[int], dict[int, SimpleNamespace] | None]:
    """Return period eligibility plus CP-2E date windows when snapshotted."""
    has_snapshot = await _period_has_driver_eligibility_snapshot(period_id, db)

    if has_snapshot:
        rows = (await db.execute(
            text("""
                SELECT driverid,
                       iseligibleforperiod,
                       eligibilityreasoncode,
                       hiredatesnapshot,
                       terminationdatesnapshot,
                       drivereffectivefromsnapshot,
                       drivereffectivetosnapshot
                FROM payroll.payrollperioddrivereligibility
                WHERE payrollperiodid = :period_id
                  AND companyid = :company_id
                  AND branchid = :branch_id
                  AND iseligibleforperiod = TRUE
            """),
            {
                "period_id": period_id,
                "company_id": company_id,
                "branch_id": branch_id,
            },
        )).mappings().all()
        by_driver = {
            int(row["driverid"]): SimpleNamespace(**dict(row))
            for row in rows
        }
        return set(by_driver), by_driver

    rows = (await db.execute(
        text("""
            SELECT DISTINCT d.driverid
            FROM core.drivers d
            JOIN core.employees e ON e.employeeid = d.employeeid
            WHERE d.companyid = :company_id
              AND d.branchid = :branch_id
              AND e.employmentstatus = 'Active'
              AND d.driverstatus = 'Active'
              AND (e.hiredate IS NULL OR e.hiredate <= :period_end)
              AND (e.terminationdate IS NULL OR e.terminationdate >= :period_start)
              AND (d.effectivefrom IS NULL OR d.effectivefrom <= :period_end)
              AND (d.effectiveto IS NULL OR d.effectiveto >= :period_start)
        """),
        {
            "company_id": company_id,
            "branch_id": branch_id,
            "period_start": start_date,
            "period_end": end_date,
        },
    )).mappings().all()
    return {int(row["driverid"]) for row in rows}, None


async def _period_metrics(
    slot: WorkflowSlotItem,
    company_id: int,
    db: AsyncConnection,
) -> CurrentPayrollHubMetrics:
    eligible_ids, snapshot_rows = await _eligible_driver_ids(
        period_id=slot.period_id,
        company_id=company_id,
        branch_id=slot.branch_id,
        start_date=slot.start_date,
        end_date=slot.end_date,
        db=db,
    )
    if not eligible_ids:
        return CurrentPayrollHubMetrics(
            total_eligible_drivers=0,
            working_drivers=0,
            fully_off_drivers=0,
        )

    # Working is operational daily entry, never Status, Bonus, system money, or
    # a financial total. Every source must be inside the period bounds. CP-2E
    # periods are additionally filtered through the canonical snapshotted
    # driver-date helper below; the SQL conditions are the legacy day-grid path.
    in_clause, in_params = _build_in_clause(sorted(eligible_ids), "driver")
    legacy_date_eligibility = ""
    if snapshot_rows is None:
        legacy_date_eligibility = """
              AND e.employmentstatus = 'Active'
              AND (
                    d.driverstatus = 'Active'
                 OR (d.driverstatus = 'Transferred'
                     AND d.effectiveto IS NOT NULL
                     AND d.effectiveto >= dl.workdate)
              )
              AND (e.hiredate IS NULL OR e.hiredate <= dl.workdate)
              AND (e.terminationdate IS NULL OR e.terminationdate >= dl.workdate)
              AND (d.effectivefrom IS NULL OR d.effectivefrom <= dl.workdate)
              AND (d.effectiveto IS NULL OR d.effectiveto >= dl.workdate)
        """
    rows = (await db.execute(
        text(f"""
            SELECT DISTINCT dl.driverid, dl.workdate
            FROM payroll.payrolldraftlines dl
            LEFT JOIN payroll.payrollperiodpayitems pppi
                   ON pppi.payrollperiodid = dl.payrollperiodid
                  AND pppi.payitemcode = dl.linetype
            LEFT JOIN payroll.payitems pi
                   ON pi.payitemcode = dl.linetype
                  AND (pi.companyid IS NULL OR pi.companyid = dl.companyid)
            LEFT JOIN core.drivers d ON d.driverid = dl.driverid
            LEFT JOIN core.employees e ON e.employeeid = d.employeeid
            WHERE dl.payrollperiodid = :period_id
              AND dl.companyid = :company_id
              AND dl.branchid = :branch_id
              AND dl.driverid IN ({in_clause})
              AND dl.status != 'Void'
              AND dl.linescope = 'Daily'
              AND dl.workdate >= :period_start
              AND dl.workdate <= :period_end
              AND dl.quantity IS NOT NULL
              AND dl.quantity <> 0
              AND dl.sourcetype NOT IN ('System', 'BonusEvent')
              AND dl.linetype NOT IN ({', '.join(repr(code) for code in _NON_WORK_DAILY_LINE_TYPES)})
              AND COALESCE(pppi.itemscope, pi.itemscope) = 'Daily'
              AND COALESCE(pppi.appearsinpayrollentry, pi.appearsinpayrollentry, FALSE) = TRUE
              AND COALESCE(pppi.isactiveinperiod, pi.status <> 'Retired', FALSE) = TRUE
              {legacy_date_eligibility}
        """),
        {
            "period_id": slot.period_id,
            "company_id": company_id,
            "branch_id": slot.branch_id,
            "period_start": slot.start_date,
            "period_end": slot.end_date,
            **in_params,
        },
    )).mappings().all()
    if snapshot_rows is not None:
        working_ids = {
            int(row["driverid"])
            for row in rows
            if _is_snapshot_row_eligible_for_workdate(
                snapshot_rows[int(row["driverid"])], row["workdate"],
            )
        }
    else:
        working_ids = {int(row["driverid"]) for row in rows}
    fully_off = await off_drivers.resolve_fully_off_drivers(slot, company_id, db)
    return CurrentPayrollHubMetrics(
        total_eligible_drivers=len(eligible_ids),
        working_drivers=len(working_ids),
        fully_off_drivers=len(fully_off),
    )


def _financial_summary(packet) -> CurrentPayrollHubFinancialSummary:
    normal_pay = sum(
        (driver.daily_pay + driver.status_pay + driver.period_pay for driver in packet.drivers),
        Decimal("0"),
    )
    bonus_total = sum((driver.bonus_total for driver in packet.drivers), Decimal("0"))
    system_adjustments = sum(
        (driver.minimum_adjustment + driver.maximum_adjustment for driver in packet.drivers),
        Decimal("0"),
    )
    top_drivers = sorted(
        packet.drivers,
        key=lambda driver: (-driver.expected_pay, driver.driver_id),
    )[:_TOP_DRIVER_LIMIT]
    return CurrentPayrollHubFinancialSummary(
        total_expected_pay=packet.total_expected_pay,
        normal_pay=normal_pay,
        bonus_total=bonus_total,
        system_adjustments=system_adjustments,
        has_blockers=bool(packet.blockers),
        blockers=packet.blockers,
        warnings=packet.warnings,
        top_drivers=[
            CurrentPayrollHubDriverSummary(
                driver_id=driver.driver_id,
                driver_code=driver.driver_code,
                driver_name=driver.driver_name,
                expected_pay=driver.expected_pay,
            )
            for driver in top_drivers
        ],
    )


async def _hub_slot(
    slot: WorkflowSlotItem | None,
    company_id: int,
    db: AsyncConnection,
) -> CurrentPayrollHubPeriodSlot | None:
    if slot is None:
        return None

    metrics = await _period_metrics(slot, company_id, db)
    financial_summary = None
    if slot.status in {"Open", "Returned"}:
        period = PeriodSummary.model_construct(
            payroll_period_id=slot.period_id,
            branch_id=slot.branch_id,
            branch_name=slot.branch_name,
            period_code=slot.period_code,
            period_name=slot.period_name,
            period_type=slot.period_type,
            start_date=slot.start_date,
            end_date=slot.end_date,
            status=slot.status,
        )
        financial_summary = _financial_summary(
            await period_calculation._build_live_calculation_packet(period, company_id, db)
        )

    return CurrentPayrollHubPeriodSlot(
        **slot.model_dump(),
        financials_available=financial_summary is not None,
        financial_summary=financial_summary,
        metrics=metrics,
    )


async def get_current_payroll_hub(
    company_id: int,
    user_id: int,
    branch_id: int | None,
    db: AsyncConnection,
) -> CurrentPayrollHubResponse:
    """Compose CP-1E workflow state with CP-2E metrics and CP-4B live totals."""
    workflow = await get_current_workflow(
        company_id=company_id,
        user_id=user_id,
        branch_id=branch_id,
        db=db,
    )
    branches: list[CurrentPayrollHubBranch] = []
    for branch in workflow.branches:
        slots = branch.slots
        branches.append(CurrentPayrollHubBranch(
            branch_id=branch.branch_id,
            branch_name=branch.branch_name,
            setup_status=branch.setup_status,
            slots=CurrentPayrollHubSlots(
                open=await _hub_slot(slots.open, company_id, db),
                prepared=await _hub_slot(slots.prepared, company_id, db),
                in_review=await _hub_slot(slots.in_review, company_id, db),
                returned=await _hub_slot(slots.returned, company_id, db),
            ),
            capabilities=branch.capabilities,
            alerts=branch.alerts,
        ))
    return CurrentPayrollHubResponse(
        scope=workflow.scope,
        company_id=workflow.company_id,
        requested_branch_id=workflow.requested_branch_id,
        generated_at_utc=datetime.now(UTC),
        branches=branches,
    )


# ===========================================================================
# CP-1E: GET /payroll/current-workflow
# ===========================================================================

_WF_READ_PERMS = ["payroll.view", "payroll.entry", "payroll.finalize"]

_LIFECYCLE_POSITION: dict[str, int] = {
    "Returned": 1,
    "Open":     2,
    "Draft":    3,
    "InReview": 4,
}


def _cap(allowed: bool, reason_code: str | None = None, reason_message: str | None = None) -> WorkflowCapability:
    return WorkflowCapability(allowed=allowed, reason_code=reason_code, reason_message=reason_message)


def _denied(reason_code: str, reason_message: str) -> WorkflowCapability:
    return WorkflowCapability(allowed=False, reason_code=reason_code, reason_message=reason_message)


def _build_slot_item(row: dict, branch_name: str) -> WorkflowSlotItem:
    status = row["status"]
    display_status = "Prepared" if status == "Draft" else status
    is_read_only = status == "InReview"
    return WorkflowSlotItem(
        period_id=row["payrollperiodid"],
        branch_id=row["branchid"],
        branch_name=branch_name,
        status=status,
        display_status=display_status,
        period_name=row["periodname"],
        period_code=row["periodcode"],
        period_type=row["periodtype"],
        start_date=row["startdate"],
        end_date=row["enddate"],
        submitted_at_utc=row.get("submittedatutc"),
        current_return_review_item_id=row.get("currentreturnreviewitemid"),
        is_active_workflow_slot=True,
        is_read_only=is_read_only,
        read_only_reason_code="PERIOD_IN_REVIEW_READ_ONLY" if is_read_only else None,
        lifecycle_position=_LIFECYCLE_POSITION.get(status, 0),
    )


def _build_branch_entry(
    branch_id: int,
    branch_name: str,
    setup_status: str,
    active_periods: list[dict],
    has_view: bool,
    has_entry: bool,
    has_finalize: bool,
    has_period_create: bool,
) -> BranchWorkflowEntry:
    open_row = next((p for p in active_periods if p["status"] == "Open"), None)
    draft_row = next((p for p in active_periods if p["status"] == "Draft"), None)
    inreview_row = next((p for p in active_periods if p["status"] == "InReview"), None)
    returned_row = next((p for p in active_periods if p["status"] == "Returned"), None)

    slots = WorkflowBranchSlots(
        open=_build_slot_item(open_row, branch_name) if open_row else None,
        prepared=_build_slot_item(draft_row, branch_name) if draft_row else None,
        in_review=_build_slot_item(inreview_row, branch_name) if inreview_row else None,
        returned=_build_slot_item(returned_row, branch_name) if returned_row else None,
    )

    alerts: list[WorkflowAlert] = []

    # SLOT_INVARIANT: Draft without Open
    draft_alone = draft_row is not None and open_row is None
    if draft_alone:
        alerts.append(WorkflowAlert(
            code="SLOT_INVARIANT",
            severity="warning",
            title="Draft period without Open",
            message="A Prepared (Draft) period exists without a corresponding Open period. This is an unexpected workflow state.",
            related_period_id=draft_row["payrollperiodid"],
            affected_action_codes=["can_submit_for_review", "can_create_open_candidate"],
        ))

    # RETURNED_BACKLOG: older Returned exists with end_date < open.start_date
    returned_backlog = (
        returned_row is not None
        and open_row is not None
        and returned_row["enddate"] < open_row["startdate"]
    )
    returned_anomaly = (
        returned_row is not None
        and open_row is not None
        and returned_row["enddate"] >= open_row["startdate"]
    )
    if returned_backlog:
        alerts.append(WorkflowAlert(
            code="RETURNED_BACKLOG",
            severity="blocker",
            title="Unresolved Returned period",
            message=(
                f"A Returned period (ending {returned_row['enddate']}) exists before the "
                f"current Open period (starting {open_row['startdate']}). "
                "Resolve the Returned period before submitting."
            ),
            related_period_id=returned_row["payrollperiodid"],
            affected_action_codes=["can_submit_for_review"],
        ))
    elif returned_anomaly:
        alerts.append(WorkflowAlert(
            code="SLOT_INVARIANT",
            severity="warning",
            title="Returned period workflow anomaly",
            message=(
                f"A Returned period (ending {returned_row['enddate']}) overlaps or is newer than "
                f"the Open period (starting {open_row['startdate']}). This is an unexpected state."
            ),
            related_period_id=returned_row["payrollperiodid"],
            affected_action_codes=["can_submit_for_review"],
        ))

    # Draft promotion conflict: Draft exists but not adjacent to Open
    draft_promotion_conflict = False
    if draft_row and open_row and not draft_alone:
        expected = open_row["enddate"] + timedelta(days=1)
        if draft_row["startdate"] != expected:
            draft_promotion_conflict = True
            alerts.append(WorkflowAlert(
                code="SLOT_INVARIANT",
                severity="warning",
                title="Draft period not adjacent to Open",
                message=(
                    f"A Draft period exists (start {draft_row['startdate']}) but does not "
                    f"immediately follow the Open period end ({open_row['enddate']}). "
                    "Submit will be blocked until resolved."
                ),
                related_period_id=draft_row["payrollperiodid"],
                affected_action_codes=["can_submit_for_review"],
            ))

    # INREVIEW_AWAITING
    if inreview_row:
        alerts.append(WorkflowAlert(
            code="INREVIEW_AWAITING",
            severity="info",
            title="Period awaiting review",
            message="A period is currently in review.",
            related_period_id=inreview_row["payrollperiodid"],
            affected_action_codes=[],
        ))

    # PREPARED_NOTICE
    if draft_row:
        alerts.append(WorkflowAlert(
            code="PREPARED_NOTICE",
            severity="info",
            title="Prepared period exists",
            message="A Prepared period exists and cannot be submitted directly.",
            related_period_id=draft_row["payrollperiodid"],
            affected_action_codes=[],
        ))

    # Setup alerts
    if setup_status != "complete":
        sev = "warning"
        if setup_status == "missing":
            msg = "No payroll setup found for this branch."
        elif setup_status == "inactive":
            msg = "The payroll setup for this branch is inactive."
        else:
            msg = "The payroll setup for this branch is incomplete."
        alerts.append(WorkflowAlert(
            code="SETUP_MISSING" if setup_status == "missing" else "SETUP_INCOMPLETE",
            severity=sev,
            title="Payroll setup issue",
            message=msg,
            affected_action_codes=["can_create_open_candidate", "can_create_prepared_candidate"],
        ))

    # Build branch-level capabilities
    can_view = _cap(has_view or has_entry or has_finalize)

    # can_create_open_candidate
    if not has_period_create:
        co_cap = _denied("PERMISSION_DENIED", "payroll.period.create required.")
    elif setup_status != "complete":
        co_cap = _denied("NO_PAYROLL_SETUP" if setup_status == "missing" else "SETUP_INCOMPLETE",
                         "Payroll setup must be complete to create candidates.")
    elif draft_alone:
        co_cap = _denied("DRAFT_WITHOUT_OPEN", "Cannot create Open candidate while a Draft exists without Open.")
    else:
        ok, code = _check_slot_matrix("OPEN_CREATION", active_periods)
        co_cap = _cap(ok, code, None if ok else f"Slot matrix blocked: {code}")

    # can_create_prepared_candidate — Returned backlog does NOT block this
    if not has_period_create:
        cp_cap = _denied("PERMISSION_DENIED", "payroll.period.create required.")
    elif setup_status != "complete":
        cp_cap = _denied("NO_PAYROLL_SETUP" if setup_status == "missing" else "SETUP_INCOMPLETE",
                         "Payroll setup must be complete to create candidates.")
    elif draft_alone:
        cp_cap = _denied("DRAFT_WITHOUT_OPEN", "Cannot create Prepared candidate while a Draft exists without Open.")
    else:
        ok, code = _check_slot_matrix("PREPARED_CREATION", active_periods)
        cp_cap = _cap(ok, code, None if ok else f"Slot matrix blocked: {code}")

    # can_view_candidates
    if not has_period_create:
        cv_cap = _denied("PERMISSION_DENIED", "payroll.period.create required.")
    else:
        cv_cap = _cap(True)

    # Period-level capabilities
    period_caps: dict[str, PeriodWorkflowCapabilities] = {}
    for row in active_periods:
        pid = row["payrollperiodid"]
        st = row["status"]
        key = str(pid)

        # can_enter_source
        # CP-2F: Draft (Prepared) supports operational source entry (day grid save,
        # daily lines). Financial entry (Period Pay, Bonus) is blocked separately.
        if not (has_entry or has_view):
            ce = _denied("PERMISSION_DENIED", "payroll.view or payroll.entry required.")
        elif st == "InReview":
            ce = _denied("PERIOD_IN_REVIEW_READ_ONLY", "Period is in review; source entry is locked.")
        elif st in ("Open", "Returned", "Draft"):
            if not has_entry:
                ce = _denied("PERMISSION_DENIED", "payroll.entry required for source entry.")
            else:
                ce = _cap(True)
        else:
            ce = _denied("PERIOD_CANCELLED", "Period is not in an editable state.")

        # can_submit_for_review
        if st != "Open":
            cs = _denied("PERIOD_NOT_OPEN", "Only Open periods can be submitted.")
        elif not has_entry:
            cs = _denied("PERMISSION_DENIED", "payroll.entry required to submit.")
        elif inreview_row:
            cs = _denied("INREVIEW_SLOT_OCCUPIED", "Another period is already in review.")
        elif returned_backlog:
            cs = _denied("RETURNED_BACKLOG_BLOCKS_SUBMIT",
                         f"Returned backlog (ending {returned_row['enddate']}) must be resolved first.")
        elif returned_anomaly:
            cs = _denied("WORKFLOW_SLOT_CONFLICT", "Returned period has an anomalous chronological position.")
        elif draft_promotion_conflict:
            cs = _denied("DRAFT_PROMOTION_CONFLICT", "Draft period is not adjacent to this Open period.")
        else:
            cs = _cap(True)

        # can_resubmit_returned
        if st != "Returned":
            cr = _denied("PERIOD_NOT_RETURNED", "Only Returned periods can be resubmitted.")
        elif not has_entry:
            cr = _denied("PERMISSION_DENIED", "payroll.entry required to resubmit.")
        elif inreview_row:
            cr = _denied("INREVIEW_SLOT_OCCUPIED", "Another period is already in review.")
        else:
            cr = _cap(True)

        # can_view_review
        if st == "InReview":
            if not (has_view or has_entry):
                cvr = _denied("PERMISSION_DENIED", "payroll.view or payroll.entry required.")
            else:
                cvr = _cap(True)
        else:
            cvr = _cap(False, "PERIOD_NOT_IN_REVIEW", "Period is not currently in review.")

        # can_cancel
        if st == "Draft":
            if not has_finalize:
                cc = _denied("PERMISSION_DENIED", "payroll.finalize required to cancel a Draft period.")
            else:
                cc = _cap(True)
        elif st == "Open":
            if not has_finalize:
                cc = _denied("PERMISSION_DENIED", "payroll.finalize required to cancel an Open period.")
            else:
                cc = _cap(True)
        else:
            cc = _cap(False, "PERIOD_NOT_OPEN", "Only Draft and Open periods can be cancelled via this workflow.")

        # can_open_day_grid
        # CP-2F: Draft (Prepared) periods expose the day grid for operational entry.
        if not (has_view or has_entry):
            cg = _denied("PERMISSION_DENIED", "payroll.view or payroll.entry required.")
        elif st in ("Open", "Returned", "Draft"):
            cg = _cap(True)
        elif st == "InReview":
            cg = _cap(True)  # read-only access allowed; is_read_only on slot signals that
        else:
            cg = _cap(False, "PERIOD_NOT_EDITABLE", "Day grid not available for this period status.")

        period_caps[key] = PeriodWorkflowCapabilities(
            can_enter_source=ce,
            can_submit_for_review=cs,
            can_resubmit_returned=cr,
            can_view_review=cvr,
            can_cancel=cc,
            can_open_day_grid=cg,
        )

    branch_caps = BranchWorkflowCapabilities(
        can_view_current_workflow=can_view,
        can_create_open_candidate=co_cap,
        can_create_prepared_candidate=cp_cap,
        can_view_candidates=cv_cap,
        periods=period_caps,
    )

    return BranchWorkflowEntry(
        branch_id=branch_id,
        branch_name=branch_name,
        setup_status=setup_status,
        slots=slots,
        capabilities=branch_caps,
        alerts=alerts,
    )


async def get_current_workflow(
    company_id: int,
    user_id: int,
    branch_id: int | None,
    db: AsyncConnection,
) -> CurrentWorkflowResponse:
    # Security: block driver and ODA roles
    await _require_not_driver_role(company_id, user_id, db)
    own_driver_id = await _get_oda_own_driver_id(company_id, user_id, db)
    if own_driver_id is not None:
        raise HTTPException(
            status_code=403,
            detail="Current workflow is not accessible to driver-role users.",
        )

    can_see_all, allowed_branch_ids = await _check_branch_access(company_id, user_id, db)

    # If a specific branch was requested, verify it's accessible
    if branch_id is not None:
        if not can_see_all and branch_id not in allowed_branch_ids:
            raise HTTPException(status_code=403, detail="Access denied to the requested branch.")

    # Determine which branches to include
    if branch_id is not None:
        target_branch_ids: list[int] | None = [branch_id]
    elif can_see_all:
        target_branch_ids = None  # query all active branches
    else:
        target_branch_ids = allowed_branch_ids if allowed_branch_ids else []

    # Load branch metadata
    if target_branch_ids is not None and len(target_branch_ids) == 0:
        return CurrentWorkflowResponse(
            scope="branch" if not can_see_all else "company",
            company_id=company_id,
            requested_branch_id=branch_id,
            branches=[],
        )

    if target_branch_ids is None:
        branch_rows = (await db.execute(
            text("""
                SELECT b.branchid, b.branchname
                FROM   core.branches b
                WHERE  b.companyid = :cid AND b.status = 'Active'
                ORDER  BY b.branchname
            """),
            {"cid": company_id},
        )).mappings().all()
    else:
        in_clause, in_params = _build_in_clause(target_branch_ids, "bid")
        branch_rows = (await db.execute(
            text(f"""
                SELECT b.branchid, b.branchname
                FROM   core.branches b
                WHERE  b.companyid = :cid AND b.status = 'Active'
                  AND  b.branchid IN ({in_clause})
                ORDER  BY b.branchname
            """),
            {"cid": company_id, **in_params},
        )).mappings().all()

    if not branch_rows:
        return CurrentWorkflowResponse(
            scope="branch" if branch_id else "company",
            company_id=company_id,
            requested_branch_id=branch_id,
            branches=[],
        )

    branch_id_list = [r["branchid"] for r in branch_rows]
    branch_name_map = {r["branchid"]: r["branchname"] for r in branch_rows}

    # Check read permission: require at least one payroll read perm on at least one accessible branch
    # We verify per-branch below when building capabilities, but first ensure user has any access
    any_read = False
    for bid in branch_id_list:
        if await _has_any_permission(company_id, user_id, bid, _WF_READ_PERMS, db):
            any_read = True
            break
    if not any_read:
        raise HTTPException(status_code=403, detail="No payroll read permission on any accessible branch.")

    # Load all active workflow periods for these branches in one query
    in_clause2, in_params2 = _build_in_clause(branch_id_list, "bid")
    period_rows = (await db.execute(
        text(f"""
            SELECT payrollperiodid, branchid, periodcode, periodname, periodtype,
                   startdate, enddate, status,
                   submittedatutc, currentreturnreviewitemid
            FROM   payroll.payrollperiods
            WHERE  companyid = :cid
              AND  branchid IN ({in_clause2})
              AND  status   IN ('Draft', 'Open', 'InReview', 'Returned')
            ORDER  BY branchid, startdate, payrollperiodid
        """),
        {"cid": company_id, **in_params2},
    )).mappings().all()

    # Group periods by branch_id
    periods_by_branch: dict[int, list[dict]] = {bid: [] for bid in branch_id_list}
    for row in period_rows:
        periods_by_branch[row["branchid"]].append(dict(row))

    # Load payroll setup status for all branches
    setup_rows = (await db.execute(
        text(f"""
            SELECT branchid, payrollfrequency, customintervaldays, isactive
            FROM   payroll.branchpayrollsettings
            WHERE  companyid = :cid
              AND  branchid IN ({in_clause2})
        """),
        {"cid": company_id, **in_params2},
    )).mappings().all()

    setup_by_branch: dict[int, str] = {}
    for sr in setup_rows:
        bid = sr["branchid"]
        if not sr["isactive"]:
            setup_by_branch[bid] = "inactive"
        elif sr["payrollfrequency"] == "Custom" and not sr.get("customintervaldays"):
            setup_by_branch[bid] = "incomplete"
        else:
            setup_by_branch[bid] = "complete"

    result_branches: list[BranchWorkflowEntry] = []
    scope = "branch" if (branch_id is not None or not can_see_all) else "company"

    for bid in branch_id_list:
        bname = branch_name_map[bid]
        setup_st = setup_by_branch.get(bid, "missing")
        active_periods = periods_by_branch.get(bid, [])

        # Per-branch permission checks
        b_view = await _has_any_permission(company_id, user_id, bid, _WF_READ_PERMS, db)
        if not b_view:
            # Include branch in response but with denied view capability
            branch_entry = BranchWorkflowEntry(
                branch_id=bid,
                branch_name=bname,
                setup_status=setup_st,
                slots=WorkflowBranchSlots(),
                capabilities=BranchWorkflowCapabilities(
                    can_view_current_workflow=_denied("PERMISSION_DENIED", "No payroll read permission for this branch."),
                    can_create_open_candidate=_denied("PERMISSION_DENIED", "No payroll read permission."),
                    can_create_prepared_candidate=_denied("PERMISSION_DENIED", "No payroll read permission."),
                    can_view_candidates=_denied("PERMISSION_DENIED", "No payroll read permission."),
                ),
                alerts=[],
            )
            result_branches.append(branch_entry)
            continue

        has_view = await _has_any_permission(company_id, user_id, bid, ["payroll.view"], db)
        has_entry = await _has_any_permission(company_id, user_id, bid, ["payroll.entry"], db)
        has_finalize = await _has_any_permission(company_id, user_id, bid, ["payroll.finalize"], db)
        has_period_create = await _has_any_permission(company_id, user_id, bid, ["payroll.period.create"], db)

        entry = _build_branch_entry(
            branch_id=bid,
            branch_name=bname,
            setup_status=setup_st,
            active_periods=active_periods,
            has_view=has_view,
            has_entry=has_entry,
            has_finalize=has_finalize,
            has_period_create=has_period_create,
        )
        result_branches.append(entry)

    return CurrentWorkflowResponse(
        scope=scope,
        company_id=company_id,
        requested_branch_id=branch_id,
        branches=result_branches,
    )
