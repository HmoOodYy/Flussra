"""Canonical legal-date navigation for publication, assignment, reassignment,
and onboarding.

The frontend must never compute payroll chronology. Every function here
answers, for a proposed payroll date: is it valid, why not (stable conflict
codes from the same read-only validators the write paths use), the nearest
valid previous/next dates, a friendly period description, and — when no date
is supplied — a server-suggested date.

Company-local "today" (clock.company_today) is used ONLY for the first-
onboarding window floor (already applied inside the validators), the
`relation` label (past/current/future), and the default requested/suggested
date when no date is given. It never influences whether a given date is
valid for publication or reassignment — validity comes solely from the
read-only validators in `payroll_policy` and `validation`.
"""

from collections.abc import Awaitable, Callable
from datetime import date, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from . import clock, payroll_policy, reads
from .chronology import (
    Schedule,
    next_period_start,
    period_end,
    previous_period_start,
    timeline_next_start,
    timeline_previous_start,
    timeline_schedule_at,
)
from .errors import PolicyError
from .onboarding import onboarding_window_floor
from .security import require_policy_permission
from .validation import terminal_segments, terminal_version, version_schedule

SEARCH_LIMIT = 60  # candidate period starts examined per direction

DateCandidateFn = Callable[[date], date | None]
EvaluateFn = Callable[[date], Awaitable[list[dict]]]
DescribeFn = Callable[[date], Awaitable[dict]]


def _relation(d: date, period_end_date: date, today: date) -> str:
    if period_end_date < today:
        return "past"
    if d <= today <= period_end_date:
        return "current"
    return "future"


def _choice(
    d: date, schedule: Schedule, today: date, *,
    predecessor_schedule: Schedule | None = None,
    predecessor_period_end_date: date | None = None,
    replaces_version_id: int | None = None,
    replaces_version_number: int | None = None,
) -> dict:
    end = period_end(schedule, d)
    return {
        "date": d,
        "period_end_date": end,
        "payroll_frequency": schedule.frequency,
        "custom_interval_days": schedule.custom_interval_days,
        "relation": _relation(d, end, today),
        "predecessor_payroll_frequency":
            predecessor_schedule.frequency if predecessor_schedule else None,
        "predecessor_custom_interval_days":
            predecessor_schedule.custom_interval_days if predecessor_schedule else None,
        "predecessor_period_end_date": predecessor_period_end_date,
        "replaces_version_id": replaces_version_id,
        "replaces_version_number": replaces_version_number,
    }


async def _navigate(
    *, today: date, requested: date, explicit: bool,
    next_candidate: DateCandidateFn, previous_candidate: DateCandidateFn,
    evaluate: EvaluateFn, describe: DescribeFn,
    floor: date | None, prefer_current_period: bool,
) -> dict:
    conflicts = await evaluate(requested)
    requested_choice = await describe(requested) if not conflicts else None

    next_choice = None
    candidate = next_candidate(requested)
    for _ in range(SEARCH_LIMIT):
        if candidate is None:
            break
        if not await evaluate(candidate):
            next_choice = await describe(candidate)
            break
        candidate = next_candidate(candidate)

    previous_choice = None
    candidate = previous_candidate(requested)
    for _ in range(SEARCH_LIMIT):
        if candidate is None or (floor is not None and candidate < floor):
            break
        if not await evaluate(candidate):
            previous_choice = await describe(candidate)
            break
        candidate = previous_candidate(candidate)

    suggested = None
    if not explicit:
        if not conflicts:
            suggested = requested_choice
        elif prefer_current_period and previous_choice is not None and previous_choice["relation"] == "current":
            suggested = previous_choice
        else:
            suggested = next_choice

    return {
        "reference_date": today,
        "requested_date": requested,
        "requested_valid": not conflicts,
        "requested": requested_choice,
        "conflicts": conflicts,
        "previous": previous_choice,
        "next": next_choice,
        "suggested": suggested,
        "earliest_allowed_date": floor,
    }


async def publication_choices(
    company_id: int, user_id: int, setup_id: int, draft_id: int | None,
    around: date | None, db: AsyncConnection, *, proposed_schedule: Schedule | None = None,
) -> dict:
    """Legal publication/correction dates for a Draft or inline schedule."""
    await require_policy_permission(company_id, user_id, "payroll_setup.view", db)
    await payroll_policy._setup(db, company_id, setup_id, active=True)
    schedule = proposed_schedule
    if proposed_schedule is None:
        if draft_id is None:
            raise PolicyError("INVALID_SCHEDULE", "A Draft or complete inline schedule is required")
        schedule_data = await reads.get_publication_schedule(company_id, setup_id, draft_id, db)
        schedule = Schedule(
            schedule_data["frequency"], schedule_data["anchor_start_date"],
            schedule_data["custom_interval_days"], schedule_data["normal_days_off_mask"],
        )
    today = await clock.company_today(company_id, db)
    requested = around if around is not None else today

    async def evaluate(d: date) -> list[dict]:
        current_at_date = await terminal_version(db, setup_id, d)
        replaces_id = (
            current_at_date["payrollsetupversionid"]
            if current_at_date is not None and current_at_date["effectivefromdate"] == d
            else None
        )
        impact = await payroll_policy._policy_impact(
            company_id, setup_id, d, schedule, db, replaces_version_id=replaces_id,
        )
        return impact["conflicts"]

    async def describe(d: date) -> dict:
        current_at_date = await terminal_version(db, setup_id, d)
        replaces_id = replaces_number = None
        if current_at_date is not None and current_at_date["effectivefromdate"] == d:
            replaces_id = current_at_date["payrollsetupversionid"]
            replaces_number = current_at_date["versionnumber"]
        return _choice(
            d, schedule, today,
            replaces_version_id=replaces_id, replaces_version_number=replaces_number,
        )

    def next_candidate(d: date) -> date | None:
        return next_period_start(schedule, d)

    def previous_candidate(d: date) -> date | None:
        return previous_period_start(schedule, d)

    return await _navigate(
        today=today, requested=requested, explicit=around is not None,
        next_candidate=next_candidate, previous_candidate=previous_candidate,
        evaluate=evaluate, describe=describe, floor=None, prefer_current_period=False,
    )


async def assignment_choices(
    company_id: int, user_id: int, branch_id: int, setup_id: int,
    around: date | None, db: AsyncConnection,
) -> dict:
    """Legal first-or-later assignment dates for a Branch onto a Setup's timeline."""
    await require_policy_permission(company_id, user_id, "payroll_setup.view", db)
    branch = (await db.execute(text("""
        SELECT BranchID FROM core.Branches WHERE CompanyID = :cid AND BranchID = :bid
    """), {"cid": company_id, "bid": branch_id})).scalar_one_or_none()
    if branch is None:
        raise PolicyError("BRANCH_NOT_FOUND", "Branch does not belong to Company")
    await reads.get_setup(company_id, setup_id, db)
    segments = await terminal_segments(db, setup_id)
    today = await clock.company_today(company_id, db)
    requested = around if around is not None else today
    timeline = await payroll_policy._assignment_timeline(db, company_id, branch_id)
    floor = onboarding_window_floor(segments, today) if not timeline else None

    async def evaluate(d: date) -> list[dict]:
        return await payroll_policy.evaluate_assignment(company_id, branch_id, setup_id, d, db)

    async def describe(d: date) -> dict:
        schedule = timeline_schedule_at(segments, d)
        return _choice(d, schedule, today)

    def next_candidate(d: date) -> date | None:
        return timeline_next_start(segments, d)

    def previous_candidate(d: date) -> date | None:
        return timeline_previous_start(segments, d)

    return await _navigate(
        today=today, requested=requested, explicit=around is not None,
        next_candidate=next_candidate, previous_candidate=previous_candidate,
        evaluate=evaluate, describe=describe, floor=floor,
        prefer_current_period=not timeline,
    )


async def reassignment_choices(
    company_id: int, user_id: int, branch_id: int, destination_setup_id: int,
    around: date | None, db: AsyncConnection,
) -> dict:
    """Legal reassignment dates for a Branch moving onto a destination Setup's timeline."""
    await require_policy_permission(company_id, user_id, "payroll_setup.view", db)
    segments = await terminal_segments(db, destination_setup_id)
    today = await clock.company_today(company_id, db)
    requested = around if around is not None else today

    async def evaluate(d: date) -> list[dict]:
        impact = await payroll_policy._reassignment_impact(
            company_id, branch_id, destination_setup_id, d, db,
        )
        return [{"branch_id": branch_id, **conflict} for conflict in impact["conflicts"]]

    async def _source_setup_id(d: date) -> int | None:
        result = await db.execute(text("""
            SELECT PayrollSetupID FROM payroll.BranchPayrollSetupAssignments
            WHERE CompanyID = :cid AND BranchID = :bid AND WithdrawnAtUtc IS NULL
              AND EffectiveFromDate < :effective
              AND (EffectiveToDate IS NULL OR EffectiveToDate > :effective)
        """), {"cid": company_id, "bid": branch_id, "effective": d})
        return result.scalar_one_or_none()

    async def describe(d: date) -> dict:
        schedule = timeline_schedule_at(segments, d)
        predecessor_schedule = None
        predecessor_end = None
        source_setup_id = await _source_setup_id(d)
        if source_setup_id is not None:
            predecessor_schedule = version_schedule(
                await terminal_version(db, source_setup_id, d - timedelta(days=1)),
            )
            if predecessor_schedule is not None:
                predecessor_end = d - timedelta(days=1)
        return _choice(
            d, schedule, today,
            predecessor_schedule=predecessor_schedule,
            predecessor_period_end_date=predecessor_end,
        )

    def next_candidate(d: date) -> date | None:
        return timeline_next_start(segments, d)

    def previous_candidate(d: date) -> date | None:
        return timeline_previous_start(segments, d)

    return await _navigate(
        today=today, requested=requested, explicit=around is not None,
        next_candidate=next_candidate, previous_candidate=previous_candidate,
        evaluate=evaluate, describe=describe, floor=None, prefer_current_period=False,
    )


async def onboarding_choices(
    company_id: int, setup_id: int, around: date | None, db: AsyncConnection,
) -> dict:
    """Legal first-payroll onboarding dates for a not-yet-created Branch.

    No authorization here — the caller (settings.service) owns it, matching
    the authority the create-with-date write requires.
    """
    segments = await terminal_segments(db, setup_id)
    today = await clock.company_today(company_id, db)
    requested = around if around is not None else today
    floor = onboarding_window_floor(segments, today)

    async def evaluate(d: date) -> list[dict]:
        return await payroll_policy.evaluate_assignment(company_id, None, setup_id, d, db)

    async def describe(d: date) -> dict:
        schedule = timeline_schedule_at(segments, d)
        return _choice(d, schedule, today)

    def next_candidate(d: date) -> date | None:
        return timeline_next_start(segments, d)

    def previous_candidate(d: date) -> date | None:
        return timeline_previous_start(segments, d)

    return await _navigate(
        today=today, requested=requested, explicit=around is not None,
        next_candidate=next_candidate, previous_candidate=previous_candidate,
        evaluate=evaluate, describe=describe, floor=floor,
        prefer_current_period=True,
    )
