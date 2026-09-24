from datetime import date

import pytest

from app.payroll_setup.chronology import Schedule, is_period_start, period_end
from app.payroll_setup.policy import canonical_config_hash


@pytest.mark.parametrize(
    "frequency, interval, next_start",
    [
        ("Week", None, date(2026, 1, 12)),
        ("Biweek", None, date(2026, 1, 19)),
        ("Custom", 5, date(2026, 1, 10)),
        ("Month", None, date(2026, 2, 5)),
    ],
)
def test_schedule_boundaries(frequency, interval, next_start):
    schedule = Schedule(frequency, date(2026, 1, 5), interval, 0)
    assert period_end(schedule, date(2026, 1, 5)).toordinal() + 1 == next_start.toordinal()
    assert is_period_start(schedule, next_start)
    assert not is_period_start(schedule, date(2026, 1, 6))


def test_month_cadence_preserves_existing_calendar_semantics():
    schedule = Schedule("Month", date(2026, 1, 31), None, 0)
    assert period_end(schedule, date(2026, 1, 31)) == date(2026, 2, 27)
    assert is_period_start(schedule, date(2026, 2, 28))
    assert is_period_start(schedule, date(2026, 3, 28))
    assert not is_period_start(schedule, date(2026, 3, 31))


def test_hash_is_canonical_and_changes_with_authoritative_fields():
    original = Schedule("Week", date(2026, 1, 5), None, 0)
    same = Schedule("Week", date(2026, 1, 5), None, 0)
    changed = Schedule("Week", date(2026, 1, 5), None, 1)
    assert canonical_config_hash(original) == canonical_config_hash(same)
    assert canonical_config_hash(original) != canonical_config_hash(changed)
