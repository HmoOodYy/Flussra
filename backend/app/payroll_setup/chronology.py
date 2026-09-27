"""Pure schedule arithmetic for company-owned Payroll Setup authority."""

import calendar
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta

MAX_NORMAL_DAYS_OFF = 2


class DaysOffLimitError(ValueError):
    """Normal days off exceed the two-day product limit."""


def validate_normal_days_off_mask(mask: int) -> None:
    if not 0 <= mask <= 127:
        raise ValueError("Invalid normal days-off mask")
    if mask.bit_count() > MAX_NORMAL_DAYS_OFF:
        raise DaysOffLimitError("Normal days off may include at most two days")


@dataclass(frozen=True)
class Schedule:
    frequency: str
    anchor_start_date: date
    custom_interval_days: int | None
    normal_days_off_mask: int

    def __post_init__(self) -> None:
        if self.frequency not in {"Week", "Biweek", "Month", "Custom"}:
            raise ValueError("Unknown payroll frequency")
        if self.frequency == "Custom":
            if self.custom_interval_days is None or self.custom_interval_days <= 0:
                raise ValueError("Custom frequency requires a positive interval")
        elif self.custom_interval_days is not None:
            raise ValueError("Non-Custom frequency must not carry a custom interval")
        validate_normal_days_off_mask(self.normal_days_off_mask)


def period_end(schedule: Schedule, start: date) -> date:
    if schedule.frequency == "Week":
        return start + timedelta(days=6)
    if schedule.frequency == "Biweek":
        return start + timedelta(days=13)
    if schedule.frequency == "Custom":
        return start + timedelta(days=schedule.custom_interval_days - 1)
    month = start.month + 1
    year = start.year + (month > 12)
    if month > 12:
        month = 1
    same_day = start.replace(year=year, month=month,
                             day=min(start.day, calendar.monthrange(year, month)[1]))
    return same_day - timedelta(days=1)


def is_period_start(schedule: Schedule, candidate: date, *, origin: date | None = None) -> bool:
    """Check cadence from the anchor, or from actual prior-period chronology."""
    start = origin or schedule.anchor_start_date
    if candidate < start:
        return False
    if schedule.frequency != "Month":
        days = {"Week": 7, "Biweek": 14}.get(
            schedule.frequency, schedule.custom_interval_days,
        )
        return (candidate - start).days % days == 0
    while start < candidate:
        start = period_end(schedule, start) + timedelta(days=1)
    return start == candidate


def _fixed_step(schedule: Schedule) -> int | None:
    """Fixed day-count step for non-Month frequencies, else None."""
    return {"Week": 7, "Biweek": 14}.get(schedule.frequency, schedule.custom_interval_days)


def next_period_start(schedule: Schedule, after: date) -> date:
    """Smallest anchor-grid period start strictly after `after`."""
    anchor = schedule.anchor_start_date
    if after < anchor:
        return anchor
    step = _fixed_step(schedule)
    if step is not None:
        return anchor + timedelta(days=((after - anchor).days // step + 1) * step)
    start = anchor
    while start <= after:
        start = period_end(schedule, start) + timedelta(days=1)
    return start


def previous_period_start(schedule: Schedule, before: date) -> date | None:
    """Largest anchor-grid period start strictly before `before`, or None."""
    anchor = schedule.anchor_start_date
    if before <= anchor:
        return None
    step = _fixed_step(schedule)
    if step is not None:
        return anchor + timedelta(days=((before - anchor).days - 1) // step * step)
    start = anchor
    last = None
    while start < before:
        last = start
        start = period_end(schedule, start) + timedelta(days=1)
    return last


@dataclass(frozen=True)
class ScheduleSegment:
    """One terminal Published Version's reign: [effective_from, next segment's effective_from)."""
    effective_from: date
    schedule: Schedule


def timeline_schedule_at(segments: Sequence[ScheduleSegment], on: date) -> Schedule | None:
    """The governing segment's schedule (max effective_from <= on), else None."""
    governing = None
    for segment in segments:
        if segment.effective_from <= on:
            governing = segment
        else:
            break
    return governing.schedule if governing else None


def is_timeline_start(segments: Sequence[ScheduleSegment], candidate: date) -> bool:
    """True when `candidate` is governed and is a period start of that governing schedule."""
    schedule = timeline_schedule_at(segments, candidate)
    return schedule is not None and is_period_start(schedule, candidate)


def timeline_next_start(segments: Sequence[ScheduleSegment], after: date) -> date | None:
    """Smallest legal period start strictly after `after`, walking version boundaries."""
    if not segments:
        return None
    start_index = 0
    for index, segment in enumerate(segments):
        if segment.effective_from <= after:
            start_index = index
        else:
            break
    for index in range(start_index, len(segments)):
        segment = segments[index]
        lower = max(after, segment.effective_from - timedelta(days=1))
        candidate = next_period_start(segment.schedule, lower)
        following = segments[index + 1] if index + 1 < len(segments) else None
        if following is None or candidate < following.effective_from:
            return candidate
    return None


def timeline_previous_start(segments: Sequence[ScheduleSegment], before: date) -> date | None:
    """Largest legal period start strictly before `before`, walking version boundaries."""
    while True:
        governing = None
        for segment in segments:
            if segment.effective_from <= before - timedelta(days=1):
                governing = segment
            else:
                break
        if governing is None:
            return None
        candidate = previous_period_start(governing.schedule, before)
        if candidate is not None and candidate >= governing.effective_from:
            return candidate
        before = governing.effective_from
