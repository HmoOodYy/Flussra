"""First payroll onboarding guardrail: the current payroll period, up to two
previous periods, or any later period."""

from collections.abc import Sequence
from datetime import date

from sqlalchemy.ext.asyncio import AsyncConnection

from . import clock
from .chronology import ScheduleSegment, is_timeline_start, timeline_previous_start
from .errors import PolicyError
from .validation import terminal_segments

ONBOARDING_LOOKBACK_PERIODS = 2


def onboarding_window_floor(segments: Sequence[ScheduleSegment], today: date) -> date | None:
    current = today if is_timeline_start(segments, today) else timeline_previous_start(segments, today)
    if current is None:
        return None  # no legal start on/before today -> only future starts exist, nothing to restrict
    floor = current
    for _ in range(ONBOARDING_LOOKBACK_PERIODS):
        prev = timeline_previous_start(segments, floor)
        if prev is None:
            break
        floor = prev
    return floor


async def ensure_onboarding_window(
    db: AsyncConnection, company_id: int, setup_id: int, start: date,
) -> None:
    segments = await terminal_segments(db, setup_id)
    today = await clock.company_today(company_id, db)
    floor = onboarding_window_floor(segments, today)
    if floor is not None and start < floor:
        raise PolicyError(
            "ONBOARDING_START_TOO_EARLY",
            "First payroll can start no earlier than two payroll periods before the current period",
        )
