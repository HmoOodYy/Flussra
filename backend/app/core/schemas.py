"""Pydantic schemas for the retained Core Branch and DriverAssignment roster reads."""

from datetime import date
from typing import Literal

from pydantic import AwareDatetime, BaseModel


class BranchSummary(BaseModel):
    branch_id: int
    branch_code: str
    branch_name: str
    time_zone_id: str
    business_date: date
    status: str
    is_default: bool
    city: str | None = None
    state_province: str | None = None
    country: str | None = None


class DriverAssignmentRosterRow(BaseModel):
    """One readable DriverAssignment with its permanent Driver's identity."""

    driver_assignment_id: int
    driver_id: int
    branch_id: int
    branch_name: str
    effective_from_at: AwareDatetime
    effective_to_at: AwareDatetime | None = None
    end_reason: str | None = None
    state: Literal["current", "pending", "ended"]

    driver_number: str | None = None
    driver_status: str
    full_name: str
    preferred_name: str | None = None
