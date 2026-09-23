"""Pure schedule arithmetic for company-owned Payroll Setup authority."""

import calendar
from dataclasses import dataclass
from datetime import date, timedelta


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
        if not 0 <= self.normal_days_off_mask <= 127:
            raise ValueError("Invalid normal days-off mask")


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
