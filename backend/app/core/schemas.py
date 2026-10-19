"""Pydantic schemas for the Core compatibility API."""

from datetime import date
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, field_validator


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


class PersonSummary(BaseModel):
    employee_id: int
    branch_id: int
    branch_name: str
    employee_key: str | None = None
    full_name: str
    preferred_name: str | None = None
    driver_state: Literal["current", "pending", "none"]
    employment_status: str
    email: str | None = None
    primary_phone: str | None = None
    hire_date: date | None = None
    driver_id: int | None = None
    driver_assignment_id: int | None = None
    driver_number: str | None = None
    driver_status: str | None = None
    cdl_number: str | None = None


class DriverSummary(BaseModel):
    """A permanent Driver with the Branch of its current (else pending, else latest) assignment."""

    driver_id: int
    employee_id: int
    driver_assignment_id: int | None = None
    branch_id: int
    branch_name: str
    full_name: str
    preferred_name: str | None = None
    employee_key: str | None = None
    driver_number: str | None = None
    cdl_number: str | None = None
    driver_status: str
    employment_status: str
    email: str | None = None
    primary_phone: str | None = None
    hire_date: date | None = None
    termination_date: date | None = None


class DriverAssignmentSummary(BaseModel):
    """One home-Branch tenure of a permanent Driver, as the user is allowed to see it."""

    driver_assignment_id: int
    driver_id: int
    branch_id: int
    branch_name: str
    effective_from_at: AwareDatetime
    effective_to_at: AwareDatetime | None = None
    end_reason: str | None = None
    state: Literal["current", "pending", "ended"]


class DriverAssignmentRosterRow(DriverAssignmentSummary):
    """An assignment with the permanent Driver's identity (the assignment-centric roster)."""

    driver_number: str | None = None
    driver_status: str
    full_name: str
    preferred_name: str | None = None


class DriverCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    branch_id: int
    full_name: str
    preferred_name: str | None = None
    employee_key: str | None = None
    email: str | None = None
    primary_phone: str | None = None
    hire_date: date | None = None
    driver_number: str | None = None
    cdl_number: str | None = None

    @field_validator("full_name")
    @classmethod
    def full_name_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("full_name must not be blank")
        return value.strip()


class DriverUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    driver_number: str | None = None
    cdl_number: str | None = None
    driver_status: str | None = None

    @field_validator("driver_status")
    @classmethod
    def driver_status_valid(cls, value: str | None) -> str | None:
        if value is not None and value not in {"Active", "Inactive", "OnLeave"}:
            raise ValueError("driver_status must be Active, Inactive, or OnLeave")
        return value
