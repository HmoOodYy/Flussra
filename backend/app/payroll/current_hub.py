"""CP-5A read model for the active Current Payroll workflow Hub."""
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.payroll import off_drivers, service
from app.payroll.schemas import (
    CurrentPayrollHubBranch,
    CurrentPayrollHubDriverSummary,
    CurrentPayrollHubFinancialSummary,
    CurrentPayrollHubMetrics,
    CurrentPayrollHubPeriodSlot,
    CurrentPayrollHubResponse,
    CurrentPayrollHubSlots,
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
    has_snapshot = await service._period_has_driver_eligibility_snapshot(period_id, db)

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
    in_clause, in_params = service._build_in_clause(sorted(eligible_ids), "driver")
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
            if service._is_snapshot_row_eligible_for_workdate(
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
        period = service.PeriodSummary.model_construct(
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
            await service._build_live_calculation_packet(period, company_id, db)
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
    workflow = await service.get_current_workflow(
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
